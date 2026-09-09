"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader } from "@/components/Loader";
import { TurnCard } from "@/components/TurnCard";
import { Badge, Empty, Panel } from "@/components/ui";
import type { CallSummary, CallTrace } from "@/lib/api";
import { formatDateTime, formatDuration, formatMs, formatUsd } from "@/lib/format";
import { useApi } from "@/lib/useApi";

function TraceView() {
  const router = useRouter();
  const params = useSearchParams();
  const requested = params.get("call");

  const calls = useApi<CallSummary[]>("/api/calls?limit=50");
  const [chosen, setChosen] = useState<string | null>(requested);

  // Derived, not stored: with nothing chosen the page shows the newest call,
  // which is what you want the moment it opens.
  const selected = chosen ?? calls.data?.[0]?.session_id ?? null;

  const trace = useApi<CallTrace>(
    selected === null ? null : `/api/calls/${encodeURIComponent(selected)}/trace`,
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Agent Trace</h1>
          <p className="mt-1 text-sm text-muted">
            Every field recorded for every turn: what was said, what the safety layer decided,
            what was extracted, which workflow ran, what it did to the record, how long each
            stage took, and what it cost.
          </p>
        </div>
        <Loader state={calls}>
          {(rows) =>
            rows.length === 0 ? (
              <span className="text-sm text-muted">No calls yet.</span>
            ) : (
              <label className="text-xs text-muted">
                <span className="mr-2 uppercase tracking-wider">Call</span>
                <select
                  value={selected ?? ""}
                  onChange={(event) => {
                    setChosen(event.target.value);
                    router.replace(`/trace?call=${encodeURIComponent(event.target.value)}`);
                  }}
                  className="rounded border border-edge bg-panel px-2 py-1 font-mono text-xs text-white"
                >
                  {rows.map((call) => (
                    <option key={call.session_id} value={call.session_id}>
                      {formatDateTime(call.started_at)} · {call.patient_name ?? "unidentified"} ·{" "}
                      {call.outcome}
                    </option>
                  ))}
                </select>
              </label>
            )
          }
        </Loader>
      </div>

      {selected === null ? (
        <Panel>
          <Empty>
            Nothing to trace yet. Run <code className="font-mono">make chat</code> and come back.
          </Empty>
        </Panel>
      ) : (
        <Loader state={trace}>
          {(data) => (
            <div className="space-y-4">
              <Panel title="Call" subtitle={data.call.session_id}>
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <Badge>{data.call.channel}</Badge>
                  <Badge tone={data.call.verification === "VERIFIED" ? "good" : "neutral"}>
                    {data.call.verification}
                  </Badge>
                  <Badge tone={data.call.escalations > 0 ? "warn" : "neutral"}>
                    {data.call.outcome}
                  </Badge>
                  <span className="text-muted">
                    {data.call.patient_name ?? "patient not identified"}
                    {data.call.patient_ref ? ` · ${data.call.patient_ref}` : ""}
                  </span>
                  <span className="ml-auto flex gap-3 font-mono text-muted">
                    <span>{formatDateTime(data.call.started_at)}</span>
                    <span>{data.turns.length} turns</span>
                    <span>{formatDuration(data.call.duration_seconds)}</span>
                    <span>{formatUsd(data.call.estimated_cost_usd)}</span>
                    <span>
                      {formatMs(
                        data.turns.reduce((sum, turn) => sum + turn.total_ms, 0) /
                          Math.max(data.turns.length, 1),
                      )}{" "}
                      avg
                    </span>
                  </span>
                </div>
              </Panel>

              {data.turns.map((turn) => (
                <TurnCard key={turn.turn_id} turn={turn} />
              ))}

              {data.unattributed_operations.length > 0 && (
                <Panel
                  title="Operations without a turn"
                  subtitle="Recorded against the call but not attributable to one exchange"
                >
                  <ul className="space-y-1 font-mono text-xs">
                    {data.unattributed_operations.map((operation) => (
                      <li key={operation.event_id}>
                        {operation.action} · {operation.outcome} ·{" "}
                        {operation.patient_ref ?? "no patient"}
                      </li>
                    ))}
                  </ul>
                </Panel>
              )}
            </div>
          )}
        </Loader>
      )}
    </div>
  );
}

export default function TracePage() {
  return (
    <Suspense fallback={<p className="text-sm text-muted">Loading…</p>}>
      <TraceView />
    </Suspense>
  );
}
