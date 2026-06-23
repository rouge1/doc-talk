import { useEffect, useState } from "react";

// A tiny stale-while-revalidate cache, keyed by the fetch `key`. Revisiting a page (e.g. coming back
// from an entity) renders the last result synchronously — no loading flash, no rebuild — while a fresh
// fetch runs in the background. Rendering at full height immediately is also what lets scroll position
// be restored on back-navigation (see ScrollManager). Mutations bump the key, so they still refetch.
const cache = new Map<string, unknown>();

// Minimal data hook: run an async fetcher, expose {data, error, loading}, re-run on key change.
export function useFetch<T>(fn: () => Promise<T>, key: string) {
  const [data, setData] = useState<T | null>(() => (cache.get(key) as T) ?? null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setData((cache.get(key) as T) ?? null); // show the cached value instantly (or clear on a new key)
    setError(null);
    fn()
      .then((d) => {
        cache.set(key, d);
        if (alive) setData(d);
      })
      .catch((e) => alive && setError(String(e)));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return { data, error, loading: !data && !error };
}
