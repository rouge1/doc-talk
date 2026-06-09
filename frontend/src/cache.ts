// Generic client-side result cache: memory (instant in-session) backed by localStorage (survives
// reloads / closed tabs). Namespaced per view so Search, Gallery, etc. don't collide. Mirrors the
// pattern proven for Ask (answerStore) but typed generically. Use it so switching nav tabs or
// following a link never throws away results the user already waited for.

const MEM = new Map<string, unknown>();
const key = (ns: string, k: string) => `doctalk:${ns}:${k}`;
const lastKey = (ns: string) => `doctalk:${ns}:__last`;

export function getCached<T>(ns: string, k: string): T | null {
  const mk = key(ns, k);
  if (MEM.has(mk)) return MEM.get(mk) as T;
  try {
    const raw = localStorage.getItem(mk);
    if (raw) {
      const v = JSON.parse(raw) as T;
      MEM.set(mk, v);
      return v;
    }
  } catch {
    /* storage blocked — fall back to network */
  }
  return null;
}

export function setCached<T>(ns: string, k: string, value: T): void {
  const mk = key(ns, k);
  MEM.set(mk, value);
  try {
    localStorage.setItem(mk, JSON.stringify(value));
    localStorage.setItem(lastKey(ns), k);
  } catch {
    /* quota / unavailable — in-memory copy still serves this session */
  }
}

// The most recent key seen in this namespace — lets a bare view (e.g. clicking the SEARCH nav tab
// with no query) restore the last result instead of showing a blank page.
export function getLastKey(ns: string): string | null {
  try {
    return localStorage.getItem(lastKey(ns));
  } catch {
    return null;
  }
}

export function setLastKey(ns: string, k: string): void {
  try {
    localStorage.setItem(lastKey(ns), k);
  } catch {
    /* storage unavailable */
  }
}
