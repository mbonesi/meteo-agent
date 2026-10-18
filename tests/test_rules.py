import pytest

from meteoagent.config import Rule
from meteoagent.rules import (
    CompiledRule,
    add_derived,
    decide,
    evaluate,
    mark_notified,
    median_step_s,
)
from meteoagent.store import Store

HOUR = 3600
T0 = 1_800_000_000  # arbitrary instant, aligned to the hour


def series(values, step=HOUR, start=T0):
    """Builds a series from a list of dicts."""
    return [(start + i * step, dict(v)) for i, v in enumerate(values)]


def wind(direction, speed, gusts=None):
    v = {"wind_direction_10m": direction, "wind_speed_10m": speed}
    if gusts is not None:
        v["wind_gusts_10m"] = gusts
    return v


def rule(**kwargs):
    base = dict(
        name="test",
        when='dir_in(wind_direction_10m, ["NE","ENE","E","ESE","SE"]) and wind_speed_10m > 20',
    )
    base.update(kwargs)
    return CompiledRule(Rule(**base))


@pytest.fixture
def store():
    with Store(":memory:") as s:
        yield s


def cycle(store, r, condition, now_ts, delivered=True):
    """One agent cycle: decide and, if it fires and delivery succeeds, record it."""
    fired, reason = decide(store, r, condition, now_ts)
    if fired and delivered:
        mark_notified(store, r.name, now_ts)
    return fired, reason


class TestMedianStep:
    def test_hourly(self):
        assert median_step_s(series([{}] * 5)) == HOUR

    def test_quarter_hourly(self):
        assert median_step_s(series([{}] * 5, step=900)) == 900

    def test_single_sample_falls_back(self):
        assert median_step_s(series([{}])) == HOUR

    def test_tolerates_a_gap(self):
        s = [(T0, {}), (T0 + HOUR, {}), (T0 + 5 * HOUR, {}), (T0 + 6 * HOUR, {})]
        assert median_step_s(s) == HOUR


class TestDerived:
    def test_rolling_sum_uses_timestamps(self):
        s = series([{"precipitation": 1.0} for _ in range(5)])
        out = add_derived(s)
        # 3h window: sample i includes itself and the two before it
        assert out[0][1]["precipitation_sum_3h"] == 1.0
        assert out[2][1]["precipitation_sum_3h"] == 3.0
        assert out[4][1]["precipitation_sum_3h"] == 3.0

    def test_rolling_sum_on_quarter_hourly_series(self):
        # 12 samples at 15 min = 3 h; the 3h window takes 12 of them (tail excluded)
        s = series([{"precipitation": 1.0} for _ in range(16)], step=900)
        out = add_derived(s)
        assert out[15][1]["precipitation_sum_3h"] == 12.0

    def test_rolling_max(self):
        s = series([{"wind_gusts_10m": v} for v in (10, 50, 20, 15, 12)])
        out = add_derived(s)
        assert out[2][1]["wind_gusts_max_3h"] == 50
        assert out[4][1]["wind_gusts_max_3h"] == 20

    def test_missing_source_var_is_skipped(self):
        out = add_derived(series([{"temperature_2m": 5.0}]))
        assert "precipitation_sum_3h" not in out[0][1]

    def test_does_not_mutate_input(self):
        s = series([{"precipitation": 1.0}])
        add_derived(s)
        assert s[0][1] == {"precipitation": 1.0}


class TestEvaluate:
    def test_any_matches_single_hour(self):
        s = series([wind(280, 30), wind(100, 25), wind(0, 5)])
        condition, matches, samples, _ = evaluate(rule(mode="any"), s)
        assert condition
        assert len(matches) == 1
        assert matches[0].ts == T0 + HOUR
        assert samples == 3

    def test_all_requires_every_sample(self):
        s = series([wind(100, 25), wind(110, 30)])
        assert evaluate(rule(mode="all"), s)[0]
        s = series([wind(100, 25), wind(280, 30)])
        assert not evaluate(rule(mode="all"), s)[0]

    def test_consecutive_needs_a_run(self):
        # two separate hours: not enough for min_hours=2 consecutive
        s = series([wind(100, 25), wind(280, 5), wind(100, 25)])
        assert not evaluate(rule(mode="consecutive", min_hours=2), s)[0]
        s = series([wind(100, 25), wind(100, 25), wind(280, 5)])
        assert evaluate(rule(mode="consecutive", min_hours=2), s)[0]

    def test_consecutive_scales_to_sample_rate(self):
        # on a 15-min series, min_hours=1 requires 4 consecutive samples
        r = rule(mode="consecutive", min_hours=1)
        s = series([wind(100, 25)] * 3 + [wind(0, 0)] * 4, step=900)
        assert not evaluate(r, s)[0]
        s = series([wind(100, 25)] * 4 + [wind(0, 0)] * 3, step=900)
        assert evaluate(r, s)[0]

    def test_step_comes_from_evaluated_samples_not_unrelated_vars(self):
        """Regression (Torre Asinelli): temperature every 15 min, wind every 30.

        The step was inferred from all instants, hence 15 min, and min_hours=1
        required 4 wind samples = 2 hours. The same rule on the same wind data
        changed outcome depending on whether the station published temperature.
        """
        r = rule(mode="consecutive", min_hours=1)
        values = []
        for i in range(12):  # 3 h at 15 min
            v = {"temperature_2m": 15.0}
            if i % 2 == 0:  # wind every 30 min, strong over the last hour and a half
                v.update(wind(90, 30 if i >= 6 else 5))
            values.append(v)
        condition, _, samples, reason = evaluate(r, series(values, step=900))
        assert samples == 6
        assert condition, reason
        assert "ne servono 2" in reason

    def test_count_mode(self):
        s = series([wind(100, 25), wind(280, 5), wind(110, 25), wind(0, 0)])
        assert evaluate(rule(mode="count", min_hours=2), s)[0]
        assert not evaluate(rule(mode="count", min_hours=3), s)[0]

    def test_empty_series(self):
        condition, matches, samples, reason = evaluate(rule(), [])
        assert not condition and not matches and samples == 0
        assert "nessun dato" in reason

    def test_missing_variables_are_skipped_not_false(self):
        # an instant missing wind_speed_10m must not count as "condition false":
        # it's a data gap, and must be excluded from the quantifier
        s = [
            (T0, {"wind_direction_10m": 100}),  # missing speed
            (T0 + HOUR, wind(100, 25)),
        ]
        condition, matches, samples, _ = evaluate(rule(mode="all"), s)
        assert condition  # the only evaluable instant satisfies the condition
        assert samples == 1

    def test_gust_direction_rule_on_observations(self):
        r = rule(
            when='dir_in(wind_gusts_direction_10m, ["NE","ENE","E","ESE","SE"]) and wind_gusts_10m > 45',
            source="observation",
            mode="any",
        )
        s = series(
            [
                {"wind_gusts_direction_10m": 280, "wind_gusts_10m": 60},  # wrong direction
                {"wind_gusts_direction_10m": 120, "wind_gusts_10m": 50},  # match
            ]
        )
        condition, matches, _, _ = evaluate(r, s)
        assert condition and len(matches) == 1


class TestDecide:
    def test_fires_on_rising_edge(self, store):
        r = rule(trigger="edge", cooldown_h=6)
        assert cycle(store, r, True, T0) == (True, "condizione soddisfatta, da notificare")

    def test_does_not_repeat_while_condition_stays_true(self, store):
        r = rule(trigger="edge", cooldown_h=0)
        cycle(store, r, True, T0)
        fired, reason = cycle(store, r, True, T0 + HOUR)
        assert not fired and "fronte gia' notificato" in reason

    def test_refires_after_condition_clears_and_cooldown_expires(self, store):
        r = rule(trigger="edge", cooldown_h=6)
        cycle(store, r, True, T0)
        cycle(store, r, False, T0 + HOUR)  # re-entry
        fired, reason = cycle(store, r, True, T0 + 2 * HOUR)
        assert not fired and "cooldown" in reason
        fired, _ = cycle(store, r, True, T0 + 7 * HOUR)
        assert fired

    def test_level_trigger_repeats_after_cooldown(self, store):
        r = rule(trigger="level", cooldown_h=2)
        assert cycle(store, r, True, T0)[0]
        assert not cycle(store, r, True, T0 + HOUR)[0]
        assert cycle(store, r, True, T0 + 2 * HOUR)[0]

    def test_state_survives_restart(self, store):
        r = rule(trigger="edge", cooldown_h=6)
        cycle(store, r, True, T0)
        # new CompiledRule object = process restart, same database
        fired, reason = cycle(store, rule(trigger="edge", cooldown_h=6), True, T0 + HOUR)
        assert not fired and "fronte" in reason

    def test_never_fires_when_condition_false(self, store):
        fired, reason = cycle(store, rule(), False, T0)
        assert not fired and reason == "condizione non soddisfatta"

    def test_edge_suppressed_by_cooldown_is_not_lost(self, store):
        """Regression: an edge suppressed by cooldown must not consume the edge.

        Otherwise the event disappears: the cooldown silences it, the state
        marks it as "already notified", and nobody ever announces it.
        """
        r = rule(trigger="edge", cooldown_h=6)
        assert cycle(store, r, True, T0)[0]  # first event: notified
        cycle(store, r, False, T0 + HOUR)  # re-entry
        fired, reason = cycle(store, r, True, T0 + 2 * HOUR)  # second event, silenced
        assert not fired and "cooldown" in reason
        assert store.get_rule_state("test")[0] == 0, "the edge has not been notified yet"
        # ...and as soon as the cooldown expires, with the condition still true, it goes out
        assert cycle(store, r, True, T0 + 7 * HOUR)[0]

    def test_condition_true_throughout_notifies_once(self, store):
        r = rule(trigger="edge", cooldown_h=6)
        fired = [cycle(store, r, True, T0 + i * 900)[0] for i in range(48)]  # 12 h at 15 min
        assert sum(fired) == 1

    def test_failed_delivery_is_retried_next_cycle(self, store):
        """Regression: the state used to be saved before sending.

        A failed Telegram send still marked the edge as notified and started
        the cooldown: the alert was lost forever.
        """
        r = rule(trigger="edge", cooldown_h=6)
        assert cycle(store, r, True, T0, delivered=False)[0]
        assert store.get_rule_state("test") == (0, None)
        assert cycle(store, r, True, T0 + 900)[0], "must retry on the next cycle"
        assert not cycle(store, r, True, T0 + 1800)[0], "delivered: no duplicates"

    def test_dry_run_decision_does_not_rearm(self, store):
        r = rule(trigger="edge", cooldown_h=0)
        assert cycle(store, r, True, T0)[0]
        # condition cleared, but in dry-run: the edge must NOT be re-armed
        assert not decide(store, r, False, T0 + 900, persist=False)[0]
        assert store.get_rule_state("test")[0] == 1
        # and the decision is the same as in a real cycle
        assert decide(store, r, True, T0 + 1800, persist=False) == decide(store, r, True, T0 + 1800)

    def test_decide_alone_does_not_consume_the_edge(self, store):
        r = rule(trigger="edge", cooldown_h=6)
        assert decide(store, r, True, T0)[0]
        assert decide(store, r, True, T0 + 900)[0]
