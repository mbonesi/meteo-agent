"""Notification message composition.

The message always has a deterministic part (title, source, table of the
instants that triggered the rule). Claude's prose, if available, is added on
top: if the API doesn't respond, the notification still goes out with just
the data. An alerting agent must not depend on an external service to tell
you the wind is about to pick up.

The notification text itself — labels, field names, the strings below — is
intentionally left in Italian: it's the actual message the user receives on
Telegram, not developer-facing documentation. Only this module's comments
and docstrings are translated.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from .expr import sector
from .rules import Evaluation
from .vars import VARS

MAX_DETAIL_ROWS = 12

# Hardcoded on purpose: strftime("%a") follows the process locale, which under
# systemd is "C" and would produce "Sat" instead of "sab". Depending on an
# installed system locale just to format a date is a dependency we don't need.
WEEKDAY_ABBR_IT = ("lun", "mar", "mer", "gio", "ven", "sab", "dom")


def local(ts: int, tz: str) -> datetime:
    return datetime.fromtimestamp(ts, ZoneInfo(tz))


def _fmt_dt(ts: int, tz: str) -> str:
    dt = local(ts, tz)
    return f"{WEEKDAY_ABBR_IT[dt.weekday()]} {dt:%d/%m %H:%M}"


def _fmt_value(var: str, value: float) -> str:
    if var.endswith("direction_10m"):
        return f"{sector(value)} ({value:.0f}°)"
    spec = VARS.get(var)
    if spec is None:
        return f"{value:.1f}"
    return f"{value:.{spec.decimals}f} {spec.unit}"


def detail_rows(ev: Evaluation, tz: str, limit: int = MAX_DETAIL_ROWS) -> list[str]:
    """Detail rows: only the variables the rule actually looks at."""
    names = sorted(_referenced(ev))
    rows = []
    for match in ev.matches[:limit]:
        parts = [_fmt_value(n, match.values[n]) for n in names if n in match.values]
        rows.append(f"{_fmt_dt(match.ts, tz)} — " + ", ".join(parts))
    return rows


def _referenced(ev: Evaluation) -> set[str]:
    from .expr import Expression

    try:
        return set(Expression(ev.rule.when).names)
    except Exception:  # pragma: no cover - the rule has already been compiled
        return set()


def facts(ev: Evaluation, source: str, fetched_at: int | None, tz: str) -> dict:
    """Structured context passed to Claude. Contains only measured/forecast data."""
    names = sorted(_referenced(ev))
    return {
        "titolo_regola": ev.rule.title or ev.rule.name,
        "tipo_dato": "previsione" if ev.rule.source == "forecast" else "osservazione",
        "sorgente": source,
        "aggiornato_il": _fmt_dt(fetched_at, tz) if fetched_at else None,
        "orizzonte_ore": ev.rule.horizon_h,
        "criterio": ev.reason.split(";")[0].strip(),
        "istanti": [
            {
                "quando": _fmt_dt(m.ts, tz),
                **{
                    n: (
                        f"{sector(m.values[n])} {m.values[n]:.0f}°"
                        if n.endswith("direction_10m")
                        else round(m.values[n], 1)
                    )
                    for n in names
                    if n in m.values
                },
            }
            for m in ev.matches[:MAX_DETAIL_ROWS]
        ],
        "istanti_totali": len(ev.matches),
    }


def build(ev: Evaluation, source: str, fetched_at: int | None, tz: str, prose: str | None) -> str:
    """Final HTML message for Telegram."""
    title = escape(ev.rule.title or ev.rule.name)
    kind = "Previsione" if ev.rule.source == "forecast" else "Osservato"
    header = f"⚠️ <b>{title}</b>"
    subheader = f"<i>{kind} · {escape(source)}"
    if fetched_at:
        subheader += f" · aggiornato {escape(_fmt_dt(fetched_at, tz))}"
    subheader += "</i>"

    lines = [header, subheader, ""]
    if prose:
        lines += [escape(prose.strip()), ""]

    rows = detail_rows(ev, tz)
    if rows:
        lines.append("<b>Istanti interessati</b> (ora locale)")
        lines += [f"• {escape(r)}" for r in rows]
        if len(ev.matches) > len(rows):
            lines.append(f"<i>… e altri {len(ev.matches) - len(rows)}</i>")

    return "\n".join(lines).strip()


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())
