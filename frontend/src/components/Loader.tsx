"use client";

import type { ReactNode } from "react";
import type { ApiState } from "@/lib/useApi";

/**
 * Renders the three states every panel has: loading, failed, and loaded.
 *
 * Failure is spelled out rather than swallowed. An empty dashboard that is
 * empty because the backend is down looks exactly like a quiet clinic, and
 * that is the one confusion this tool cannot afford.
 */
export function Loader<T>({
  state,
  children,
}: {
  state: ApiState<T>;
  children: (data: T) => ReactNode;
}) {
  if (state.loading && state.data === null) {
    return <p className="py-6 text-center text-sm text-muted">Loading…</p>;
  }
  if (state.error !== null) {
    return (
      <div className="rounded border border-rose-500/40 bg-rose-500/5 p-4 text-sm">
        <p className="font-semibold text-danger">Could not load this view.</p>
        <p className="mt-1 text-muted">{state.error}</p>
        <button
          type="button"
          onClick={state.reload}
          className="mt-3 rounded border border-edge px-2 py-1 text-xs hover:bg-edge/50"
        >
          Retry
        </button>
      </div>
    );
  }
  if (state.data === null) return null;
  return <>{children(state.data)}</>;
}
