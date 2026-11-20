"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, getExpiries } from "./api";

export function useExpiries(asset: string) {
  const [dates, setDates] = useState<string[] | null>(null);
  const [choice, choose] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const active = useRef<AbortController | null>(null);
  const refresh = useCallback(async () => {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    try {
      const next = await getExpiries(asset, controller.signal);
      if (controller.signal.aborted) return;
      const sorted = [...new Set(next)].sort();
      setDates(sorted);
      choose(current => sorted.includes(current) ? current : sorted[0] ?? "");
      setError(null);
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof ApiError ? e : new ApiError("Could not load end times", 0));
    } finally {
      if (active.current === controller) active.current = null;
    }
  }, [asset]);
  useEffect(() => {
    const initial = window.setTimeout(refresh, 0);
    const timer = window.setInterval(refresh, 60_000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
      active.current?.abort();
      active.current = null;
    };
  }, [refresh]);
  return {expiries: dates ?? [], expiry: choice, setExpiry: choose, loading: dates === null && !error, error, refresh};
}
