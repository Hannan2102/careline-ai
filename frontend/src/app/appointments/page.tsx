"use client";

import { useMemo, useState } from "react";
import { Loader } from "@/components/Loader";
import { Badge, Empty, Panel } from "@/components/ui";
import type { AppointmentView, PractitionerView } from "@/lib/api";
import { formatDate, formatTime } from "@/lib/format";
import { useApi } from "@/lib/useApi";

function isoDate(offsetDays: number): string {
  const day = new Date();
  day.setDate(day.getDate() + offsetDays);
  return day.toISOString().slice(0, 10);
}

const STATUS_TONE: Record<string, string> = {
  booked: "good",
  cancelled: "danger",
  fulfilled: "neutral",
  noshow: "warn",
};

export default function AppointmentsPage() {
  const [start, setStart] = useState(() => isoDate(0));
  const [end, setEnd] = useState(() => isoDate(14));
  const [practitioner, setPractitioner] = useState("");
  const [includeCancelled, setIncludeCancelled] = useState(false);

  const providers = useApi<PractitionerView[]>("/api/providers");
  const query = new URLSearchParams({
    start_date: start,
    end_date: end,
    include_cancelled: String(includeCancelled),
  });
  if (practitioner) query.set("practitioner_ref", practitioner);
  const appointments = useApi<AppointmentView[]>(`/api/appointments?${query.toString()}`);

  const grouped = useMemo(() => {
    const days = new Map<string, AppointmentView[]>();
    for (const appointment of appointments.data ?? []) {
      const key = appointment.start.slice(0, 10);
      days.set(key, [...(days.get(key) ?? []), appointment]);
    }
    return [...days.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [appointments.data]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Appointments</h1>
        <p className="mt-1 text-sm text-muted">
          The clinic schedule, by day. Booked through the agent or seeded — the record does not
          distinguish, and neither does this page.
        </p>
      </div>

      <Panel>
        <div className="flex flex-wrap items-end gap-4 text-sm">
          <label className="space-y-1">
            <span className="block text-xs uppercase tracking-wider text-muted">From</span>
            <input
              type="date"
              value={start}
              onChange={(event) => setStart(event.target.value)}
              className="rounded border border-edge bg-panel px-2 py-1 text-sm"
            />
          </label>
          <label className="space-y-1">
            <span className="block text-xs uppercase tracking-wider text-muted">To</span>
            <input
              type="date"
              value={end}
              onChange={(event) => setEnd(event.target.value)}
              className="rounded border border-edge bg-panel px-2 py-1 text-sm"
            />
          </label>
          <label className="space-y-1">
            <span className="block text-xs uppercase tracking-wider text-muted">Provider</span>
            <select
              value={practitioner}
              onChange={(event) => setPractitioner(event.target.value)}
              className="rounded border border-edge bg-panel px-2 py-1 text-sm"
            >
              <option value="">All providers</option>
              {(providers.data ?? []).map((provider) => (
                <option key={provider.reference} value={provider.reference}>
                  {provider.display_name} — {provider.specialty}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 pb-1 text-xs text-muted">
            <input
              type="checkbox"
              checked={includeCancelled}
              onChange={(event) => setIncludeCancelled(event.target.checked)}
            />
            Include cancelled
          </label>
        </div>
      </Panel>

      <Loader state={appointments}>
        {(rows) =>
          rows.length === 0 ? (
            <Panel>
              <Empty>No appointments in this window.</Empty>
            </Panel>
          ) : (
            <div className="space-y-4">
              {grouped.map(([day, entries]) => (
                <Panel key={day} title={formatDate(day)} subtitle={`${entries.length} booked`}>
                  <ul className="space-y-1.5">
                    {entries.map((appointment) => (
                      <li
                        key={appointment.appointment_id}
                        className="flex flex-wrap items-center gap-3 border-b border-edge/40 py-1.5 text-sm last:border-0"
                      >
                        <span className="w-32 shrink-0 font-mono text-xs">
                          {formatTime(appointment.start)} – {formatTime(appointment.end)}
                        </span>
                        <span className="w-14 shrink-0 text-xs text-muted">
                          {appointment.duration_minutes}m
                        </span>
                        <span className="w-52 shrink-0">{appointment.practitioner_name}</span>
                        <span className="w-56 shrink-0 text-xs">
                          {appointment.appointment_type}
                        </span>
                        <Badge tone={STATUS_TONE[appointment.status] ?? "neutral"}>
                          {appointment.status}
                        </Badge>
                        <span className="font-mono text-xs text-muted">
                          {appointment.patient_ref}
                        </span>
                        {appointment.reason && (
                          <span className="text-xs text-muted">“{appointment.reason}”</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </Panel>
              ))}
            </div>
          )
        }
      </Loader>
    </div>
  );
}
