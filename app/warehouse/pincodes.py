"""The pincode table behind the nearest-warehouse default - offline, no maps API.

Source: India Post's All-India Pincode Directory (with latitude and longitude),
published on data.gov.in under the Government Open Data Licence - India. It
can only be downloaded in a browser (a form and captcha), so:

  flask pincodes-build <directory.csv>   compact the downloaded CSV into
                                         data/pincode_geo.csv.gz (one row per
                                         pincode) - committed to the repo
  flask pincodes-load                    load that file into pincode_geo (run
                                         by the deploy's migrate step; does
                                         nothing when already loaded)

A pincode's point is the average of its offices' coordinates that fall inside
India; with none, the average of its district's pincodes ("district"), then of
the pincodes sharing its first three digits - its sorting district ("area").
Each row records which level was used; a pincode with no point at all
(the 2019 directory has none) is kept with its district and state ("none").

Bundled now: Pincode_30052019.csv (India Post directory of 30 May 2019, 19,252
pincodes, no coordinates) - warehouses are ranked by district, area and state
until a directory with latitude/longitude replaces it (same command).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import logging
from collections import defaultdict
from pathlib import Path

from app.extensions import db
from app.models.warehouse import PincodeGeo

log = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "pincode_geo.csv.gz"
INDIA = (6.0, 37.6, 68.0, 97.5)   # lat min, lat max, lng min, lng max
COLUMNS = ("pincode", "lat", "lng", "district", "state", "level")


def _num(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _in_india(lat, lng) -> bool:
    return lat is not None and lng is not None and INDIA[0] <= lat <= INDIA[1] and INDIA[2] <= lng <= INDIA[3]


def build(rows) -> list[dict]:
    """Directory rows (dicts with pincode, latitude, longitude, district,
    statename - any case) -> one row per pincode with its level."""
    points: dict[str, list[tuple[float, float]]] = defaultdict(list)
    meta: dict[str, tuple[str, str]] = {}
    for raw in rows:
        row = {str(k or "").strip().lower(): v for k, v in raw.items()}
        pin = "".join(c for c in str(row.get("pincode") or "") if c.isdigit())
        if len(pin) != 6:
            continue
        meta.setdefault(pin, (str(row.get("district") or row.get("districtname") or "").strip().title()[:80],
                              str(row.get("statename") or row.get("state") or "").strip().title()[:60]))
        lat, lng = _num(row.get("latitude")), _num(row.get("longitude"))
        if _in_india(lat, lng):
            points[pin].append((lat, lng))
    own = {pin: (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) for pin, pts in points.items()}
    by_district, by_area = defaultdict(list), defaultdict(list)
    for pin, point in own.items():
        by_district[meta[pin]].append(point)
        by_area[pin[:3]].append(point)

    def mean(pts):
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) if pts else None

    out = []
    for pin in sorted(meta):
        district, state = meta[pin]
        if pin in own:
            point, level = own[pin], "own"
        elif mean(by_district.get(meta[pin], [])):
            point, level = mean(by_district[meta[pin]]), "district"
        elif mean(by_area.get(pin[:3], [])):
            point, level = mean(by_area[pin[:3]]), "area"
        else:
            point, level = None, "none"          # no point anywhere near: its district and state still help
        out.append({"pincode": pin, "lat": round(point[0], 5) if point else "", "lng": round(point[1], 5) if point else "",
                    "district": district, "state": state, "level": level})
    return out


def write(rows: list[dict], path: Path = DATA_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        fh.write(buf.getvalue())


def read(path: Path = DATA_FILE) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def checksum(path: Path = DATA_FILE) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def load(path: Path = DATA_FILE, force: bool = False) -> int:
    """Replace pincode_geo with the bundled file (skipped when the table already
    has as many pincodes, and as many with a point, unless force). Returns rows
    loaded (0 = nothing to do)."""
    if not path.exists():
        log.warning("pincodes: %s isn't there - the nearest-warehouse default falls back to each branch's "
                    "fallback warehouse until it is", path)
        return 0
    rows = read(path)
    with_points = sum(1 for r in rows if r.get("lat"))
    if not force and PincodeGeo.query.count() == len(rows) and \
            PincodeGeo.query.filter(PincodeGeo.lat.isnot(None)).count() == with_points:
        return 0
    PincodeGeo.query.delete()
    db.session.bulk_insert_mappings(PincodeGeo, [
        {"pincode": r["pincode"], "lat": float(r["lat"]) if r.get("lat") else None,
         "lng": float(r["lng"]) if r.get("lng") else None, "district": r.get("district") or None,
         "state": r.get("state") or None, "level": r.get("level") or "own"} for r in rows])
    db.session.commit()
    levels = defaultdict(int)
    for r in rows:
        levels[r.get("level") or "own"] += 1
    log.info("pincodes: loaded %s (%s)", len(rows), dict(levels))
    return len(rows)


def register(app) -> None:
    import click

    @app.cli.command("pincodes-build")
    @click.argument("source", type=click.Path(exists=True, dir_okay=False))
    def pincodes_build(source):
        """Compact India Post's directory CSV into data/pincode_geo.csv.gz."""
        # India Post's files are not always UTF-8 (a stray Latin-1 byte in an office name).
        with open(source, encoding="latin-1", newline="") as fh:
            rows = build(csv.DictReader(fh))
        write(rows)
        levels = defaultdict(int)
        for r in rows:
            levels[r["level"]] += 1
        click.echo(f"{len(rows)} pincodes -> {DATA_FILE} ({dict(levels)})")

    @app.cli.command("pincodes-load")
    @click.option("--force", is_flag=True)
    def pincodes_load(force):
        """Load data/pincode_geo.csv.gz into the pincode table."""
        n = load(force=force)
        click.echo(f"{n} pincodes loaded" if n else "pincode table already up to date (or no file)")
