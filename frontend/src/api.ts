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

export interface SourceCard {
  title: string;
  stem: string;
  format: string;
  chapters: number;
  entities: number;
  claims: number;
  ingested: string | null;
}

export interface Library {
  documents: Doc[];
  images: number;
  sources: SourceCard[];
}

export interface SourceProfile {
  title: string;
  hash: string;
  format: string;
  size: string;
  chapters: number;
  entities: number;
  claims: number;
  ingested: string | null;
  lead: string; // rendered HTML (entity wikilinks)
  contents: { title: string; chapter_id: number; entities: number }[];
  key_entities: { name: string; stem: string | null }[];
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
  source?: "keyword" | "semantic" | "both" | null; // which arm surfaced the hit
}

export type SearchMode = "hybrid" | "simple";

export interface SearchResult {
  query: string;
  hits: SearchHit[];
  mode?: SearchMode;
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
  chapters: {
    id: number;
    title: string;
    level: number;
    page: number;
    first_chunk: number | null;
  }[];
}

const RENDERABLE_FMT = /^(pdf|docx|doc|odt|rtf|pptx|ppt|xlsx)$/i;

// Open a document section on its ORIGINAL rendered page — but plainly (no highlight): browsing the
// table of contents isn't a search hit, so there's no specific passage to mark. The reflowed-text
// reader is the fallback for formats we can't rasterize.
export const chapterPath = (
  hash: string,
  format: string,
  c: { id: number; page: number; first_chunk: number | null },
) => {
  if (format.toLowerCase() === "pdf" && c.page) return `/doc/${hash}/page/${c.page}`;
  if (RENDERABLE_FMT.test(format) && c.first_chunk)
    return `/doc/${hash}/passage/${c.first_chunk}?nohl=1`; // locate the page, but don't highlight
  return `/doc/${hash}/chapter/${c.id}`;
};

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

export interface JobStage {
  name: string;
  status: "done" | "running" | "pending" | "error";
}

export interface JobFile {
  hash: string;
  name: string;
  format: string;
  stages: JobStage[];
  done: number;
  total: number;
  state: "done" | "running" | "pending" | "error";
}

export interface JobsData {
  totals: { done: number; running: number; pending: number; error: number };
  files: JobFile[];
  errors: { hash: string; name: string; stage: string; error: string }[];
}

export interface GalleryItem {
  file_id: number;
  name: string;
  desc: string | null;
  fmt: string;
  kb: number;
  score: number | null;
  when: string | null;
  geo: string | null;
  dups: number;
  image: string;
}

export interface Gallery {
  query: string;
  items: GalleryItem[];
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
  jobs: () => get<JobsData>("/api/jobs"),
  entity: (stem: string) => get<Entity>(`/api/wiki/entity/${stem}`),
  source: (stem: string) => get<SourceProfile>(`/api/wiki/source/${stem}`),
  query: (stem: string) => get<QueryPage>(`/api/wiki/query/${stem}`),
  search: (q: string, mode: SearchMode = "hybrid") =>
    get<SearchResult>(`/api/search?q=${encodeURIComponent(q)}&mode=${mode}`),
  chat: (q: string) => get<ChatAnswer>(`/api/chat?q=${encodeURIComponent(q)}`),
  doc: (hash: string) => get<Outline>(`/api/doc/${hash}`),
  chapter: (hash: string, id: number) => get<ChapterRead>(`/api/doc/${hash}/chapter/${id}`),
  page: (hash: string, page: number, chunk?: number | null, hl?: string | null) => {
    const p = new URLSearchParams();
    if (hl) p.set("q", hl); // highlight the search query's terms (takes precedence)
    else if (chunk) p.set("chunk_id", String(chunk)); // else highlight the whole cited chunk
    return get<PageInfo>(`/api/doc/${hash}/page/${page}${p.toString() ? `?${p}` : ""}`);
  },
  find: (hash: string, chunk: number) =>
    get<{ page: number }>(`/api/doc/${hash}/find?chunk_id=${chunk}`),
  gallery: (q: string, fmt: string, minKb: string) => {
    const p = new URLSearchParams();
    if (q) p.set("q", q);
    if (fmt) p.set("fmt", fmt);
    if (minKb) p.set("min_kb", minKb);
    return get<Gallery>(`/api/gallery${p.toString() ? `?${p}` : ""}`);
  },
};

const reExt = (file: string | null | undefined, re: RegExp) => !!file && re.test(file);
const PDF = /\.pdf$/i;
// LibreOffice-renderable office formats — shown as the real page too (via a one-time locate).
const OFFICE = /\.(docx|doc|odt|rtf|pptx|ppt|xlsx)$/i;

// Where a search hit / citation should open, keeping the original document on screen:
//  - native PDF: straight to the page viewer (the page is known);
//  - office doc: the passage route, which locates the rendered page first;
//  - otherwise: the reflowed-text chapter reader.
// `highlight` (the search query) is threaded through so a search click lights up the words you
// searched; without it (an Ask citation) the whole cited chunk is highlighted via ?focus.
export const sourcePath = (
  h: {
    content_hash: string | null;
    file?: string | null;
    page?: number | null;
    chapter_id?: number | null;
    chunk_id?: number | null;
  },
  highlight?: string,
) => {
  if (!h.content_hash) return "#";
  const hl = highlight ? `q=${encodeURIComponent(highlight)}` : "";
  if (reExt(h.file, PDF) && h.page) {
    const qs = hl || (h.chunk_id ? `focus=${h.chunk_id}` : "");
    return `/doc/${h.content_hash}/page/${h.page}${qs ? `?${qs}` : ""}`;
  }
  if (reExt(h.file, OFFICE) && h.chunk_id)
    return `/doc/${h.content_hash}/passage/${h.chunk_id}${hl ? `?${hl}` : ""}`;
  if (h.chapter_id)
    return `/doc/${h.content_hash}/chapter/${h.chapter_id}${h.chunk_id ? `?focus=${h.chunk_id}` : ""}`;
  return `/doc/${h.content_hash}`;
};
