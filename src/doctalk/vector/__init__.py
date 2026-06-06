"""Derived vector index (LanceDB). Never authoritative — ``doctalk rebuild-index`` regenerates
it from MySQL. Stores only vectors + the scalars needed to filter and to join back to the truth
store (chunk_id/file_id/chapter_id/page); chunk text lives in MySQL."""
