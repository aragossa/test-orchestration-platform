import { useCallback, useEffect, useState } from "react";

interface PollingState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => Promise<void>;
}

/**
 * Fetch `load()` immediately and then every `intervalMs` while `active` is
 * true. A new `load` (for example after a filter change) refetches at once.
 * The backend owns the run lifecycle; the UI only re-reads it.
 */
export function usePolling<T>(
  load: () => Promise<T>,
  intervalMs: number,
  active = true,
): PollingState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const next = await load();
      setData(next);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [load]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      if (cancelled) return;
      await refresh();
      if (!cancelled && active) {
        timer = window.setTimeout(tick, intervalMs);
      }
    };
    void tick();

    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [refresh, intervalMs, active]);

  return { data, error, loading, refresh };
}
