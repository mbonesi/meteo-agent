"""Orchestration: one cycle = fetch -> store -> purge -> rules -> notify.

Core principle: no source can take down the cycle. If ARPAE doesn't respond,
the forecast rules run anyway; if Open-Meteo is down, so do the observation
ones. The agent degrades, it doesn't stop.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass

from . import message, rules, telegram
from .config import Config
from .http import FetchError, make_session
from .redactor import Redactor
from .sources import arpae, openmeteo
from .store import Store

log = logging.getLogger(__name__)

ARPAE_SLOT_GRACE_S = 60


@dataclass
class CycleResult:
    forecast_rows: int = 0
    observation_rows: int = 0
    evaluations: list = None
    notified: int = 0
    errors: list = None

    def __post_init__(self):
        if self.evaluations is None:
            self.evaluations = []
        if self.errors is None:
            self.errors = []


class Agent:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.session = make_session()
        self.redactor = Redactor(cfg.llm)
        self.compiled = rules.compile_rules(cfg.rules)
        self.forecast_source = openmeteo.source_id(cfg.openmeteo) if cfg.openmeteo.enabled else None
        self.observation_source = arpae.source_id(cfg.arpae) if cfg.arpae.enabled else None
        # Conditional GET and download window for the ARPAE feed (see _fetch_observations).
        self._arpae_validators = arpae.FeedValidators()
        self._arpae_last_slot: int | None = None
        self._stop = threading.Event()
        log.info(
            "agent ready: %d rules, interval %d min, retention %d h, db %s",
            len(self.compiled),
            cfg.interval_minutes,
            cfg.store.retention_h,
            cfg.store.path,
        )

    # ------------------------------------------------------------------ fetch

    def _fetch_forecast(self, now_ts: int, result: CycleResult) -> None:
        if not self.cfg.openmeteo.enabled:
            return
        try:
            rows = openmeteo.fetch(self.session, self.cfg.openmeteo, self.cfg.location)
        except FetchError as exc:
            log.error("%s", exc)
            result.errors.append(str(exc))
            return
        # Cap the horizon to the retention window: no point keeping forecasts
        # beyond the window we later reason over.
        horizon = now_ts + self.cfg.store.retention_h * 3600
        rows = [r for r in rows if r[0] <= horizon]
        result.forecast_rows = self.store.save_forecast(self.forecast_source, now_ts, rows)

    def _fetch_observations(self, now_ts: int, result: CycleResult) -> None:
        if not self.cfg.arpae.enabled:
            return
        # The feed is downloaded once per [arpae].interval_minutes window,
        # aligned to the clock (e.g. 30 -> :00 and :30). The grace absorbs a
        # cycle that starts a moment before its quarter hour.
        interval_s = (self.cfg.arpae.interval_minutes or self.cfg.interval_minutes) * 60
        slot = (now_ts + ARPAE_SLOT_GRACE_S) // interval_s
        if slot == self._arpae_last_slot:
            log.debug("ARPAE: feed already downloaded in this %d-min window", interval_s // 60)
            return
        try:
            fetched = arpae.fetch(
                self.session, self.cfg.arpae, self.cfg.location, self._arpae_validators
            )
        except FetchError as exc:
            # The window isn't consumed: retry on the next cycle.
            log.error("%s", exc)
            result.errors.append(str(exc))
            return
        self._arpae_last_slot = slot
        if fetched is None:
            log.info("ARPAE: feed unchanged since the last download (304), nothing new")
            return
        station, rows = fetched
        self.observation_source = station.source_id()
        result.observation_rows = self.store.save_observations(self.observation_source, rows)

    # ------------------------------------------------------------------ cycle

    def cycle(self, now_ts: int | None = None, dry_run: bool = False) -> CycleResult:
        now_ts = now_ts if now_ts is not None else message.now_ts()
        result = CycleResult()

        self._fetch_forecast(now_ts, result)
        self._fetch_observations(now_ts, result)
        self.store.purge(now_ts, self.cfg.store.retention_h)

        # If a source failed just now but wrote in the past, the rules work
        # on the data that's there: that's the whole point of the history.
        observation_source = self.observation_source or _only(self.store.observation_sources())

        result.evaluations = rules.run(
            self.store,
            self.compiled,
            now_ts,
            forecast_source=self.forecast_source,
            observation_source=observation_source,
            apply_state=not dry_run,
        )

        for evaluation in result.evaluations:
            if not evaluation.fired:
                continue
            if self._notify(evaluation, now_ts, dry_run):
                result.notified += 1

        counts = self.store.counts()
        log.info(
            "cycle completed: +%d forecast, +%d observed, %d notifications, db %s",
            result.forecast_rows,
            result.observation_rows,
            result.notified,
            counts,
        )
        return result

    def _notify(self, evaluation, now_ts: int, dry_run: bool) -> bool:
        rule = evaluation.rule
        if rule.source == "forecast":
            source = self.forecast_source or "?"
            fetched_at = self.store.latest_fetch_time(source) if self.forecast_source else None
        else:
            source = self.observation_source or "?"
            fetched_at = evaluation.matches[-1].ts if evaluation.matches else None

        tz = self.cfg.location.timezone
        facts = message.facts(evaluation, source, fetched_at, tz)
        prose = self.redactor.write(facts)
        text = message.build(evaluation, source, fetched_at, tz, prose)

        if dry_run:
            log.info("[dry-run] notification NOT sent for '%s':\n%s", rule.name, text)
            return False

        if self.cfg.telegram.enabled:
            delivered = telegram.send(self.session, self.cfg.telegram, text)
        else:
            # The log is the only configured channel: the notification is "delivered" there.
            log.info("Telegram disabled; notification for '%s':\n%s", rule.name, text)
            delivered = True

        self.store.log_notification(now_ts, rule.name, text, delivered)
        if delivered:
            rules.mark_notified(self.store, rule.name, now_ts)
        else:
            log.warning("notification for '%s' not delivered: retrying next cycle", rule.name)
        return delivered

    # ------------------------------------------------------------------ loop

    def request_stop(self, *_args) -> None:
        log.info("stop request received")
        self._stop.set()

    def run_forever(self) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        interval_s = self.cfg.interval_minutes * 60

        # First cycle runs immediately: on restart we want fresh state, not a
        # wait until the next quarter hour.
        while not self._stop.is_set():
            started = time.time()
            try:
                self.cycle()
            except Exception:
                # A bug in a rule must not kill the service: log the
                # traceback and retry on the next cycle.
                log.exception("unhandled error in cycle")
            # Clock alignment: cycles fall on :00, :15, :30, :45.
            next_tick = ((int(started) // interval_s) + 1) * interval_s
            sleep_s = max(1.0, next_tick - time.time())
            log.debug("next cycle in %.0f s", sleep_s)
            self._stop.wait(sleep_s)

        log.info("agent stopped")
        return 0


def _only(values: list[str]) -> str | None:
    """The single source present, if there is exactly one."""
    return values[0] if len(values) == 1 else None
