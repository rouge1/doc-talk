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

export interface SearchHit {
  chunk_id: number;
  file: string;
  chapter: string | null;
  page: number;
  text: string;
  score: number;
  rerank_score: number | null;
  content_hash: string | null;
  chapter_id: number | null;
}

export interface SearchResult {
  query: string;
  hits: SearchHit[];
}

export interface WikiCitation {
  name: string;
  type: string;
  stem: string | null;
}

export interface Citation {
  n: number;
  file: string;
  chapter: string | null;
  page: number;
  content_hash: string | null;
  chapter_id: number | null;
  chunk_id: number | null;
}

export interface ChatAnswer {
  query: string;
  answer: string;
  wiki_citations: WikiCitation[];
  citations: Citation[];
}

export interface Outline {
  hash: string;
  name: string;
  format: string;
  chapters: { id: number; title: string; level: number; page: number }[];
}

export interface ChapterRead {
  hash: string;
  doc_name: string;
  chapter: { id: number; title: string; page: number };
  chunks: { id: number; page: number; text: string }[];
  nav: { prev: number | null; next: number | null };
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface PageInfo {
  hash: string;
  doc_name: string;
  page: number;
  page_count: number;
  width: number;
  height: number;
  image: string;
  rects: Rect[];
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
  search: (q: string) => get<SearchResult>(`/api/search?q=${encodeURIComponent(q)}`),
  chat: (q: string) => get<ChatAnswer>(`/api/chat?q=${encodeURIComponent(q)}`),
  doc: (hash: string) => get<Outline>(`/api/doc/${hash}`),
  chapter: (hash: string, id: number) => get<ChapterRead>(`/api/doc/${hash}/chapter/${id}`),
  page: (hash: string, page: number, chunk?: number | null) =>
    get<PageInfo>(`/api/doc/${hash}/page/${page}${chunk ? `?chunk_id=${chunk}` : ""}`),
};

const isPdf = (file?: string | null) => !!file && file.toLowerCase().endsWith(".pdf");

// Where a search hit / citation should open. PDFs go to the original-page viewer (words
// highlighted on the real page); everything else to the reflowed-text chapter reader.
export const sourcePath = (h: {
  content_hash: string | null;
  file?: string | null;
  page?: number | null;
  chapter_id?: number | null;
  chunk_id?: number | null;
}) => {
  if (!h.content_hash) return "#";
  if (isPdf(h.file) && h.page)
    return `/doc/${h.content_hash}/page/${h.page}${h.chunk_id ? `?focus=${h.chunk_id}` : ""}`;
  if (h.chapter_id)
    return `/doc/${h.content_hash}/chapter/${h.chapter_id}${h.chunk_id ? `?focus=${h.chunk_id}` : ""}`;
  return `/doc/${h.content_hash}`;
};
