"use client";

/**
 * Talking to the agent -- the front door.
 *
 * The conversation is rendered from the same events the turn manager acts on:
 * what the caller said as the recogniser heard it (interim results in grey,
 * because the agent never acts on one), and what the agent said in its written
 * form, so a spelled-back name reads "J-O-H-N" on screen while the caller
 * hears each letter.
 */

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Logo, PhoneIcon } from "@/components/Chrome";
import { VoiceClient, type VoiceStatus } from "@/lib/voiceClient";

interface Line {
  id: number;
  speaker: "caller" | "agent";
  text: string;
  interim: boolean;
}

const STATUS_LABEL: Record<VoiceStatus, string> = {
  idle: "Ready when you are",
  connecting: "Connecting…",
  listening: "Listening",
  speaking: "CareLine is speaking",
  closed: "Call ended",
  error: "Something went wrong",
};

const CAPABILITIES = [
  { title: "Appointments", body: "Book, move, cancel or check a visit." },
  { title: "Prescriptions", body: "Hear exactly what your record says." },
  { title: "Refill requests", body: "Sent to a clinician for review." },
  { title: "New patients", body: "Register over the phone in a minute." },
];

const SAFEGUARDS = [
  {
    title: "Identity first",
    body: "Every call confirms who you are: your name is spelled back letter by letter, your date of birth read back in words.",
  },
  {
    title: "No medical advice",
    body: "Questions about symptoms or doses are never answered by the AI. They go straight to clinical staff, with your own words attached.",
  },
  {
    title: "Every turn on the record",
    body: "What was heard, what was decided and what was touched in the chart is traced and audited for staff to review.",
  },
];

export default function CallPage() {
  const [status, setStatus] = useState<VoiceStatus>("idle");
  const [detail, setDetail] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [lines, setLines] = useState<Line[]>([]);
  const [aec, setAec] = useState<boolean | null>(null);

  const clientRef = useRef<VoiceClient | null>(null);
  const nextId = useRef(0);
  const scroller = useRef<HTMLDivElement>(null);

  const addCaller = useCallback((text: string, isFinal: boolean) => {
    setLines((current) => {
      const settled = current.filter((line) => !line.interim);
      if (!text.trim()) return settled;
      return [...settled, { id: (nextId.current += 1), speaker: "caller", text, interim: !isFinal }];
    });
  }, []);

  const addAgent = useCallback((text: string) => {
    if (!text.trim()) return;
    setLines((current) => [
      ...current.filter((line) => !line.interim),
      { id: (nextId.current += 1), speaker: "agent", text, interim: false },
    ]);
  }, []);

  const start = useCallback(async () => {
    setLines([]);
    setDetail(undefined);
    setNotice(undefined);
    const client = new VoiceClient({
      onStatus: (next, why) => {
        setStatus(next);
        if (why) setDetail(why);
      },
      onNotice: setNotice,
      onTranscript: addCaller,
      onAgent: addAgent,
      onInterrupt: () => undefined,
      onClosed: () => setDetail(undefined),
    });
    clientRef.current = client;
    await client.start();
    setAec(client.echoCancellationApplied);
  }, [addCaller, addAgent]);

  const stop = useCallback(async () => {
    await clientRef.current?.stop();
    clientRef.current = null;
    setStatus("closed");
  }, []);

  // A call must not outlive the page: navigating away with the microphone
  // open leaves the recording indicator on and the session ticking.
  useEffect(() => {
    return () => {
      void clientRef.current?.stop();
    };
  }, []);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [lines]);

  const live = status === "listening" || status === "speaking" || status === "connecting";

  return (
    <div className="space-y-14">
      <section className="grid items-start gap-10 lg:grid-cols-[1fr_minmax(0,34rem)]">
        <div className="pt-4">
          <p className="inline-flex items-center gap-2 rounded-full border border-teal-200 bg-accent-soft px-3 py-1 text-xs font-semibold text-accent">
            <span className="h-1.5 w-1.5 rounded-full bg-accent" />
            AI patient line · Oakwood Family Medicine
          </p>
          <h1 className="mt-5 text-4xl font-semibold leading-[1.1] tracking-tight text-strong sm:text-5xl">
            Your clinic, on the line
            <span className="block text-brand">whenever you need it.</span>
          </h1>
          <p className="mt-5 max-w-xl text-base leading-relaxed text-muted">
            CareLine answers like a front desk: it confirms who you are, books and changes
            appointments, reads back what your prescription says, and sends refill requests
            to a clinician. Press call and talk to it.
          </p>

          <ul className="mt-8 grid max-w-xl gap-3 sm:grid-cols-2">
            {CAPABILITIES.map((item) => (
              <li
                key={item.title}
                className="flex gap-3 rounded-2xl border border-edge bg-panel p-4 shadow-[0_1px_2px_rgba(15,34,54,0.04)]"
              >
                <CheckIcon />
                <span>
                  <span className="block text-sm font-semibold text-strong">{item.title}</span>
                  <span className="block text-xs text-muted">{item.body}</span>
                </span>
              </li>
            ))}
          </ul>

          <div className="mt-8 max-w-xl rounded-2xl border border-dashed border-teal-300 bg-white/70 p-4 text-sm">
            <p className="font-semibold text-strong">Try it as a patient on file</p>
            <p className="mt-1 text-muted">
              Say you&rsquo;re an existing patient, then give the name{" "}
              <span className="font-medium text-strong">John Smith</span>, born{" "}
              <span className="font-medium text-strong">15 February 1985</span>. Or say
              you&rsquo;re new and register yourself.
            </p>
          </div>
        </div>

        <CallCard
          status={status}
          live={live}
          lines={lines}
          detail={detail}
          notice={notice}
          aec={aec}
          scroller={scroller}
          onStart={start}
          onStop={stop}
        />
      </section>

      <section>
        <h2 className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">
          Built to be safe
        </h2>
        <div className="mt-4 grid gap-4 md:grid-cols-3">
          {SAFEGUARDS.map((item) => (
            <div key={item.title} className="rounded-2xl border border-edge bg-panel p-6">
              <ShieldIcon />
              <h3 className="mt-4 text-base font-semibold text-strong">{item.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-muted">{item.body}</p>
            </div>
          ))}
        </div>
        <p className="mt-6 text-sm text-muted">
          Staff can see every call, turn by turn, on the{" "}
          <Link href="/dashboard" className="font-medium text-accent hover:underline">
            dashboard
          </Link>
          .
        </p>
      </section>
    </div>
  );
}

function CallCard({
  status,
  live,
  lines,
  detail,
  notice,
  aec,
  scroller,
  onStart,
  onStop,
}: {
  status: VoiceStatus;
  live: boolean;
  lines: Line[];
  detail?: string;
  notice?: string;
  aec: boolean | null;
  scroller: React.RefObject<HTMLDivElement | null>;
  onStart: () => void;
  onStop: () => void;
}) {
  const speaking = status === "speaking";
  return (
    <div className="overflow-hidden rounded-3xl border border-edge bg-panel shadow-[0_8px_40px_rgba(11,92,138,0.10)]">
      <div className="bg-gradient-to-br from-brand to-accent px-6 pb-8 pt-6 text-white">
        <div className="flex items-center justify-between">
          <span className="flex items-center gap-2 text-sm font-semibold">
            <Logo className="h-7 w-7" />
            CareLine
          </span>
          <span className="flex items-center gap-2 rounded-full bg-white/15 px-3 py-1 text-xs font-medium">
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                live ? "bg-emerald-300" : status === "error" ? "bg-rose-300" : "bg-white/60"
              }`}
            />
            {STATUS_LABEL[status]}
          </span>
        </div>

        <div className="mt-8 flex flex-col items-center">
          <div className="relative flex h-28 w-28 items-center justify-center">
            {live && <span className="pulse-ring absolute inset-0 rounded-full bg-white/40" />}
            <button
              type="button"
              onClick={live ? onStop : onStart}
              aria-label={live ? "End call" : "Start call"}
              className={`relative flex h-24 w-24 items-center justify-center rounded-full shadow-lg transition-transform active:scale-95 ${
                live ? "bg-rose-500 hover:bg-rose-600" : "bg-white hover:scale-105"
              }`}
            >
              <PhoneIcon
                className={`h-9 w-9 ${live ? "rotate-[135deg] text-white" : "text-accent"}`}
              />
            </button>
          </div>
          <div className="mt-4 flex h-6 items-end gap-1" aria-hidden="true">
            {[0, 1, 2, 3, 4].map((bar) => (
              <span
                key={bar}
                className={`w-1 rounded-full bg-white/80 ${speaking ? "voice-bar" : ""}`}
                style={{ height: speaking ? "100%" : "25%", animationDelay: `${bar * 0.12}s` }}
              />
            ))}
          </div>
          <p className="mt-3 text-sm text-white/85">
            {live ? "Tap to hang up" : "Tap to call · uses your microphone"}
          </p>
        </div>
      </div>

      <div ref={scroller} className="h-[22rem] space-y-3 overflow-y-auto bg-ink/60 px-5 py-5">
        {lines.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center text-center text-sm text-muted">
            <p className="font-medium text-strong">The conversation appears here.</p>
            <p className="mt-1 max-w-xs">
              CareLine greets you first. Interrupt it at any time — it stops and listens.
            </p>
          </div>
        ) : (
          lines.map((line) =>
            line.speaker === "agent" ? (
              <div key={line.id} className="flex items-start gap-2.5">
                <Logo className="mt-0.5 h-7 w-7 shrink-0" />
                <p className="max-w-[85%] rounded-2xl rounded-tl-md bg-white px-4 py-2.5 text-sm leading-relaxed text-strong shadow-sm ring-1 ring-edge">
                  {line.text}
                </p>
              </div>
            ) : (
              <div key={line.id} className="flex justify-end">
                <p
                  className={`max-w-[85%] rounded-2xl rounded-tr-md px-4 py-2.5 text-sm leading-relaxed ${
                    line.interim
                      ? "bg-accent-soft italic text-muted"
                      : "bg-accent text-white shadow-sm"
                  }`}
                >
                  {line.text}
                </p>
              </div>
            ),
          )
        )}
      </div>

      {(detail || notice || aec === false) && (
        <div className="space-y-2 border-t border-edge px-5 py-4 text-sm">
          {detail && (
            <p className={status === "error" ? "text-danger" : "text-muted"}>{detail}</p>
          )}
          {notice && (
            <p className="text-warn">
              <span className="font-medium">CareLine is answering, but you may hear silence.</span>{" "}
              {notice}
            </p>
          )}
          {aec === false && (
            <p className="text-warn">
              Your browser didn&rsquo;t apply echo cancellation — use headphones so CareLine
              doesn&rsquo;t hear itself.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function CheckIcon() {
  return (
    <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent-soft text-accent">
      <svg viewBox="0 0 20 20" fill="currentColor" className="h-3.5 w-3.5" aria-hidden="true">
        <path
          fillRule="evenodd"
          d="M16.7 5.3a1 1 0 0 1 0 1.4l-8 8a1 1 0 0 1-1.4 0l-4-4a1 1 0 1 1 1.4-1.4L8 12.6l7.3-7.3a1 1 0 0 1 1.4 0Z"
          clipRule="evenodd"
        />
      </svg>
    </span>
  );
}

function ShieldIcon() {
  return (
    <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent-soft text-accent">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="h-5 w-5" aria-hidden="true">
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M12 3 5 6v6c0 4.2 3 7.6 7 9 4-1.4 7-4.8 7-9V6l-7-3Zm-3 9 2 2 4-4"
        />
      </svg>
    </span>
  );
}
