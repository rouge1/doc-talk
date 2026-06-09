import type { ChatAnswer } from "./api";

// Persist chat answers so navigating to a citation (then Back), switching tabs, or reloading restores
// the answer instantly instead of re-running the ~minute-long local pipeline. In-memory Map covers
// in-session navigation; localStorage survives a reload / closed tab. Keyed by the normalized question.

const MEM = new Map<string, ChatAnswer>();
const norm = (q: string) => q.trim().toLowerCase();
const lsKey = (q: string) => `doctalk:answer:${norm(q)}`;
const LAST_KEY = "doctalk:lastQuestion";

// The most recent question asked — so landing on a bare /chat (e.g. clicking the ASK nav tab) can
// restore the last answer instead of showing a blank page.
export function getLastQuestion(): string | null {
  try {
    return localStorage.getItem(LAST_KEY);
  } catch {
    return null;
  }
}

export function setLastQuestion(q: string): void {
  if (!q.trim()) return;
  try {
    localStorage.setItem(LAST_KEY, q.trim());
  } catch {
    /* storage unavailable */
  }
}

export function getCachedAnswer(q: string): ChatAnswer | null {
  const k = norm(q);
  if (!k) return null;
  const hit = MEM.get(k);
  if (hit) return hit;
  try {
    const raw = localStorage.getItem(lsKey(q));
    if (raw) {
      const data = JSON.parse(raw) as ChatAnswer;
      MEM.set(k, data);
      return data;
    }
  } catch {
    /* storage blocked/unavailable — fall back to network */
  }
  return null;
}

export function setCachedAnswer(q: string, data: ChatAnswer): void {
  const k = norm(q);
  if (!k) return;
  MEM.set(k, data);
  try {
    localStorage.setItem(lsKey(q), JSON.stringify(data));
    localStorage.setItem(LAST_KEY, q.trim());
  } catch {
    /* quota exceeded / unavailable — the in-memory copy still serves this session */
  }
}
