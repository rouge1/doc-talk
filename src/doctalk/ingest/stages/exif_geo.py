"""exif_geo — extract capture time + GPS from a photo's EXIF, reverse-geocode to a country.

Reads EXIF via Pillow (datetime + GPS). Reverse geocoding uses the optional ``reverse_geocoder``
package (offline); if it's not installed, GPS coordinates are still stored and country is left
null. Images without EXIF (e.g. most PNGs) simply get null fields — that's expected, not an error.
"""

from __future__ import annotations

from datetime import datetime

from PIL import ExifTags, Image

from doctalk.db import repo
from doctalk.ingest.dag import StageContext

_DATETIME_TAGS = (36867, 306)  # DateTimeOriginal (Exif IFD), DateTime (base IFD)


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%Y:%m:%d %H:%M:%S")
    except (ValueError, AttributeError):
        return None


def _ratio_to_float(values) -> float:
    d, m, s = (float(v) for v in values)
    return d + m / 60.0 + s / 3600.0


def _read_exif(path: str) -> tuple[datetime | None, float | None, float | None]:
    with Image.open(path) as im:
        exif = im.getexif()
        if not exif:
            return None, None, None

        dt = None
        for tag in _DATETIME_TAGS:
            if tag in exif:
                dt = _parse_dt(str(exif[tag]))
                if dt:
                    break
        # DateTimeOriginal lives in the Exif sub-IFD on many cameras.
        if dt is None:
            sub = exif.get_ifd(ExifTags.IFD.Exif)
            if 36867 in sub:
                dt = _parse_dt(str(sub[36867]))

        lat = lon = None
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        if gps and 2 in gps and 4 in gps:
            lat = _ratio_to_float(gps[2])
            lon = _ratio_to_float(gps[4])
            if str(gps.get(1, "N")).upper().startswith("S"):
                lat = -lat
            if str(gps.get(3, "E")).upper().startswith("W"):
                lon = -lon
        return dt, lat, lon


def _reverse_geocode(lat: float, lon: float) -> tuple[str | None, str | None]:
    try:
        import reverse_geocoder as rg  # optional dependency
    except ImportError:
        return None, None
    result = rg.search((lat, lon), mode=1)[0]  # mode=1: single-threaded
    return result.get("cc"), result.get("name")


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"exif_geo: no file row for {ctx.content_hash}")

    dt, lat, lon = _read_exif(ctx.file_path)
    country = place = None
    if lat is not None and lon is not None:
        country, place = _reverse_geocode(lat, lon)

    repo.upsert_image(
        ctx.session,
        file_id,
        exif_datetime=dt,
        gps_lat=lat,
        gps_lon=lon,
        geo_country=country,
        geo_place=place,
    )
    ctx.scratch["exif"] = {"datetime": dt, "gps": (lat, lon), "country": country}
