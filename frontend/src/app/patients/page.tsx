"use client";

import { useState } from "react";
import { Loader } from "@/components/Loader";
import { Badge, Empty, Field, Panel, Value } from "@/components/ui";
import type { PatientDetail, PatientView } from "@/lib/api";
import { formatDate, formatDateTime } from "@/lib/format";
import { useApi } from "@/lib/useApi";

function Chart({ reference }: { reference: string }) {
  const id = reference.replace(/^Patient\//, "");
  const detail = useApi<PatientDetail>(`/api/patients/${encodeURIComponent(id)}`);

  return (
    <Loader state={detail}>
      {(data) => (
        <div className="space-y-4">
          <Panel
            title={data.patient.full_name}
            subtitle={data.patient.reference}
            actions={<Badge tone="warn">SYNTHETIC</Badge>}
          >
            <dl>
              <Field label="Date of birth">{formatDate(data.patient.date_of_birth)}</Field>
              <Field label="Phone">
                <Value>{data.patient.phone}</Value>
              </Field>
              <Field label="Email">
                <Value>{data.patient.email}</Value>
              </Field>
              <Field label="Postal code">
                <Value>{data.patient.postal_code}</Value>
              </Field>
            </dl>
          </Panel>

          <Panel
            title="Medications"
            subtitle="Dosage text is shown exactly as the record stores it, never rephrased"
          >
            {data.medications.length === 0 ? (
              <Empty>No active prescriptions.</Empty>
            ) : (
              <ul className="space-y-2">
                {data.medications.map((medication) => (
                  <li
                    key={medication.medication_request_id}
                    className="rounded border border-edge/60 bg-ink/40 p-3"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{medication.display_name}</span>
                      <Badge>{medication.status}</Badge>
                      {medication.refills_remaining !== null && (
                        <Badge>{medication.refills_remaining} refills left</Badge>
                      )}
                    </div>
                    <p className="mt-1 text-sm">
                      {medication.dosage_instruction ?? (
                        <span className="text-warn">
                          No dosage instruction on file — the agent says exactly this and
                          escalates rather than inferring one.
                        </span>
                      )}
                    </p>
                    {medication.prescriber_name && (
                      <p className="mt-1 text-xs text-muted">
                        Prescriber: {medication.prescriber_name}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <div className="grid gap-4 lg:grid-cols-2">
            <Panel title="Appointments">
              {data.appointments.length === 0 ? (
                <Empty>None on file.</Empty>
              ) : (
                <ul className="space-y-1.5 text-sm">
                  {data.appointments.map((appointment) => (
                    <li key={appointment.appointment_id} className="flex flex-wrap gap-2">
                      <span className="font-mono text-xs text-muted">
                        {formatDateTime(appointment.start)}
                      </span>
                      <span>{appointment.appointment_type}</span>
                      <Badge>{appointment.status}</Badge>
                      <span className="text-muted">{appointment.practitioner_name}</span>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
            <Panel title="Conditions and allergies">
              <div className="flex flex-wrap gap-1.5">
                {data.conditions.map((condition) => (
                  <Badge key={condition.condition_id}>{condition.display_name}</Badge>
                ))}
                {data.allergies.map((allergy) => (
                  <Badge key={allergy.allergy_id} tone="warn">
                    {allergy.substance}
                    {allergy.reaction ? ` — ${allergy.reaction}` : ""}
                  </Badge>
                ))}
                {data.conditions.length === 0 && data.allergies.length === 0 && (
                  <Empty>Nothing recorded.</Empty>
                )}
              </div>
            </Panel>
          </div>
        </div>
      )}
    </Loader>
  );
}

export default function PatientsPage() {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const roster = useApi<PatientView[]>(
    `/api/patients?limit=100${query ? `&query=${encodeURIComponent(query)}` : ""}`,
  );

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Patients</h1>
        <p className="mt-1 text-sm text-muted">
          The synthetic roster, read through the same EHR interface the agent uses. Every
          person on this page is invented.
        </p>
      </div>

      <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
        <div className="space-y-3">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search by name"
            className="w-full rounded border border-edge bg-panel px-3 py-2 text-sm outline-none focus:border-accent"
          />
          <Loader state={roster}>
            {(patients) =>
              patients.length === 0 ? (
                <Empty>Nobody matches that.</Empty>
              ) : (
                <ul className="divide-y divide-edge/60 rounded-lg border border-edge bg-panel/70">
                  {patients.map((patient) => (
                    <li key={patient.reference}>
                      <button
                        type="button"
                        onClick={() => setSelected(patient.reference)}
                        className={`w-full px-3 py-2 text-left text-sm hover:bg-edge/40 ${
                          selected === patient.reference ? "bg-edge/60" : ""
                        }`}
                      >
                        <span className="block">{patient.full_name}</span>
                        <span className="block text-xs text-muted">
                          born {formatDate(patient.date_of_birth)}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )
            }
          </Loader>
        </div>

        {selected === null ? (
          <Panel>
            <Empty>Select a patient to see the chart the agent can read.</Empty>
          </Panel>
        ) : (
          <Chart reference={selected} />
        )}
      </div>
    </div>
  );
}
