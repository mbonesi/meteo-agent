import pytest

from meteoagent import agent as agent_module
from meteoagent.agent import Agent
from meteoagent.config import Config, LLMConfig, Location, Rule, TelegramConfig
from meteoagent.store import Store

HOUR = 3600
NOW = 1_800_000_000


@pytest.fixture
def setup(monkeypatch):
    cfg = Config(
        location=Location(latitude=44.5, longitude=11.3, elevation_m=60),
        telegram=TelegramConfig(token="123:abc", chat_id="42"),
        llm=LLMConfig(enabled=False),
        rules=(
            Rule(
                name="wind",
                when="wind_speed_10m > 20",
                source="forecast",
                horizon_h=6,
                cooldown_h=6,
            ),
        ),
    )
    # No network: the forecasts are already in the archive.
    monkeypatch.setattr(Agent, "_fetch_forecast", lambda self, now_ts, result: None)
    monkeypatch.setattr(Agent, "_fetch_observations", lambda self, now_ts, result: None)

    outcomes = []
    sent = []

    def fake_send(session, tg_cfg, text):
        sent.append(text)
        return outcomes.pop(0)

    monkeypatch.setattr(agent_module.telegram, "send", fake_send)

    with Store(":memory:") as store:
        source = "open-meteo:best_match"
        store.save_forecast(source, NOW, [(NOW + h * HOUR, "wind_speed_10m", 30.0) for h in range(6)])
        yield Agent(cfg, store), store, outcomes, sent


def test_failed_delivery_is_retried_on_next_cycle(setup):
    """Regression: a failed Telegram send consumed the edge and the alert was lost."""
    agent, store, outcomes, sent = setup
    outcomes += [False, True]

    first = agent.cycle(now_ts=NOW)
    assert first.notified == 0 and len(sent) == 1
    assert store.get_rule_state("wind") == (0, None), "not delivered: state untouched"

    second = agent.cycle(now_ts=NOW + 900)
    assert second.notified == 1 and len(sent) == 2
    assert store.get_rule_state("wind") == (1, NOW + 900)

    third = agent.cycle(now_ts=NOW + 1800)
    assert third.notified == 0 and len(sent) == 2, "delivered: no duplicates"


def test_delivery_logged_in_notification_table(setup):
    agent, store, outcomes, _ = setup
    outcomes += [False, True]
    agent.cycle(now_ts=NOW)
    agent.cycle(now_ts=NOW + 900)
    rows = store.conn.execute("SELECT delivered FROM notification ORDER BY id").fetchall()
    assert [r["delivered"] for r in rows] == [0, 1]


def test_dry_run_composes_message_without_sending_or_touching_state(setup, caplog):
    """Regression: in dry-run fired was forced to False, so the message that
    would have gone out was never composed, contrary to what the README says."""
    agent, store, outcomes, sent = setup
    with caplog.at_level("INFO"):
        result = agent.cycle(now_ts=NOW, dry_run=True)
    assert sent == [], "nothing is sent in dry-run"
    assert result.notified == 0
    assert store.get_rule_state("wind") == (0, None)
    assert store.counts()["notification"] == 0
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "[dry-run] notification NOT sent for 'wind'" in logged
    assert "Istanti interessati" in logged  # notification text stays Italian


def test_dry_run_respects_cooldown(setup, caplog):
    agent, store, outcomes, sent = setup
    outcomes += [True]
    agent.cycle(now_ts=NOW)  # real notification: the cooldown starts
    assert len(sent) == 1
    with caplog.at_level("INFO"):
        agent.cycle(now_ts=NOW + 900, dry_run=True)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "[dry-run] notification NOT sent" not in logged
    assert "fronte gia' notificato" in logged


# --------------------------------------------------------------------- ARPAE

from meteoagent.config import ArpaeConfig  # noqa: E402
from meteoagent.http import FetchError  # noqa: E402
from meteoagent.sources.arpae import StationInfo  # noqa: E402

T00 = 1_800_000_000 - 1_800_000_000 % 1800  # aligned to :00 or :30


@pytest.fixture
def arpae_agent(monkeypatch):
    """Observation-only agent with a fake ARPAE fetch that counts calls."""

    def make(arpae_interval, outcomes):
        cfg = Config(
            location=Location(latitude=44.5, longitude=11.3, elevation_m=60),
            telegram=TelegramConfig(enabled=False),
            llm=LLMConfig(enabled=False),
            arpae=ArpaeConfig(
                station_lon=1, station_lat=2, station_network="x", interval_minutes=arpae_interval
            ),
        )
        monkeypatch.setattr(Agent, "_fetch_forecast", lambda self, now_ts, result: None)
        calls = []

        def fake_fetch(session, arpae_cfg, loc, validators):
            calls.append(validators)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(agent_module.arpae, "fetch", fake_fetch)
        store = Store(":memory:")
        return Agent(cfg, store), calls

    return make


def fresh_rows(ts):
    return StationInfo(ident=None, network="x", lon=1, lat=2), [(ts, "wind_speed_10m", 5.0)]


def test_arpae_default_downloads_every_cycle(arpae_agent):
    agent, calls = arpae_agent(0, [fresh_rows(T00)] * 3)
    for i in range(3):
        agent.cycle(now_ts=T00 + i * 900)
    assert len(calls) == 3


def test_arpae_interval_downloads_once_per_window(arpae_agent):
    """With [arpae].interval_minutes = 30 the feed is downloaded at :00 and :30, not :15 and :45."""
    agent, calls = arpae_agent(30, [fresh_rows(T00), fresh_rows(T00 + 1800)])
    for i in range(4):  # :00 :15 :30 :45
        agent.cycle(now_ts=T00 + i * 900)
    assert len(calls) == 2


def test_arpae_cycle_starting_slightly_early_still_opens_new_window(arpae_agent):
    agent, calls = arpae_agent(30, [fresh_rows(T00), fresh_rows(T00 + 1800)])
    agent.cycle(now_ts=T00)
    agent.cycle(now_ts=T00 + 1800 - 2)  # the timer woke up 2 s before :30
    assert len(calls) == 2


def test_arpae_failed_download_is_retried_next_cycle(arpae_agent):
    agent, calls = arpae_agent(30, [FetchError("down"), fresh_rows(T00)])
    first = agent.cycle(now_ts=T00)
    assert first.errors
    second = agent.cycle(now_ts=T00 + 900)  # same window, but the download had failed
    assert len(calls) == 2 and second.observation_rows == 1


def test_arpae_unchanged_feed_keeps_rules_running(arpae_agent):
    agent, calls = arpae_agent(0, [fresh_rows(T00), None])
    agent.cycle(now_ts=T00)
    second = agent.cycle(now_ts=T00 + 900)
    assert second.observation_rows == 0 and not second.errors
    assert calls[0] is calls[1], "same validators across cycles"
    assert agent.observation_source == "arpae:x:1,2"
