import pytest

from meteoagent.store import Store

HOUR = 3600
NOW = 1_800_000_000


@pytest.fixture
def store():
    with Store(":memory:") as s:
        yield s


class TestObservations:
    def test_roundtrip(self, store):
        store.save_observations("arpae:x", [(NOW, "wind_speed_10m", 12.5)])
        assert store.observation_series("arpae:x", NOW - HOUR, NOW + HOUR) == [
            (NOW, {"wind_speed_10m": 12.5})
        ]

    def test_writes_are_idempotent(self, store):
        rows = [(NOW, "wind_speed_10m", 12.5)]
        store.save_observations("arpae:x", rows)
        store.save_observations("arpae:x", rows)
        assert store.counts()["observation"] == 1

    def test_correction_overwrites(self, store):
        # ARPAE retransmits corrected data: the last one received must win
        store.save_observations("arpae:x", [(NOW, "temperature_2m", 20.0)])
        store.save_observations("arpae:x", [(NOW, "temperature_2m", 21.0)])
        assert store.observation_series("arpae:x", 0, 2**31)[0][1]["temperature_2m"] == 21.0

    def test_sources_are_isolated(self, store):
        store.save_observations("a", [(NOW, "v", 1.0)])
        store.save_observations("b", [(NOW, "v", 2.0)])
        assert store.observation_series("a", 0, 2**31) == [(NOW, {"v": 1.0})]
        assert sorted(store.observation_sources()) == ["a", "b"]

    def test_series_groups_by_timestamp_and_sorts(self, store):
        store.save_observations(
            "x",
            [
                (NOW + HOUR, "a", 2.0),
                (NOW, "a", 1.0),
                (NOW, "b", 9.0),
            ],
        )
        assert store.observation_series("x", 0, 2**31) == [
            (NOW, {"a": 1.0, "b": 9.0}),
            (NOW + HOUR, {"a": 2.0}),
        ]


class TestForecast:
    def test_latest_snapshot_wins(self, store):
        store.save_forecast("om", NOW, [(NOW + HOUR, "temperature_2m", 20.0)])
        store.save_forecast("om", NOW + 900, [(NOW + HOUR, "temperature_2m", 22.0)])
        assert store.latest_fetch_time("om") == NOW + 900
        series = store.latest_forecast_series("om", 0, 2**31)
        assert series == [(NOW + HOUR, {"temperature_2m": 22.0})]

    def test_old_snapshots_are_kept_for_history(self, store):
        # that's the whole point of keeping snapshots: seeing how the forecast changes
        store.save_forecast("om", NOW, [(NOW + 24 * HOUR, "wind_gusts_10m", 30.0)])
        store.save_forecast("om", NOW + 900, [(NOW + 24 * HOUR, "wind_gusts_10m", 55.0)])
        rows = store.conn.execute(
            "SELECT fetched_at, value FROM forecast WHERE valid_ts = ? ORDER BY fetched_at",
            (NOW + 24 * HOUR,),
        ).fetchall()
        assert [r["value"] for r in rows] == [30.0, 55.0]

    def test_window_filters_validity(self, store):
        store.save_forecast(
            "om",
            NOW,
            [(NOW + i * HOUR, "temperature_2m", float(i)) for i in range(10)],
        )
        series = store.latest_forecast_series("om", NOW + 2 * HOUR, NOW + 4 * HOUR)
        assert [ts for ts, _ in series] == [NOW + 2 * HOUR, NOW + 3 * HOUR, NOW + 4 * HOUR]

    def test_empty_when_no_snapshot(self, store):
        assert store.latest_forecast_series("om", 0, 2**31) == []
        assert store.latest_fetch_time("om") is None


class TestPurge:
    def test_observations_purged_by_timestamp(self, store):
        store.save_observations("x", [(NOW - 100 * HOUR, "v", 1.0), (NOW - HOUR, "v", 2.0)])
        deleted = store.purge(NOW, retention_h=72)
        assert deleted["observation"] == 1
        assert len(store.observation_series("x", 0, 2**31)) == 1

    def test_forecast_purged_by_fetch_time_not_validity(self, store):
        # an old snapshot gets thrown away entirely, including its future forecasts
        store.save_forecast("om", NOW - 100 * HOUR, [(NOW + 24 * HOUR, "v", 1.0)])
        store.save_forecast("om", NOW, [(NOW + 24 * HOUR, "v", 2.0)])
        deleted = store.purge(NOW, retention_h=72)
        assert deleted["forecast"] == 1
        assert store.latest_forecast_series("om", 0, 2**31) == [(NOW + 24 * HOUR, {"v": 2.0})]

    def test_purge_keeps_the_window_boundary(self, store):
        store.save_observations("x", [(NOW - 72 * HOUR, "v", 1.0)])
        assert store.purge(NOW, retention_h=72)["observation"] == 0

    def test_purge_on_empty_db_is_harmless(self, store):
        assert store.purge(NOW, 72) == {"observation": 0, "forecast": 0, "notification": 0}


class TestRuleState:
    def test_default_state(self, store):
        assert store.get_rule_state("never_seen") == (0, None)

    def test_upsert(self, store):
        store.set_rule_state("r", 1, NOW)
        assert store.get_rule_state("r") == (1, NOW)
        store.set_rule_state("r", 0, NOW)
        assert store.get_rule_state("r") == (0, NOW)

    def test_notification_log(self, store):
        store.log_notification(NOW, "r", "text", delivered=True)
        assert store.counts()["notification"] == 1
