/** Display helpers. Nothing here interprets clinical content -- it formats. */

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { timeStyle: "short" });
}

export function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" });
}

export function formatMs(ms: number | null): string {
  if (ms === null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${ms.toFixed(1)} ms`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  const whole = Math.round(seconds);
  const minutes = Math.floor(whole / 60);
  return minutes > 0 ? `${minutes}m ${whole % 60}s` : `${whole}s`;
}

/**
 * Money is formatted from the string the API sent, never re-parsed into a
 * float and back: the backend prices in Decimal and the dashboard must not
 * quietly disagree with the ledger that enforces the budget.
 */
export function formatUsd(value: string): string {
  const amount = Number(value);
  if (Number.isNaN(amount)) return value;
  return amount === 0 ? "$0.00" : `$${amount.toFixed(amount < 0.01 ? 4 : 2)}`;
}

export function titleise(value: string): string {
  return value.replace(/[._-]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
