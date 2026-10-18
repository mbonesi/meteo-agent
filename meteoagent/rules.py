"""Rules engine.

Deterministic and LLM-free: a boolean condition must give the same outcome on
the same data, today and in six months. Claude comes in afterward, only to
write the message text.

Note: the `reason` strings this module produces (Evaluation.reason, the
per-quantifier messages, decide()'s outcome text) stay in Italian on purpose
— they flow into message.py's facts() payload as the "criterio" field, which
is part of the Telegram notification content pipeline (see message.py's
module docstring). Everything else in this module (comments, docstrings, log
messages) is translated.

Each rule:
  1. takes a time series (forecast or observation) over the requested window;
  2. adds the derived variables to it (rolling sums and maxima);
  3. evaluates the expression instant by instant;
  4. applies a quantifier (any / all / count / consecutive);
  5. decides whether to notify, applying edge detection and cooldown.

Steps 4 and 5 are what prevents spam: without them, a condition true for 12
consecutive hours would generate 48 notifications 15 minutes apart.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass

from .config import Rule
from .expr import Expression, ExprError
from .store import Series, Store
from .vars import DERIVED

log = logging.getLogger(__name__)

DEFAULT_STEP_S = 3600


@dataclass(frozen=True)
class Match:
    ts: int
    values: dict[str, float]


@dataclass(frozen=True)
class Evaluation:
    rule: Rule
    condition: bool  # is the condition satisfied right now?
    fired: bool  # ...and should it be notified? (edge + cooldown)
    matches: tuple[Match, ...]
    samples: int
    reason: str

    @property
    def first_match(self) -> Match | None:
        return self.matches[0] if self.matches else None


class CompiledRule:
    """Rule with its expression already compiled and validated."""

    __slots__ = ("rule", "expr")

    def __init__(self, rule: Rule):
        self.rule = rule
        try:
            self.expr = Expression(rule.when)
        except ExprError as exc:
            raise ExprError(f"rule '{rule.name}': {exc}") from exc

    @property
    def name(self) -> str:
        return self.rule.name


def compile_rules(rules: tuple[Rule, ...]) -> list[CompiledRule]:
    """Compiles all enabled rules. Fails immediately on an invalid expression."""
    return [CompiledRule(r) for r in rules if r.enabled]


def median_step_s(series: Series) -> int:
    """Nominal sampling step of the series, inferred from the data."""
    if len(series) < 2:
        return DEFAULT_STEP_S
    deltas = [b[0] - a[0] for a, b in zip(series, series[1:]) if b[0] > a[0]]
    if not deltas:
        return DEFAULT_STEP_S
    return max(1, int(statistics.median(deltas)))


def add_derived(series: Series) -> Series:
    """Adds the rolling-window derived variables.

    The window is defined on timestamps, not on sample count: it works
    identically on hourly series (forecast) and 15-minute ones (observation).
    """
    if not series:
        return series
    present = {var for _, values in series for var in values}
    out: Series = [(ts, dict(values)) for ts, values in series]

    for name, src, hours, func in DERIVED:
        if src not in present:
            continue
        window_s = hours * 3600
        for i, (ts, values) in enumerate(out):
            collected = [
                v[src]
                for t, v in out[max(0, i - 4 * hours - 8) : i + 1]
                if ts - window_s < t <= ts and src in v
            ]
            if not collected:
                continue
            values[name] = sum(collected) if func == "sum" else max(collected)
    return out


def _min_samples(rule: Rule, step_s: int) -> int:
    """Converts min_hours to a sample count, given the series' actual step."""
    return max(1, math.ceil(rule.min_hours * 3600 / step_s))


def _quantify(rule: Rule, flags: list[bool], step_s: int) -> tuple[bool, str]:
    """Applies the quantifier to the sequence of per-instant outcomes.

    Reason strings stay in Italian: see this module's docstring.
    """
    if not flags:
        return False, "serie vuota"
    needed = _min_samples(rule, step_s)
    hits = sum(flags)

    if rule.mode == "any":
        return hits > 0, f"{hits}/{len(flags)} istanti soddisfano la condizione"
    if rule.mode == "all":
        return hits == len(flags), f"{hits}/{len(flags)} istanti soddisfano la condizione"
    if rule.mode == "count":
        return hits >= needed, f"{hits}/{len(flags)} istanti (ne servono {needed})"
    if rule.mode == "consecutive":
        longest = current = 0
        for flag in flags:
            current = current + 1 if flag else 0
            longest = max(longest, current)
        return (
            longest >= needed,
            f"sequenza consecutiva piu' lunga: {longest} istanti (ne servono {needed})",
        )
    raise ValueError(f"unhandled mode: {rule.mode}")


def evaluate(compiled: CompiledRule, series: Series) -> tuple[bool, tuple[Match, ...], int, str]:
    """Evaluates the condition on the series. Returns (outcome, matching instants, samples, reason)."""
    rule = compiled.rule
    enriched = add_derived(series)
    if not enriched:
        return False, (), 0, "nessun dato disponibile nella finestra"

    flags: list[bool] = []
    evaluated: Series = []
    matches: list[Match] = []
    skipped = 0

    for ts, values in enriched:
        # An instant with missing variables is undefined, not false: it's skipped.
        # Faking "false" would hide a data gap behind an "all clear".
        if not compiled.expr.names <= set(values):
            skipped += 1
            continue
        try:
            ok = compiled.expr.evaluate(values)
        except ExprError as exc:
            log.error("rule '%s' at ts=%d: %s", rule.name, ts, exc)
            skipped += 1
            continue
        flags.append(ok)
        evaluated.append((ts, values))
        if ok:
            matches.append(Match(ts, dict(values)))

    # The step must be inferred from the evaluated instants, not from the whole
    # series: a station publishing temperature every 15 min and wind every 30
    # has instants every 15 min, but a wind rule has samples every 30. With the
    # whole series' step, min_hours = 1 would become 4 wind samples, i.e. 2 h.
    step_s = median_step_s(evaluated)

    if skipped:
        log.warning(
            "rule '%s': %d of %d instants skipped for missing variables (needed: %s)",
            rule.name,
            skipped,
            len(enriched),
            sorted(compiled.expr.names),
        )
    if not flags:
        return False, (), 0, f"nessun istante valutabile su {len(enriched)} disponibili"

    condition, reason = _quantify(rule, flags, step_s)
    return condition, tuple(matches), len(flags), reason


def decide(
    store: Store, compiled: CompiledRule, condition: bool, now_ts: int, persist: bool = True
) -> tuple[bool, str]:
    """Applies edge detection and cooldown.

    trigger = "edge"  -> notifies only on the false->true transition
    trigger = "level" -> notifies on every cycle where it's true, once cooldown has expired

    Mind the semantics of rule_state.last_value: it is NOT "the condition was
    true last cycle", it's "the edge has already been notified". The
    difference matters when an edge falls inside a previous notification's
    cooldown: if we marked the edge as consumed without having notified it,
    the event would disappear forever. Instead it gets announced as soon as
    the cooldown expires.

    For the same reason decide() does NOT record the fire: mark_notified()
    does, and the caller invokes it only once delivery has succeeded. If the
    send fails the state stays as it was, and the rule fires again next cycle.

    With persist=False (dry-run) the decision is the same, but the state is
    not touched, not even to re-arm the edge.

    Reason strings stay in Italian: see this module's docstring.
    """
    rule = compiled.rule
    notified, last_fired = store.get_rule_state(rule.name)
    fire = False

    if not condition:
        reason = "condizione non soddisfatta"
    elif rule.trigger == "edge" and notified == 1:
        reason = "fronte gia' notificato, condizione ancora attiva"
    elif last_fired is not None and now_ts - last_fired < rule.cooldown_h * 3600:
        remaining = rule.cooldown_h * 3600 - (now_ts - last_fired)
        reason = f"cooldown attivo, mancano {remaining // 60} min"
    else:
        fire = True
        reason = "condizione soddisfatta, da notificare"

    if not condition and notified and persist:
        store.set_rule_state(rule.name, 0, last_fired)  # re-entering re-arms the edge
    # if the condition is true the state is left alone: if the rule fires,
    # mark_notified() updates it after delivery; if not, it stays as it was
    return fire, reason


def mark_notified(store: Store, rule_name: str, now_ts: int) -> None:
    """Records a delivered notification: consumes the edge and starts the cooldown."""
    store.set_rule_state(rule_name, 1, now_ts)


def run(
    store: Store,
    compiled_rules: list[CompiledRule],
    now_ts: int,
    forecast_source: str | None,
    observation_source: str | None,
    apply_state: bool = True,
) -> list[Evaluation]:
    """Evaluates all rules. With apply_state=False, state is left untouched (dry-run).

    Edge detection and cooldown apply in dry-run too: fired says whether the
    rule would really fire right now, so the caller can compose the message
    that would go out, without sending it.
    """
    results: list[Evaluation] = []
    for compiled in compiled_rules:
        rule = compiled.rule
        if rule.source == "forecast":
            if forecast_source is None:
                log.warning("rule '%s': no forecast available", rule.name)
                continue
            series = store.latest_forecast_series(
                forecast_source, now_ts, now_ts + rule.horizon_h * 3600
            )
        else:
            if observation_source is None:
                log.warning("rule '%s': no observation available", rule.name)
                continue
            series = store.observation_series(
                observation_source, now_ts - rule.horizon_h * 3600, now_ts
            )

        condition, matches, samples, reason = evaluate(compiled, series)
        fired, decision = decide(store, compiled, condition, now_ts, persist=apply_state)
        if not apply_state:
            decision = f"dry-run, stato non aggiornato: {decision}"

        results.append(
            Evaluation(
                rule=rule,
                condition=condition,
                fired=fired,
                matches=matches,
                samples=samples,
                reason=f"{reason}; {decision}",
            )
        )
        log.info(
            "rule '%s': condition=%s notify=%s (%s)",
            rule.name,
            condition,
            fired,
            results[-1].reason,
        )
    return results
