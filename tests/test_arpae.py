"""Tests for the RMAP parser against a synthetic feed built per the RFC.

The real feed isn't reachable from the tests (and shouldn't be: a test that
depends on the network isn't a test). The lines below follow the structure
documented at https://doc.rmap.cc/rmap_rfc/rfc.html
"""

import gzip
import json

import pytest

from meteoagent.config import ArpaeConfig, Location
from meteoagent.http import FetchError
from meteoagent.sources.arpae import (
    FeedValidators,
    discover_stations,
    download,
    extract,
    fetch,
    haversine_km,
    iter_reports,
    select_station,
    source_id,
)

# Bologna city centre: a placeholder, as in config.example.toml.
LOC = Location(latitude=44.49373453937952, longitude=11.342959900671165, elevation_m=54)

STATION_LON = 1134296  # synthetic station about 400 m north of LOC
STATION_LAT = 4449733
FAR_LON = 1200000  # ~12.0 E, beyond 50 km
FAR_LAT = 4450000

ANAGRAFICA = {"vars": {"B01019": {"v": "Bologna Test"}, "B07030": {"v": 60.0}}}


def report(ts, lon=STATION_LON, lat=STATION_LAT, network="locali", data=None):
    return {
        "ident": None,
        "network": network,
        "lon": lon,
        "lat": lat,
        "date": ts,
        "data": [ANAGRAFICA] + list(data or []),
    }


def wind_element(direction, speed_ms, pindicator=254, attrs=None):
    return {
        "level": [103, 10000, None, None],
        "timerange": [pindicator, 0, 0],
        "vars": {
            "B11001": {"v": direction, "a": attrs or {}},
            "B11002": {"v": speed_ms, "a": attrs or {}},
        },
    }


def gust_element(direction, speed_ms):
    return {
        "level": [103, 10000, None, None],
        "timerange": [2, 0, 3600],
        "vars": {"B11041": {"v": speed_ms, "a": {}}, "B11043": {"v": direction, "a": {}}},
    }


def temp_element(kelvin, attrs=None):
    return {
        "level": [103, 2000, None, None],
        "timerange": [254, 0, 0],
        "vars": {"B12101": {"v": kelvin, "a": attrs or {}}},
    }


def rain_element(mm):
    return {
        "level": [1, None, None, None],
        "timerange": [1, 0, 3600],
        "vars": {"B13011": {"v": mm, "a": {}}},
    }


def feed(*reports) -> bytes:
    return "\n".join(json.dumps(r) for r in reports).encode()


@pytest.fixture
def station():
    raw = feed(report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0)]))
    return discover_stations(raw, LOC, radius_km=15)[0]


class TestGeo:
    def test_haversine_against_known_distance(self):
        # Bologna center -> Modena center, about 37 km
        d = haversine_km(44.4949, 11.3426, 44.6471, 10.9252)
        assert 36 < d < 40

    def test_zero_distance(self):
        assert haversine_km(44.5, 11.3, 44.5, 11.3) == pytest.approx(0)


class TestIterReports:
    def test_parses_station_metadata(self):
        raw = feed(report("2026-07-17T12:00:00Z"))
        r = list(iter_reports(raw))[0]
        assert r.station_name == "Bologna Test"
        assert r.station_height_m == 60.0
        assert r.network == "locali"
        assert r.ident is None

    def test_timestamp_is_utc_epoch(self):
        r = list(iter_reports(feed(report("2026-07-17T12:00:00Z"))))[0]
        assert r.ts == 1784289600

    def test_accepts_space_and_missing_z(self):
        a = list(iter_reports(feed(report("2026-07-17 12:00:00"))))[0]
        b = list(iter_reports(feed(report("2026-07-17T12:00:00Z"))))[0]
        assert a.ts == b.ts

    def test_gzip_is_transparent(self):
        raw = gzip.compress(feed(report("2026-07-17T12:00:00Z")))
        assert len(list(iter_reports(raw))) == 1

    def test_bad_lines_are_skipped_not_fatal(self):
        raw = b"\n".join(
            [
                b"{ this is not json",
                json.dumps(report("2026-07-17T12:00:00Z")).encode(),
                b"",
                b'{"lon": 1, "lat": 2}',  # missing date
            ]
        )
        assert len(list(iter_reports(raw))) == 1

    def test_summaries_response_is_ignored(self):
        # in "summaries" responses, date is an array and there is no data
        obj = report("x")
        obj["date"] = ["2026-01-01T00:00:00", "2026-07-17T00:00:00"]
        assert list(iter_reports(feed(obj))) == []


class TestDiscover:
    def test_finds_nearby_and_excludes_far(self):
        raw = feed(
            report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0)]),
            report("2026-07-17T12:00:00Z", lon=FAR_LON, lat=FAR_LAT),
        )
        stations = discover_stations(raw, LOC, radius_km=15)
        assert len(stations) == 1
        assert stations[0].distance_km < 1

    def test_merges_multiple_instants_of_same_station(self):
        raw = feed(
            report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0)]),
            report("2026-07-17T12:15:00Z", data=[wind_element(110, 12.0), gust_element(120, 20.0)]),
        )
        stations = discover_stations(raw, LOC, radius_km=15)
        assert len(stations) == 1
        assert stations[0].last_ts == 1784290500
        assert {"wind_speed_10m", "wind_gusts_direction_10m"} <= stations[0].canonical_vars

    def test_sorted_by_distance(self):
        raw = feed(
            report("2026-07-17T12:00:00Z", lon=STATION_LON + 5000, lat=STATION_LAT),
            report("2026-07-17T12:00:00Z"),
        )
        stations = discover_stations(raw, LOC, radius_km=15)
        assert stations[0].distance_km < stations[1].distance_km

    def test_canonical_vars_ignores_wrong_level(self):
        wrong = {
            "level": [103, 2000, None, None],  # wind at 2 m: not our level
            "timerange": [254, 0, 0],
            "vars": {"B11002": {"v": 5.0}},
        }
        stations = discover_stations(feed(report("2026-07-17T12:00:00Z", data=[wrong])), LOC, 15)
        assert "wind_speed_10m" not in stations[0].canonical_vars

    def test_select_station_requires_wind(self):
        raw = feed(
            report("2026-07-17T12:00:00Z", lon=STATION_LON + 100, data=[temp_element(295.0)]),
            report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0)]),
        )
        stations = discover_stations(raw, LOC, radius_km=15)
        chosen = select_station(stations)
        assert chosen is not None
        assert "wind_speed_10m" in chosen.canonical_vars

    def test_select_station_returns_none_if_nothing_usable(self):
        raw = feed(report("2026-07-17T12:00:00Z", data=[temp_element(295.0)]))
        assert select_station(discover_stations(raw, LOC, 15)) is None


class TestExtract:
    def test_unit_conversions(self, station):
        raw = feed(
            report(
                "2026-07-17T12:00:00Z",
                data=[wind_element(100, 10.0), temp_element(295.15), rain_element(2.5)],
            )
        )
        values = dict((var, v) for _, var, v in extract(raw, station))
        assert values["wind_speed_10m"] == pytest.approx(36.0)  # 10 m/s -> km/h
        assert values["temperature_2m"] == pytest.approx(22.0)  # 295.15 K -> C
        assert values["wind_direction_10m"] == 100  # degrees stay degrees
        assert values["precipitation"] == 2.5  # kg/m2 = mm

    def test_gust_direction_is_extracted(self, station):
        raw = feed(report("2026-07-17T12:00:00Z", data=[gust_element(120, 15.0)]))
        values = dict((var, v) for _, var, v in extract(raw, station))
        assert values["wind_gusts_direction_10m"] == 120
        assert values["wind_gusts_10m"] == pytest.approx(54.0)

    def test_keeps_every_instant(self, station):
        raw = feed(
            report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0)]),
            report("2026-07-17T12:15:00Z", data=[wind_element(110, 12.0)]),
        )
        rows = extract(raw, station)
        assert len({ts for ts, _, _ in rows}) == 2

    def test_other_stations_are_ignored(self, station):
        raw = feed(
            report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0)]),
            report("2026-07-17T12:00:00Z", lon=STATION_LON + 900, data=[wind_element(280, 30.0)]),
        )
        values = dict((var, v) for _, var, v in extract(raw, station))
        assert values["wind_direction_10m"] == 100

    def test_output_is_sorted_by_timestamp(self, station):
        raw = feed(
            report("2026-07-17T12:15:00Z", data=[wind_element(110, 12.0)]),
            report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0)]),
        )
        timestamps = [ts for ts, _, _ in extract(raw, station)]
        assert timestamps == sorted(timestamps)


class TestQualityControl:
    """The RFC requires obscuring data that fails quality control."""

    def test_invalidated_value_is_dropped(self, station):
        raw = feed(
            report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0, attrs={"B33007": 0})])
        )
        assert extract(raw, station) == []

    def test_high_confidence_is_kept(self, station):
        raw = feed(
            report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0, attrs={"B33007": 100})])
        )
        assert len(extract(raw, station)) == 2

    def test_manual_invalidation_flag(self, station):
        raw = feed(report("2026-07-17T12:00:00Z", data=[temp_element(295.15, attrs={"B33196": 1})]))
        assert extract(raw, station) == []

    def test_no_attributes_means_valid(self, station):
        raw = feed(report("2026-07-17T12:00:00Z", data=[temp_element(295.15)]))
        assert len(extract(raw, station)) == 1

    def test_null_value_is_dropped(self, station):
        element = {
            "level": [103, 2000, None, None],
            "timerange": [254, 0, 0],
            "vars": {"B12101": {"v": None, "a": {"B33007": 0}}},
        }
        assert extract(feed(report("2026-07-17T12:00:00Z", data=[element])), station) == []

    def test_threshold_is_configurable(self, station):
        raw = feed(
            report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0, attrs={"B33007": 50})])
        )
        assert len(extract(raw, station, min_confidence=1.0)) == 2
        assert extract(raw, station, min_confidence=90.0) == []


class TestTimerangePriority:
    def test_prefers_vectorial_mean_over_instantaneous(self, station):
        raw = feed(
            report(
                "2026-07-17T12:00:00Z",
                data=[
                    wind_element(100, 10.0, pindicator=254),  # instantaneous
                    wind_element(110, 12.0, pindicator=200),  # vector average: preferred
                ],
            )
        )
        values = dict((var, v) for _, var, v in extract(raw, station))
        assert values["wind_direction_10m"] == 110
        assert values["wind_speed_10m"] == pytest.approx(43.2)

    def test_unexpected_pindicator_is_used_as_last_resort(self, station):
        raw = feed(report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0, pindicator=99)]))
        values = dict((var, v) for _, var, v in extract(raw, station))
        assert values["wind_speed_10m"] == pytest.approx(36.0)


class TestSourceId:
    def test_pinned_station(self):
        cfg = ArpaeConfig(station_lon=STATION_LON, station_lat=STATION_LAT, station_network="locali")
        assert source_id(cfg) == f"arpae:locali:{STATION_LON},{STATION_LAT}"

    def test_none_when_not_pinned(self):
        assert source_id(ArpaeConfig()) is None

    def test_station_source_id_matches_config(self, station):
        cfg = ArpaeConfig(
            station_lon=station.lon, station_lat=station.lat, station_network=station.network
        )
        assert station.source_id() == source_id(cfg)


# --------------------------------------------------------------------- download

PINNED = ArpaeConfig(station_lon=STATION_LON, station_lat=STATION_LAT, station_network="locali")


class FakeResponse:
    def __init__(self, status_code=200, body=b"", headers=None, chunk=7):
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}
        self.chunk = chunk  # small chunks: lines get split in the middle

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), self.chunk):
            yield self.body[i : i + self.chunk]


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, timeout, stream, headers):
        self.requests.append(dict(headers))
        return self.responses.pop(0)


OTHER = report("2026-07-17T12:00:00Z", lon=FAR_LON, lat=FAR_LAT, data=[wind_element(10, 1.0)])
MINE = report("2026-07-17T12:00:00Z", data=[wind_element(100, 10.0)])


class TestDownload:
    def test_lines_survive_arbitrary_chunk_boundaries(self):
        body = feed(MINE, OTHER, MINE)
        raw = download(FakeSession(FakeResponse(body=body, chunk=5)), ArpaeConfig())
        assert raw.splitlines() == body.splitlines()

    def test_gzip_is_decompressed_while_streaming(self):
        body = feed(MINE, OTHER)
        raw = download(FakeSession(FakeResponse(body=gzip.compress(body), chunk=11)), ArpaeConfig())
        assert raw.splitlines() == body.splitlines()

    def test_keep_filters_lines_before_parsing(self):
        from meteoagent.sources.arpae import pinned_line_filter

        body = feed(OTHER, MINE, OTHER)
        raw = download(FakeSession(FakeResponse(body=body)), PINNED, keep=pinned_line_filter(PINNED))
        assert len(raw.splitlines()) == 1
        assert json.loads(raw)["lon"] == STATION_LON

    def test_cap_on_size(self):
        with pytest.raises(FetchError, match="exceeds"):
            download(FakeSession(FakeResponse(body=feed(MINE) * 10)), ArpaeConfig(), max_bytes=100)

    def test_conditional_get_returns_none_on_304(self):
        validators = FeedValidators()
        session = FakeSession(
            FakeResponse(body=feed(MINE), headers={"ETag": '"abc"', "Last-Modified": "Sun, 18 Oct"}),
            FakeResponse(status_code=304),
        )
        assert download(session, ArpaeConfig(), validators=validators) is not None
        assert validators.etag == '"abc"'
        assert download(session, ArpaeConfig(), validators=validators) is None
        assert session.requests[0] == {}
        assert session.requests[1] == {"If-None-Match": '"abc"', "If-Modified-Since": "Sun, 18 Oct"}

    def test_interrupted_download_does_not_store_validators(self):
        """A half-finished download must not make the next round believe "unchanged"."""
        validators = FeedValidators()
        response = FakeResponse(body=feed(MINE) * 10, headers={"ETag": '"abc"'})
        with pytest.raises(FetchError):
            download(FakeSession(response), ArpaeConfig(), max_bytes=100, validators=validators)
        assert validators.etag is None


class TestFetch:
    def test_pinned_station_parses_only_its_lines(self):
        session = FakeSession(FakeResponse(body=feed(OTHER, MINE, OTHER)))
        station, rows = fetch(session, PINNED, LOC)
        assert (station.lon, station.lat) == (STATION_LON, STATION_LAT)
        assert {var for _, var, _ in rows} == {"wind_speed_10m", "wind_direction_10m"}

    def test_pinned_station_missing_from_feed(self):
        session = FakeSession(FakeResponse(body=feed(OTHER)))
        with pytest.raises(FetchError, match="pinned"):
            fetch(session, PINNED, LOC)

    def test_unchanged_feed_returns_none(self):
        session = FakeSession(FakeResponse(status_code=304))
        assert fetch(session, PINNED, LOC, FeedValidators(etag='"abc"')) is None

    def test_unpinned_still_reads_whole_feed(self):
        session = FakeSession(FakeResponse(body=feed(OTHER, MINE)))
        station, _ = fetch(session, ArpaeConfig(), LOC)
        assert (station.lon, station.lat) == (STATION_LON, STATION_LAT)
