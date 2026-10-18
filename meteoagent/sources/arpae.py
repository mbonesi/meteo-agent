"""Observation source: ARPAE Emilia-Romagna hydro-meteorological network.

Feed: realtime.jsonl (CC-BY license), line-delimited JSON following the RMAP
data model. Each line is a report with all the data for ONE station at ONE
reference instant. The station is identified by (ident, lon, lat, network),
with lon and lat as integers = degrees * 1e5. Data lives in "data": each
element has "level", "timerange", and "vars" with WMO table B codes. The one
element without level and timerange holds the station metadata (B01019 name,
B07030 elevation).

The timestamp is always the FINAL instant of the measurement: a 30-minute
accumulation dated 12:00:00Z covers 11:30-12:00.

References:
  RMAP RFC    https://doc.rmap.cc/rmap_rfc/rfc.html
  Table B     https://github.com/ARPA-SIMC/dballe/blob/master/tables/dballe.txt
  Timerange   https://arpa-simc.github.io/dballe/general_ref/tranges.html
  Level       https://arpa-simc.github.io/dballe/general_ref/ltypes.html
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import zlib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from ..config import ArpaeConfig, Location
from ..http import FetchError

log = logging.getLogger(__name__)

# --------------------------------------------------------------------- tables

# level: (leveltype1, l1, leveltype2, l2). 103 = height above ground, l1 in mm.
LEVEL_2M = (103, 2000, None, None)
LEVEL_10M = (103, 10000, None, None)
LEVEL_GROUND = (1, None, None, None)
LEVEL_MSL = (101, None, None, None)

# timerange pindicator: 0 average, 1 accumulated, 2 maximum, 3 minimum,
# 200 vector average, 205 product valid over the period, 254 instantaneous.


@dataclass(frozen=True)
class VarMapping:
    canonical: str
    bcode: str
    level: tuple
    # accepted pindicators, in order of preference
    pindicators: tuple[int, ...]
    convert: object  # Callable[[float], float]


def _identity(v: float) -> float:
    return v


def _kelvin_to_celsius(v: float) -> float:
    return v - 273.15


def _ms_to_kmh(v: float) -> float:
    return v * 3.6


def _pa_to_hpa(v: float) -> float:
    return v / 100.0


MAPPINGS: tuple[VarMapping, ...] = (
    VarMapping("temperature_2m", "B12101", LEVEL_2M, (254, 0), _kelvin_to_celsius),
    VarMapping("relative_humidity_2m", "B13003", LEVEL_2M, (254, 0), _identity),
    VarMapping("precipitation", "B13011", LEVEL_GROUND, (1,), _identity),
    VarMapping("pressure_msl", "B10051", LEVEL_MSL, (254, 0), _pa_to_hpa),
    VarMapping("wind_speed_10m", "B11002", LEVEL_10M, (200, 0, 254), _ms_to_kmh),
    VarMapping("wind_direction_10m", "B11001", LEVEL_10M, (200, 0, 254), _identity),
    VarMapping("wind_gusts_10m", "B11041", LEVEL_10M, (2, 205, 254), _ms_to_kmh),
    VarMapping("wind_gusts_direction_10m", "B11043", LEVEL_10M, (2, 205, 254), _identity),
)

# Quality control attributes (table B).
ATTR_CONFIDENCE = "B33007"  # % confidence; 0 = invalidated value
ATTR_INVALIDATED = "B33196"  # "invalidated data" flag


@dataclass
class Report:
    ident: str | None
    network: str
    lon: int  # gradi * 1e5
    lat: int  # gradi * 1e5
    ts: int  # epoch UTC
    data: list
    station_name: str | None = None
    station_height_m: float | None = None

    @property
    def station_key(self) -> tuple:
        return (self.ident, self.lon, self.lat, self.network)


@dataclass
class StationInfo:
    ident: str | None
    network: str
    lon: int
    lat: int
    name: str | None = None
    height_m: float | None = None
    distance_km: float = 0.0
    last_ts: int = 0
    # (bcode, level, timerange) effettivamente pubblicati
    channels: set = field(default_factory=set)

    @property
    def canonical_vars(self) -> set[str]:
        out = set()
        for mapping in MAPPINGS:
            for bcode, level, trange in self.channels:
                if bcode == mapping.bcode and level == mapping.level:
                    out.add(mapping.canonical)
        return out

    def source_id(self) -> str:
        return f"arpae:{self.network}:{self.lon},{self.lat}"


def source_id(cfg: ArpaeConfig) -> str | None:
    """Source id if the station is pinned in configuration, otherwise None."""
    if cfg.station_lon is None or cfg.station_lat is None or not cfg.station_network:
        return None
    return f"arpae:{cfg.station_network}:{cfg.station_lon},{cfg.station_lat}"


# --------------------------------------------------------------------- download

@dataclass
class FeedValidators:
    """ETag and Last-Modified of the last complete download, for the conditional GET.

    The feed is ~8.6 MB and the server doesn't compress it, but it answers 304
    (zero bytes) if the file hasn't changed. Kept in memory: after a restart
    the first download is always a full one, which is what we want.
    """

    etag: str | None = None
    last_modified: str | None = None


def pinned_line_filter(cfg: ArpaeConfig) -> Callable[[bytes], bool] | None:
    """Coarse filter for the pinned station's lines, before JSON parsing.

    Every feed line carries its station's lon and lat: keeping only the lines
    containing both digit strings drops tens of thousands of lines without
    decoding them. A false positive (the same digits elsewhere) is harmless:
    the real station_key comparison in extract() drops it anyway.
    """
    if cfg.station_lon is None or cfg.station_lat is None:
        return None
    lon = str(cfg.station_lon).encode()
    lat = str(cfg.station_lat).encode()
    return lambda line: lon in line and lat in line


def download(
    session: requests.Session,
    cfg: ArpaeConfig,
    max_bytes: int = 256 * 1024 * 1024,
    keep: Callable[[bytes], bool] | None = None,
    validators: FeedValidators | None = None,
) -> bytes | None:
    """Downloads the feed by streaming it, line by line.

    keep        if given, only lines for which it returns True are kept: a few
                KB stay in memory instead of the whole file.
    validators  if given, conditional GET: returns None if the server answers
                304 (feed unchanged since the last download). They're updated
                only once the download completes, so an interrupted download
                doesn't make the next round believe "unchanged".

    The max_bytes cap is only a safety net against an anomalous feed.
    """
    headers = {}
    if validators is not None:
        if validators.etag:
            headers["If-None-Match"] = validators.etag
        if validators.last_modified:
            headers["If-Modified-Since"] = validators.last_modified
    try:
        with session.get(
            cfg.endpoint, timeout=cfg.timeout_s, stream=True, headers=headers
        ) as response:
            if response.status_code == 304:
                return None
            response.raise_for_status()
            chunks = _capped(response.iter_content(chunk_size=1 << 16), max_bytes)
            kept = [line for line in _iter_lines(chunks) if keep is None or keep(line)]
            if validators is not None:
                validators.etag = response.headers.get("ETag")
                validators.last_modified = response.headers.get("Last-Modified")
            return b"\n".join(kept)
    except requests.RequestException as exc:
        raise FetchError(f"ARPAE: download failed: {exc}") from exc
    except zlib.error as exc:
        raise FetchError(f"ARPAE: corrupt gzip feed: {exc}") from exc


def _capped(chunks: Iterable[bytes], max_bytes: int) -> Iterator[bytes]:
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > max_bytes:
            raise FetchError(f"ARPAE: feed exceeds {max_bytes} bytes, aborted")
        yield chunk


def _iter_lines(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Splits a stream of chunks into lines, decompressing it if it's gzip.

    Today the feed arrives uncompressed, but iter_reports() has always accepted
    gzip too: it's recognized by the first chunk's magic number and decompressed
    piece by piece, without ever holding the whole file in memory.
    """
    decompressor = None
    first = True
    carry = b""
    for chunk in chunks:
        if first:
            first = False
            if chunk[:2] == b"\x1f\x8b":
                decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        if decompressor is not None:
            chunk = decompressor.decompress(chunk)
        carry += chunk
        lines = carry.split(b"\n")
        carry = lines.pop()  # the last one is incomplete: wait for the next chunk
        for line in lines:
            if line.strip():
                yield line
    if decompressor is not None:
        carry += decompressor.flush()
    if carry.strip():
        yield carry


def iter_reports(raw: bytes) -> Iterator[Report]:
    """Iterates the feed's reports, skipping malformed lines.

    A corrupted line must not throw away the whole cycle: log it and continue.
    """
    if raw[:2] == b"\x1f\x8b":  # gzip magic: the feed might be compressed
        raw = gzip.decompress(raw)

    bad = 0
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            report = _parse_report(obj)
        except (ValueError, KeyError, TypeError) as exc:
            bad += 1
            if bad <= 5:
                log.warning("ARPAE: line %d not parseable (%s)", lineno, exc)
            continue
        if report is not None:
            yield report
    if bad:
        log.warning("ARPAE: %d lines discarded because unparseable", bad)


def _parse_report(obj: dict) -> Report | None:
    if not isinstance(obj, dict):
        raise TypeError("report is not an object")
    # The RFC names the field "datetime" but the published format uses "date":
    # we accept both.
    raw_date = obj.get("date") or obj.get("datetime")
    if raw_date is None:
        raise KeyError("date")
    if isinstance(raw_date, list):  # "summaries"-type response: no data
        return None

    report = Report(
        ident=obj.get("ident"),
        network=str(obj.get("network") or ""),
        lon=int(obj["lon"]),
        lat=int(obj["lat"]),
        ts=_parse_ts(raw_date),
        data=list(obj.get("data") or []),
    )

    for element in report.data:
        if not isinstance(element, dict):
            continue
        # The one element without level and timerange is the station metadata.
        if "level" not in element and "timerange" not in element:
            variables = element.get("vars") or {}
            name = variables.get("B01019", {}).get("v")
            height = variables.get("B07030", {}).get("v")
            if name is not None:
                report.station_name = str(name)
            if height is not None:
                try:
                    report.station_height_m = float(height)
                except (TypeError, ValueError):
                    pass
    return report


def _parse_ts(value: str) -> int:
    """Restricted ISO 8601: YYYY-MM-DDTHH:MM:SSZ, with 'T' or space and optional 'Z'."""
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1]
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


# --------------------------------------------------------------------- geo

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _element_key(element: dict) -> tuple:
    level = tuple(element.get("level") or (None, None, None, None))
    trange = tuple(element.get("timerange") or (None, None, None))
    return level, trange


# --------------------------------------------------------------------- discovery

def discover_stations(raw: bytes, loc: Location, radius_km: float) -> list[StationInfo]:
    """Lists the stations within radius_km, with the channels they actually publish.

    Used to choose the station to pin in configuration: automatic selection
    is fine for the first run, but long-term you want to know which station
    you're actually reading.
    """
    stations: dict[tuple, StationInfo] = {}
    for report in iter_reports(raw):
        distance = haversine_km(loc.latitude, loc.longitude, report.lat / 1e5, report.lon / 1e5)
        if distance > radius_km:
            continue
        info = stations.get(report.station_key)
        if info is None:
            info = StationInfo(
                ident=report.ident,
                network=report.network,
                lon=report.lon,
                lat=report.lat,
                distance_km=distance,
            )
            stations[report.station_key] = info
        info.last_ts = max(info.last_ts, report.ts)
        if report.station_name:
            info.name = report.station_name
        if report.station_height_m is not None:
            info.height_m = report.station_height_m
        for element in report.data:
            if not isinstance(element, dict) or "level" not in element:
                continue
            level, trange = _element_key(element)
            for bcode in (element.get("vars") or {}):
                info.channels.add((bcode, level, trange))

    return sorted(stations.values(), key=lambda s: s.distance_km)


def select_station(stations: list[StationInfo], required: set[str] | None = None) -> StationInfo | None:
    """Nearest usable station: the closest one publishing the required variables."""
    required = required or {"wind_speed_10m", "wind_direction_10m"}
    for station in stations:
        if required <= station.canonical_vars:
            return station
    return None


# --------------------------------------------------------------------- extract

def _quality_ok(attrs: dict, min_confidence: float) -> bool:
    """Applies the quality control described by the RFC.

    A reading that fails QC is retransmitted with a null value and attribute
    B33007 = 0; it must be filtered out at the application level. B33196 is
    the explicit invalidation.
    """
    if not attrs:
        return True
    if attrs.get(ATTR_INVALIDATED):
        return False
    confidence = attrs.get(ATTR_CONFIDENCE)
    if confidence is None:
        return True
    try:
        return float(confidence) >= min_confidence
    except (TypeError, ValueError):
        return False


def extract(
    raw: bytes, station: StationInfo, min_confidence: float = 1.0
) -> list[tuple[int, str, float]]:
    """Extracts the canonical rows (ts, var, value) for the given station.

    The feed contains multiple instants per station: we keep them all, so a
    skipped cycle doesn't leave gaps in the history.
    """
    key = (station.ident, station.lon, station.lat, station.network)
    # (ts, canonical) -> (priority, value). Lower priority = preferred pindicator.
    best: dict[tuple[int, str], tuple[int, float]] = {}
    seen_levels: dict[str, set] = {}

    for report in iter_reports(raw):
        if report.station_key != key:
            continue
        for element in report.data:
            if not isinstance(element, dict) or "level" not in element:
                continue
            level, trange = _element_key(element)
            pindicator = trange[0] if trange else None
            for bcode, payload in (element.get("vars") or {}).items():
                seen_levels.setdefault(bcode, set()).add(level)
                for mapping in MAPPINGS:
                    if mapping.bcode != bcode or mapping.level != level:
                        continue
                    value = payload.get("v")
                    if value is None:
                        continue
                    if not _quality_ok(payload.get("a") or {}, min_confidence):
                        continue
                    try:
                        converted = mapping.convert(float(value))
                    except (TypeError, ValueError):
                        continue
                    # Among multiple timeranges for the same quantity, the pindicator
                    # higher in the preference list wins; if none is expected it's
                    # still accepted, but last.
                    try:
                        priority = mapping.pindicators.index(pindicator)
                    except ValueError:
                        priority = len(mapping.pindicators)
                    slot = (report.ts, mapping.canonical)
                    current = best.get(slot)
                    if current is None or priority < current[0]:
                        best[slot] = (priority, converted)

    found = {canonical for _, canonical in best}
    for mapping in MAPPINGS:
        if mapping.canonical in found:
            continue
        levels = seen_levels.get(mapping.bcode)
        if levels:
            log.warning(
                "ARPAE: %s (%s) expected at level %s but the station publishes it at %s: "
                "variable not imported",
                mapping.canonical,
                mapping.bcode,
                mapping.level,
                sorted(levels),
            )

    return [(ts, canonical, value) for (ts, canonical), (_, value) in sorted(best.items())]


def fetch(
    session: requests.Session,
    cfg: ArpaeConfig,
    loc: Location,
    validators: FeedValidators | None = None,
) -> tuple[StationInfo, list[tuple[int, str, float]]] | None:
    """Downloads the feed, picks the station, and extracts the observations.

    Returns None if the feed hasn't changed since the last download (304).
    With a pinned station only its lines are downloaded and decoded; without
    a pin the whole feed is needed, to pick the station.
    """
    pinned = cfg.station_lon is not None and cfg.station_lat is not None
    raw = download(
        session, cfg, keep=pinned_line_filter(cfg) if pinned else None, validators=validators
    )
    if raw is None:
        return None
    stations = discover_stations(raw, loc, cfg.max_distance_km)
    if not stations and not pinned:
        raise FetchError(f"ARPAE: no station within {cfg.max_distance_km} km")

    if pinned:
        station = next(
            (
                s
                for s in stations
                if s.lon == cfg.station_lon
                and s.lat == cfg.station_lat
                and (not cfg.station_network or s.network == cfg.station_network)
            ),
            None,
        )
        if station is None:
            raise FetchError(
                f"ARPAE: pinned station {cfg.station_lon},{cfg.station_lat} "
                f"({cfg.station_network}) is not in the feed. "
                f"Check with: meteo-agent stations"
            )
    else:
        station = select_station(stations)
        if station is None:
            raise FetchError(
                f"ARPAE: no station with wind data within {cfg.max_distance_km} km"
            )
        log.warning(
            "ARPAE: station chosen automatically (%s at %.1f km). "
            "Pin it in configuration: station_lon = %d, station_lat = %d, station_network = %r",
            station.name or "unnamed",
            station.distance_km,
            station.lon,
            station.lat,
            station.network,
        )

    rows = extract(raw, station, cfg.min_confidence)
    log.info(
        "ARPAE %s (%s, %.1f km, elevation %s m): %d values, variables %s",
        station.name or "?",
        station.network,
        station.distance_km,
        station.height_m,
        len(rows),
        sorted({var for _, var, _ in rows}),
    )
    return station, rows
