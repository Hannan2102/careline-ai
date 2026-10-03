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
    <section className="rounded-2xl border border-edge bg-panel shadow-[0_1px_2px_rgba(15,34,54,0.04),0_4px_16px_rgba(15,34,54,0.04)]">
      {(title || actions) && (
        <header className="flex items-start justify-between gap-4 border-b border-edge px-5 py-4">
          <div>
            {title && <h2 className="text-sm font-semibold text-strong">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
          </div>
          {actions}
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

/** A page heading with an optional eyebrow and description. */
export function PageHeader({
  eyebrow,
  title,
  children,
}: {
  eyebrow?: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div>
      {eyebrow && (
        <p className="text-xs font-semibold uppercase tracking-[0.14em] text-accent">{eyebrow}</p>
      )}
      <h1 className="mt-1 text-2xl font-semibold tracking-tight text-strong">{title}</h1>
      {children && <p className="mt-1.5 max-w-3xl text-sm text-muted">{children}</p>}
    </div>
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
    default: "text-strong",
    good: "text-accent",
    warn: "text-warn",
    danger: "text-danger",
  }[tone];
  const dot = {
    default: "bg-slate-300",
    good: "bg-accent",
    warn: "bg-amber-500",
    danger: "bg-rose-500",
  }[tone];
  return (
    <div className="rounded-2xl border border-edge bg-panel px-5 py-4 shadow-[0_1px_2px_rgba(15,34,54,0.04)]">
      <div className="flex items-center gap-2 text-xs font-medium text-muted">
        <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
        {label}
      </div>
      <div className={`mt-2 text-2xl font-semibold tracking-tight ${toneClass}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-muted">{hint}</div>}
    </div>
  );
}

const NEUTRAL = "border-edge bg-ink text-slate-700";

const BADGE_TONES: Record<string, string> = {
  neutral: NEUTRAL,
  good: "border-teal-200 bg-accent-soft text-accent",
  warn: "border-amber-200 bg-amber-50 text-warn",
  danger: "border-rose-200 bg-rose-50 text-danger",
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
      className={`inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[11px] ${style}`}
    >
      {children}
    </span>
  );
}

/** Key/value rows. Used wherever a record has to be shown in full. */
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-3 border-b border-edge/70 py-2 last:border-0">
      <dt className="w-44 shrink-0 text-xs font-medium text-muted">{label}</dt>
      <dd className="min-w-0 flex-1 break-words text-sm text-strong">{children}</dd>
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
  return <p className="py-8 text-center text-sm text-muted">{children}</p>;
}
