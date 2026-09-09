"use client";

import { Badge, Field, Value } from "@/components/ui";
import type { Operation, TurnDetail } from "@/lib/api";
import { formatMs, formatTime, formatUsd, titleise } from "@/lib/format";

const SAFETY_TONE: Record<string, string> = {
  ALLOW: "good",
  REFUSE_AND_ESCALATE: "danger",
  ESCALATE: "warn",
};

const OPERATION_TONE: Record<string, string> = {
  success: "good",
  denied: "warn",
  failure: "danger",
};

/** Stage latency, to scale. The stages are the ones docs/latency.md budgets. */
function LatencyBar({ turn }: { turn: TurnDetail }) {
  const stages: [string, number, string][] = [
    ["Safety", turn.safety_ms, "bg-rose-400/70"],
    ["Extraction", turn.extraction_ms, "bg-amber-400/70"],
    ["Workflow", turn.workflow_ms, "bg-teal-400/70"],
  ];
  const measured = stages.reduce((sum, [, ms]) => sum + ms, 0);
  const other = Math.max(turn.total_ms - measured, 0);
  const total = measured + other || 1;

  return (
    <div>
      <div className="flex h-2.5 overflow-hidden rounded bg-edge/50">
        {stages.map(([label, ms, colour]) => (
          <span
            key={label}
            title={`${label}: ${formatMs(ms)}`}
            className={colour}
            style={{ width: `${(ms / total) * 100}%` }}
          />
        ))}
        <span
          title={`Other: ${formatMs(other)}`}
          className="bg-slate-500/50"
          style={{ width: `${(other / total) * 100}%` }}
        />
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-x-4 text-xs sm:grid-cols-3 lg:grid-cols-6">
        {[
          ["Safety", turn.safety_ms],
          ["Extraction", turn.extraction_ms],
          ["Workflow", turn.workflow_ms],
          ["STT", turn.stt_ms],
          ["TTS first audio", turn.tts_first_audio_ms],
          ["Total", turn.total_ms],
        ].map(([label, ms]) => (
          <div key={label as string} className="flex justify-between gap-2 py-0.5">
            <dt className="text-muted">{label}</dt>
            <dd className="font-mono">{formatMs(ms as number | null)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function Operations({ operations }: { operations: Operation[] }) {
  if (operations.length === 0) {
    return (
      <p className="text-sm text-muted">
        No record operations. This turn read and wrote nothing.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead className="uppercase tracking-wider text-muted">
          <tr className="border-b border-edge">
            <th className="py-1.5 pr-3 font-medium">Action</th>
            <th className="py-1.5 pr-3 font-medium">Outcome</th>
            <th className="py-1.5 pr-3 font-medium">Patient</th>
            <th className="py-1.5 pr-3 font-medium">Resource</th>
            <th className="py-1.5 pr-3 font-medium">Detail</th>
            <th className="py-1.5 font-medium">At</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {operations.map((operation) => (
            <tr key={operation.event_id} className="border-b border-edge/40">
              <td className="py-1.5 pr-3">{operation.action}</td>
              <td className="py-1.5 pr-3">
                <Badge tone={OPERATION_TONE[operation.outcome] ?? "neutral"}>
                  {operation.outcome}
                </Badge>
              </td>
              <td className="py-1.5 pr-3">{operation.patient_ref ?? "—"}</td>
              <td className="py-1.5 pr-3">
                {operation.resource_type
                  ? `${operation.resource_type}/${operation.resource_id ?? "?"}`
                  : "—"}
              </td>
              <td className="py-1.5 pr-3 font-sans">{operation.detail ?? "—"}</td>
              <td className="py-1.5 whitespace-nowrap">{formatTime(operation.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * One turn, in full.
 *
 * Every field of the persisted turn record is on this card, including the ones
 * that are usually null. A trace that hides empty fields is a trace you cannot
 * trust when a field is unexpectedly empty.
 */
export function TurnCard({ turn }: { turn: TurnDetail }) {
  return (
    <article className="rounded-lg border border-edge bg-panel/70">
      <header className="flex flex-wrap items-center gap-2 border-b border-edge px-4 py-2.5">
        <span className="rounded bg-edge px-2 py-0.5 font-mono text-xs">
          turn {turn.turn_number}
        </span>
        <Badge tone={SAFETY_TONE[turn.safety_outcome] ?? "neutral"}>{turn.safety_outcome}</Badge>
        <Badge>{titleise(turn.intent)}</Badge>
        {turn.workflow && <Badge>{turn.workflow}</Badge>}
        <span className="ml-auto flex items-center gap-3 font-mono text-xs text-muted">
          <span>{formatTime(turn.created_at)}</span>
          <span title="total latency">{formatMs(turn.total_ms)}</span>
          <span title="estimated cost">{formatUsd(turn.estimated_cost_usd)}</span>
        </span>
      </header>

      <div className="space-y-4 p-4">
        <div className="grid gap-3 lg:grid-cols-2">
          <div className="rounded border border-edge/60 bg-ink/40 p-3">
            <div className="text-xs uppercase tracking-wider text-muted">Caller said</div>
            <p className="mt-1 text-sm">{turn.utterance}</p>
          </div>
          <div className="rounded border border-edge/60 bg-ink/40 p-3">
            <div className="text-xs uppercase tracking-wider text-muted">Agent replied</div>
            <p className="mt-1 text-sm whitespace-pre-wrap">{turn.response}</p>
          </div>
        </div>

        <div className="grid gap-x-8 lg:grid-cols-2">
          <dl>
            <Field label="Turn id">
              <span className="font-mono text-xs">{turn.turn_id}</span>
            </Field>
            <Field label="Safety outcome">{turn.safety_outcome}</Field>
            <Field label="Safety category">
              <Value>{turn.safety_category}</Value>
            </Field>
            <Field label="Safety rule">
              <Value>
                {turn.safety_rule ? (
                  <span className="font-mono text-xs">{turn.safety_rule}</span>
                ) : null}
              </Value>
            </Field>
            <Field label="Intent">
              {turn.intent}{" "}
              <span className="text-muted">
                (confidence {turn.confidence.toFixed(2)})
              </span>
            </Field>
            <Field label="Entities">
              {Object.keys(turn.entities).length === 0 ? (
                <span className="text-muted">none extracted</span>
              ) : (
                <span className="flex flex-wrap gap-1">
                  {Object.entries(turn.entities).map(([key, value]) => (
                    <Badge key={key}>
                      {key}: {value}
                    </Badge>
                  ))}
                </span>
              )}
            </Field>
          </dl>
          <dl>
            <Field label="Workflow">
              <Value>{turn.workflow}</Value>
            </Field>
            <Field label="Workflow state">
              <Value>{turn.workflow_state}</Value>
            </Field>
            <Field label="Workflow status">
              <Value>{turn.workflow_status}</Value>
            </Field>
            <Field label="Verification">{turn.verification_state}</Field>
            <Field label="Escalation">
              <Value>
                {turn.escalation_id ? (
                  <span className="font-mono text-xs">{turn.escalation_id}</span>
                ) : null}
              </Value>
            </Field>
            <Field label="Estimated cost">{formatUsd(turn.estimated_cost_usd)}</Field>
          </dl>
        </div>

        <div>
          <div className="mb-2 text-xs uppercase tracking-wider text-muted">Latency</div>
          <LatencyBar turn={turn} />
        </div>

        <div>
          <div className="mb-2 text-xs uppercase tracking-wider text-muted">
            Record operations
          </div>
          <Operations operations={turn.operations} />
        </div>
      </div>
    </article>
  );
}
