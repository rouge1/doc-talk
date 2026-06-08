import { useEffect, useState } from "react";

// Minimal data hook: run an async fetcher, expose {data, error, loading}, re-run on key change.
export function useFetch<T>(fn: () => Promise<T>, key: string) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    setError(null);
    fn()
      .then((d) => alive && setData(d))
      .catch((e) => alive && setError(String(e)));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return { data, error, loading: !data && !error };
}
