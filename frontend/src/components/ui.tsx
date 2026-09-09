import type { ReactNode } from "react";

export function Panel({
  title,
  subtitle,
  children,
  actions,
}: {
  title?: string;
  subtitle?: string;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-edge bg-panel/70">
      {(title || actions) && (
        <header className="flex items-start justify-between gap-4 border-b border-edge px-4 py-3">
          <div>
            {title && <h2 className="text-sm font-semibold tracking-wide">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
          </div>
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: string | number;
  hint?: string;
  tone?: "default" | "warn" | "danger" | "good";
}) {
  const toneClass = {
    default: "text-white",
    good: "text-accent",
    warn: "text-warn",
    danger: "text-danger",
  }[tone];
  return (
    <div className="rounded-lg border border-edge bg-panel/70 px-4 py-3">
      <div className="text-xs uppercase tracking-wider text-muted">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${toneClass}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
}

const NEUTRAL = "border-edge bg-edge/40 text-slate-200";

const BADGE_TONES: Record<string, string> = {
  neutral: NEUTRAL,
  good: "border-teal-500/40 bg-teal-500/10 text-accent",
  warn: "border-amber-500/40 bg-amber-500/10 text-warn",
  danger: "border-rose-500/40 bg-rose-500/10 text-danger",
};

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ReactNode;
  tone?: keyof typeof BADGE_TONES | string;
  title?: string;
}) {
  const style = BADGE_TONES[tone] ?? NEUTRAL;
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[11px] ${style}`}
    >
      {children}
    </span>
  );
}

/** Key/value rows. Used wherever a record has to be shown in full. */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-3 border-b border-edge/60 py-1.5 last:border-0">
      <dt className="w-44 shrink-0 text-xs uppercase tracking-wider text-muted">{label}</dt>
      <dd className="min-w-0 flex-1 break-words text-sm">{children}</dd>
    </div>
  );
}

/** A value that may be absent. Renders an em dash rather than nothing at all,
 *  so "the field is empty" and "the field is missing" look different. */
export function Value({ children }: { children: ReactNode }) {
  if (children === null || children === undefined || children === "") {
    return <span className="text-muted">—</span>;
  }
  return <>{children}</>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-muted">{children}</p>;
}
