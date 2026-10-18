from meteoagent.config import Rule
from meteoagent.message import _fmt_dt, build, detail_rows, facts
from meteoagent.rules import Evaluation, Match

TZ = "Europe/Rome"
TS = 1784289600  # 2026-07-17 12:00 UTC = 14:00 local (daylight saving)

RULE = Rule(
    name="vento_est_previsto",
    title="Vento da E/SE in arrivo",
    when='dir_in(wind_direction_10m, ["NE","ENE","E","ESE","SE"]) and wind_speed_10m > 20',
    source="forecast",
    horizon_h=48,
)


def evaluation(n_matches=2, rule=RULE):
    matches = tuple(
        Match(
            TS + i * 3600,
            {"wind_direction_10m": 100.0 + i, "wind_speed_10m": 25.0 + i, "temperature_2m": 30.0},
        )
        for i in range(n_matches)
    )
    return Evaluation(
        rule=rule,
        condition=True,
        fired=True,
        matches=matches,
        samples=48,
        reason="2/48 istanti soddisfano la condizione; notifica inviata",
    )


class TestFormatting:
    def test_italian_weekday_regardless_of_locale(self):
        assert _fmt_dt(TS, TZ) == "ven 17/07 14:00"

    def test_uses_local_timezone_not_utc(self):
        # TS is 12:00 UTC; in July Rome is UTC+2
        assert "14:00" in _fmt_dt(TS, TZ)
        assert "12:00" in _fmt_dt(TS, "UTC")


class TestDetailRows:
    def test_shows_only_variables_the_rule_looks_at(self):
        rows = detail_rows(evaluation(1), TZ)
        assert len(rows) == 1
        assert "E (100°)" in rows[0]  # direction formatted with sector
        assert "25.0 km/h" in rows[0]
        assert "30.0" not in rows[0]  # temperature_2m is not in the rule: must not be shown

    def test_limit(self):
        assert len(detail_rows(evaluation(50), TZ, limit=5)) == 5


class TestBuild:
    def test_structure_without_prose(self):
        text = build(evaluation(), "open-meteo:best_match", TS, TZ, None)
        assert "<b>Vento da E/SE in arrivo</b>" in text
        assert "Previsione · open-meteo:best_match" in text
        assert "aggiornato ven 17/07 14:00" in text
        assert text.count("•") == 2

    def test_prose_is_included(self):
        text = build(evaluation(), "om", TS, TZ, "Vento in rinforzo da sudest domani mattina.")
        assert "Vento in rinforzo da sudest domani mattina." in text

    def test_observation_label(self):
        rule = Rule(name="r", title="T", when="wind_speed_10m > 20", source="observation")
        text = build(evaluation(1, rule), "arpae:x", TS, TZ, None)
        assert "Osservato · arpae:x" in text

    def test_html_is_escaped(self):
        rule = Rule(name="r", title="vento <b>forte</b> & pioggia", when="wind_speed_10m > 20")
        text = build(evaluation(1, rule), "om", TS, TZ, "prosa con <script>")
        assert "&lt;b&gt;forte&lt;/b&gt; &amp; pioggia" in text
        assert "&lt;script&gt;" in text
        assert "<script>" not in text

    def test_truncation_notice(self):
        text = build(evaluation(20), "om", TS, TZ, None)
        assert "e altri 8" in text


class TestFacts:
    def test_only_contains_measured_data(self):
        f = facts(evaluation(2), "open-meteo:best_match", TS, TZ)
        assert f["titolo_regola"] == "Vento da E/SE in arrivo"
        assert f["tipo_dato"] == "previsione"
        assert f["sorgente"] == "open-meteo:best_match"
        assert f["istanti_totali"] == 2
        assert f["istanti"][0]["quando"] == "ven 17/07 14:00"
        assert f["istanti"][0]["wind_direction_10m"] == "E 100°"
        assert f["istanti"][0]["wind_speed_10m"] == 25.0
        # only the rule's variables: no noise to feed to the model
        assert "temperature_2m" not in f["istanti"][0]

    def test_observation_kind(self):
        rule = Rule(name="r", when="wind_speed_10m > 20", source="observation")
        assert facts(evaluation(1, rule), "arpae:x", None, TZ)["tipo_dato"] == "osservazione"
