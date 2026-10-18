# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A weather alerting agent for a home NUC. Every 15 minutes it fetches forecast
(Open-Meteo) and observation (ARPAE Emilia-Romagna) data for one point,
archives it in SQLite with a 72h rolling window, evaluates a set of boolean
rules over the data, and sends a Telegram notification when a rule fires.
An LLM (via `redactor.py`; Anthropic by default, or any OpenAI-compatible
endpoint) only writes the notification *prose* — the fire/no-fire decision is
fully deterministic and never touches an LLM.

This is a Python POC intentionally structured to be ported to C++ later (see
"Toward C++" in README.md) — favor explicit, translatable code over clever
Python idioms.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate   # bare `python` isn't on PATH outside the venv
pip install -e ".[dev]"
python -m pytest                              # full suite (fast, no network)
python -m pytest tests/test_expr.py            # single file
python -m pytest tests/test_rules.py -k edge   # single test by keyword

python -m meteoagent -c config.dev.toml check              # validate config + compile rules, no network
python -m meteoagent -c config.dev.toml stations -v        # discover ARPAE stations near the configured point (--radius KM to widen)
python -m meteoagent -c config.dev.toml once --dry-run      # one full cycle, composes messages, sends nothing, doesn't persist rule state
python -m meteoagent -c config.dev.toml status              # what's in the DB
python -m meteoagent -c config.dev.toml rules-test           # re-evaluate rules against archived data, no network
python -m meteoagent -c config.dev.toml telegram-test
python -m meteoagent chat-id <BOT_TOKEN>                    # find TELEGRAM_CHAT_ID via getUpdates
python -m meteoagent -c config.dev.toml run                 # continuous scheduler, aligned to :00/:15/:30/:45
```

There is no linter/formatter configured. `.vscode/launch.json` has debug
configs for the main subcommands.

`config.dev.toml` runs without root/systemd/LLM key: DB in `./data/`,
DEBUG logging, `[llm].enabled = false`, thresholds lowered so rules
actually fire on a normal day (must be retuned before treating as production
config). `config.example.toml` is the production template.

Secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and the LLM key — env var
*named* by `[llm].api_key_env`, default `ANTHROPIC_API_KEY`) live in
a `.env`-style file (`KEY=VALUE`, no `export`, no quotes — read directly by
systemd's `EnvironmentFile=`), passed via `--env <path>` or auto-discovered
next to the config file. Never put secrets in `*.toml`. The env file is
loaded with `os.environ.setdefault`, so variables already set in the real
environment win over the file.

## Architecture

Pipeline, one independent cycle every 15 minutes — a failure in one data
source never blocks the other:

```
Open-Meteo (forecast) ─┐
                        ├─► store (SQLite) ─► purge (72h window) ─► rules (AST eval) ─► message ─► Telegram
ARPAE E-R (observation)┘                                                 │
                                                                          ▼
                                                                 redactor.py (LLM, prose only)
```

- **`sources/openmeteo.py`** — hourly forecast JSON, up to 72h horizon. No
  gust *direction* (`wind_gusts_direction_10m` is observation-only).
- **`sources/arpae.py`** — ARPAE JSONL feed, RMAP data model, WMO table B
  codes. `select_station()` picks the nearest station publishing the required
  canonical vars; pin a station explicitly via `[arpae].station_lon/lat/network`
  once identified with `stations -v` (network is unstable otherwise — silently
  swaps station if the pinned one falls offline). `extract()` applies QC per
  reading: B33196 = explicit invalidation (always dropped), B33007 = confidence
  0–100 checked against `[arpae].min_confidence` (default 1.0 — permissive,
  only drops fully-invalidated readings). QC failure means the row is dropped
  entirely, not zeroed — downstream this becomes a "missing" sample, not a
  false one. The feed is one ~8.6 MB uncompressed file for the whole region,
  grouped by station but rebuilt each time (rolling ~6 h), so byte ranges are
  useless. `download()` streams it line by line (gzip handled incrementally);
  with a pinned station `pinned_line_filter()` keeps only lines containing its
  lon/lat digits *before* JSON parsing. It's a conditional GET
  (`FeedValidators`: ETag/Last-Modified, in memory, updated only after a
  complete download) → `fetch()` returns `None` on 304. `agent.py` downloads
  once per clock-aligned `[arpae].interval_minutes` window (0 = every cycle,
  must be a multiple of `interval_minutes`, 60 s grace for early wake-ups); a
  failed download doesn't consume the window.
- **`vars.py`** — the canonical variable vocabulary (`temperature_2m`,
  `wind_speed_10m`, `wind_gusts_direction_10m`, …) that both sources map into,
  in shared units. This is the contract rules are written against. Derived
  rolling-window variables are declared here too (`DERIVED`) and computed in
  `rules.py`. Adding a derived variable = one tuple in `DERIVED`. Unknown
  identifiers in `when` are *not* rejected at compile time: a misspelled
  variable makes every sample "missing", so the rule silently never fires
  (only a skip warning is logged). `check` prints the variables each rule uses.
- **`http.py`** — shared `requests` session with retry/backoff; anything that
  survives the retries raises `FetchError`, which the cycle logs and moves past.
  Retries cover GET only. Telegram POSTs retry once on `ConnectionError` in
  `telegram.send()` (stale pooled keep-alive after hours idle — this lost ~40%
  of production alerts before the fix), never on `ReadTimeout` (could duplicate).
  The bot token is part of the Telegram URL path, and connection-phase errors
  (urllib3 retry warnings, `MaxRetryError`) quote that path: `telegram.redact()`
  masks it in our own log calls, and `setup_logging()` installs
  `TokenRedactingFilter` on the root handlers so third-party records and
  tracebacks are masked too. Never log a Telegram exception or URL unredacted.
- **`store.py`** — SQLite persistence. Two data tables: `observation` (indexed on
  measurement `ts`, rolling window) and `forecast` (indexed on
  `(fetched_at, valid_ts)` — **every fetched snapshot is kept**, not just the
  latest, so forecast drift over time is queryable; rolling window is on
  `fetched_at` so each retained snapshot stays horizon-complete). Writes are
  idempotent; ARPAE corrections overwrite prior values (last-received-wins,
  per RMAP). Plus `rule_state` (edge/cooldown state) and `notification`
  (log of what was sent). `agent.py` clips the stored forecast horizon to
  `[store].retention_h`.
- **`expr.py`** — the `when` expression language. Parses via Python's `ast`
  module but walks and rejects every node not in an explicit whitelist before
  evaluating (`_check`) — config files cannot execute arbitrary code. Custom
  functions: `dir_in`, `dir_between`, `sector` (16-point compass, 22.5° each,
  centered on the name — `dir_in(d, [...])` for wind sector matching),
  `min/max/abs/round`. This is the module flagged in README as the hardest
  piece to port to C++ (~300 lines of hand-written recursive-descent parser,
  or freeze the grammar into a TOML-compiled struct instead).
- **`rules.py`** — derives rolling-window aggregates (`precipitation_sum_3h`,
  `_6h`, `_24h`, `wind_gusts_max_3h`), applies the rule's quantifier `mode`
  over the window (`any`/`all`/`count`/`consecutive`; the last two use
  `min_hours`, converted to a sample count from the step of the samples
  *actually evaluated* — not the whole series: ARPAE stations publish
  different vars at different cadences, e.g. temperature every 15 min, wind
  every 30), then applies edge/level
  trigger + cooldown semantics (`decide()`) using persisted state from
  `store.py`. A sample with missing variables is *skipped*, never coerced to
  false — a data gap must not read as "all clear". Subtle invariant in
  `decide()`: `rule_state.last_value` means "this edge has already been
  *notified*", not "condition was true last cycle" — an edge suppressed by
  cooldown stays armed and fires when the cooldown expires. Don't "simplify"
  this into a plain previous-value flag. `decide()` never records a fire
  itself: `agent._notify()` calls `rules.mark_notified()` only after Telegram
  confirms delivery, so a failed send is retried next cycle instead of being
  silently consumed. `run(..., apply_state=False)` is the dry-run path: it
  calls `decide(..., persist=False)`, so `fired` still reflects edge/cooldown
  and `agent._notify()` composes and logs the would-be message, but no state
  is written and nothing is sent.
- **`message.py`** — deterministic message composition (title, source, update
  time, table of triggering instants + values). This runs regardless of
  the LLM's availability.
- **`redactor.py`** — the *only* place an LLM is called. `[llm].provider`:
  `anthropic` (SDK) or `openai` (plain `requests.post` to any OpenAI-compatible
  `chat/completions` URL in `[llm].endpoint`, e.g. GitHub Models with a PAT).
  `[claude]` is accepted as a legacy alias of `[llm]` (production configs
  still use it). Disabled by default in both config templates: without it,
  notifications are the deterministic message alone. Receives JSON of the
  already-decided facts, writes 2-3 sentences of prose. On any API error,
  timeout, or missing credit, `write()` returns `None` and the caller sends
  the plain deterministic message instead — notification delivery must never
  depend on the LLM being up. Also degrades cleanly if `[llm].enabled =
  false` or the `anthropic` package isn't installed.
- **`agent.py`** — orchestration: runs a cycle, wires sources → store → rules
  → redactor → telegram, and the `:00/:15/:30/:45`-aligned scheduler for
  `run`.
- **`config.py`** — TOML config + secrets from env, with strict validation
  (e.g. a rule with `source = "observation"` requires `[arpae].enabled`).
- **`__main__.py`** — CLI entry point (`meteo-agent` console script); each
  subcommand maps to a `cmd_*` function.

Test coverage (`tests/`) deliberately excludes anything network-dependent:
the expression evaluator (including rejection of disallowed AST nodes),
quantifiers, edge/cooldown logic, rolling window behavior, RMAP parsing
against a synthetic feed, QC (B33007/B33196), unit conversions. Live-feed
integration is *not* covered by tests and must be validated manually against
the running NUC (see README.md "Validation" for the exact command sequence:
`check` → `stations -v` → `telegram-test` → `once --dry-run` → `status` →
`rules-test`).

## Notes

- Single branch, English code base (since 2026-10-18; a separate `english`
  translation branch existed before and was retired). Comments, docstrings,
  log messages, CLI output, config error messages are in English. Deliberately
  kept in Italian, because they're what the user actually receives on
  Telegram: the notification text (`message.py`), `VarSpec.label` in
  `vars.py`, the LLM `SYSTEM_PROMPT` (`redactor.py`), the rule reason strings
  (`rules.py`, they flow into the message), and the failure-alert text in
  `systemd/meteo-agent-failure.service`. Tests assert on those Italian strings
  where relevant — don't translate them.
- Two READMEs: `README.md` (English, authoritative) and `README.it.md`
  (Italian translation). Any README change must be mirrored in both; when the
  Italian one quotes log lines or CLI output, quote the real (English) strings.
- README.md is the primary source of truth for
  installation, Telegram bot setup, rule-writing syntax/examples, and the
  systemd deployment (`ProtectSystem=strict`, only `/var/lib/meteo-agent` is
  writable — update `ReadWritePaths=` if `[store].path` changes).
- Deployment is a *copy* in `/opt/meteo-agent` with its own venv, separate
  from this repo: code changes don't reach the service until copied there.
  The venv breaks on any Ubuntu upgrade that changes the system Python
  version (happened 2026-10-06: 24.04→26.04, 3.12→3.14) — rebuild it (README
  "Installation", step 2 "Code and virtualenv"). `systemd/meteo-agent-failure.service` (via
  `OnFailure=`) sends a Telegram alert via `curl` when systemd gives up
  restarting the agent; it deliberately doesn't depend on the venv.
- **Placeholder location — pending TODO for the user.** Every coordinate in
  the repo is a deliberate placeholder for Bologna city centre (`44.49373453937952,
  11.342959900671165`; example station pin = ARPAE Torre Asinelli
  `1134677`/`4449420`/`simnbo`): README, `config.example.toml`,
  `config.dev.toml`, the `store.py` comment, `tests/test_arpae.py`. The user's
  home coordinates were removed for privacy on 2026-10-18. **When the user
  starts reworking this project, remind them they planned to add their home
  coordinates**, and ask where they want them: production
  `/etc/meteo-agent/config.toml` or an untracked local config keeps them out of
  the GitHub repo; committing them puts them back in history. Tests only need
  *a* location with the synthetic station ~400 m from it, not the real one.
- Two config files exist for a reason: `config.example.toml` (production
  values, real thresholds) vs `config.dev.toml` (local dev, low thresholds
  meant to be retuned, not deployed as-is).
