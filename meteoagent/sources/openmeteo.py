"""Forecast source: Open-Meteo.

Public API, no key required. We request km/h and UTC directly so the
conversion to canonical units is already done by the server.

Known limitation: Open-Meteo does NOT provide gust direction. On forecast
only wind_gusts_10m (intensity) exists; the mean wind direction
(wind_direction_10m) is used as its direction. The true gust direction only
exists in the ARPAE observation (B11043).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from ..config import Location, OpenMeteoConfig
from ..http import FetchError, get_json

log = logging.getLogger(__name__)

# Requested variables, already in canonical names (Open-Meteo uses the same naming).
HOURLY_VARS = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "pressure_msl",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
)


def source_id(cfg: OpenMeteoConfig) -> str:
    return f"open-meteo:{cfg.model}"


def fetch(
    session: requests.Session, cfg: OpenMeteoConfig, loc: Location
) -> list[tuple[int, str, float]]:
    """Downloads the hourly forecast. Returns rows (valid_ts, var, value)."""
    params = {
        "latitude": f"{loc.latitude:.6f}",
        "longitude": f"{loc.longitude:.6f}",
        "elevation": f"{loc.elevation_m:.0f}",
        "hourly": ",".join(HOURLY_VARS),
        "forecast_days": cfg.forecast_days,
        "timezone": "UTC",
        "wind_speed_unit": "kmh",
        "temperature_unit": "celsius",
        "precipitation_unit": "mm",
        "cell_selection": "nearest",
    }
    if cfg.model and cfg.model != "best_match":
        params["models"] = cfg.model

    payload = get_json(session, cfg.endpoint, params, cfg.timeout_s)

    if payload.get("error"):
        raise FetchError(f"Open-Meteo: {payload.get('reason', 'unspecified error')}")

    hourly = payload.get("hourly")
    if not isinstance(hourly, dict) or "time" not in hourly:
        raise FetchError("Open-Meteo: response missing the 'hourly' block")

    times = hourly["time"]
    timestamps = [_parse_utc(t) for t in times]

    rows: list[tuple[int, str, float]] = []
    for var in HOURLY_VARS:
        values = hourly.get(var)
        if values is None:
            log.warning("Open-Meteo: variable %s missing from response", var)
            continue
        if len(values) != len(timestamps):
            raise FetchError(f"Open-Meteo: inconsistent length for {var}")
        for ts, value in zip(timestamps, values):
            if value is None:  # gap in the model: skipped, not interpolated
                continue
            rows.append((ts, var, float(value)))

    # Useful for diagnosing discrepancies: the model responds on the nearest
    # grid cell, not the exact coordinates.
    log.info(
        "Open-Meteo %s: %d values over %d instants; cell at %.4f,%.4f elevation %sm (requested %.4f,%.4f elevation %.0fm)",
        cfg.model,
        len(rows),
        len(timestamps),
        payload.get("latitude", float("nan")),
        payload.get("longitude", float("nan")),
        payload.get("elevation", "?"),
        loc.latitude,
        loc.longitude,
        loc.elevation_m,
    )
    return rows


def _parse_utc(value: str) -> int:
    """With timezone=UTC the API returns ISO without offset: '2026-07-17T14:00'."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FetchError(f"Open-Meteo: unparseable timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())
