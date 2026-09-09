"use client";

import Link from "next/link";
import { useState } from "react";
import { Loader } from "@/components/Loader";
import { Badge, Empty, Panel } from "@/components/ui";
import type { EscalationView, RefillRequestView } from "@/lib/api";
import { formatDateTime, titleise } from "@/lib/format";
import { useApi } from "@/lib/useApi";

const CATEGORIES = [
  "",
  "clinical",
  "failed-verification",
  "patient-requested",
  "system-uncertainty",
  "administrative",
];

const PRIORITY_TONE: Record<string, string> = {
  urgent: "danger",
  clinical: "warn",
  "patient-requested": "neutral",
  "system-uncertainty": "warn",
  administrative: "neutral",
};

export default function EscalationsPage() {
  const [category, setCategory] = useState("");
  const escalations = useApi<EscalationView[]>(
    `/api/escalations?limit=200${category ? `&category=${category}` : ""}`,
  );
  const refills = useApi<RefillRequestView[]>("/api/medications/refill-requests?limit=200");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Escalations</h1>
        <p className="mt-1 text-sm text-muted">
          Everything the agent handed to a human, with the context staff need so the patient is
          not asked to start again.
        </p>
      </div>

      <Panel
        title="Handoffs"
        actions={
          <select
            value={category}
            onChange={(event) => setCategory(event.target.value)}
            className="rounded border border-edge bg-panel px-2 py-1 text-xs"
          >
            {CATEGORIES.map((option) => (
              <option key={option || "all"} value={option}>
                {option === "" ? "All categories" : titleise(option)}
              </option>
            ))}
          </select>
        }
      >
        <Loader state={escalations}>
          {(rows) =>
            rows.length === 0 ? (
              <Empty>No escalations in this view.</Empty>
            ) : (
              <ul className="space-y-3">
                {rows.map((escalation) => (
                  <li
                    key={escalation.escalation_id}
                    className="rounded border border-edge/60 bg-ink/40 p-3"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={PRIORITY_TONE[escalation.priority] ?? "neutral"}>
                        {escalation.priority}
                      </Badge>
                      <Badge>{escalation.category}</Badge>
                      <Badge>{escalation.destination}</Badge>
                      <Badge tone={escalation.verification_state === "VERIFIED" ? "good" : "neutral"}>
                        {escalation.verification_state}
                      </Badge>
                      <span className="ml-auto font-mono text-xs text-muted">
                        {formatDateTime(escalation.created_at)}
                      </span>
                    </div>

                    <p className="mt-2 text-sm">{escalation.summary}</p>

                    {escalation.patient_question && (
                      <p className="mt-2 text-sm">
                        <span className="text-xs uppercase tracking-wider text-muted">
                          Patient asked{" "}
                        </span>
                        “{escalation.patient_question}”
                      </p>
                    )}

                    <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                      <span>
                        <span className="uppercase tracking-wider">AI action:</span>{" "}
                        {escalation.ai_action}
                      </span>
                      {escalation.medication_display && (
                        <span>Medication: {escalation.medication_display}</span>
                      )}
                      {escalation.patient_ref && <span>{escalation.patient_ref}</span>}
                      {escalation.session_id && (
                        <Link
                          href={`/trace?call=${encodeURIComponent(escalation.session_id)}`}
                          className="text-accent underline-offset-2 hover:underline"
                        >
                          View trace
                        </Link>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )
          }
        </Loader>
      </Panel>

      <Panel
        title="Refill requests"
        subtitle="Queued for clinician review. The agent never authorises a refill."
      >
        <Loader state={refills}>
          {(rows) =>
            rows.length === 0 ? (
              <Empty>No refill requests.</Empty>
            ) : (
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase tracking-wider text-muted">
                  <tr className="border-b border-edge">
                    <th className="py-2 pr-3 font-medium">Requested</th>
                    <th className="py-2 pr-3 font-medium">Medication</th>
                    <th className="py-2 pr-3 font-medium">Patient</th>
                    <th className="py-2 pr-3 font-medium">Status</th>
                    <th className="py-2 font-medium">Call</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((refill) => (
                    <tr key={refill.refill_request_id} className="border-b border-edge/50">
                      <td className="py-2 pr-3 whitespace-nowrap">
                        {formatDateTime(refill.requested_at)}
                      </td>
                      <td className="py-2 pr-3">{refill.medication_display}</td>
                      <td className="py-2 pr-3 font-mono text-xs text-muted">
                        {refill.patient_ref}
                      </td>
                      <td className="py-2 pr-3">
                        <Badge tone={refill.status === "PENDING_REVIEW" ? "warn" : "neutral"}>
                          {refill.status}
                        </Badge>
                      </td>
                      <td className="py-2">
                        {refill.session_id ? (
                          <Link
                            href={`/trace?call=${encodeURIComponent(refill.session_id)}`}
                            className="text-accent underline-offset-2 hover:underline"
                          >
                            Open
                          </Link>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          }
        </Loader>
      </Panel>
    </div>
  );
}
