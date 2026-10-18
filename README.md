# meteo-agent

**Versione italiana: [README.it.md](README.it.md).**

Code, comments, logs, and CLI output are in English. Telegram notifications are
in Italian: they're the message the user receives (see `meteoagent/message.py`).

Weather agent for a NUC. Every 15 minutes it downloads forecasts and
observations for a point, archives them in SQLite with a 72-hour rolling
window, evaluates a set of boolean rules, and sends a Telegram notification
when they fire. An LLM can enrich the notification text (optional, disabled
by default), but only the text: the decision is deterministic.

Python POC, meant to be ported to C++ once the flow is validated.

---

## What it actually does

```
                  ┌───────────────┐
   Open-Meteo ───►│               │
   (forecast)     │    fetch      │
                  │               │──► store ──► purge ──► rules ──► notify
   ARPAE E-R  ───►│               │    SQLite    72h       (AST)     Telegram
   (observed)     └───────────────┘                          │
                                                              ▼
                                                        Claude (writer)
```

Each cycle is independent and no source can bring it down: if ARPAE doesn't
respond, the forecast rules run anyway, and vice versa.

### The two sources

| | Open-Meteo | ARPAE Emilia-Romagna |
|---|---|---|
| What | hourly forecast up to 72 h | observed from the regional hydro-meteo network |
| Format | JSON | JSONL, RMAP data model, WMO table B |
| Key | not needed | not needed (CC-BY) |
| Gust direction | **not available** | yes (B11043) |

### What gets archived

Both of the things needed for reasoning:

- **`observation`** — ARPAE observations, indexed on the measurement instant.
  Rolling window on `ts`.
- **`forecast`** — **every** downloaded forecast snapshot, indexed on
  `(fetched_at, valid_ts)`. Not just the latest forecast: keeping the
  snapshots lets you see how the forecast for a given instant changed over
  time. Rolling window on `fetched_at`, so every retained snapshot is
  complete with its own horizon.

At steady state that's ~20 MB of database (about 290 snapshots of ~600
values each). Writes are idempotent: re-running
a cycle duplicates nothing, and ARPAE corrections overwrite the old data, as
their model expects (the last data received wins).

---

## A limitation you should know before writing rules

**Open-Meteo does not forecast gust direction**, only intensity
(`wind_gusts_10m`). The true gust direction only exists in the ARPAE
observation (code B11043 → `wind_gusts_direction_10m`).

So a rule like *"gusts from E/SE above 45 km/h"* is written two ways:

```toml
# on FORECAST: the direction is the mean wind's, used as a proxy
when = 'dir_in(wind_direction_10m, ["NE","ENE","E","ESE","SE"]) and wind_gusts_10m > 45'

# on OBSERVATION: true gust direction
when = 'dir_in(wind_gusts_direction_10m, ["NE","ENE","E","ESE","SE"]) and wind_gusts_10m > 45'
```

`config.example.toml` contains both.

---

## Installation on Ubuntu 24.04

Tested on Python 3.12.3, the same version shipped with 24.04.

### 1. Dependencies and service user

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip git

# System user with no login and no home: the agent doesn't need one
sudo useradd --system --no-create-home --shell /usr/sbin/nologin meteo
```

### 2. Code and virtualenv

```bash
sudo mkdir -p /opt/meteo-agent/meteoagent
sudo cp -r meteoagent/* /opt/meteo-agent/meteoagent
sudo cp pyproject.toml /opt/meteo-agent/

cd /opt/meteo-agent

sudo python3 -m venv venv
sudo ./venv/bin/pip install -e .

# Data and configuration directories
sudo mkdir -p /var/lib/meteo-agent /etc/meteo-agent
sudo chown meteo:meteo /var/lib/meteo-agent
```

> **After an Ubuntu upgrade that changes the Python version** (e.g. 24.04 →
> 26.04, Python 3.12 → 3.14) the venv breaks: `python3` points to the new
> interpreter, which can't see the packages installed for the old one. The
> service dies with `ModuleNotFoundError: No module named 'meteoagent'`. The
> venv has to be recreated:
>
> ```bash
> sudo rm -rf /opt/meteo-agent/venv
> sudo python3 -m venv /opt/meteo-agent/venv
> sudo /opt/meteo-agent/venv/bin/pip install -e /opt/meteo-agent
> sudo systemctl reset-failed meteo-agent && sudo systemctl restart meteo-agent
> ```

### 3. Configuration

```bash
sudo cp config.example.toml /etc/meteo-agent/config.toml
sudo cp meteo-agent.env.example /etc/meteo-agent/meteo-agent.env

# The secrets file is readable only by systemd and the meteo group
sudo chown root:meteo /etc/meteo-agent/meteo-agent.env
sudo chmod 640 /etc/meteo-agent/meteo-agent.env
```

The coordinates in `config.example.toml` (`44.49373453937952, 11.342959900671165`,
Bologna city centre) are a **placeholder**: replace them in
`/etc/meteo-agent/config.toml` with those of the point you want to monitor,
elevation (`elevation_m`) included.

---

## Creating the Telegram bot

Needed only once, from your phone or Telegram Desktop.

### 1. Create the bot

1. Open Telegram and search for **@BotFather** (the one with the blue check).
2. `/start`, then `/newbot`.
3. It asks for a **name** (the one you see, e.g. `Home Weather`).
4. Then a **username**, which must end in `bot` and be available
   (e.g. `home_weather_1234_bot`).
5. BotFather replies with the **token**, a string like
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxx`.

The token is a credential: whoever has it can post as your bot.
If you lose it or expose it, `/revoke` on BotFather gives you a new one.

Optional, to show a decent description: `/setdescription`, `/setuserpic`.

### 2. Finding the chat_id

A bot **cannot start a conversation**: you have to message it first,
otherwise it has no permission to send you anything.

1. Search for your bot by username, open it, press **Start** (`/start`).
2. Send any message, even just `hi`.
3. On the NUC:

```bash
/opt/meteo-agent/venv/bin/meteo-agent chat-id 123456789:AAExxxxxxxxxxxx
```

Output:

```
chat_id = 123456789   (private: mario)
```

If it says *"No messages received"*, you either haven't messaged the bot
yet, or you messaged the wrong bot.

### 3. Writing the secrets

```bash
sudo nano /etc/meteo-agent/meteo-agent.env
```

```ini
TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789
```

No `export`, no quotes: systemd would take them literally. The LLM key is
only needed if you enable it (step 4).

### 4. LLM writer (optional)

**It's disabled by default** (`[llm].enabled = false`): notifications go out
with title, source, update time, and the table of instants, without prose,
and no key is needed. Alerts fire in exactly the same cases: the LLM only
summarizes the table in two sentences. Example notification (the message
text is deliberately in Italian — it's what you receive on Telegram, not
documentation):

```
⚠️ Vento da E/SE in arrivo
Previsione · open-meteo:best_match · aggiornato sab 17/10 11:30

Istanti interessati (ora locale)
• sab 17/10 13:00 — E (95°), 22.0 km/h
• sab 17/10 14:00 — E (100°), 26.0 km/h
• sab 17/10 15:00 — ESE (110°), 28.0 km/h
• sab 17/10 16:00 — ESE (120°), 21.0 km/h
```

To enable it, set `enabled = true` in the `[llm]` section (the old name
`[claude]` is still accepted) and put the key in the secrets file: the TOML
only holds the **name** of the environment variable that contains it, via
`api_key_env`.

**Anthropic (default provider).** Key from
<https://console.anthropic.com/settings/keys>, in `ANTHROPIC_API_KEY`. You
need a bit of credit on the workspace. The redaction uses `claude-haiku-4-5`,
a few hundred tokens per notification: the cost is negligible and in any
case capped by `cooldown_h`.

**GitHub Models, with a Personal Access Token.** Create a fine-grained PAT
with the *Models: read* permission (<https://github.com/settings/tokens>),
put it in the secrets file as `GITHUB_TOKEN=github_pat_...`, and in
`config.toml`:

```toml
[llm]
enabled     = true
provider    = "openai"     # any OpenAI chat/completions-compatible endpoint
endpoint    = "https://models.github.ai/inference/chat/completions"
model       = "openai/gpt-4.1-mini"
api_key_env = "GITHUB_TOKEN"
max_tokens  = 400
```

The same `provider = "openai"` works with OpenAI or a local proxy: only
`endpoint`, `model`, and `api_key_env` change. `check` prints the provider,
model, and key variable actually in use.

---

## Validation (in order)

Run on the NUC. The first commands send nothing to anyone.

### 1. The configuration holds up (with the environment sourced)

```bash
cd /opt/meteo-agent
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml --env /etc/meteo-agent/meteo-agent.env check
```

Compiles every rule expression and tells you which variables they use.
Doesn't touch the network. If a rule has a syntax error you find out here,
not in production three weeks from now.

> `check` reads secrets from the environment. If you run it by hand:
> `sudo -u meteo env $(sudo cat /etc/meteo-agent/meteo-agent.env | xargs) ./venv/bin/meteo-agent ... check`
> or pass `--env /etc/meteo-agent/meteo-agent.env`.

### 2. Which ARPAE station we're reading

```bash
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml --env /etc/meteo-agent/meteo-agent.env stations -v > arpae-stations.txt
```

Downloads the feed, looks for stations within 15 km, and prints **what they
actually publish**: table B code, level, and timerange for each channel.
Stations usable for wind are marked with `*` before the distance.
[TIP: to search it you can use `grep -B 5 -A 2 wind_ arpae-stations.txt`]

This is the single most important command in the list. The ARPAE network is
not uniform: not every station has an anemometer, and the ones that do don't
all use the same timerange. Look at the output, pick a station, and **pin
it** in `/etc/meteo-agent/config.toml`:

```toml
[arpae]
station_lon     = 1134677
station_lat     = 4449420
station_network = "simnbo"
```

The command already prints the block ready to paste in. Without a pin the
agent picks the nearest one on its own and logs which — handy on the first
run, but long-term, if that station goes offline you'd start reading a
different one without noticing.

Pinning the station also matters for traffic: the ARPAE feed is a single
~8.6 MB file with every station in the region, and the server doesn't
compress it. With a pinned station the agent streams it and keeps only your
station's lines (a few KB in memory instead of the whole file); without a pin
it has to read all of it to choose. On top of that it only asks for the file
if it has changed (`ETag`): if not, the server answers `304` and sends nothing.

The download is still the bulk of the traffic, ~825 MB a day when done every
cycle. To reduce it:

```toml
[arpae]
interval_minutes = 30   # 0 = every cycle (default); a multiple of interval_minutes
```

The feed keeps about 6 hours of history, so downloading it every 30 or 60
minutes loses no data: observation rules see the same instants, just up to 30
or 60 minutes later. Forecast rules still run every cycle. A failed download
doesn't consume the window: it's retried on the next cycle.

If `--radius 15` finds nothing with wind data, widen it: `--radius 30`.

### 3. Telegram

```bash
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml --env /etc/meteo-agent/meteo-agent.env telegram-test
```

You get a test message. If it fails, the log says why (`chat not found` =
wrong chat_id, `Unauthorized` = wrong token).

### 4. A full cycle, without sending anything

```bash
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml --env /etc/meteo-agent/meteo-agent.env once --dry-run
```

Does everything — downloads, archives, evaluates the rules, composes the
messages — but doesn't send and doesn't save rule state. The messages it
*would have* sent end up in the log, prefixed by `[dry-run] notification NOT
sent`. This is the command to tune thresholds with.

Edge and cooldown apply here too: a rule already notified, or in cooldown,
composes nothing, exactly as it wouldn't go out in the real service. If the
LLM writer is enabled, the dry-run really calls it, to show you the full
text.

### 5. What's in the archive

```bash
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml status
```

Rows, snapshots, time range covered, variables actually present per source,
state of each rule.

If after the first cycle `wind_gusts_direction_10m` doesn't show up among the
observed variables, your station doesn't publish B11043 and the
`east_gusts_ongoing` rule will never fire: change station or remove the rule
from config.toml.
(section `--- East/SE gusts ongoing (true direction, B11043) ---`)

### 6. Retuning rules against already-downloaded data

```bash
sudo -u meteo ./venv/bin/meteo-agent -c /etc/meteo-agent/config.toml rules-test
```

Re-evaluates everything against the archive, no network and no
notifications. Change a threshold, rerun, see what would have fired. A
one-second tuning loop.

---

## Running as a service

```bash
sudo cp systemd/meteo-agent.service systemd/meteo-agent-failure.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meteo-agent

systemctl status meteo-agent
journalctl -u meteo-agent -f
```

Cycles are clock-aligned: they fall on :00, :15, :30, :45. On restart the
first cycle runs immediately, without waiting for the next quarter hour.

If the agent goes into a crash loop (5 restarts in 10 minutes) systemd stops
restarting it and launches `meteo-agent-failure.service`, which sends you a
Telegram message via `curl`, independent of the agent's venv. Without it, a
stopped agent is indistinguishable from a period with no alerts. To try it:
`sudo systemctl start meteo-agent-failure.service`. It doesn't cover the NUC
being off or offline.

If the Telegram send of an alert fails, the rule stays "to be notified" and
is retried on the next cycle: an alert isn't lost to a network error.

The unit runs under `ProtectSystem=strict`: the only writable directory is
`/var/lib/meteo-agent`. If you change `[store].path` you must update
`ReadWritePaths=`.

To check the hardening: `systemd-analyze security meteo-agent`.

---

## Updating the service (deploy)

The service runs from the **copy** in `/opt/meteo-agent`, not from the
workspace: a code change doesn't reach the agent until you copy it there. Run
on the NUC, from the repository root, in this order.

### 1. Check in the workspace

```bash
source .venv/bin/activate
python -m pytest                     # all must pass, none touch the network
git status                           # know what you're about to put in production
```

### 2. Copy code and units

```bash
# --delete: a file removed from the repository disappears from /opt too
sudo rsync -a --delete --exclude __pycache__ meteoagent/ /opt/meteo-agent/meteoagent/
sudo cp pyproject.toml /opt/meteo-agent/
sudo /opt/meteo-agent/venv/bin/pip install -q -e /opt/meteo-agent

sudo cp systemd/meteo-agent.service systemd/meteo-agent-failure.service /etc/systemd/system/
sudo systemctl daemon-reload
```

The `pip install` is only needed if the dependencies in `pyproject.toml`
changed, but it's idempotent and takes a few seconds: might as well always
run it. If it fails with Python version errors, the venv has to be
recreated: see the note in [Code and virtualenv](#2-code-and-virtualenv).

### 3. Check the configuration with the new code

Before restarting, so you see an error here and not in a crash loop:

```bash
sudo -u meteo /opt/meteo-agent/venv/bin/meteo-agent -c /etc/meteo-agent/config.toml \
    --env /etc/meteo-agent/meteo-agent.env check
```

New options have compatible defaults: a `config.toml` that worked keeps
working (e.g. the `[claude]` section stays valid as the old name of `[llm]`).
To see what the new version offers:
`diff /etc/meteo-agent/config.toml config.example.toml`.

### 4. Restart and verify

```bash
sudo systemctl reset-failed meteo-agent    # resets the counter if it was in a crash loop
sudo systemctl restart meteo-agent
journalctl -u meteo-agent -n 30 --no-pager
```

In the log you should see `agent ready: N rules ...` and, within a few
seconds, the first `cycle completed`. If you change something in Telegram or
the writer, a test without waiting for a real alert:
`sudo -u meteo /opt/meteo-agent/venv/bin/meteo-agent -c /etc/meteo-agent/config.toml --env /etc/meteo-agent/meteo-agent.env telegram-test`.

To try the stopped-agent alert (it really sends the "has stopped" message):
`sudo systemctl start meteo-agent-failure.service`.

### Rolling back

```bash
git log --oneline                    # find the commit that worked
git checkout <commit>
# ...and repeat steps 2-4
git checkout master                  # then go back to the branch
```

As long as `SCHEMA_VERSION` in `store.py` is the same between the two
versions, the rollback doesn't touch the data. If a commit changes it, check
what it does first.

---

## Writing rules

```toml
[[rules]]
name       = "east_wind_forecast"
title      = "Vento da E/SE in arrivo"
source     = "forecast"        # forecast (looks ahead) | observation (looks back)
horizon_h  = 48
when       = 'dir_in(wind_direction_10m, ["NE","ENE","E","ESE","SE"]) and wind_speed_10m > 20'
mode       = "consecutive"
min_hours  = 2
cooldown_h = 12
trigger    = "edge"
```

### `when`

Boolean expression evaluated at **every instant** of the window. No `eval`:
it's compiled into an AST and only whitelisted nodes are accepted, so a
configuration file cannot execute code. Errors surface at `check`.

Variables (canonical units, identical for forecast and observation):

| variable | unit | forecast | observation |
|---|---|:---:|:---:|
| `temperature_2m` | °C | ✓ | ✓ |
| `relative_humidity_2m` | % | ✓ | ✓ |
| `precipitation` | mm | ✓ | ✓ |
| `pressure_msl` | hPa | ✓ | ✓ |
| `wind_speed_10m` | km/h | ✓ | ✓ |
| `wind_direction_10m` | degrees | ✓ | ✓ |
| `wind_gusts_10m` | km/h | ✓ | ✓ |
| `wind_gusts_direction_10m` | degrees | — | ✓ |

Derived, rolling-window, computed automatically:
`precipitation_sum_3h`, `_6h`, `_24h`, `wind_gusts_max_3h`.

Functions: `dir_in(degrees, ["NE","E"])`, `dir_between(degrees, 30, 150)`,
`sector(degrees)`, `min`, `max`, `abs`, `round`.

Operators: `and or not`, `< <= > >= == != in`, `+ - * / %`, chained
comparisons (`0 < temperature_2m < 5`).

Sectors are 16-point, 22.5° wide, and **centered** on the name: `E` covers
78.75°–101.25°. `dir_in(d, ["NE","ENE","E","ESE","SE"])` covers
33.75°–146.25°. The Italian notation `O` for West is also accepted.

### `mode` — the quantifier

What has to happen in the window for the condition to count:

- `any` — at least one instant
- `all` — every instant
- `count` — at least `min_hours` hours of instants, even scattered
- `consecutive` — at least `min_hours` hours **in a row**

`min_hours` is in hours and gets converted into a sample count on the
series' actual step: 2 hours are 2 samples on an hourly forecast and 8 on a
15-minute observation. This exists so a single jittery hour of the model
doesn't trigger an alert.

An instant with missing variables is **skipped**, not treated as false: a
data gap must not disguise itself as "all clear".

### `trigger` and `cooldown_h` — the anti-spam

Without these, a condition true for 12 hours would generate 48
notifications.

- `trigger = "edge"` — notifies only on the false → true transition. One
  event, one notification.
- `trigger = "level"` — notifies on every cycle where it's true, once the
  cooldown has expired.
- `cooldown_h` — minimum silence between two notifications of the same rule.

State is persisted in SQLite: restarting the agent doesn't resend
notifications that already went out. An edge suppressed by the cooldown
isn't lost: it goes out as soon as the cooldown expires.

---

## The LLM's role (optional, disabled by default)

Deliberately narrow: it receives in JSON the data that has **already**
triggered the rule and writes two sentences. It doesn't decide whether to
notify, doesn't interpret thresholds, doesn't see anything beyond the values
of the variables the rule references.

The reason is that a boolean condition must give the same outcome on the
same data, today and in six months. An LLM at that point would make the
system unverifiable.

If the API is slow, errors out, or has no credit, the notification **still
goes out**, with just the deterministic part. An alerting agent must not
depend on an external service to tell you the wind is about to pick up.

The message always has: title, source, update time, and a table of the
instants that triggered the rule with the values of the variables it looked
at.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q     # 176 tests, ~0.5 s
```

Structure:

```
meteoagent/
├── config.py        TOML + secrets from env, strict validation
├── expr.py          whitelisted AST evaluator
├── vars.py          canonical variable vocabulary
├── store.py         SQLite, rolling window, rule state
├── http.py          session with retry/backoff
├── sources/
│   ├── openmeteo.py forecasts
│   └── arpae.py     observations: RMAP parsing, QC, station selection
├── rules.py         derived variables, quantifiers, edge, cooldown
├── message.py       deterministic composition
├── redactor.py      LLM (Anthropic or OpenAI-compatible), with controlled degradation
├── telegram.py      sendMessage / getUpdates
├── agent.py         orchestration and scheduler
└── __main__.py      CLI

config.example.toml     production configuration (db in /var/lib, real thresholds)
config.dev.toml         development configuration (db in the workspace, low thresholds)
meteo-agent.env.example template for secrets
systemd/                service unit, with hardening, and Telegram alert if it stops
.vscode/                interpreter, tests, and debug configurations
tests/                  176 tests, none touch the network
```

Tests cover the pieces that don't depend on the network: the evaluator
(including rejection of dangerous constructs), quantifiers, edge/cooldown,
rolling window, RMAP parsing against a synthetic feed, quality control
(B33007/B33196), unit conversions. **Integration with the live feeds has to
be validated on the NUC** with the sequence above.

### Trying it from the workspace, with VSCode Remote on the NUC

`config.dev.toml` is built for this: it writes the database to `./data/`,
raises logging to DEBUG, and keeps `[llm].enabled = false`, so the agent
runs **without root, without systemd, and without an LLM key**.
Thresholds are lowered relative to `config.example.toml` so something
triggers even on a quiet day: **they need retuning** before moving to
production.

From VSCode's integrated terminal, connected to the NUC:

```bash
cd meteo-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp meteo-agent.env.example .env      # picked up automatically: it sits next to the config
$EDITOR .env                         # at least TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

python -m pytest                     # 176 tests, all must pass
python -m meteoagent -c config.dev.toml check
python -m meteoagent -c config.dev.toml stations -v      # <-- the first real network test
python -m meteoagent -c config.dev.toml once --dry-run
```

Then <kbd>Ctrl+Shift+P</kbd> → *Python: Select Interpreter* → `./.venv/bin/python`.
From there the **Testing** panel finds the 176 tests and **Run and Debug**
(<kbd>F5</kbd>) already has the six commands ready with breakpoints: `check`,
`stations -v`, `once --dry-run`, `rules-test`, `status`, `run`.

The place to put your first breakpoint, if something doesn't add up, is
`arpae.extract()`: that's where table B codes become canonical variables,
and it's the one piece I couldn't test against the real feed.

`.gitignore` already excludes `.env` and `data/`. `.env` must never end up in
git: in production the same file goes to
`/etc/meteo-agent/meteo-agent.env` at `0640 root:meteo`, read only by
systemd.

---

## References

- Open-Meteo — <https://open-meteo.com/en/docs>
- ARPAE, observed data — <https://dati.arpae.it/dataset/dati-dalle-stazioni-meteo-locali-della-rete-idrometeorologica-regionale>
- RMAP RFC (data model, JSON format, QC) — <https://doc.rmap.cc/rmap_rfc/rfc.html>
- Table B — <https://github.com/ARPA-SIMC/dballe/blob/master/tables/dballe.txt>
- Levels and timeranges — <https://arpa-simc.github.io/dballe/general_ref/ltypes.html>, <https://arpa-simc.github.io/dballe/general_ref/tranges.html>
- Telegram Bot API — <https://core.telegram.org/bots/api>

ARPAE data: CC-BY license.

---

## Toward C++

The POC is structured to be ported without surprises. The map:

| module | C++ equivalent |
|---|---|
| `http.py` | libcurl (`CURLOPT_*`, manual retry) |
| `sources/*` | nlohmann/json |
| `store.py` | SQLite3 C API, prepared statements |
| `expr.py` | the real piece of work: a recursive-descent parser over your own AST |
| `rules.py` | direct translation |
| `redactor.py` | JSON POST via libcurl |
| logging | spdlog |
| `config.py` | toml++ |

The delicate part is `expr.py`: in Python the `ast` module hands you the
parser for free, in C++ it has to be written. About 300 lines for the
supported grammar. Alternatively, freeze the grammar into a struct compiled
from the TOML, losing some expressiveness but avoiding the parser.

The rest is mechanical. The database schema doesn't change: the DB written
by the POC stays readable by the C++ version, so you can run both in
parallel and compare.

---

## Uninstallation and System Cleanup

The reverse of [Installation on Ubuntu 24.04](#installation-on-ubuntu-2404):
removes the service, code, data, configuration, and system user. Run on the
NUC, in this order.

### 0. Backup (optional)

Step 3 deletes history and configuration for good. If you might reinstall,
or want to keep the data:

```bash
mkdir -p ~/meteo-agent-backup
sudo cp /var/lib/meteo-agent/meteo.db /etc/meteo-agent/config.toml ~/meteo-agent-backup/
sudo chown -R "$USER": ~/meteo-agent-backup
```

The secrets file (`/etc/meteo-agent/meteo-agent.env`) is left out on
purpose: copy it only if you're reinstalling, and don't leave it lying
around in your home.

### 1. Stop and disable the service

```bash
sudo systemctl stop meteo-agent
sudo systemctl disable meteo-agent
sudo rm -f /etc/systemd/system/meteo-agent.service /etc/systemd/system/meteo-agent-failure.service
sudo systemctl daemon-reload
sudo systemctl reset-failed
```

Verify nothing is running anymore:

```bash
systemctl status meteo-agent   # should say "could not be found" or "inactive"
```

### 2. Remove code and virtualenv

```bash
sudo rm -rf /opt/meteo-agent
```

### 3. Remove data and configuration

The SQLite database (observations, forecast snapshots, rule state) and the
secrets (Telegram, LLM key) live outside `/opt`, so they need removing
separately. **This step deletes all history**: if you need a backup, see
[step 0](#0-backup-optional).

```bash
sudo rm -rf /var/lib/meteo-agent
sudo rm -rf /etc/meteo-agent
```

### 4. Remove the service user

```bash
sudo userdel meteo
```

Being a `--system --no-create-home` user, it leaves no home to clean up. If
you created it differently in the past and it still owns files elsewhere:

```bash
sudo find / -xdev -user meteo 2>/dev/null
```

### 5. System dependencies (optional)

`python3-venv`, `python3-pip`, and `git` are generic packages, almost
certainly used by other things on the NUC: **normally leave them installed**.
If you're sure they were only needed for this agent:

```bash
sudo apt remove -y python3-venv python3-pip
sudo apt autoremove -y
```

### 6. Telegram bot (optional)

The agent leaves no trace on the Telegram side, but if the bot is no longer
needed:

1. Open **@BotFather**, `/mybots`, select the bot.
2. **Delete Bot** to remove it permanently, or `/revoke` to invalidate just
   the current token while keeping the bot.

### 7. LLM writer key (optional)

If the key was dedicated to this agent, revoke it so it doesn't stay valid
after uninstallation:

- `ANTHROPIC_API_KEY`: at <https://console.anthropic.com/settings/keys>;
- GitHub PAT (if you used `provider = "openai"` with GitHub Models): at
  <https://github.com/settings/tokens>.

### 8. Development workspace (optional)

The steps above don't touch the repository copy you installed from. Inside
it are the development venv (`.venv/`), the development database if any
(`data/`, created by `config.dev.toml`), and the development secrets file
(`.env`), all outside git:

```bash
cd meteo-agent
rm -rf .venv data .env
```

To delete the repository too, first check with `git status` and
`git log origin/master..` that you have no changes or commits not yet
pushed: deleting the folder would lose them.

### Final check

```bash
systemctl status meteo-agent    # unit does not exist
id meteo                        # user does not exist
ls /opt/meteo-agent \
   /etc/meteo-agent \
   /var/lib/meteo-agent 2>&1    # "No such file or directory" on all three
```

If all checks fail as expected (i.e. find nothing), uninstallation is
complete and the system is back to its pre-installation state.

## License and Copyright

Copyright (c) 2026 Maurizio Bonesi

This project is open source and is distributed under the [MIT](LICENSE) license.

You are free to use, modify, and distribute this software for personal,
academic, or commercial purposes, provided that the original copyright
notice and attribution to the author are retained in all copies or
substantial portions of the code.
