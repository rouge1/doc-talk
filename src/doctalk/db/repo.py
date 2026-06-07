"""The ONLY metadata writer (per ``CLAUDE.md``).

Every mutation of the truth store funnels through here; everything else reads. Callers own the
transaction (they pass in a ``Session``); these functions never commit, so they compose inside
the DAG's per-stage ``session_scope``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from doctalk.db.models import (
    Chapter,
    Chunk,
    Claim,
    ClaimSource,
    Entity,
    EntityMerge,
    EntityReview,
    Figure,
    File,
    Image,
    Job,
    JobStatus,
    Link,
    Mention,
    Relation,
    WikiPage,
    utcnow,
)


# --- files -----------------------------------------------------------------


def get_file(session: Session, content_hash: str) -> File | None:
    return session.scalar(select(File).where(File.content_hash == content_hash))


def get_file_id(session: Session, content_hash: str) -> int | None:
    return session.scalar(select(File.id).where(File.content_hash == content_hash))


def upsert_file(
    session: Session,
    *,
    content_hash: str,
    path: str,
    filename: str,
    format: str,
    mime: str,
    byte_size: int,
) -> File:
    """Insert the source row, or refresh the mutable last-seen fields if the bytes already exist.

    Identity is the content hash, so a re-drop from a new path updates ``path`` but creates no
    duplicate row.
    """
    file = get_file(session, content_hash)
    if file is None:
        file = File(
            content_hash=content_hash,
            path=path,
            filename=filename,
            format=format,
            mime=mime,
            byte_size=byte_size,
        )
        session.add(file)
    else:
        file.path = path
        file.filename = filename
        file.format = format
        file.mime = mime
        file.byte_size = byte_size
    return file


# --- jobs ledger -----------------------------------------------------------


def get_job(session: Session, input_hash: str) -> Job | None:
    return session.scalar(select(Job).where(Job.input_hash == input_hash))


def is_stage_done(session: Session, input_hash: str) -> bool:
    """True only when a committed ``done`` row exists for this exact (source, stage, model, params)."""
    return (
        session.scalar(select(Job.status).where(Job.input_hash == input_hash))
        == JobStatus.done
    )


def begin_job(
    session: Session,
    *,
    content_hash: str,
    stage: str,
    input_hash: str,
    model_version: str = "",
    params: dict[str, Any] | None = None,
) -> Job:
    """Mark a stage ``running`` — upserting so a prior ``error``/``running`` row is reused (the
    ``input_hash`` is unique, so we never insert a duplicate)."""
    job = get_job(session, input_hash)
    if job is None:
        job = Job(
            content_hash=content_hash,
            stage=stage,
            input_hash=input_hash,
            model_version=model_version,
            params=params or {},
        )
        session.add(job)
    job.status = JobStatus.running
    job.error = None
    job.started_at = utcnow()
    job.finished_at = None
    return job


def complete_job(session: Session, input_hash: str) -> None:
    job = get_job(session, input_hash)
    if job is None:  # pragma: no cover - defensive
        raise ValueError(f"complete_job: no job row for input_hash={input_hash}")
    job.status = JobStatus.done
    job.error = None
    job.finished_at = utcnow()


def fail_job(session: Session, input_hash: str, error: str) -> None:
    job = get_job(session, input_hash)
    if job is None:  # pragma: no cover - defensive
        raise ValueError(f"fail_job: no job row for input_hash={input_hash}")
    job.status = JobStatus.error
    job.error = error[:4000]
    job.finished_at = utcnow()


# --- document structure (chapters / chunks / links) ------------------------
# Each stage clears its own prior output for a file before writing, so a re-run (e.g. after a
# model/param upgrade) never duplicates rows — "never process processed data" stays honest.


def clear_chapters_for_file(session: Session, file_id: int) -> None:
    # Chunks reference chapters; delete them first to avoid dangling rows.
    session.execute(delete(Chunk).where(Chunk.file_id == file_id))
    session.execute(delete(Chapter).where(Chapter.file_id == file_id))


def clear_links_for_file(session: Session, file_id: int) -> None:
    session.execute(delete(Link).where(Link.file_id == file_id))


def insert_chapters(
    session: Session, file_id: int, rows: list[dict[str, Any]]
) -> list[Chapter]:
    """Insert outline rows and resolve the tree. Each row carries ``parent_ord`` (the ``ord`` of
    its parent, or None); parents are linked after the flush assigns ids. Returns the persisted
    Chapter objects (with ids), in input order."""
    chapters = [
        Chapter(
            file_id=file_id,
            level=r["level"],
            ord=r["ord"],
            title=r["title"],
            page_start=r["page_start"],
            page_end=r["page_end"],
            source=r.get("source", "outline"),
        )
        for r in rows
    ]
    session.add_all(chapters)
    session.flush()  # assign ids
    ord_to_id = {c.ord: c.id for c in chapters}
    for row, chapter in zip(rows, chapters):
        parent_ord = row.get("parent_ord")
        if parent_ord is not None:
            chapter.parent_id = ord_to_id.get(parent_ord)
    session.flush()
    return chapters


def insert_chunks(session: Session, file_id: int, rows: list[dict[str, Any]]) -> None:
    if rows:
        session.execute(insert(Chunk), [{"file_id": file_id, **r} for r in rows])


def insert_links(session: Session, file_id: int, rows: list[dict[str, Any]]) -> None:
    if rows:
        session.execute(insert(Link), [{"file_id": file_id, **r} for r in rows])


def get_chapters(session: Session, file_id: int) -> list[Chapter]:
    return list(
        session.scalars(
            select(Chapter).where(Chapter.file_id == file_id).order_by(Chapter.ord)
        )
    )


def get_chunks(session: Session, file_id: int) -> list[Chunk]:
    return list(
        session.scalars(select(Chunk).where(Chunk.file_id == file_id).order_by(Chunk.ord))
    )


def get_all_file_ids(session: Session) -> list[int]:
    return list(session.scalars(select(File.id).order_by(File.id)))


# --- semantic relations (cross-corpus links) -------------------------------


def clear_relations_for_file(session: Session, file_id: int) -> None:
    """Remove the relations this file authored (src side); a re-run rebuilds them."""
    session.execute(delete(Relation).where(Relation.src_file_id == file_id))


def insert_relations(session: Session, rows: list[dict[str, Any]]) -> None:
    if rows:
        session.execute(insert(Relation), rows)


def get_relations_for_chapter(session: Session, chapter_id: int) -> list[Relation]:
    """Both directions touching this chapter (it as source, or as a target of others)."""
    return list(
        session.scalars(
            select(Relation).where(
                (Relation.src_chapter_id == chapter_id)
                | (Relation.dst_chapter_id == chapter_id)
            )
        )
    )


def get_relations_for_file(session: Session, file_id: int) -> list[Relation]:
    return list(session.scalars(select(Relation).where(Relation.src_file_id == file_id)))


# --- figures / tables ------------------------------------------------------


def clear_figures_for_file(session: Session, file_id: int) -> None:
    session.execute(delete(Figure).where(Figure.file_id == file_id))


def insert_figures(session: Session, file_id: int, rows: list[dict[str, Any]]) -> None:
    if rows:
        session.execute(insert(Figure), [{"file_id": file_id, **r} for r in rows])


def get_figures(session: Session, file_id: int) -> list[Figure]:
    return list(
        session.scalars(select(Figure).where(Figure.file_id == file_id).order_by(Figure.ord))
    )


def get_figures_for_page(session: Session, file_id: int, page: int) -> list[Figure]:
    return list(
        session.scalars(
            select(Figure)
            .where(Figure.file_id == file_id, Figure.page == page)
            .order_by(Figure.ord)
        )
    )


def get_figure(session: Session, figure_id: int) -> Figure | None:
    return session.get(Figure, figure_id)


def figures_needing_ocr(session: Session, file_id: int) -> list[Figure]:
    """Figure rasters (have an ``image_path``) whose ``ocr_text`` has not been set yet."""
    return list(
        session.scalars(
            select(Figure).where(
                Figure.file_id == file_id,
                Figure.image_path.is_not(None),
                Figure.ocr_text.is_(None),
            )
        )
    )


def set_figure_fields(session: Session, figure_id: int, **fields: Any) -> None:
    figure = session.get(Figure, figure_id)
    if figure is None:  # pragma: no cover - defensive
        raise ValueError(f"set_figure_fields: no figure row id={figure_id}")
    for key, value in fields.items():
        setattr(figure, key, value)


# --- images ----------------------------------------------------------------


def upsert_image(session: Session, file_id: int, **fields: Any) -> Image:
    """Create-or-update the images row for a file, setting only the provided fields. Lets the
    image stages (extract -> exif_geo -> vlm_describe) each contribute their slice idempotently."""
    image = session.scalar(select(Image).where(Image.file_id == file_id))
    if image is None:
        image = Image(file_id=file_id)
        session.add(image)
    for key, value in fields.items():
        setattr(image, key, value)
    return image


def get_image(session: Session, file_id: int) -> Image | None:
    return session.scalar(select(Image).where(Image.file_id == file_id))


def get_all_image_file_ids(session: Session) -> list[int]:
    return list(session.scalars(select(Image.file_id).order_by(Image.file_id)))


def get_image_clusters(session: Session, file_ids: list[int]) -> dict[int, int | None]:
    """Current cluster_id for each requested image (file_id -> cluster_id|None). Missing images
    are absent from the result."""
    rows = session.execute(
        select(Image.file_id, Image.cluster_id).where(Image.file_id.in_(file_ids))
    ).all()
    return {fid: cid for fid, cid in rows}


def set_image_cluster(session: Session, file_id: int, cluster_id: int) -> None:
    """Assign an image to a near-duplicate cluster (cluster_id = the component's min file_id)."""
    image = session.scalar(select(Image).where(Image.file_id == file_id))
    if image is None:
        raise ValueError(f"set_image_cluster: no image row for file_id={file_id}")
    image.cluster_id = cluster_id


def relabel_cluster(session: Session, old_cluster_id: int, new_cluster_id: int) -> None:
    """Repoint every image in ``old_cluster_id`` to ``new_cluster_id`` — the single-link merge
    that fires when a freshly-added image bridges two previously-separate clusters."""
    if old_cluster_id == new_cluster_id:
        return
    session.execute(
        update(Image).where(Image.cluster_id == old_cluster_id).values(cluster_id=new_cluster_id)
    )


# --- synthesis layer (Phase 4) ---------------------------------------------


def find_entity_by_norm_key(session: Session, norm_key: str, type_: str) -> Entity | None:
    """The blocking-key lookup the (placeholder) resolver uses: exact normalized name + type.
    The full ``synth_resolve`` (fuzzy + embedding + two-threshold band) supersedes this later."""
    return session.scalar(
        select(Entity).where(Entity.norm_key == norm_key, Entity.type == type_)
    )


def get_or_create_entity(
    session: Session, *, name: str, type_: str, norm_key: str, aliases: list[str] | None = None
) -> Entity:
    """Resolve an extracted entity to a canonical row, creating it if new. PLACEHOLDER resolver:
    exact ``(norm_key, type)`` match only. Merges any new aliases into the existing row."""
    entity = find_entity_by_norm_key(session, norm_key, type_)
    if entity is None:
        entity = Entity(name=name, type=type_, norm_key=norm_key, aliases=list(aliases or []))
        session.add(entity)
        session.flush()  # assign id for claims/mentions in the same stage transaction
        return entity
    if aliases:  # accumulate surface variants we hadn't seen
        merged = list(dict.fromkeys([*(entity.aliases or []), *aliases]))
        if merged != (entity.aliases or []):
            entity.aliases = merged
    return entity


def insert_claim(
    session: Session, *, entity_id: int, file_id: int, text: str, confidence: float = 1.0
) -> Claim:
    """Record one asserted fact about an entity, attributed to the asserting source file."""
    claim = Claim(entity_id=entity_id, file_id=file_id, text=text, confidence=confidence)
    session.add(claim)
    session.flush()
    return claim


def insert_claim_sources(session: Session, claim_id: int, rows: list[dict[str, Any]]) -> None:
    """Provenance rows for a claim: each needs ``file_id`` and an optional ``chunk_id``."""
    if rows:
        session.execute(insert(ClaimSource), [{"claim_id": claim_id, **r} for r in rows])


def insert_mentions(session: Session, file_id: int, rows: list[dict[str, Any]]) -> None:
    """``entity_id`` (+ optional ``chunk_id``) per row — which entities this source touched."""
    if rows:
        session.execute(insert(Mention), [{"file_id": file_id, **r} for r in rows])


def recompute_entity_source_count(session: Session, entity_id: int) -> None:
    """Set ``source_count`` to the number of distinct files mentioning the entity (kept correct
    across re-synths, which delete+reinsert a file's mentions)."""
    n = session.scalar(
        select(func.count(func.distinct(Mention.file_id))).where(Mention.entity_id == entity_id)
    )
    entity = session.get(Entity, entity_id)
    if entity is not None:
        entity.source_count = n or 0


def clear_synth_for_file(session: Session, file_id: int) -> list[int]:
    """Idempotent re-synth: drop this file's mentions + claims (claim_sources cascade). Returns the
    entity ids it touched so the caller can recompute their source counts. Entities themselves are
    canonical/shared and are left in place (a later lint prunes any left orphaned)."""
    touched = set(
        session.scalars(select(Mention.entity_id).where(Mention.file_id == file_id))
    )
    touched |= set(session.scalars(select(Claim.entity_id).where(Claim.file_id == file_id)))
    session.execute(delete(Mention).where(Mention.file_id == file_id))
    session.execute(delete(Claim).where(Claim.file_id == file_id))  # claim_sources cascade
    session.execute(delete(EntityReview).where(EntityReview.file_id == file_id))
    return sorted(touched)


def get_entities(session: Session, limit: int | None = None) -> list[Entity]:
    """All entities, most-referenced first (the catalog view)."""
    query = select(Entity).order_by(Entity.source_count.desc(), Entity.name)
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def get_claims_for_entity(session: Session, entity_id: int) -> list[Claim]:
    return list(
        session.scalars(
            select(Claim).where(Claim.entity_id == entity_id).order_by(Claim.id)
        )
    )


def get_mentions_for_file(session: Session, file_id: int) -> list[Mention]:
    return list(session.scalars(select(Mention).where(Mention.file_id == file_id)))


def get_entity_ids_for_file(session: Session, file_id: int) -> list[int]:
    """Distinct entities a source touched (the pages ``synth_integrate`` must rewrite)."""
    return list(
        dict.fromkeys(session.scalars(select(Mention.entity_id).where(Mention.file_id == file_id)))
    )


def get_claim_sources(session: Session, claim_id: int) -> list[ClaimSource]:
    return list(session.scalars(select(ClaimSource).where(ClaimSource.claim_id == claim_id)))


def get_comention_entity_ids(session: Session, entity_id: int, limit: int = 12) -> list[int]:
    """Entities that co-occur with this one — same chunk first (tight), else same file (loose).
    Drives the ``[[wikilinks]]`` between pages so the wiki stays interlinked, not a bag of orphans."""
    chunk_ids = [
        c
        for c in session.scalars(
            select(Mention.chunk_id).where(
                Mention.entity_id == entity_id, Mention.chunk_id.is_not(None)
            )
        )
    ]
    ids: list[int] = []
    if chunk_ids:
        ids = list(
            dict.fromkeys(
                session.scalars(
                    select(Mention.entity_id).where(
                        Mention.chunk_id.in_(chunk_ids), Mention.entity_id != entity_id
                    )
                )
            )
        )
    if not ids:  # fall back to file-level co-mention
        file_ids = list(
            session.scalars(select(Mention.file_id).where(Mention.entity_id == entity_id))
        )
        if file_ids:
            ids = list(
                dict.fromkeys(
                    session.scalars(
                        select(Mention.entity_id).where(
                            Mention.file_id.in_(file_ids), Mention.entity_id != entity_id
                        )
                    )
                )
            )
    return ids[:limit]


def set_entity_wiki_path(session: Session, entity_id: int, path: str) -> None:
    entity = session.get(Entity, entity_id)
    if entity is not None:
        entity.wiki_path = path


def upsert_wiki_page(session: Session, *, path: str, **fields: Any) -> WikiPage:
    """Create-or-update the catalog row for a page (the body lives on disk; this is the index)."""
    page = session.scalar(select(WikiPage).where(WikiPage.path == path))
    if page is None:
        page = WikiPage(path=path)
        session.add(page)
    for key, value in fields.items():
        setattr(page, key, value)
    session.flush()  # make it visible to the index-regeneration query in the same transaction
    return page


def get_wiki_page_by_path(session: Session, path: str) -> WikiPage | None:
    return session.scalar(select(WikiPage).where(WikiPage.path == path))


def get_wiki_pages_by_kind(session: Session, kind: str) -> list[WikiPage]:
    return list(
        session.scalars(select(WikiPage).where(WikiPage.kind == kind).order_by(WikiPage.title))
    )


# --- entity resolution (synth_resolve) -------------------------------------


def create_entity(
    session: Session,
    *,
    name: str,
    type_: str,
    norm_key: str,
    aliases: list[str] | None = None,
    acronyms: list[str] | None = None,
    status: str = "active",
    glossary_defined: bool = False,
) -> Entity:
    """Mint a new canonical entity (a resolver NEW/DEFER decision). Embedding is attached after."""
    entity = Entity(
        name=name,
        type=type_,
        norm_key=norm_key,
        aliases=list(aliases or []),
        acronyms=list(acronyms or []),
        status=status,
        glossary_defined=glossary_defined,
    )
    session.add(entity)
    session.flush()
    return entity


def add_entity_aliases(session: Session, entity_id: int, surfaces: list[str]) -> None:
    """Accumulate new surface variants onto an entity (a MATCH that saw a fresh spelling)."""
    entity = session.get(Entity, entity_id)
    if entity is None or not surfaces:
        return
    merged = list(dict.fromkeys([*(entity.aliases or []), *surfaces]))
    if merged != (entity.aliases or []):
        entity.aliases = merged


def set_entity_name_embedding_id(session: Session, entity_id: int, embedding_id: int | None) -> None:
    entity = session.get(Entity, entity_id)
    if entity is not None:
        entity.name_embedding_id = embedding_id


def set_entity_status(session: Session, entity_id: int, status: str) -> None:
    entity = session.get(Entity, entity_id)
    if entity is not None:
        entity.status = status


def find_entities_by_norm_keys(
    session: Session, keys: set[str], types: set[str] | None = None
) -> list[Entity]:
    """Indexed blocking: entities whose norm_key is in ``keys`` (exact/alias/acronym-normalized),
    excluding ones already merged away. Optionally gated to compatible types."""
    if not keys:
        return []
    query = select(Entity).where(
        Entity.norm_key.in_(list(keys)), Entity.status != "merged_into"
    )
    if types:
        query = query.where(Entity.type.in_(list(types)))
    return list(session.scalars(query))


def scan_alias_acronym_candidates(
    session: Session, surface_norms: set[str], types: set[str] | None = None, limit: int = 300
) -> list[Entity]:
    """Bounded blocking by stored alias/acronym surfaces (the acronym↔expansion bridge). Compares
    lowercased surfaces; ``limit`` caps the scan (a learned/indexed path supersedes this at scale)."""
    if not surface_norms:
        return []
    query = select(Entity).where(Entity.status != "merged_into")
    if types:
        query = query.where(Entity.type.in_(list(types)))
    out: list[Entity] = []
    for entity in session.scalars(query.limit(limit)):
        toks = {a.lower().strip() for a in (entity.aliases or [])}
        toks |= {a.lower().strip() for a in (entity.acronyms or [])}
        if toks & surface_norms:
            out.append(entity)
    return out


def add_entity_review(
    session: Session,
    *,
    mention_surface: str,
    mention_type: str,
    file_id: int,
    entity_id: int | None,
    payload: dict,
    llm_verdict: str | None = None,
) -> EntityReview:
    """Queue an ambiguous resolution for human review (the genuinely-hard slice)."""
    review = EntityReview(
        mention_surface=mention_surface,
        mention_type=mention_type,
        file_id=file_id,
        entity_id=entity_id,
        payload=payload,
        llm_verdict=llm_verdict,
    )
    session.add(review)
    session.flush()
    return review


def get_open_reviews(session: Session, limit: int | None = None) -> list[EntityReview]:
    query = select(EntityReview).where(EntityReview.state == "open").order_by(EntityReview.id)
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def merge_entities(
    session: Session, from_id: int, into_id: int, reason: str = ""
) -> EntityMerge:
    """Fold ``from`` into ``into`` (reversible, auditable). Repoints mentions + claims (claim_sources
    ride along on the claim), unions aliases/acronyms, marks ``from`` merged_into, recomputes the
    survivor's source count, and records an ``entity_merges`` row. DB-only — the caller handles the
    name-vector cleanup, page redirect, and git commit."""
    if from_id == into_id:
        raise ValueError("merge_entities: from and into are the same entity")
    src = session.get(Entity, from_id)
    dst = session.get(Entity, into_id)
    if src is None or dst is None:
        raise ValueError(f"merge_entities: missing entity ({from_id} -> {into_id})")

    session.execute(update(Mention).where(Mention.entity_id == from_id).values(entity_id=into_id))
    session.execute(update(Claim).where(Claim.entity_id == from_id).values(entity_id=into_id))

    dst.aliases = list(dict.fromkeys([*(dst.aliases or []), *(src.aliases or []), src.name]))
    dst.acronyms = list(dict.fromkeys([*(dst.acronyms or []), *(src.acronyms or [])]))
    src.status = "merged_into"
    src.wiki_path = dst.wiki_path  # redirect

    merge = EntityMerge(from_id=from_id, into_id=into_id, reason=reason)
    session.add(merge)
    session.flush()
    recompute_entity_source_count(session, into_id)
    return merge


def get_entity_merges(session: Session) -> list[EntityMerge]:
    return list(session.scalars(select(EntityMerge).order_by(EntityMerge.id)))
