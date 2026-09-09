/**
 * The browser half of the voice call.
 *
 * Three jobs: capture the microphone, play what the agent says, and drop the
 * playback queue the instant the caller interrupts.
 *
 * The third is the one that makes barge-in real. The server stops synthesising
 * as soon as it hears speech, but audio already sent is sitting in scheduled
 * Web Audio nodes with start times in the future -- so without an explicit
 * flush the agent keeps talking for another second or two after being cut off,
 * which is precisely the behaviour barge-in exists to prevent.
 *
 * Echo cancellation is the browser's, via `getUserMedia`. Without it the
 * microphone hears the speakers, the server transcribes the agent's own voice,
 * and the agent interrupts itself in a loop. It is a constraint request, not a
 * guarantee: on hardware that refuses it, headphones remove the echo path
 * instead. `echoCancellationApplied` reports which of those happened.
 */

import { API_BASE } from "@/lib/api";

export type VoiceStatus =
  | "idle"
  | "connecting"
  | "listening"
  | "speaking"
  | "closed"
  | "error";

export interface VoiceLine {
  readonly id: number;
  readonly speaker: "caller" | "agent";
  readonly text: string;
  readonly interim: boolean;
}

export interface VoiceClientEvents {
  onStatus(status: VoiceStatus, detail?: string): void;
  onTranscript(text: string, isFinal: boolean): void;
  onInterrupt(): void;
  onClosed(reason: string): void;
}

/** How far ahead of the clock audio is scheduled, to absorb network jitter. */
const PLAYOUT_LEAD_SECONDS = 0.08;

export class VoiceClient {
  private socket: WebSocket | null = null;
  private capture: AudioContext | null = null;
  private playback: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private worklet: AudioWorkletNode | null = null;
  private sources = new Set<AudioBufferSourceNode>();
  private nextStartAt = 0;
  private outputRate = 24000;

  /** Whether the browser actually applied echo cancellation to the mic. */
  echoCancellationApplied = false;

  constructor(private readonly events: VoiceClientEvents) {}

  async start(): Promise<void> {
    this.events.onStatus("connecting");
    try {
      await this.openMicrophone();
      await this.openSocket();
    } catch (error) {
      this.events.onStatus("error", describe(error));
      await this.stop();
    }
  }

  async stop(): Promise<void> {
    // Sent before tearing anything down, so the server records a hang-up
    // rather than inferring one from a dropped socket.
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: "hangup" }));
    }
    this.flushPlayback();
    this.worklet?.port.close();
    this.worklet?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    await this.capture?.close().catch(() => undefined);
    await this.playback?.close().catch(() => undefined);
    this.socket?.close();
    this.socket = null;
    this.capture = null;
    this.playback = null;
    this.stream = null;
    this.worklet = null;
  }

  // ------------------------------------------------------------- microphone
  private async openMicrophone(): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error(
        "This browser exposes no microphone API. Voice needs a secure context: " +
          "localhost or https.",
      );
    }
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });
    // What was asked for and what was granted are different things. Reported
    // rather than assumed, because a failure to apply AEC does not raise -- it
    // just makes the agent interrupt itself, which looks like a server bug.
    const settings = this.stream.getAudioTracks()[0]?.getSettings();
    this.echoCancellationApplied = settings?.echoCancellation === true;

    // 16 kHz at the context, so the browser resamples the device for us and
    // no interpolation code has to exist in this project.
    this.capture = new AudioContext({ sampleRate: 16000 });
    await this.capture.audioWorklet.addModule("/mic-worklet.js");
    const source = this.capture.createMediaStreamSource(this.stream);
    this.worklet = new AudioWorkletNode(this.capture, "mic-processor");
    this.worklet.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
      if (this.socket?.readyState === WebSocket.OPEN) {
        this.socket.send(event.data);
      }
    };
    // Not connected to the destination: routing the microphone to the speakers
    // would produce exactly the feedback loop echo cancellation is for.
    source.connect(this.worklet);
  }

  // ----------------------------------------------------------------- socket
  private openSocket(): Promise<void> {
    const url = `${API_BASE.replace(/^http/, "ws")}/ws/voice`;
    const socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";
    this.socket = socket;

    return new Promise((resolve, reject) => {
      socket.onerror = () =>
        reject(new Error(`Cannot reach the voice endpoint at ${url}. Is the backend running?`));
      socket.onclose = () => this.events.onStatus("closed");
      socket.onopen = () => resolve();
      socket.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) {
          this.enqueue(event.data);
        } else {
          this.handleEvent(JSON.parse(event.data as string));
        }
      };
    });
  }

  private handleEvent(message: Record<string, unknown>): void {
    switch (message.type) {
      case "ready":
        this.outputRate = Number(message.output_sample_rate) || 24000;
        this.playback = new AudioContext({ sampleRate: this.outputRate });
        this.events.onStatus("listening");
        return;
      case "transcript":
        this.events.onTranscript(String(message.text), Boolean(message.is_final));
        return;
      case "interrupt":
        // The whole point. Everything already scheduled is now stale.
        this.flushPlayback();
        this.events.onInterrupt();
        this.events.onStatus("listening");
        return;
      case "closed":
        this.events.onClosed(String(message.reason ?? "ended"));
        this.events.onStatus("closed");
        return;
      case "error":
        this.events.onStatus("error", String(message.detail ?? "unknown error"));
        return;
    }
  }

  // --------------------------------------------------------------- playback
  private enqueue(pcm: ArrayBuffer): void {
    const context = this.playback;
    if (!context || pcm.byteLength < 2) return;

    // 16-bit signed PCM, headerless: the server strips the WAV container, so
    // the sample rate is the one it named in `ready` and not one carried here.
    const samples = new Int16Array(pcm);
    const buffer = context.createBuffer(1, samples.length, this.outputRate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i += 1) {
      channel[i] = samples[i]! / 0x8000;
    }

    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);

    // Scheduled against a running cursor rather than "now", so consecutive
    // chunks butt up against each other. Starting each one at `currentTime`
    // would overlap them into noise.
    const startAt = Math.max(context.currentTime + PLAYOUT_LEAD_SECONDS, this.nextStartAt);
    source.start(startAt);
    this.nextStartAt = startAt + buffer.duration;

    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
    this.events.onStatus("speaking");
  }

  private flushPlayback(): void {
    for (const source of this.sources) {
      try {
        source.stop();
      } catch {
        // Already finished. Stopping a stopped source throws; nothing to do.
      }
    }
    this.sources.clear();
    // Reset the cursor too: leaving it in the future would silently delay the
    // agent's *next* reply by however much audio was just discarded.
    this.nextStartAt = 0;
  }
}

function describe(error: unknown): string {
  if (error instanceof DOMException && error.name === "NotAllowedError") {
    return "Microphone permission was denied. Allow it in the browser and try again.";
  }
  if (error instanceof DOMException && error.name === "NotFoundError") {
    return "No microphone was found.";
  }
  return error instanceof Error ? error.message : String(error);
}
