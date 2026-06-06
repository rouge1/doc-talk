"""Pure unit tests for the hybrid image-search filter (no models, no DB)."""

from __future__ import annotations

from datetime import datetime, timezone

from doctalk.query.hybrid import ImageFilter, build_where, month_range


def test_build_where_none_when_empty():
    assert build_where(ImageFilter()) is None


def test_build_where_combines_clauses():
    where = build_where(ImageFilter(format="PNG", min_bytes=102400, geo_country="CA"))
    assert "format = 'png'" in where
    assert "byte_size > 102400" in where
    assert "geo_country = 'CA'" in where
    assert " AND " in where


def test_build_where_sanitizes_format_against_injection():
    where = build_where(ImageFilter(format="pn'g; drop"))
    assert where == "format = 'pngdrop'"  # quotes/semicolons/spaces stripped
    assert ";" not in where and where.count("'") == 2


def test_month_range_single_month():
    start, end = month_range(2026, 4)
    assert datetime.fromtimestamp(start, timezone.utc) == datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert datetime.fromtimestamp(end + 1, timezone.utc) == datetime(2026, 5, 1, tzinfo=timezone.utc)


def test_month_range_whole_year():
    start, end = month_range(2026)
    assert datetime.fromtimestamp(start, timezone.utc) == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert datetime.fromtimestamp(end + 1, timezone.utc) == datetime(2027, 1, 1, tzinfo=timezone.utc)
