#!/usr/bin/env python3
"""Classify historical 506 Sports NFL coverage maps by Nielsen DMA polygon.

Inputs
------
* Acquisition manifest.csv produced by acquire_506_maps.py
* Downloaded map_archive directory referenced by the manifest
* DMA boundary file. Recommended: simzou/nielsen-dma `nielsentopo.json`
  (TopoJSON in longitude/latitude). Standard lon/lat GeoJSON is also supported.

Outputs
-------
* historical_dma_assignments.csv
* map_diagnostics.csv
* legend_review.csv
* classifier_summary.json

The classifier:
1. Normalizes network/window from the original image URL (not the occasionally
   incorrect `slot` column in older acquisition manifests).
2. Selects the final/highest revision for each season/week/network/window.
3. Projects every DMA polygon into the 1280x720 506 map coordinate system.
4. Samples many points across the polygon and classifies the dominant map color.
5. Attempts to obtain color->game legends from the 506 weekly HTML. If that
   fails, the DMA/color result is retained and the map is listed for review.

The image georeference is calibrated from the validated 2025 Week 1 506 maps.
The calibration fit to the 41-market validation coordinates with sub-pixel
residuals and is scaled automatically for map images that are not 1280x720.

This is a research/prototype pipeline. 506 Sports maps are unofficial and can
change during the week. The script therefore records the selected map revision,
source URL, SHA-256, confidence, and legend resolution status for auditability.
"""
from __future__ import annotations

import argparse
import colorsys
import csv
import hashlib
import json
import math
import os
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup, NavigableString, Tag
    from PIL import Image
    from shapely.geometry import MultiPolygon, Point, Polygon, shape
    from shapely.prepared import prep
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency. Run:\n"
        "  py -m pip install pillow shapely beautifulsoup4 requests\n\n"
        f"Import error: {exc}"
    )

BASE_W = 1280.0
BASE_H = 720.0

# Calibrated against the 2025 Week 1 506 maps using 41 major-market points.
# Features are [longitude_deg, web_mercator_y, 1].
PX_COEF = (21.10656425, 9.17874552, 2656.72497789)
PY_COEF = (-0.0419217112, -1210.80604, 1239.89688)

UA = "nfl-dma-history-research/0.4 (+historical broadcast-map research)"

# Stable 506-style fill prototypes observed on the validated maps.  The nearest
# prototype is used only after a saturation/value sanity check.
COLOR_PROTOTYPES: dict[str, tuple[int, int, int]] = {
    "red": (255, 129, 129),
    "blue": (131, 172, 255),
    "green": (158, 229, 158),
    "yellow": (255, 255, 130),
    "orange": (255, 193, 131),
    "light_blue": (131, 255, 255),
    "purple": (211, 158, 229),
    "pink": (255, 158, 211),
    "grey": (190, 190, 190),
}

SWATCH_COLOR_CACHE: dict[str, str] = {}


COLOR_WORD_RE = re.compile(
    r"\b(red|blue|green|yellow|orange|purple|pink|gray|grey|light\s*blue|cyan)\b",
    re.I,
)
GAME_RE = re.compile(
    r"([A-Za-z0-9 .'-]+?)\s+(?:at|@|vs\.?|versus)\s+([A-Za-z0-9 .'-]+)", re.I
)
VERSION_RE = re.compile(r"(?:^|[-_])V(\d+)(?:\.|[-_]|$)", re.I)


@dataclass
class MapRow:
    season: int
    week: int
    normalized_slot: str
    network: str
    window: str
    page_source: str
    page_url: str
    capture_timestamp: str
    image_url: str
    local_path: str
    status: str
    sha256: str
    revision: int
    manifest_row: dict[str, str]


@dataclass
class DMAFeature:
    dma_code: str
    dma_name: str
    geometry: Polygon | MultiPolygon
    latitude: float | None = None
    longitude: float | None = None


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def mercator_y(lat_deg: float) -> float:
    lat = max(-85.0, min(85.0, lat_deg))
    phi = math.radians(lat)
    return math.log(math.tan(math.pi / 4.0 + phi / 2.0))


def lonlat_to_pixel(lon: float, lat: float, width: int, height: int) -> tuple[float, float]:
    m = mercator_y(lat)
    x = PX_COEF[0] * lon + PX_COEF[1] * m + PX_COEF[2]
    y = PY_COEF[0] * lon + PY_COEF[1] * m + PY_COEF[2]
    return x * (width / BASE_W), y * (height / BASE_H)


def normalize_slot(image_url: str, context: str = "", manifest_slot: str = "") -> tuple[str, str, str]:
    """Return normalized_slot, network, window, preferring URL filename."""
    stem = Path(urlparse(image_url).path).stem.upper().replace("_", "-")
    fallback = f"{context} {manifest_slot}".upper()

    # The original image filename is authoritative when it names a network.
    if "CBS" in stem:
        network = "CBS"
    elif "FOX" in stem:
        network = "FOX"
    elif "CBS" in fallback:
        network = "CBS"
    elif "FOX" in fallback:
        network = "FOX"
    else:
        return "UNKNOWN", "", ""

    if re.search(rf"(?:^|-)({network})-E(?:-|$|\d)", stem):
        window = "EARLY"
    elif re.search(rf"(?:^|-)({network})-L(?:-|$|\d)", stem):
        window = "LATE"
    elif re.search(rf"(?:^|-)({network})-S(?:-|$|\d)", stem):
        window = "SINGLE"
    elif network in stem:
        # Bare 01-FOX-V4.png / 03-CBS-V3.png denotes that network's single map.
        window = "SINGLE"
    elif "EARLY" in context.upper():
        window = "EARLY"
    elif "LATE" in context.upper():
        window = "LATE"
    elif "SINGLE" in fallback:
        window = "SINGLE"
    else:
        return "UNKNOWN", network, ""

    return f"{network}_{window}", network, window


def revision_from_url(url: str) -> int:
    stem = Path(urlparse(url).path).stem
    m = VERSION_RE.search(stem)
    return int(m.group(1)) if m else 0


def load_manifest(path: Path) -> list[MapRow]:
    rows: list[MapRow] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for raw in csv.DictReader(f):
            if raw.get("status") not in {"downloaded", "exists"}:
                continue
            slot, net, window = normalize_slot(
                raw.get("image_url", ""), raw.get("context", ""), raw.get("slot", "")
            )
            if slot == "UNKNOWN":
                continue
            rows.append(
                MapRow(
                    season=int(raw["season"]),
                    week=int(raw["week"]),
                    normalized_slot=slot,
                    network=net,
                    window=window,
                    page_source=raw.get("page_source", ""),
                    page_url=raw.get("page_url", ""),
                    capture_timestamp=raw.get("capture_timestamp", ""),
                    image_url=raw.get("image_url", ""),
                    local_path=raw.get("local_path", ""),
                    status=raw.get("status", ""),
                    sha256=raw.get("sha256", ""),
                    revision=revision_from_url(raw.get("image_url", "")),
                    manifest_row=raw,
                )
            )
    return rows


def select_final_maps(rows: list[MapRow]) -> list[MapRow]:
    groups: dict[tuple[int, int, str], list[MapRow]] = defaultdict(list)
    for r in rows:
        groups[(r.season, r.week, r.normalized_slot)].append(r)

    selected: list[MapRow] = []
    for key, items in groups.items():
        # Deduplicate identical binaries first. Live is preferred when it has the
        # highest known revision; otherwise latest Wayback capture breaks ties.
        def rank(r: MapRow) -> tuple[int, int, str, int]:
            source_rank = 2 if r.page_source == "live" else 1 if r.page_source == "wayback" else 0
            capture = r.capture_timestamp or ""
            return (r.revision, source_rank, capture, int(r.manifest_row.get("bytes") or 0))

        selected.append(max(items, key=rank))
    return sorted(selected, key=lambda r: (r.season, r.week, r.network, r.window))


def resolve_map_path(row: MapRow, archive: Path, manifest_path: Path) -> Path | None:
    candidates: list[Path] = []
    raw = (row.local_path or "").replace("\\", os.sep).replace("/", os.sep)
    if raw:
        rp = Path(raw)
        if rp.is_absolute():
            candidates.append(rp)
        candidates.extend([
            manifest_path.parent / rp,
            archive.parent / rp,
            archive / rp,
            archive / str(row.season) / f"week_{row.week:02d}" / rp.name,
        ])
    # Strong fallback: match by SHA or original URL basename within week folder.
    weekdir = archive / str(row.season) / f"week_{row.week:02d}"
    if weekdir.exists():
        candidates.extend(weekdir.glob("*"))

    seen: set[Path] = set()
    for p in candidates:
        p = Path(p)
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        if row.sha256:
            try:
                h = hashlib.sha256(p.read_bytes()).hexdigest()
                if h == row.sha256:
                    return p
            except OSError:
                pass
        # If no SHA match, accept explicit path or exact original basename.
        if raw and p.name == Path(raw).name:
            return p
        if p.name == Path(urlparse(row.image_url).path).name:
            return p
    return None


# ---------- DMA boundary loading ----------

def _decode_topo_arcs(data: dict[str, Any]) -> list[list[tuple[float, float]]]:
    transform = data.get("transform") or {}
    scale = transform.get("scale", [1.0, 1.0])
    translate = transform.get("translate", [0.0, 0.0])
    out: list[list[tuple[float, float]]] = []
    for arc in data["arcs"]:
        x = y = 0.0
        pts: list[tuple[float, float]] = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * scale[0] + translate[0], y * scale[1] + translate[1]))
        out.append(pts)
    return out


def _join_arcs(indexes: Iterable[int], arcs: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    ring: list[tuple[float, float]] = []
    for idx in indexes:
        if idx < 0:
            pts = list(reversed(arcs[~idx]))
        else:
            pts = arcs[idx]
        if ring and pts and ring[-1] == pts[0]:
            ring.extend(pts[1:])
        else:
            ring.extend(pts)
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _polygon_from_topo_arcs(poly_arcs: list[list[int]], arcs: list[list[tuple[float, float]]]) -> Polygon:
    rings = [_join_arcs(r, arcs) for r in poly_arcs]
    if not rings:
        return Polygon()
    return Polygon(rings[0], rings[1:])


def load_dmas(path: Path) -> list[DMAFeature]:
    data = json.loads(path.read_text(encoding="utf-8"))
    dmas: list[DMAFeature] = []

    if data.get("type") == "Topology":
        arcs = _decode_topo_arcs(data)
        objects = data.get("objects", {})
        obj = objects.get("nielsen_dma") or next(iter(objects.values()))
        geometries = obj.get("geometries", [])
        for g in geometries:
            props = g.get("properties") or {}
            gtype = g.get("type")
            if gtype == "Polygon":
                geom = _polygon_from_topo_arcs(g["arcs"], arcs)
            elif gtype == "MultiPolygon":
                geom = MultiPolygon([_polygon_from_topo_arcs(p, arcs) for p in g["arcs"]])
            else:
                continue
            if not geom.is_valid:
                geom = geom.buffer(0)
            code = str(g.get("id") or props.get("dma") or props.get("DMA Code") or "").strip()
            name = str(
                props.get("dma1")
                or props.get("dma_name")
                or props.get("Designated Market Area (DMA)")
                or props.get("name")
                or code
            ).strip()
            dmas.append(
                DMAFeature(
                    dma_code=code,
                    dma_name=name,
                    geometry=geom,
                    latitude=_float_or_none(props.get("latitude")),
                    longitude=_float_or_none(props.get("longitude")),
                )
            )
    elif data.get("type") == "FeatureCollection":
        for feat in data.get("features", []):
            props = feat.get("properties") or {}
            geom = shape(feat.get("geometry"))
            if not geom.is_valid:
                geom = geom.buffer(0)
            if not isinstance(geom, (Polygon, MultiPolygon)):
                continue
            minx, miny, maxx, maxy = geom.bounds
            # Full polygon sampling requires actual lon/lat coordinates.
            if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
                raise ValueError(
                    "The supplied GeoJSON polygons are not longitude/latitude. "
                    "Use simzou/nielsen-dma nielsentopo.json (recommended), or a lon/lat DMA GeoJSON."
                )
            code = str(props.get("dma_code") or props.get("dma") or props.get("DMA Code") or feat.get("id") or "").strip()
            name = str(props.get("dma_name") or props.get("dma1") or props.get("Designated Market Area (DMA)") or props.get("name") or code).strip()
            dmas.append(
                DMAFeature(
                    dma_code=code,
                    dma_name=name,
                    geometry=geom,
                    latitude=_float_or_none(props.get("latitude")),
                    longitude=_float_or_none(props.get("longitude")),
                )
            )
    else:
        raise ValueError("Unsupported DMA boundary format; expected TopoJSON or GeoJSON FeatureCollection")

    if not dmas:
        raise ValueError("No DMA polygons found")
    return dmas


def _float_or_none(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def transform_geometry_to_pixels(geom: Polygon | MultiPolygon, width: int, height: int) -> Polygon | MultiPolygon:
    def ring_px(coords: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
        return [lonlat_to_pixel(float(lon), float(lat), width, height) for lon, lat, *_ in coords]

    if isinstance(geom, Polygon):
        return Polygon(ring_px(geom.exterior.coords), [ring_px(r.coords) for r in geom.interiors])
    return MultiPolygon([
        Polygon(ring_px(p.exterior.coords), [ring_px(r.coords) for r in p.interiors])
        for p in geom.geoms
    ])


# ---------- Color sampling ----------

def rgb_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    # Perceptual-ish weighted RGB distance, sufficient for the widely separated
    # 506 palette and substantially cheaper than converting every sample to Lab.
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return math.sqrt(2.0 * dr * dr + 4.0 * dg * dg + 3.0 * db * db)


def classify_rgb(rgb: tuple[int, int, int], max_distance: float = 155.0) -> str | None:
    r, g, b = rgb
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    # Reject white/black/most map outlines and text.
    if v < 0.30:
        return None
    if s < 0.12:
        dgrey = rgb_distance(rgb, COLOR_PROTOTYPES["grey"])
        return "grey" if dgrey <= 60 else None
    # Ocean/background is the dominant pale blue.
    if rgb_distance(rgb, (209, 240, 255)) < 75:
        return None

    # 506 uses strongly separated hues. Hue classification is more robust than
    # raw RGB distance on the darker diagonal hatch pixels and anti-aliased edges.
    deg = (h * 360.0) % 360.0
    if deg >= 345 or deg < 15:
        return "red"
    if deg < 50:
        return "orange"
    if deg < 80:
        return "yellow"
    if deg < 165:
        return "green"
    if deg < 200:
        return "light_blue"
    if deg < 260:
        return "blue"
    if deg < 315:
        return "purple"
    return "pink"


def sample_dma_polygon(
    image: Image.Image,
    polygon_px: Polygon | MultiPolygon,
    target_points: int,
    min_valid: int,
    seed: int,
) -> dict[str, Any]:
    width, height = image.size
    if polygon_px.is_empty:
        return {"status": "empty_polygon", "color": "", "confidence": 0.0, "valid": 0, "total": 0, "counts": {}}

    clipped = polygon_px.intersection(Polygon([(0, 0), (width - 1, 0), (width - 1, height - 1), (0, height - 1)]))
    if clipped.is_empty or clipped.area < 1.0:
        return {"status": "outside_map", "color": "", "confidence": 0.0, "valid": 0, "total": 0, "counts": {}}

    minx, miny, maxx, maxy = clipped.bounds
    area = max(clipped.area, 1.0)
    spacing = max(2.0, math.sqrt(area / max(target_points, 1)))
    prepared = prep(clipped)
    rng = random.Random(seed)
    ox = rng.random() * spacing
    oy = rng.random() * spacing

    points: list[tuple[int, int]] = []
    y = miny + oy
    while y <= maxy:
        x = minx + ox
        while x <= maxx:
            if prepared.contains(Point(x, y)):
                points.append((int(round(x)), int(round(y))))
            x += spacing
        y += spacing

    # Very small markets can miss the grid. Add representative points from the
    # polygon geometry itself, then random rejection samples if needed.
    rp = clipped.representative_point()
    points.append((int(round(rp.x)), int(round(rp.y))))
    if len(points) < max(20, min_valid):
        attempts = 0
        while len(points) < max(30, min_valid * 2) and attempts < 3000:
            attempts += 1
            x = rng.uniform(minx, maxx)
            y = rng.uniform(miny, maxy)
            if prepared.contains(Point(x, y)):
                points.append((int(round(x)), int(round(y))))

    counts: Counter[str] = Counter()
    total = 0
    for x, y in points:
        if not (0 <= x < width and 0 <= y < height):
            continue
        total += 1
        rgb = image.getpixel((x, y))[:3]
        cname = classify_rgb(rgb)
        if cname:
            counts[cname] += 1

    valid = sum(counts.values())
    if valid == 0:
        return {"status": "no_color", "color": "", "confidence": 0.0, "valid": 0, "total": total, "counts": {}}
    color, top = counts.most_common(1)[0]
    confidence = top / valid
    valid_share = valid / max(total, 1)
    status = "ok"
    if valid < min_valid:
        status = "low_samples"
    elif confidence < 0.60:
        status = "mixed_boundary"
    elif valid_share < 0.20:
        status = "low_colored_share"

    return {
        "status": status,
        "color": color,
        "confidence": confidence,
        "valid_share": valid_share,
        "valid": valid,
        "total": total,
        "counts": dict(counts),
    }


# ---------- Legend extraction ----------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,*/*"})
    # Respect the corporate/system CA configuration that may already have been
    # required by the acquisition step. Do not disable certificate verification.
    ca = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca:
        s.verify = ca
    return s


def canonical_color_name(raw: str) -> str:
    s = raw.lower().replace("gray", "grey")
    s = re.sub(r"\s+", "_", s.strip())
    if s == "cyan":
        return "light_blue"
    return s


def clean_game_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" -–—:|\t\r\n")
    text = re.sub(r"\s*\([^)]*(?:announc|late|early|blackout|subject)[^)]*\)\s*$", "", text, flags=re.I)
    return text[:180]


def infer_slot_from_heading(text: str) -> str | None:
    t = re.sub(r"\s+", " ", text.upper())
    net = "CBS" if "CBS" in t else "FOX" if "FOX" in t else None
    if not net:
        return None
    if any(k in t for k in ["EARLY", "1:00", "1 PM", "1PM"]):
        return f"{net}_EARLY"
    if any(k in t for k in ["LATE", "4:05", "4:25", "4 PM", "4PM"]):
        return f"{net}_LATE"
    if "SINGLE" in t:
        return f"{net}_SINGLE"
    return None


def nearest_heading_slot(tag: Tag) -> str | None:
    # Search backwards through rendered elements for the nearest explicit slot heading.
    for prev in tag.find_all_previous(limit=80):
        if not isinstance(prev, Tag):
            continue
        if prev.name in {"h1", "h2", "h3", "h4", "h5", "b", "strong", "div", "td"}:
            txt = prev.get_text(" ", strip=True)
            slot = infer_slot_from_heading(txt)
            if slot:
                return slot
    return None


def nearby_text_after_image(img: Tag, limit_chars: int = 240) -> str:
    parts: list[str] = []
    # First, siblings in the same parent are normally the legend label.
    node: Any = img.next_sibling
    while node is not None and sum(len(p) for p in parts) < limit_chars:
        if isinstance(node, NavigableString):
            parts.append(str(node))
        elif isinstance(node, Tag):
            if node.name in {"br", "hr", "img"}:
                if parts:
                    break
            txt = node.get_text(" ", strip=True)
            if txt:
                parts.append(txt)
        node = getattr(node, "next_sibling", None)
    text = clean_game_text(" ".join(parts))
    if len(text) >= 4:
        return text

    # Fallback: tight ancestor text (but avoid grabbing an entire page/table).
    for parent in img.parents:
        if not isinstance(parent, Tag):
            continue
        txt = clean_game_text(parent.get_text(" ", strip=True))
        if 4 <= len(txt) <= limit_chars:
            return txt
        if parent.name in {"body", "html"}:
            break
    return ""


def swatch_color(session: requests.Session | None, url: str, timeout: int) -> str:
    if not session or not url:
        return ""
    if url in SWATCH_COLOR_CACHE:
        return SWATCH_COLOR_CACHE[url]
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        from io import BytesIO
        im = Image.open(BytesIO(resp.content)).convert("RGB")
        counts: Counter[str] = Counter()
        for rgb in im.getdata():
            c = classify_rgb(rgb)
            if c and c != "grey":
                counts[c] += 1
        color = counts.most_common(1)[0][0] if counts else ""
    except Exception:
        color = ""
    SWATCH_COLOR_CACHE[url] = color
    return color


def extract_legends_from_html(html: str, session: requests.Session | None = None, base_url: str = "", timeout: int = 25) -> dict[str, dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    legends: dict[str, dict[str, str]] = defaultdict(dict)

    # Pattern 1: explicit color words in text, e.g. "Red: Steelers at Jets".
    current_slot: str | None = None
    for element in soup.find_all(["h1", "h2", "h3", "h4", "h5", "p", "li", "div", "td", "b", "strong"]):
        txt = clean_game_text(element.get_text(" ", strip=True))
        maybe = infer_slot_from_heading(txt)
        if maybe and len(txt) < 100:
            current_slot = maybe
        m = re.match(r"^\s*(red|blue|green|yellow|orange|purple|pink|gray|grey|light\s*blue|cyan)\s*[:\-]\s*(.+)$", txt, re.I)
        if m and current_slot:
            game = clean_game_text(m.group(2))
            if GAME_RE.search(game) or "NO GAME" in game.upper():
                legends[current_slot][canonical_color_name(m.group(1))] = game

    # Pattern 2: 506 swatch images plus adjacent game text. Swatch numbers are
    # page-item identifiers, not reliable color identifiers, so inspect the
    # swatch image itself instead of assuming 1=red, 2=blue, etc.
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if not re.search(r"/swatches/\d+\.(?:png|gif|jpe?g|webp)", src, re.I):
            continue
        color = swatch_color(session, urljoin(base_url, src), timeout) if session else ""
        if not color:
            continue
        slot = nearest_heading_slot(img)
        if not slot:
            continue
        game = nearby_text_after_image(img)
        game = re.sub(r"^(?:red|blue|green|yellow|orange|purple|pink|gray|grey|light\s*blue|cyan)\s*[:\-]?\s*", "", game, flags=re.I)
        game = clean_game_text(game)
        if GAME_RE.search(game) or "NO GAME" in game.upper():
            legends[slot][color] = game

    return {k: dict(v) for k, v in legends.items()}


def load_legend_override(path: Path | None) -> dict[tuple[int, int, str], dict[str, str]]:
    out: dict[tuple[int, int, str], dict[str, str]] = defaultdict(dict)
    if not path:
        return out
    with path.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            slot = r.get("slot") or r.get("map_slot") or ""
            color = canonical_color_name(r.get("color", ""))
            game = r.get("game", "").strip()
            if slot and color and game:
                out[(int(r["season"]), int(r["week"]), slot.upper())][color] = game
    return out


def get_week_legends(
    session: requests.Session,
    season: int,
    week: int,
    selected_rows: list[MapRow],
    cache: dict[str, Any],
    timeout: int,
    allow_network: bool,
) -> tuple[dict[str, dict[str, str]], str]:
    key = f"{season}-{week}"
    if key in cache:
        return cache[key].get("legends", {}), cache[key].get("source", "cache")
    if not allow_network:
        return {}, "network_disabled"

    urls: list[str] = []
    # Live historic pages normally remain available and represent the latest revision.
    urls.append(f"https://506sports.com/nfl.php?yr={season}&wk={week}")
    # Archived page URLs are fallbacks. If acquisition recorded a Wayback
    # timestamp, reconstruct the replay URL so older legend HTML remains usable.
    for r in selected_rows:
        if r.capture_timestamp and r.page_url:
            wb = f"https://web.archive.org/web/{r.capture_timestamp}id_/{r.page_url}"
            if wb not in urls:
                urls.append(wb)
        if r.page_url and r.page_url not in urls:
            urls.append(r.page_url)

    errors: list[str] = []
    for url in urls:
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            resp.raise_for_status()
            legends = extract_legends_from_html(resp.text, session=session, base_url=resp.url, timeout=timeout)
            if legends:
                cache[key] = {"source": url, "legends": legends}
                return legends, url
        except requests.RequestException as e:
            errors.append(f"{url}: {e}")
    cache[key] = {"source": "failed", "legends": {}, "errors": errors}
    return {}, "failed"


# ---------- Output ----------
ASSIGN_FIELDS = [
    "season", "week", "network", "window", "map_slot",
    "dma_code", "dma_name", "coverage_color", "game",
    "confidence", "colored_sample_share", "valid_samples", "total_samples",
    "sample_counts", "classification_status", "review_flag",
    "map_source", "map_revision", "map_image_url", "map_local_path", "map_sha256",
    "legend_source", "legend_status",
]

DIAG_FIELDS = [
    "season", "week", "network", "window", "map_slot", "selected_image_url",
    "selected_local_path", "page_source", "map_revision", "sha256", "width", "height",
    "legend_source", "legend_colors", "dma_rows", "ok_rows", "review_rows", "notes",
]

LEGEND_REVIEW_FIELDS = [
    "season", "week", "network", "window", "map_slot", "coverage_color",
    "dma_count", "selected_image_url", "legend_source", "reason",
]


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", type=Path, required=True, help="acquisition manifest.csv")
    p.add_argument("--map-archive", type=Path, required=True, help="root map_archive directory")
    p.add_argument("--dma-boundaries", "--dma-geojson", dest="dma_boundaries", type=Path, required=True,
                   help="recommended: nielsentopo.json; lon/lat GeoJSON also supported")
    p.add_argument("--output", type=Path, default=Path("historical_dma_assignments.csv"))
    p.add_argument("--diagnostics", type=Path, default=Path("map_diagnostics.csv"))
    p.add_argument("--legend-review", type=Path, default=Path("legend_review.csv"))
    p.add_argument("--summary", type=Path, default=Path("classifier_summary.json"))
    p.add_argument("--legend-cache", type=Path, default=Path("legend_cache.json"))
    p.add_argument("--legend-csv", type=Path, default=None,
                   help="optional manual override CSV: season,week,slot,color,game")
    p.add_argument("--samples", type=int, default=240, help="target polygon samples per DMA")
    p.add_argument("--min-valid", type=int, default=12, help="minimum colored samples before review")
    p.add_argument("--timeout", type=int, default=25)
    p.add_argument("--no-network-legends", action="store_true",
                   help="do not fetch 506 pages; output colors and rely on --legend-csv/cache")
    p.add_argument("--season", type=int, default=None, help="optional single-season filter")
    p.add_argument("--week", type=int, default=None, help="optional single-week filter")
    args = p.parse_args()

    for path in [args.manifest, args.dma_boundaries]:
        if not path.exists():
            p.error(f"File not found: {path}")
    if not args.map_archive.exists():
        p.error(f"Map archive not found: {args.map_archive}")

    all_rows = load_manifest(args.manifest)
    selected = select_final_maps(all_rows)
    if args.season is not None:
        selected = [r for r in selected if r.season == args.season]
    if args.week is not None:
        selected = [r for r in selected if r.week == args.week]
    if not selected:
        raise SystemExit("No qualifying downloaded maps found after filters")

    log(f"Loaded {len(all_rows)} successful manifest rows; selected {len(selected)} final map revisions")
    dmas = load_dmas(args.dma_boundaries)
    log(f"Loaded {len(dmas)} DMA polygons")

    manual_legends = load_legend_override(args.legend_csv)
    cache: dict[str, Any] = {}
    if args.legend_cache.exists():
        try:
            cache = json.loads(args.legend_cache.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"Warning: ignoring unreadable legend cache: {e}")
    session = make_session()

    by_week: dict[tuple[int, int], list[MapRow]] = defaultdict(list)
    for r in selected:
        by_week[(r.season, r.week)].append(r)

    assignment_rows: list[dict[str, Any]] = []
    diag_rows: list[dict[str, Any]] = []
    legend_review_rows: list[dict[str, Any]] = []

    for (season, week), week_maps in sorted(by_week.items()):
        auto_legends, legend_source = get_week_legends(
            session, season, week, week_maps, cache, args.timeout, not args.no_network_legends
        )
        log(f"[{season} W{week:02d}] {len(week_maps)} final maps; legend source={legend_source}")

        for mr in week_maps:
            path = resolve_map_path(mr, args.map_archive, args.manifest)
            if not path:
                diag_rows.append({
                    "season": season, "week": week, "network": mr.network, "window": mr.window,
                    "map_slot": mr.normalized_slot, "selected_image_url": mr.image_url,
                    "selected_local_path": mr.local_path, "page_source": mr.page_source,
                    "map_revision": mr.revision, "sha256": mr.sha256, "notes": "map_file_not_found",
                })
                continue

            try:
                image = Image.open(path).convert("RGB")
            except Exception as e:
                diag_rows.append({
                    "season": season, "week": week, "network": mr.network, "window": mr.window,
                    "map_slot": mr.normalized_slot, "selected_image_url": mr.image_url,
                    "selected_local_path": str(path), "page_source": mr.page_source,
                    "map_revision": mr.revision, "sha256": mr.sha256, "notes": f"image_open_failed: {e}",
                })
                continue

            legend = dict(auto_legends.get(mr.normalized_slot, {}))
            legend.update(manual_legends.get((season, week, mr.normalized_slot), {}))
            map_rows: list[dict[str, Any]] = []
            colors_seen: Counter[str] = Counter()

            for dma in dmas:
                try:
                    pxgeom = transform_geometry_to_pixels(dma.geometry, image.width, image.height)
                    result = sample_dma_polygon(
                        image, pxgeom, args.samples, args.min_valid,
                        seed=season * 100000 + week * 1000 + int(dma.dma_code or 0),
                    )
                except Exception as e:
                    result = {"status": f"error:{type(e).__name__}", "color": "", "confidence": 0.0,
                              "valid_share": 0.0, "valid": 0, "total": 0, "counts": {}}

                color = result.get("color", "")
                if color:
                    colors_seen[color] += 1
                game = legend.get(color, "") if color else ""
                legend_status = "resolved" if game else ("not_applicable" if not color else "unresolved")
                review = (
                    result.get("status") != "ok"
                    or (bool(color) and not game)
                    or float(result.get("confidence", 0.0)) < 0.70
                )
                map_rows.append({
                    "season": season,
                    "week": week,
                    "network": mr.network,
                    "window": mr.window,
                    "map_slot": mr.normalized_slot,
                    "dma_code": dma.dma_code,
                    "dma_name": dma.dma_name,
                    "coverage_color": color,
                    "game": game,
                    "confidence": f"{float(result.get('confidence', 0.0)):.4f}",
                    "colored_sample_share": f"{float(result.get('valid_share', 0.0)):.4f}",
                    "valid_samples": result.get("valid", 0),
                    "total_samples": result.get("total", 0),
                    "sample_counts": json.dumps(result.get("counts", {}), sort_keys=True),
                    "classification_status": result.get("status", ""),
                    "review_flag": str(bool(review)).lower(),
                    "map_source": mr.page_source,
                    "map_revision": mr.revision,
                    "map_image_url": mr.image_url,
                    "map_local_path": str(path),
                    "map_sha256": mr.sha256,
                    "legend_source": legend_source,
                    "legend_status": legend_status,
                })
            assignment_rows.extend(map_rows)

            for color, count in sorted(colors_seen.items()):
                if color not in legend:
                    legend_review_rows.append({
                        "season": season, "week": week, "network": mr.network, "window": mr.window,
                        "map_slot": mr.normalized_slot, "coverage_color": color, "dma_count": count,
                        "selected_image_url": mr.image_url, "legend_source": legend_source,
                        "reason": "coverage color found on map but no color->game legend was resolved",
                    })

            ok_rows = sum(1 for r in map_rows if r["classification_status"] == "ok" and r["legend_status"] == "resolved")
            review_rows = sum(1 for r in map_rows if r["review_flag"] == "true")
            diag_rows.append({
                "season": season, "week": week, "network": mr.network, "window": mr.window,
                "map_slot": mr.normalized_slot, "selected_image_url": mr.image_url,
                "selected_local_path": str(path), "page_source": mr.page_source,
                "map_revision": mr.revision, "sha256": mr.sha256,
                "width": image.width, "height": image.height,
                "legend_source": legend_source,
                "legend_colors": json.dumps(legend, sort_keys=True),
                "dma_rows": len(map_rows), "ok_rows": ok_rows, "review_rows": review_rows,
                "notes": "",
            })

    write_csv(args.output, ASSIGN_FIELDS, assignment_rows)
    write_csv(args.diagnostics, DIAG_FIELDS, diag_rows)
    write_csv(args.legend_review, LEGEND_REVIEW_FIELDS, legend_review_rows)
    args.legend_cache.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")

    summary = {
        "selected_maps": len(selected),
        "dma_polygons": len(dmas),
        "assignment_rows": len(assignment_rows),
        "resolved_games": sum(1 for r in assignment_rows if r.get("game")),
        "review_rows": sum(1 for r in assignment_rows if r.get("review_flag") == "true"),
        "legend_review_maps_colors": len(legend_review_rows),
        "maps_missing_from_disk": sum(1 for r in diag_rows if r.get("notes") == "map_file_not_found"),
        "outputs": {
            "assignments": str(args.output),
            "diagnostics": str(args.diagnostics),
            "legend_review": str(args.legend_review),
            "legend_cache": str(args.legend_cache),
        },
    }
    args.summary.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"Wrote {args.output} ({len(assignment_rows)} rows)")
    print(f"Wrote {args.diagnostics}")
    print(f"Wrote {args.legend_review}")
    print(f"Wrote {args.summary}")
    if legend_review_rows:
        print(f"WARNING: {len(legend_review_rows)} map/color legend items need review; see {args.legend_review}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
