"""Phase 1 image half (hermetic): image_extract + exif_geo on a generated PNG.

Covers the deterministic, offline part of the image pipeline (dimensions + graceful no-EXIF
handling). CLIP embedding and VLM description need models/network and are verified manually.
"""

from __future__ import annotations

import pytest
from PIL import Image as PILImage

from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.hashing import hash_file
from doctalk.ingest.dag import Stage, run_dag
from doctalk.ingest.stages import exif_geo, identify, image_extract


def _image_stages() -> list[Stage]:
    return [
        Stage("identify", identify.run),
        Stage("image_extract", image_extract.run, deps=("identify",)),
        Stage("exif_geo", exif_geo.run, deps=("image_extract",)),
    ]


@pytest.fixture
def sample_png(tmp_path):
    path = tmp_path / "red.png"
    PILImage.new("RGB", (64, 48), (200, 10, 10)).save(path)
    return path


def test_image_extract_dims_and_graceful_no_exif(db, sample_png):
    content_hash = hash_file(sample_png)
    with session_scope() as s:
        repo.upsert_file(
            s,
            content_hash=content_hash,
            path=str(sample_png),
            filename=sample_png.name,
            format="png",
            mime="image/png",
            byte_size=sample_png.stat().st_size,
        )
    results = run_dag(content_hash, _image_stages(), file_path=str(sample_png))
    assert [r.status for r in results] == ["done", "done", "done"]

    with session_scope() as s:
        image = repo.get_image(s, repo.get_file_id(s, content_hash))
        assert image is not None
        assert image.width == 64 and image.height == 48
        # a generated PNG has no EXIF -> null capture time / GPS, handled without error
        assert image.exif_datetime is None
        assert image.gps_lat is None and image.geo_country is None
