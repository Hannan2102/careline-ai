"use client";

import Link from "next/link";
import { Loader } from "@/components/Loader";
import { Badge, Empty, Panel } from "@/components/ui";
import type { CallSummary } from "@/lib/api";
import { formatDateTime, formatDuration, formatUsd, titleise } from "@/lib/format";
import { useApi } from "@/lib/useApi";

const OUTCOME_TONE: Record<string, string> = {
  resolved: "good",
  escalated: "warn",
  "verification-failed": "danger",
  "in-progress": "neutral",
};

const VERIFICATION_TONE: Record<string, string> = {
  VERIFIED: "good",
  PENDING_SECOND_FACTOR: "warn",
  FAILED: "danger",
  UNVERIFIED: "neutral",
};

export default function CallsPage() {
  const calls = useApi<CallSummary[]>("/api/calls?limit=100");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Calls</h1>
        <p className="mt-1 text-sm text-muted">
          Every conversation the agent has handled, newest first. Open one to see the full
          per-turn trace.
        </p>
      </div>

      <Loader state={calls}>
        {(rows) => (
          <Panel>
            {rows.length === 0 ? (
              <Empty>
                No calls recorded yet. Run <code className="font-mono">make chat</code> to have
                one.
              </Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs uppercase tracking-wider text-muted">
                    <tr className="border-b border-edge">
                      <th className="py-2 pr-3 font-medium">Started</th>
                      <th className="py-2 pr-3 font-medium">Patient</th>
                      <th className="py-2 pr-3 font-medium">Verification</th>
                      <th className="py-2 pr-3 font-medium">Last intent</th>
                      <th className="py-2 pr-3 font-medium">Workflow</th>
                      <th className="py-2 pr-3 text-right font-medium">Turns</th>
                      <th className="py-2 pr-3 text-right font-medium">Duration</th>
                      <th className="py-2 pr-3 text-right font-medium">Cost</th>
                      <th className="py-2 pr-3 font-medium">Outcome</th>
                      <th className="py-2 font-medium">Trace</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((call) => (
                      <tr
                        key={call.session_id}
                        className="border-b border-edge/50 hover:bg-edge/20"
                      >
                        <td className="py-2 pr-3 whitespace-nowrap">
                          {formatDateTime(call.started_at)}
                        </td>
                        <td className="py-2 pr-3">
                          {call.patient_name ?? (
                            <span className="text-muted">not identified</span>
                          )}
                        </td>
                        <td className="py-2 pr-3">
                          <Badge tone={VERIFICATION_TONE[call.verification] ?? "neutral"}>
                            {call.verification}
                          </Badge>
                        </td>
                        <td className="py-2 pr-3">
                          {call.last_intent ? titleise(call.last_intent) : "—"}
                        </td>
                        <td className="py-2 pr-3 font-mono text-xs text-muted">
                          {call.workflow ?? "—"}
                        </td>
                        <td className="py-2 pr-3 text-right font-mono">{call.turns}</td>
                        <td className="py-2 pr-3 text-right font-mono">
                          {formatDuration(call.duration_seconds)}
                        </td>
                        <td className="py-2 pr-3 text-right font-mono">
                          {formatUsd(call.estimated_cost_usd)}
                        </td>
                        <td className="py-2 pr-3">
                          <Badge tone={OUTCOME_TONE[call.outcome] ?? "neutral"}>
                            {call.outcome}
                            {call.escalations > 0 ? ` x${call.escalations}` : ""}
                          </Badge>
                        </td>
                        <td className="py-2">
                          <Link
                            href={`/trace?call=${encodeURIComponent(call.session_id)}`}
                            className="text-accent underline-offset-2 hover:underline"
                          >
                            Open
                          </Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        )}
      </Loader>
    </div>
  );
}
