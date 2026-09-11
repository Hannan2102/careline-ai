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

export class VoiceClient {
  private socket: WebSocket | null = null;
  private capture: AudioContext | null = null;
  private playback: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private worklet: AudioWorkletNode | null = null;
  private player: AudioWorkletNode | null = null;
  private outputRate = 24000;
  /**
   * A trailing byte held over from the previous frame.
   *
   * The server streams bare PCM and the network breaks it wherever it likes:
   * measured against Groq, 50 of 88 frames for one sentence had an *odd* byte
   * count. A 16-bit sample split across two frames must be rejoined, not
   * dropped — `new Int16Array(buffer)` throws outright on an odd length, which
   * silently discarded half the audio and made the agent sound like static.
   */
  private carry: Uint8Array | null = null;

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
    this.player?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    await this.capture?.close().catch(() => undefined);
    await this.playback?.close().catch(() => undefined);
    this.socket?.close();
    this.socket = null;
    this.capture = null;
    this.playback = null;
    this.stream = null;
    this.worklet = null;
    this.player = null;
    this.carry = null;
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
        void this.openPlayback();
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
  private async openPlayback(): Promise<void> {
    this.playback = new AudioContext({ sampleRate: this.outputRate });
    await this.playback.audioWorklet.addModule("/player-worklet.js");
    this.player = new AudioWorkletNode(this.playback, "player-processor");
    this.player.connect(this.playback.destination);
    this.player.port.onmessage = (event: MessageEvent<{ type: string }>) => {
      if (event.data.type === "empty") {
        this.events.onStatus("listening");
      } else if (event.data.type === "overrun") {
        // The playback buffer lapped itself: audio was dropped and the caller
        // heard it break up. Surfaced rather than swallowed, because silent
        // corruption here is indistinguishable from a bad voice model.
        this.events.onStatus("error", "Audio buffer overran — playback was dropped.");
      }
    };
  }

  private enqueue(pcm: ArrayBuffer): void {
    if (!this.player) return;

    // Rejoin a sample split across two network frames before doing anything
    // else. Without this the odd-length frames throw and are dropped, and the
    // ones that survive are shifted by a byte — which is static, not speech.
    let bytes = new Uint8Array(pcm);
    if (this.carry) {
      const joined = new Uint8Array(this.carry.length + bytes.length);
      joined.set(this.carry, 0);
      joined.set(bytes, this.carry.length);
      bytes = joined;
      this.carry = null;
    }
    if (bytes.length % 2 === 1) {
      this.carry = bytes.slice(bytes.length - 1);
      bytes = bytes.subarray(0, bytes.length - 1);
    }
    if (bytes.length === 0) return;

    // `bytes` may be a view at a non-even offset into its buffer, which
    // `Int16Array` refuses to wrap. Reading through a DataView sidesteps
    // alignment entirely, and little-endian is what the server sends.
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
    const samples = new Float32Array(bytes.length / 2);
    for (let i = 0; i < samples.length; i += 1) {
      samples[i] = view.getInt16(i * 2, true) / 0x8000;
    }

    this.player.port.postMessage({ type: "samples", samples }, [samples.buffer]);
    this.events.onStatus("speaking");
  }

  private flushPlayback(): void {
    // A pointer reset on the audio thread: instant, and with no per-chunk
    // bookkeeping that could be left half-undone.
    this.carry = null;
    this.player?.port.postMessage({ type: "flush" });
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
