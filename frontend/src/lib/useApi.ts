"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, apiGet } from "./api";

export interface ApiState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

interface Result<T> {
  key: string;
  data: T | null;
  error: string | null;
}

/**
 * Fetch on mount, on path change, and on demand.
 *
 * The dashboard reads a local API from the browser rather than on the server:
 * `next build` then needs no backend, and a backend that is down is a visible
 * message in the UI instead of a broken page.
 *
 * State is written only from the fetch callbacks, never synchronously in the
 * effect body. "Loading" is derived by comparing the key of the result we hold
 * against the key we want, which avoids a cascading render per request.
 */
export function useApi<T>(path: string | null): ApiState<T> {
  const [nonce, setNonce] = useState(0);
  const [result, setResult] = useState<Result<T>>({ key: "", data: null, error: null });

  const key = path === null ? "" : `${path}#${nonce}`;
  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (path === null) return;
    let cancelled = false;
    apiGet<T>(path)
      .then((body) => {
        if (!cancelled) setResult({ key: `${path}#${nonce}`, data: body, error: null });
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setResult({
          key: `${path}#${nonce}`,
          data: null,
          error: cause instanceof ApiError ? cause.message : String(cause),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [path, nonce]);

  const current = result.key === key;
  return {
    data: current ? result.data : null,
    error: current ? result.error : null,
    loading: path !== null && !current,
    reload,
  };
}
