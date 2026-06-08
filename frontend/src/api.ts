// Typed client for the doctalk JSON API (/api). Dev requests are proxied to FastAPI by Vite.

export interface Stats {
  documents: number;
  images: number;
  entities: number;
  claims: number;
  queries: number;
  reviews: number;
}

export interface Doc {
  hash: string;
  name: string;
  format: string;
  chapters: number;
  chunks: number;
}

export interface Library {
  documents: Doc[];
  images: number;
}

export interface EntityRef {
  name: string;
  stem: string | null;
  claims: number;
  sources: number;
}

export interface WikiIndex {
  groups: { type: string; entities: EntityRef[] }[];
  queries: { title: string; stem: string }[];
  reviews: number;
  totals: { entities: number; claims: number; queries: number };
}

export interface Claim {
  text: string;
  status: string;
  sources: string[];
}

export interface Entity {
  name: string;
  type: string;
  aliases: string[];
  sources: number;
  claims: Claim[];
  related: { name: string; stem: string }[];
}

export interface QueryPage {
  title: string;
  html: string;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return (await res.json()) as T;
}

export const api = {
  stats: () => get<Stats>("/api/stats"),
  library: () => get<Library>("/api/library"),
  wiki: () => get<WikiIndex>("/api/wiki"),
  entity: (stem: string) => get<Entity>(`/api/wiki/entity/${stem}`),
  query: (stem: string) => get<QueryPage>(`/api/wiki/query/${stem}`),
};
