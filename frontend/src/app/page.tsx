"use client";

import Link from "next/link";
import { Loader } from "@/components/Loader";
import { Badge, Empty, Panel, Stat } from "@/components/ui";
import type { Overview, SystemStatus } from "@/lib/api";
import { formatMs, formatUsd, titleise } from "@/lib/format";
import { useApi } from "@/lib/useApi";

function Bars({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) return <Empty>Nothing recorded in this window.</Empty>;
  const highest = Math.max(...entries.map(([, count]) => count));
  return (
    <ul className="space-y-1.5">
      {entries.map(([label, count]) => (
        <li key={label} className="flex items-center gap-3 text-sm">
          <span className="w-56 shrink-0 truncate text-muted" title={label}>
            {titleise(label)}
          </span>
          <span className="h-2 flex-1 rounded bg-edge/50">
            <span
              className="block h-2 rounded bg-accent/70"
              style={{ width: `${(count / highest) * 100}%` }}
            />
          </span>
          <span className="w-8 text-right font-mono text-xs">{count}</span>
        </li>
      ))}
    </ul>
  );
}

export default function OverviewPage() {
  const overview = useApi<Overview>("/api/overview");
  const system = useApi<SystemStatus>("/api/system/status");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Overview</h1>
        <p className="mt-1 text-sm text-muted">
          What the agent handled in the last 24 hours, and what it cost.
        </p>
      </div>

      <Loader state={overview}>
        {(data) => (
          <div className="space-y-6">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Stat label="Calls" value={data.calls} hint={`last ${data.window_hours}h`} />
              <Stat label="Turns" value={data.turns} hint={`${data.refused_turns} refused and escalated`} />
              <Stat
                label="Escalations"
                value={data.escalations}
                tone={data.escalations > 0 ? "warn" : "default"}
                hint="handed to a human"
              />
              <Stat
                label="Average turn"
                value={formatMs(data.average_turn_ms)}
                hint="safety + extraction + workflow"
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Stat
                label="Spend, last 24h"
                value={formatUsd(data.usage.estimated_cost_today_usd)}
              />
              <Stat
                label="Spend, project to date"
                value={formatUsd(data.usage.estimated_project_cost_usd)}
                hint={`limit ${formatUsd(data.usage.project_limit_usd)}`}
              />
              <Stat
                label="Remaining budget"
                value={formatUsd(data.usage.remaining_usd)}
                tone={data.usage.status === "ok" ? "good" : data.usage.status === "warn" ? "warn" : "danger"}
                hint={`guard: ${data.usage.status}`}
              />
              <Stat
                label="Paid providers"
                value={data.usage.can_spend_money ? "enabled" : "none"}
                tone={data.usage.can_spend_money ? "warn" : "good"}
                hint={data.usage.can_spend_money ? "a call can cost money" : "mock providers only"}
              />
            </div>

            <div className="grid gap-4 lg:grid-cols-2">
              <Panel title="Turns by intent" subtitle="What callers asked for">
                <Bars counts={data.turns_by_intent} />
              </Panel>
              <Panel
                title="Record operations"
                subtitle="Counted from the audit trail, not from intent"
              >
                <Bars counts={data.actions} />
              </Panel>
            </div>
          </div>
        )}
      </Loader>

      <Loader state={system}>
        {(status) => (
          <Panel
            title="System"
            subtitle="How this instance is configured"
            actions={
              <Badge tone={status.status === "ok" ? "good" : "warn"}>{status.status}</Badge>
            }
          >
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge>v{status.version}</Badge>
              <Badge>env: {status.environment}</Badge>
              <Badge tone={status.ehr.reachable ? "good" : "danger"}>
                EHR: {status.ehr.provider} {status.ehr.reachable ? "reachable" : "unreachable"}
              </Badge>
              <Badge>AI mode: {status.ai.mode}</Badge>
              <Badge>LLM: {status.ai.llm_provider}</Badge>
              <Badge>STT: {status.ai.stt_provider}</Badge>
              <Badge>TTS: {status.ai.tts_provider}</Badge>
              <Badge tone={status.ai.can_spend_money ? "warn" : "good"}>
                {status.ai.can_spend_money ? "paid calls possible" : "zero-cost configuration"}
              </Badge>
            </div>
            <p className="mt-3 text-xs text-muted">
              Every record shown in this dashboard is synthetic.{" "}
              <Link href="/patients" className="underline hover:text-white">
                Browse the roster
              </Link>{" "}
              to see exactly what the agent can read.
            </p>
          </Panel>
        )}
      </Loader>
    </div>
  );
}
