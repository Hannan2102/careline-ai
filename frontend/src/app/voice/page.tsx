"use client";

/**
 * Talking to the agent.
 *
 * The transcript is rendered from the same events the turn manager acts on, so
 * what is on screen is what the agent heard -- including the interim results it
 * deliberately does not act on, shown in grey. Watching an interim get revised
 * a moment before the agent answers is the clearest demonstration of why only
 * finals reach the orchestrator.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Panel } from "@/components/ui";
import { VoiceClient, type VoiceStatus } from "@/lib/voiceClient";

interface Line {
  id: number;
  speaker: "caller";
  text: string;
  interim: boolean;
}

const STATUS_LABEL: Record<VoiceStatus, string> = {
  idle: "Not connected",
  connecting: "Connecting",
  listening: "Listening",
  speaking: "Speaking",
  closed: "Call ended",
  error: "Error",
};

const STATUS_TONE: Record<VoiceStatus, string> = {
  idle: "bg-edge text-muted",
  connecting: "bg-edge text-white",
  listening: "bg-accent/20 text-accent",
  speaking: "bg-warn/20 text-warn",
  closed: "bg-edge text-muted",
  error: "bg-danger/20 text-danger",
};

export default function VoicePage() {
  const [status, setStatus] = useState<VoiceStatus>("idle");
  const [detail, setDetail] = useState<string>();
  const [lines, setLines] = useState<Line[]>([]);
  const [barges, setBarges] = useState(0);
  const [aec, setAec] = useState<boolean | null>(null);
  // Degradations that did not end the call — speech falling back to silence,
  // most of all. Kept separate from `detail` so a still-running call is not
  // painted as a failed one.
  const [notice, setNotice] = useState<string>();

  const clientRef = useRef<VoiceClient | null>(null);
  const nextId = useRef(0);

  const addTranscript = useCallback((text: string, isFinal: boolean) => {
    setLines((current) => {
      const trimmed = current.filter((line) => !line.interim);
      if (!text.trim()) return trimmed;
      return [...trimmed, { id: (nextId.current += 1), speaker: "caller", text, interim: !isFinal }];
    });
  }, []);

  const start = useCallback(async () => {
    setLines([]);
    setBarges(0);
    setDetail(undefined);
    setNotice(undefined);
    const client = new VoiceClient({
      onStatus: (next, why) => {
        setStatus(next);
        setDetail(why);
      },
      onNotice: setNotice,
      onTranscript: addTranscript,
      onInterrupt: () => setBarges((n) => n + 1),
      onClosed: (reason) => setDetail(`Call ended: ${reason}`),
    });
    clientRef.current = client;
    await client.start();
    setAec(client.echoCancellationApplied);
  }, [addTranscript]);

  const stop = useCallback(async () => {
    await clientRef.current?.stop();
    clientRef.current = null;
    setStatus("closed");
  }, []);

  // A call must not outlive the page. Navigating away with the microphone
  // still open leaves the browser's recording indicator on and the session
  // ticking until its ten-minute limit.
  useEffect(() => {
    return () => {
      void clientRef.current?.stop();
    };
  }, []);

  const live = status === "listening" || status === "speaking" || status === "connecting";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold">Voice</h1>
        <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_TONE[status]}`}>
          {STATUS_LABEL[status]}
        </span>
        {barges > 0 && (
          <span className="rounded bg-edge px-2 py-0.5 text-xs text-muted">
            {barges} interruption{barges === 1 ? "" : "s"}
          </span>
        )}
        <div className="ml-auto flex gap-2">
          <button
            onClick={live ? stop : start}
            className={`rounded px-3 py-1.5 text-sm font-medium ${
              live ? "bg-danger text-white" : "bg-accent text-black"
            }`}
          >
            {live ? "End call" : "Start call"}
          </button>
        </div>
      </div>

      {detail && (
        <div
          className={`rounded border px-3 py-2 text-sm ${
            status === "error"
              ? "border-danger/40 bg-danger/10 text-danger"
              : "border-edge bg-panel/70 text-muted"
          }`}
        >
          {detail}
        </div>
      )}

      {notice && (
        <div className="rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
          <span className="font-medium">The agent is answering, but you will hear silence.</span>{" "}
          {notice}
        </div>
      )}

      {aec === false && (
        <div className="rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
          This browser did not apply echo cancellation. The agent will hear itself through
          the speakers and interrupt its own sentences — use headphones.
        </div>
      )}

      <Panel
        title="Transcript"
        subtitle="Grey lines are interim results. The agent never acts on one."
      >
        {lines.length === 0 ? (
          <p className="text-sm text-muted">
            {live ? "Say something." : "Start a call and speak. Interrupt the agent mid-sentence to see barge-in."}
          </p>
        ) : (
          <ul className="space-y-1.5 text-sm">
            {lines.map((line) => (
              <li key={line.id} className={line.interim ? "text-muted italic" : "text-white"}>
                {line.text}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <p className="text-xs text-muted">
        Voice runs on Deepgram streaming speech-to-text, Groq for the model and for speech
        synthesis. The agent answers through the same orchestrator the text chat uses.
      </p>
    </div>
  );
}
