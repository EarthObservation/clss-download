#!/usr/bin/env python
"""Resolve and download CLSS tiles from the public asset portal.

The CLI uses the official CLSS tile GeoPackage to avoid scraping the web UI.
It supports:

- explicit tile names, e.g. ``451_111``
- tile lists from a text file
- area-of-interest selection from GeoJSON or GeoPackage polygon layers

Examples
--------

List tiles intersecting an AOI:

    python clss_download.py tiles --area data/Prispevna_povrsina_Cerknisko.geojson

Download GKOT for a few named tiles:

    python clss_download.py download --tile 450_124 --tile 451_124 --product gkot

Download DMR + DMP for all tiles intersecting an AOI:

    python clss_download.py download ^
        --area data/Prispevna_povrsina_Cerknisko.geojson ^
        --product dmr,dmp ^
        --out-dir downloads
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib.parse import urljoin

import requests

try:
    from tqdm.auto import tqdm
except ImportError:  # pragma: no cover - optional dependency fallback
    tqdm = None

BASE_URL = "https://assets.flycom.si/"
DEFAULT_TILE_INDEX = Path(__file__).resolve().parent / "data" / "clss_mreza_2325.gpkg"
DEFAULT_HEADERS_FILE = Path(__file__).resolve().parent / ".clss_headers.env"
SUPPORTED_PRODUCTS = ("gkot", "dmr", "dmp", "ndmp", "pas", "pof", "pofi")
GEOMETRY_EPSILON = 1e-9


@dataclass(frozen=True)
class PolygonShape:
    outer: tuple[tuple[float, float], ...]
    holes: tuple[tuple[tuple[float, float], ...], ...]
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class TileRecord:
    tile_name: str
    bbox: tuple[float, float, float, float]
    origin_name: str
    paths: dict[str, str]
    sizes_mb: dict[str, float]


@dataclass(frozen=True)
class DownloadPlan:
    tile_name: str
    product: str
    url: str
    destination: Path
    expected_size_mb: float | None


class ClssError(RuntimeError):
    """Domain error for user-facing CLI failures."""


class DownloadCancelled(Exception):
    """Raised when a download is cancelled by user interrupt."""


class ProgressReporter:
    def __init__(self, total_files: int, mode: str):
        self.mode = mode
        self._lock = threading.Lock()
        self._files_bar = None
        self._bytes_bar = None
        self._stream = sys.stdout
        self._total_files = total_files
        self._completed_files = 0
        self._total_bytes: int | None = None
        self._completed_bytes = 0
        self._last_plain_emit = 0.0
        self._plain_interval_seconds = 0.75
        if self.mode == "tqdm":
            self._files_bar = tqdm(
                total=total_files,
                desc="Files",
                unit="file",
                position=0,
                leave=True,
                dynamic_ncols=True,
                file=self._stream,
            )
            self._bytes_bar = tqdm(
                total=0,
                desc="Bytes",
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                position=1,
                leave=True,
                dynamic_ncols=True,
                file=self._stream,
            )
        elif self.mode == "plain":
            self._emit_plain(force=True)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def _render_bar(self, completed: int, total: int | None, width: int = 24) -> str:
        if total is None or total <= 0:
            return "[" + ("." * width) + "]"
        ratio = max(0.0, min(1.0, completed / total))
        filled = int(round(ratio * width))
        return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"

    def _format_total_bytes(self) -> str:
        if self._total_bytes is None:
            return "unknown"
        return f"{self._total_bytes / (1024 * 1024):.1f} MiB"

    def _emit_plain(self, force: bool = False) -> None:
        if self.mode != "plain":
            return
        now = time.monotonic()
        if not force and now - self._last_plain_emit < self._plain_interval_seconds:
            return
        self._last_plain_emit = now

        files_bar = self._render_bar(self._completed_files, self._total_files)
        files_text = f"files {self._completed_files}/{self._total_files} {files_bar}"

        bytes_bar = self._render_bar(self._completed_bytes, self._total_bytes)
        bytes_total = self._format_total_bytes()
        bytes_text = f"bytes {self._completed_bytes / (1024 * 1024):.1f}/{bytes_total} {bytes_bar}"

        print(f"PROGRESS {files_text}  {bytes_text}", file=self._stream, flush=True)

    def add_file_total(self, total_bytes: int | None) -> None:
        if not self.enabled or total_bytes is None:
            return
        with self._lock:
            current_total = self._total_bytes or 0
            self._total_bytes = current_total + total_bytes
            if self.mode == "tqdm" and self._bytes_bar is not None:
                self._bytes_bar.total = self._total_bytes
                self._bytes_bar.refresh()
            else:
                self._emit_plain()

    def advance_bytes(self, chunk_size: int) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._completed_bytes += chunk_size
            if self.mode == "tqdm" and self._bytes_bar is not None:
                self._bytes_bar.update(chunk_size)
            else:
                self._emit_plain()

    def mark_file_finished(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._completed_files += 1
            if self.mode == "tqdm" and self._files_bar is not None:
                self._files_bar.update(1)
            else:
                self._emit_plain(force=True)

    def write(self, message: str, *, stream = None) -> None:
        target = stream or sys.stdout
        if self.mode == "tqdm" and tqdm is not None:
            tqdm.write(message, file=target)
        else:
            print(message, file=target)

    def close(self) -> None:
        if self.mode == "tqdm":
            if self._bytes_bar is not None:
                self._bytes_bar.close()
            if self._files_bar is not None:
                self._files_bar.close()
        elif self.mode == "plain":
            with self._lock:
                self._emit_plain(force=True)


def resolve_progress_mode(progress_option: str) -> tuple[str, str | None]:
    if progress_option == "off":
        return "off", None
    if progress_option == "plain":
        return "plain", None
    if progress_option == "tqdm":
        if tqdm is not None:
            return "tqdm", None
        return "plain", "tqdm is not installed; falling back to plain progress."
    if progress_option == "auto":
        if tqdm is not None and sys.stdout.isatty():
            return "tqdm", None
        if tqdm is None:
            return "plain", "tqdm is not installed; using plain progress."
        return "plain", None
    raise ClssError(f"Unsupported progress mode {progress_option!r}.")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ClssError(f"Invalid line in headers file {path} at line {line_number}: expected KEY=VALUE.")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_request_headers(
    referer_arg: str | None,
    user_agent_arg: str | None,
    headers_file: Path | None,
) -> tuple[str, str]:
    file_values: dict[str, str] = {}
    if headers_file is not None and headers_file.exists():
        file_values = parse_env_file(headers_file)

    referer = referer_arg or os.environ.get("CLSS_REFERER") or file_values.get("CLSS_REFERER")
    user_agent = user_agent_arg or os.environ.get("CLSS_USER_AGENT") or file_values.get("CLSS_USER_AGENT")

    missing: list[str] = []
    if not referer:
        missing.append("Referer")
    if not user_agent:
        missing.append("User-Agent")
    if missing:
        file_hint = f" or add them to {headers_file}" if headers_file is not None else ""
        raise ClssError(
            "Missing required download header(s): "
            + ", ".join(missing)
            + ". Provide them via --referer/--user-agent, the CLSS_REFERER/CLSS_USER_AGENT environment variables"
            + file_hint
            + "."
        )

    return referer, user_agent


def normalize_tile_name(value: str) -> str:
    token = value.strip().replace("-", "_")
    if not token:
        raise ClssError("Encountered an empty tile name.")
    return token


def parse_tile_tokens(values: Sequence[str]) -> list[str]:
    tiles: list[str] = []
    for value in values:
        for chunk in value.replace("\n", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                tiles.append(normalize_tile_name(chunk))
    return tiles


def read_tiles_file(path: Path) -> list[str]:
    return parse_tile_tokens(path.read_text(encoding="utf-8").splitlines())


def parse_srid(value: str | None) -> int | None:
    if value is None:
        return None
    cleaned = value.strip().upper()
    if not cleaned:
        return None
    if cleaned.startswith("EPSG:"):
        cleaned = cleaned.split(":", 1)[1]
    if cleaned.startswith("URN:OGC:DEF:CRS:EPSG::"):
        cleaned = cleaned.rsplit(":", 1)[1]
    try:
        return int(cleaned)
    except ValueError as exc:
        raise ClssError(f"Could not parse CRS/SRID value {value!r}.") from exc


def bbox_from_points(points: Sequence[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def point_in_rect(point: tuple[float, float], rect: tuple[float, float, float, float]) -> bool:
    x, y = point
    return rect[0] - GEOMETRY_EPSILON <= x <= rect[2] + GEOMETRY_EPSILON and rect[1] - GEOMETRY_EPSILON <= y <= rect[3] + GEOMETRY_EPSILON


def orientation(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def on_segment(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> bool:
    return (
        min(a[0], b[0]) - GEOMETRY_EPSILON <= c[0] <= max(a[0], b[0]) + GEOMETRY_EPSILON
        and min(a[1], b[1]) - GEOMETRY_EPSILON <= c[1] <= max(a[1], b[1]) + GEOMETRY_EPSILON
        and abs(orientation(a, b, c)) <= GEOMETRY_EPSILON
    )


def segments_intersect(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> bool:
    o1 = orientation(a1, a2, b1)
    o2 = orientation(a1, a2, b2)
    o3 = orientation(b1, b2, a1)
    o4 = orientation(b1, b2, a2)

    if (
        ((o1 > GEOMETRY_EPSILON and o2 < -GEOMETRY_EPSILON) or (o1 < -GEOMETRY_EPSILON and o2 > GEOMETRY_EPSILON))
        and ((o3 > GEOMETRY_EPSILON and o4 < -GEOMETRY_EPSILON) or (o3 < -GEOMETRY_EPSILON and o4 > GEOMETRY_EPSILON))
    ):
        return True

    return any(
        (
            abs(o1) <= GEOMETRY_EPSILON and on_segment(a1, a2, b1),
            abs(o2) <= GEOMETRY_EPSILON and on_segment(a1, a2, b2),
            abs(o3) <= GEOMETRY_EPSILON and on_segment(b1, b2, a1),
            abs(o4) <= GEOMETRY_EPSILON and on_segment(b1, b2, a2),
        )
    )


def iter_ring_segments(ring: Sequence[tuple[float, float]]) -> Iterator[tuple[tuple[float, float], tuple[float, float]]]:
    for index in range(len(ring) - 1):
        yield ring[index], ring[index + 1]


def point_in_ring(point: tuple[float, float], ring: Sequence[tuple[float, float]]) -> bool:
    for start, end in iter_ring_segments(ring):
        if on_segment(start, end, point):
            return True

    inside = False
    px, py = point
    for start, end in iter_ring_segments(ring):
        x1, y1 = start
        x2, y2 = end
        crosses = (y1 > py) != (y2 > py)
        if crosses:
            x_at_y = (x2 - x1) * (py - y1) / (y2 - y1) + x1
            if x_at_y >= px - GEOMETRY_EPSILON:
                inside = not inside
    return inside


def point_in_polygon(point: tuple[float, float], polygon: PolygonShape) -> bool:
    if not point_in_ring(point, polygon.outer):
        return False
    return not any(point_in_ring(point, hole) for hole in polygon.holes)


def ring_intersects_rect(ring: Sequence[tuple[float, float]], rect: tuple[float, float, float, float]) -> bool:
    rect_corners = (
        (rect[0], rect[1]),
        (rect[2], rect[1]),
        (rect[2], rect[3]),
        (rect[0], rect[3]),
        (rect[0], rect[1]),
    )

    if any(point_in_rect(point, rect) for point in ring):
        return True

    if any(point_in_ring(corner, ring) for corner in rect_corners[:-1]):
        return True

    rect_edges = list(iter_ring_segments(rect_corners))
    for seg_start, seg_end in iter_ring_segments(ring):
        for edge_start, edge_end in rect_edges:
            if segments_intersect(seg_start, seg_end, edge_start, edge_end):
                return True
    return False


def polygon_intersects_rect(polygon: PolygonShape, rect: tuple[float, float, float, float]) -> bool:
    if not bbox_overlaps(polygon.bbox, rect):
        return False

    if ring_intersects_rect(polygon.outer, rect):
        return True

    rect_corners = (
        (rect[0], rect[1]),
        (rect[2], rect[1]),
        (rect[2], rect[3]),
        (rect[0], rect[3]),
    )
    if any(point_in_polygon(corner, polygon) for corner in rect_corners):
        return True

    return False


def geometry_intersects_rect(polygons: Sequence[PolygonShape], rect: tuple[float, float, float, float]) -> bool:
    return any(polygon_intersects_rect(polygon, rect) for polygon in polygons)


def ensure_closed_ring(ring: Sequence[Sequence[float]]) -> tuple[tuple[float, float], ...]:
    if len(ring) < 4:
        raise ClssError("Encountered a polygon ring with fewer than 4 coordinates.")
    normalized = tuple((float(point[0]), float(point[1])) for point in ring)
    if normalized[0] != normalized[-1]:
        normalized = normalized + (normalized[0],)
    return normalized


def polygon_from_rings(rings: Sequence[Sequence[Sequence[float]]]) -> PolygonShape:
    if not rings:
        raise ClssError("Encountered an empty polygon geometry.")
    outer = ensure_closed_ring(rings[0])
    holes = tuple(ensure_closed_ring(ring) for ring in rings[1:])
    bbox = bbox_from_points(outer)
    return PolygonShape(outer=outer, holes=holes, bbox=bbox)


def polygons_from_geojson_geometry(geometry: dict) -> list[PolygonShape]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        return [polygon_from_rings(coordinates)]
    if geometry_type == "MultiPolygon":
        return [polygon_from_rings(rings) for rings in coordinates]
    raise ClssError(f"Unsupported geometry type {geometry_type!r}. Only Polygon and MultiPolygon are supported.")


def load_geojson_polygons(path: Path, assume_srid: int | None) -> tuple[list[PolygonShape], int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    crs_block = payload.get("crs")
    srid = None
    if isinstance(crs_block, dict):
        properties = crs_block.get("properties") or {}
        srid = parse_srid(properties.get("name"))
    if srid is None:
        srid = assume_srid
    if srid is None:
        raise ClssError(
            f"{path} does not declare a CRS. Pass --assume-srid 3794 if the file is already in EPSG:3794."
        )

    geometries: list[dict] = []
    payload_type = payload.get("type")
    if payload_type == "FeatureCollection":
        for feature in payload.get("features", []):
            if feature.get("geometry"):
                geometries.append(feature["geometry"])
    elif payload_type == "Feature":
        if payload.get("geometry"):
            geometries.append(payload["geometry"])
    else:
        geometries.append(payload)

    polygons: list[PolygonShape] = []
    for geometry in geometries:
        polygons.extend(polygons_from_geojson_geometry(geometry))
    return polygons, srid


def gpkg_wkb_offset(blob: bytes) -> tuple[int, int]:
    if len(blob) < 8 or blob[:2] != b"GP":
        raise ClssError("Encountered an invalid GeoPackage geometry blob.")

    flags = blob[3]
    little_endian = bool(flags & 1)
    empty = bool(flags & 0b10000)
    envelope_indicator = (flags >> 1) & 0b111
    endian = "<" if little_endian else ">"
    srid = struct.unpack(endian + "i", blob[4:8])[0]
    if empty:
        raise ClssError("Encountered an empty GeoPackage geometry.")

    offset = 8
    if envelope_indicator == 1:
        offset += 32
    elif envelope_indicator in (2, 3):
        offset += 48
    elif envelope_indicator == 4:
        offset += 64
    return srid, offset


def normalize_wkb_type(geometry_type: int) -> tuple[int, int]:
    dims = 2
    if geometry_type >= 3000:
        return geometry_type - 3000, 4
    if geometry_type >= 2000:
        return geometry_type - 2000, 3
    if geometry_type >= 1000:
        return geometry_type - 1000, 3
    return geometry_type, dims


def parse_wkb_points(data: bytes, offset: int, endian: str, dimensions: int, count: int) -> tuple[list[tuple[float, float]], int]:
    points: list[tuple[float, float]] = []
    for _ in range(count):
        coords = struct.unpack_from(endian + ("d" * dimensions), data, offset)
        offset += 8 * dimensions
        points.append((float(coords[0]), float(coords[1])))
    return points, offset


def parse_wkb_polygon(data: bytes, offset: int) -> tuple[PolygonShape, int]:
    byte_order = data[offset]
    endian = "<" if byte_order == 1 else ">"
    offset += 1
    geometry_type = struct.unpack_from(endian + "I", data, offset)[0]
    offset += 4
    base_type, dimensions = normalize_wkb_type(geometry_type)
    if base_type != 3:
        raise ClssError(f"Expected a WKB Polygon, got type {geometry_type}.")
    ring_count = struct.unpack_from(endian + "I", data, offset)[0]
    offset += 4
    rings: list[list[tuple[float, float]]] = []
    for _ in range(ring_count):
        point_count = struct.unpack_from(endian + "I", data, offset)[0]
        offset += 4
        ring, offset = parse_wkb_points(data, offset, endian, dimensions, point_count)
        rings.append(ring)
    return polygon_from_rings(rings), offset


def parse_wkb_multipolygon(data: bytes, offset: int) -> tuple[list[PolygonShape], int]:
    byte_order = data[offset]
    endian = "<" if byte_order == 1 else ">"
    offset += 1
    geometry_type = struct.unpack_from(endian + "I", data, offset)[0]
    offset += 4
    base_type, _ = normalize_wkb_type(geometry_type)
    if base_type != 6:
        raise ClssError(f"Expected a WKB MultiPolygon, got type {geometry_type}.")
    polygon_count = struct.unpack_from(endian + "I", data, offset)[0]
    offset += 4
    polygons: list[PolygonShape] = []
    for _ in range(polygon_count):
        polygon, offset = parse_wkb_polygon(data, offset)
        polygons.append(polygon)
    return polygons, offset


def polygons_from_gpkg_blob(blob: bytes) -> tuple[list[PolygonShape], int]:
    srid, offset = gpkg_wkb_offset(blob)
    byte_order = blob[offset]
    endian = "<" if byte_order == 1 else ">"
    geometry_type = struct.unpack_from(endian + "I", blob, offset + 1)[0]
    base_type, _ = normalize_wkb_type(geometry_type)
    if base_type == 3:
        polygon, _ = parse_wkb_polygon(blob, offset)
        return [polygon], srid
    if base_type == 6:
        polygons, _ = parse_wkb_multipolygon(blob, offset)
        return polygons, srid
    raise ClssError(f"Unsupported GeoPackage geometry type {geometry_type}. Only Polygon and MultiPolygon are supported.")


def get_default_gpkg_layer(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            """
            SELECT c.table_name
            FROM gpkg_contents AS c
            JOIN gpkg_geometry_columns AS g
              ON c.table_name = g.table_name
            WHERE c.data_type = 'features'
            ORDER BY c.table_name
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        raise ClssError(f"No feature layers were found in {path}.")
    return str(row[0])


def get_gpkg_geometry_column(path: Path, layer: str) -> str:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?",
            (layer,),
        ).fetchone()
    if row is None:
        raise ClssError(f"Layer {layer!r} was not found in {path}.")
    return str(row[0])


def load_gpkg_polygons(path: Path, layer: str | None) -> tuple[list[PolygonShape], int]:
    selected_layer = layer or get_default_gpkg_layer(path)
    geometry_column = get_gpkg_geometry_column(path, selected_layer)
    polygons: list[PolygonShape] = []
    srid: int | None = None
    with sqlite3.connect(path) as conn:
        query = f'SELECT "{geometry_column}" FROM "{selected_layer}" WHERE "{geometry_column}" IS NOT NULL'
        for (blob,) in conn.execute(query):
            feature_polygons, feature_srid = polygons_from_gpkg_blob(blob)
            if srid is None:
                srid = feature_srid
            elif srid != feature_srid:
                raise ClssError(f"Layer {selected_layer!r} contains mixed SRIDs, which is not supported.")
            polygons.extend(feature_polygons)
    if not polygons:
        raise ClssError(f"Layer {selected_layer!r} in {path} does not contain polygon features.")
    if srid is None:
        raise ClssError(f"Could not determine the SRID for layer {selected_layer!r}.")
    return polygons, srid


def load_area_polygons(path: Path, layer: str | None, assume_srid: int | None) -> tuple[list[PolygonShape], int]:
    suffix = path.suffix.lower()
    if suffix in {".geojson", ".json"}:
        return load_geojson_polygons(path, assume_srid=assume_srid)
    if suffix == ".gpkg":
        polygons, srid = load_gpkg_polygons(path, layer=layer)
        return polygons, srid
    raise ClssError(
        f"Unsupported AOI format {path.suffix!r}. Supported inputs are GeoJSON (.geojson/.json) and GeoPackage (.gpkg)."
    )


def parse_tile_bbox(blob: bytes) -> tuple[float, float, float, float]:
    srid, offset = gpkg_wkb_offset(blob)
    del srid
    flags = blob[3]
    little_endian = bool(flags & 1)
    envelope_indicator = (flags >> 1) & 0b111
    endian = "<" if little_endian else ">"
    envelope_offset = 8
    if envelope_indicator == 0:
        raise ClssError("Tile geometry blob does not include an envelope.")
    if envelope_indicator == 1:
        min_x, max_x, min_y, max_y = struct.unpack_from(endian + "4d", blob, envelope_offset)
        return float(min_x), float(min_y), float(max_x), float(max_y)
    raise ClssError("Encountered a tile geometry envelope format that this tool does not support.")


class TileCatalog:
    def __init__(self, path: Path):
        self.path = path
        if not self.path.exists():
            raise ClssError(f"Tile index not found: {self.path}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def by_names(self, tile_names: Sequence[str]) -> list[TileRecord]:
        if not tile_names:
            return []

        ordered_unique_names = list(dict.fromkeys(tile_names))
        rows_by_name: dict[str, TileRecord] = {}
        with self._connect() as conn:
            for chunk_start in range(0, len(ordered_unique_names), 900):
                chunk = ordered_unique_names[chunk_start : chunk_start + 900]
                placeholders = ",".join("?" for _ in chunk)
                query = f"""
                    SELECT ti_name, orig_ime, geom,
                           path_gkot, path_dmr, path_dmp, path_ndmp, path_pas, path_pof, path_pofi,
                           size_gkot, size_dmr, size_dmp, size_ndmp, size_pas, size_pof, size_pofi
                    FROM clss_mreza_2023_2025
                    WHERE ti_name IN ({placeholders})
                """
                for row in conn.execute(query, chunk):
                    rows_by_name[str(row["ti_name"])] = row_to_tile_record(row)

        missing = [name for name in ordered_unique_names if name not in rows_by_name]
        if missing:
            sample = ", ".join(missing[:10])
            raise ClssError(f"{len(missing)} tile(s) were not found in the tile index: {sample}")
        return [rows_by_name[name] for name in ordered_unique_names]

    def by_area(self, polygons: Sequence[PolygonShape]) -> list[TileRecord]:
        if not polygons:
            return []

        min_x = min(polygon.bbox[0] for polygon in polygons)
        min_y = min(polygon.bbox[1] for polygon in polygons)
        max_x = max(polygon.bbox[2] for polygon in polygons)
        max_y = max(polygon.bbox[3] for polygon in polygons)

        x_min = math.floor(min_x / 1000.0)
        x_max = math.floor(max_x / 1000.0)
        y_min = math.floor(min_y / 1000.0)
        y_max = math.floor(max_y / 1000.0)

        matches: list[TileRecord] = []
        with self._connect() as conn:
            query = """
                SELECT ti_name, orig_ime, geom,
                       path_gkot, path_dmr, path_dmp, path_ndmp, path_pas, path_pof, path_pofi,
                       size_gkot, size_dmr, size_dmp, size_ndmp, size_pas, size_pof, size_pofi
                FROM clss_mreza_2023_2025
                WHERE CAST(x_ime AS INTEGER) BETWEEN ? AND ?
                  AND CAST(y_ime AS INTEGER) BETWEEN ? AND ?
                ORDER BY CAST(y_ime AS INTEGER), CAST(x_ime AS INTEGER)
            """
            for row in conn.execute(query, (x_min, x_max, y_min, y_max)):
                tile = row_to_tile_record(row)
                if geometry_intersects_rect(polygons, tile.bbox):
                    matches.append(tile)
        return matches


def row_to_tile_record(row: sqlite3.Row) -> TileRecord:
    return TileRecord(
        tile_name=str(row["ti_name"]),
        bbox=parse_tile_bbox(row["geom"]),
        origin_name=str(row["orig_ime"]),
        paths={product: str(row[f"path_{product}"]) for product in SUPPORTED_PRODUCTS if row[f"path_{product}"]},
        sizes_mb={product: float(row[f"size_{product}"]) for product in SUPPORTED_PRODUCTS if row[f"size_{product}"] is not None},
    )


def resolve_output_layout(flat: bool, semi_flat: bool) -> str:
    if flat and semi_flat:
        raise ClssError("Choose only one output layout override: --flat or --semi-flat.")
    if flat:
        return "flat"
    if semi_flat:
        return "semi"
    return "full"


def relative_download_path(tile: TileRecord, product: str, layout: str) -> Path:
    source_path = tile.paths[product].lstrip("/")
    trimmed = source_path.removeprefix("clss/raw/")
    if layout == "full":
        return Path(trimmed)
    if layout == "semi":
        return Path(product.lower()) / Path(trimmed).name
    if layout == "flat":
        return Path(Path(trimmed).name)
    raise ClssError(f"Unsupported output layout {layout!r}.")


def build_download_plan(
    tiles: Sequence[TileRecord],
    products: Sequence[str],
    out_dir: Path,
    layout: str,
) -> list[DownloadPlan]:
    plan: list[DownloadPlan] = []
    for tile in tiles:
        for product in products:
            if product not in tile.paths:
                raise ClssError(f"Tile {tile.tile_name} does not have a path for product {product}.")
            rel_path = relative_download_path(tile, product, layout=layout)
            plan.append(
                DownloadPlan(
                    tile_name=tile.tile_name,
                    product=product,
                    url=urljoin(BASE_URL, tile.paths[product]),
                    destination=out_dir / rel_path,
                    expected_size_mb=tile.sizes_mb.get(product),
                )
            )
    return plan


def format_size_mb(size_mb: float | None) -> str:
    if size_mb is None:
        return "unknown"
    return f"{size_mb:.2f} MB"


def resolve_products(product_values: Sequence[str]) -> list[str]:
    if not product_values:
        return ["gkot"]

    requested: list[str] = []
    for value in product_values:
        for token in value.replace("\n", ",").split(","):
            token = token.strip().lower()
            if not token:
                continue
            if token == "all":
                return list(SUPPORTED_PRODUCTS)
            if token not in SUPPORTED_PRODUCTS:
                raise ClssError(
                    f"Unsupported product {token!r}. Choose from: {', '.join(SUPPORTED_PRODUCTS)} or use 'all'."
                )
            requested.append(token)
    return list(dict.fromkeys(requested))


def resolve_tiles(
    catalog: TileCatalog,
    area_path: Path | None,
    area_layer: str | None,
    assume_srid: int | None,
    explicit_tiles: Sequence[str],
) -> list[TileRecord]:
    resolved: dict[str, TileRecord] = {}

    if area_path is not None:
        polygons, srid = load_area_polygons(area_path, layer=area_layer, assume_srid=assume_srid)
        if srid != 3794:
            raise ClssError(
                f"AOI SRID is {srid}, but the official CLSS tile index is in EPSG:3794. Reproject the AOI first."
            )
        for tile in catalog.by_area(polygons):
            resolved[tile.tile_name] = tile

    if explicit_tiles:
        for tile in catalog.by_names(explicit_tiles):
            resolved[tile.tile_name] = tile

    if not resolved:
        raise ClssError("No tiles were resolved. Provide --area and/or --tile.")

    return sorted(resolved.values(), key=lambda item: tuple(int(part) for part in item.tile_name.split("_", 1)))


def make_session(referer: str, user_agent: str, trust_env_proxy: bool) -> requests.Session:
    session = requests.Session()
    session.trust_env = trust_env_proxy
    session.headers.update(
        {
            "Referer": referer,
            "User-Agent": user_agent,
        }
    )
    return session


def download_one(
    session: requests.Session,
    plan: DownloadPlan,
    overwrite: bool,
    timeout: float,
    reporter: ProgressReporter | None = None,
    stop_event: threading.Event | None = None,
) -> tuple[str, DownloadPlan]:
    destination = plan.destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    if stop_event is not None and stop_event.is_set():
        raise DownloadCancelled()

    if destination.exists() and not overwrite:
        return "skipped", plan

    temp_path = destination.with_suffix(destination.suffix + ".part")
    try:
        with session.get(plan.url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            content_length_header = response.headers.get("Content-Length")
            content_length = int(content_length_header) if content_length_header else None
            if reporter is not None:
                reporter.add_file_total(content_length)
            with temp_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if stop_event is not None and stop_event.is_set():
                        raise DownloadCancelled()
                    if chunk:
                        handle.write(chunk)
                        if reporter is not None:
                            reporter.advance_bytes(len(chunk))
        temp_path.replace(destination)
    except BaseException:
        if temp_path.exists():
            temp_path.unlink()
        raise
    return "downloaded", plan


def print_tile_table(tiles: Sequence[TileRecord]) -> None:
    print(f"Resolved {len(tiles)} tile(s):")
    for tile in tiles:
        products = ", ".join(product.upper() for product in tile.paths)
        print(f"  {tile.tile_name}  {tile.origin_name}  [{products}]")


def warn_if_limits_are_large(tile_count: int, file_count: int) -> None:
    if tile_count > 10:
        print(
            "Warning: the official CLSS instructions mention a 10-tile limit for bulk selection in the viewer.",
            file=sys.stderr,
        )
    if file_count > 20:
        print(
            "Warning: the official CLSS instructions mention a 20-download daily limit in the viewer.",
            file=sys.stderr,
        )


def run_tiles_command(args: argparse.Namespace) -> int:
    catalog = TileCatalog(args.tile_index)
    explicit_tiles = parse_tile_tokens(args.tile or [])
    if args.tiles_file:
        explicit_tiles.extend(read_tiles_file(args.tiles_file))

    tiles = resolve_tiles(
        catalog=catalog,
        area_path=args.area,
        area_layer=args.layer,
        assume_srid=args.assume_srid,
        explicit_tiles=explicit_tiles,
    )

    if args.json:
        print(json.dumps([tile.tile_name for tile in tiles], indent=2))
    else:
        print_tile_table(tiles)
    return 0


def run_download_command(args: argparse.Namespace) -> int:
    catalog = TileCatalog(args.tile_index)
    products = resolve_products(args.product or [])
    explicit_tiles = parse_tile_tokens(args.tile or [])
    if args.tiles_file:
        explicit_tiles.extend(read_tiles_file(args.tiles_file))

    tiles = resolve_tiles(
        catalog=catalog,
        area_path=args.area,
        area_layer=args.layer,
        assume_srid=args.assume_srid,
        explicit_tiles=explicit_tiles,
    )
    output_layout = resolve_output_layout(flat=args.flat, semi_flat=args.semi_flat)
    plan = build_download_plan(tiles, products=products, out_dir=args.out_dir, layout=output_layout)

    print_tile_table(tiles)
    print(f"Prepared {len(plan)} file(s) for download:")
    total_size_mb = 0.0
    total_size_known = True
    for item in plan:
        if item.expected_size_mb is None:
            total_size_known = False
        else:
            total_size_mb += item.expected_size_mb
        print(f"  {item.product.upper():4}  {item.tile_name:>8}  {format_size_mb(item.expected_size_mb):>10}  {item.destination}")
    if total_size_known:
        print(f"Estimated total size: {total_size_mb:.2f} MB")

    warn_if_limits_are_large(tile_count=len(tiles), file_count=len(plan))

    if args.dry_run:
        return 0

    referer, user_agent = resolve_request_headers(
        referer_arg=args.referer,
        user_agent_arg=args.user_agent,
        headers_file=args.headers_file,
    )
    session = make_session(
        referer=referer,
        user_agent=user_agent,
        trust_env_proxy=args.trust_env_proxy,
    )
    progress_mode = "off"
    progress_note = None
    if not args.no_progress:
        progress_mode, progress_note = resolve_progress_mode(args.progress)
    reporter = ProgressReporter(total_files=len(plan), mode=progress_mode)
    if progress_note:
        reporter.write(f"Note: {progress_note}")

    successes = 0
    skipped = 0
    cancelled = 0
    failures: list[tuple[DownloadPlan, Exception]] = []
    interrupted = False
    stop_event = threading.Event()

    try:
        with session:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(download_one, session, item, args.overwrite, args.timeout, reporter, stop_event): item
                    for item in plan
                }
                try:
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            status, finished = future.result()
                        except DownloadCancelled:
                            cancelled += 1
                            reporter.mark_file_finished()
                            reporter.write(f"CANCEL   {item.product.upper():4} {item.tile_name:>8}  {item.destination}")
                            continue
                        except Exception as exc:  # noqa: BLE001
                            failures.append((item, exc))
                            reporter.mark_file_finished()
                            reporter.write(
                                f"FAILED   {item.product.upper():4} {item.tile_name:>8}  {item.url}  ({exc})",
                                stream=sys.stderr,
                            )
                            continue

                        reporter.mark_file_finished()
                        if status == "skipped":
                            skipped += 1
                            reporter.write(f"SKIPPED  {finished.product.upper():4} {finished.tile_name:>8}  {finished.destination}")
                        else:
                            successes += 1
                            reporter.write(f"DONE     {finished.product.upper():4} {finished.tile_name:>8}  {finished.destination}")
                except KeyboardInterrupt:
                    interrupted = True
                    stop_event.set()
                    cancelled += sum(1 for future in futures if future.cancel())
                    executor.shutdown(wait=False, cancel_futures=True)
                    reporter.write("Interrupted by user. Stopping active downloads...", stream=sys.stderr)
    finally:
        reporter.close()

    print(f"Finished: {successes} downloaded, {skipped} skipped, {cancelled} cancelled, {len(failures)} failed.")
    if interrupted:
        return 130
    return 0 if not failures else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve CLSS tiles from an AOI or tile names and download products from the public asset host."
    )
    parser.add_argument(
        "--tile-index",
        type=Path,
        default=DEFAULT_TILE_INDEX,
        help=f"Path to the official CLSS tile GeoPackage. Default: {DEFAULT_TILE_INDEX}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_selection_arguments(target: argparse.ArgumentParser) -> None:
        target.add_argument("--area", type=Path, help="AOI vector file in GeoJSON or GeoPackage format.")
        target.add_argument("--layer", help="Layer name to read when --area points to a GeoPackage.")
        target.add_argument(
            "--assume-srid",
            type=int,
            help="SRID to assume when a GeoJSON file does not declare its CRS. Use 3794 for CLSS AOIs.",
        )
        target.add_argument(
            "--tile",
            action="append",
            help="Tile name, or a comma-separated list of tile names. Can be provided multiple times.",
        )
        target.add_argument("--tiles-file", type=Path, help="Text file containing tile names.")

    tiles_parser = subparsers.add_parser("tiles", help="List tiles resolved from the supplied AOI and/or tile names.")
    add_selection_arguments(tiles_parser)
    tiles_parser.add_argument("--json", action="store_true", help="Print only the tile names as JSON.")
    tiles_parser.set_defaults(handler=run_tiles_command)

    download_parser = subparsers.add_parser("download", help="Download CLSS products for the resolved tiles.")
    add_selection_arguments(download_parser)
    download_parser.add_argument(
        "--product",
        action="append",
        help="Product name(s): gkot, dmr, dmp, ndmp, pas, pof, pofi, or all. Defaults to gkot.",
    )
    download_parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("downloads"),
        help="Destination directory for downloaded files. Default: ./downloads",
    )
    download_parser.add_argument(
        "--flat",
        action="store_true",
        help="Store files directly in --out-dir instead of preserving the CLSS folder structure.",
    )
    download_parser.add_argument(
        "--semi-flat",
        action="store_true",
        help="Store files under --out-dir/<product>/filename without preserving the full CLSS path tree.",
    )
    download_parser.add_argument("--overwrite", action="store_true", help="Overwrite files that already exist.")
    download_parser.add_argument("--dry-run", action="store_true", help="Resolve tiles and print the download plan without downloading.")
    download_parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Concurrent downloads to run at once. Default: 2",
    )
    download_parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-request timeout in seconds. Default: 120",
    )
    download_parser.add_argument("--referer", help="HTTP Referer header. Overrides env/config values.")
    download_parser.add_argument(
        "--user-agent",
        help="HTTP User-Agent header. Overrides env/config values.",
    )
    download_parser.add_argument(
        "--headers-file",
        type=Path,
        default=DEFAULT_HEADERS_FILE,
        help=f"Optional env-style file with CLSS_REFERER and CLSS_USER_AGENT. Default: {DEFAULT_HEADERS_FILE}",
    )
    download_parser.add_argument(
        "--trust-env-proxy",
        action="store_true",
        help="Use proxy settings from the environment. Disabled by default to avoid broken proxy configs.",
    )
    download_parser.add_argument(
        "--progress",
        choices=("auto", "tqdm", "plain", "off"),
        default="auto",
        help="Progress output mode. Default: auto",
    )
    download_parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable all progress output for real downloads.",
    )
    download_parser.set_defaults(handler=run_download_command)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ClssError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except requests.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"Request error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
