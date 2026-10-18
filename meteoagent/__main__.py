"""Command-line interface.

  run             continuous cycle (this is what systemd launches)
  once            a single cycle, with --dry-run to skip notifying
  check           validates configuration and compiles the rules, no network
  stations        lists nearby ARPAE stations and what they publish
  telegram-test   checks token/chat_id and sends a test message
  status          what's in the database
  rules-test      re-evaluates the rules against archived data, no notifications
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config as config_module
from . import message, rules, telegram
from .agent import Agent
from .expr import ExprError
from .http import FetchError, make_session
from .sources import arpae
from .store import Store

DEFAULT_CONFIG = "/etc/meteo-agent/config.toml"


def setup_logging(level: str) -> None:
    # No timestamp: under systemd the journal already adds one.
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(levelname)-7s %(name)-24s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Bot token out of the journal, whoever logs it (us or urllib3).
    telegram.install_log_redaction()


def load_config(args) -> config_module.Config:
    if args.env:
        config_module.load_env_file(args.env)
    else:
        # Development convenience: .env next to the config file, if present.
        config_module.load_env_file(Path(args.config).parent / ".env")
    return config_module.load(args.config)


# --------------------------------------------------------------------- commands

def cmd_check(args) -> int:
    cfg = load_config(args)
    setup_logging(cfg.log_level)
    print(f"Configuration OK: {args.config}")
    print(f"  location        {cfg.location.latitude}, {cfg.location.longitude} "
          f"({cfg.location.elevation_m:.0f} m asl, {cfg.location.timezone})")
    print(f"  interval        {cfg.interval_minutes} min")
    print(f"  history         {cfg.store.retention_h} h in {cfg.store.path}")
    print(f"  forecast        {'enabled' if cfg.openmeteo.enabled else 'disabled'} "
          f"(model {cfg.openmeteo.model})")
    print(f"  observations    {'enabled' if cfg.arpae.enabled else 'disabled'} "
          f"(station {arpae.source_id(cfg.arpae) or 'automatic'})")
    print(f"  telegram        {'enabled' if cfg.telegram.enabled else 'disabled'}")
    print(f"  redaction       {'enabled' if cfg.llm.enabled else 'disabled'} "
          f"({cfg.llm.provider}, model {cfg.llm.model_name}, key from ${cfg.llm.key_env})")
    print(f"  rules           {len(cfg.rules)} defined")
    try:
        compiled = rules.compile_rules(cfg.rules)
    except ExprError as exc:
        print(f"\nERROR in rules: {exc}", file=sys.stderr)
        return 1
    for item in compiled:
        rule = item.rule
        print(f"    - {rule.name}: {rule.source}/{rule.horizon_h}h, "
              f"{rule.mode}(min_hours={rule.min_hours}), cooldown {rule.cooldown_h}h")
        print(f"      variables: {', '.join(sorted(item.expr.names))}")
    disabled = [r.name for r in cfg.rules if not r.enabled]
    if disabled:
        print(f"    (disabled: {', '.join(disabled)})")
    return 0


def cmd_stations(args) -> int:
    cfg = load_config(args)
    setup_logging(cfg.log_level)
    session = make_session()
    radius = args.radius or cfg.arpae.max_distance_km
    print(f"Downloading {cfg.arpae.endpoint} ...", file=sys.stderr)
    try:
        raw = arpae.download(session, cfg.arpae)
    except FetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"{len(raw) / 1e6:.1f} MB downloaded.\n", file=sys.stderr)

    stations = arpae.discover_stations(raw, cfg.location, radius)
    if not stations:
        print(f"No station within {radius} km.")
        return 1

    print(f"Stations within {radius} km of {cfg.location.latitude}, {cfg.location.longitude}:\n")
    for station in stations:
        usable = station.canonical_vars
        mark = "*" if {"wind_speed_10m", "wind_direction_10m"} <= usable else " "
        print(f"{mark} {station.distance_km:5.1f} km  {station.name or '(unnamed)'}")
        print(f"           network={station.network} lon={station.lon} lat={station.lat} "
              f"elevation={station.height_m} m")
        print(f"           last reading: {message._fmt_dt(station.last_ts, cfg.location.timezone)}")
        print(f"           usable variables: {', '.join(sorted(usable)) or 'none'}")
        if args.verbose:
            for bcode, level, trange in sorted(station.channels):
                print(f"             {bcode}  level={level}  timerange={trange}")
        print()

    best = arpae.select_station(stations)
    if best:
        print("To pin the best station for wind, in config.toml:\n")
        print("[arpae]")
        print(f"station_lon     = {best.lon}")
        print(f"station_lat     = {best.lat}")
        print(f'station_network = "{best.network}"')
    else:
        print("No station with wind data at 10 m within the given radius: "
              "try widening it with --radius.")
    return 0


def cmd_telegram_test(args) -> int:
    cfg = load_config(args)
    setup_logging(cfg.log_level)
    session = make_session()
    me = telegram.get_me(session, cfg.telegram.token)
    if me is None:
        print("Invalid token or network unreachable.", file=sys.stderr)
        return 1
    print(f"Bot: @{me.get('username')} ({me.get('first_name')})")
    ok = telegram.send(
        session,
        cfg.telegram,
        "✅ <b>meteo-agent</b>\nTest di connettivita' riuscito.",
    )
    print("Message sent." if ok else "Send failed, check the log.")
    return 0 if ok else 1


def cmd_chat_id(args) -> int:
    """Doesn't load the configuration: needed before you even have a chat_id."""
    setup_logging("INFO")
    session = make_session()
    updates = telegram.get_updates(session, args.token)
    if not updates:
        print("No messages received. Write something to the bot on Telegram and try again.")
        return 1
    seen = {}
    for update in updates:
        chat = (update.get("message") or update.get("channel_post") or {}).get("chat")
        if chat:
            seen[chat["id"]] = chat
    for chat_id, chat in seen.items():
        label = chat.get("username") or chat.get("title") or chat.get("first_name") or "?"
        print(f"chat_id = {chat_id}   ({chat.get('type')}: {label})")
    return 0


def cmd_status(args) -> int:
    cfg = load_config(args)
    setup_logging(cfg.log_level)
    with Store(cfg.store.path) as store:
        counts = store.counts()
        print(f"Database: {cfg.store.path}")
        print(f"  observations       {counts['observation']} rows")
        print(f"  forecasts          {counts['forecast']} rows "
              f"in {counts['forecast_snapshots']} snapshots")
        print(f"  notifications      {counts['notification']}")
        for source in store.observation_sources():
            series = store.observation_series(source, 0, 2**31)
            if series:
                print(f"\n  [obs] {source}: {len(series)} instants, "
                      f"from {message._fmt_dt(series[0][0], cfg.location.timezone)} "
                      f"to {message._fmt_dt(series[-1][0], cfg.location.timezone)}")
                print(f"        variables: {', '.join(sorted(series[-1][1]))}")
        for source in store.forecast_sources():
            fetched_at = store.latest_fetch_time(source)
            series = store.latest_forecast_series(source, 0, 2**31)
            print(f"\n  [fcst] {source}: latest snapshot "
                  f"{message._fmt_dt(fetched_at, cfg.location.timezone)}, "
                  f"{len(series)} instants up to "
                  f"{message._fmt_dt(series[-1][0], cfg.location.timezone) if series else '-'}")
        rows = store.conn.execute(
            "SELECT rule, last_value, last_fired FROM rule_state ORDER BY rule"
        ).fetchall()
        if rows:
            print("\n  rule state:")
            for row in rows:
                fired = (
                    message._fmt_dt(row["last_fired"], cfg.location.timezone)
                    if row["last_fired"]
                    else "never"
                )
                print(f"    {row['rule']}: active={bool(row['last_value'])}, "
                      f"last notification {fired}")
    return 0


def cmd_rules_test(args) -> int:
    cfg = load_config(args)
    setup_logging(cfg.log_level)
    with Store(cfg.store.path) as store:
        agent = Agent(cfg, store)
        now = message.now_ts()
        observation_source = agent.observation_source or (
            store.observation_sources()[0] if store.observation_sources() else None
        )
        results = rules.run(
            store,
            agent.compiled,
            now,
            forecast_source=agent.forecast_source,
            observation_source=observation_source,
            apply_state=False,
        )
        for evaluation in results:
            status = "MET" if evaluation.condition else "not met"
            print(f"\n{evaluation.rule.name}: {status}")
            print(f"  {evaluation.reason}")
            print(f"  samples evaluated: {evaluation.samples}")
            for row in message.detail_rows(evaluation, cfg.location.timezone):
                print(f"    {row}")
    return 0


def cmd_once(args) -> int:
    cfg = load_config(args)
    setup_logging(cfg.log_level)
    with Store(cfg.store.path) as store:
        agent = Agent(cfg, store)
        result = agent.cycle(dry_run=args.dry_run)
        return 1 if result.errors and not (result.forecast_rows or result.observation_rows) else 0


def cmd_run(args) -> int:
    cfg = load_config(args)
    setup_logging(cfg.log_level)
    with Store(cfg.store.path) as store:
        return Agent(cfg, store).run_forever()


# --------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meteo-agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="configuration file")
    parser.add_argument("--env", help="file with environment variables (secrets)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="continuous cycle").set_defaults(func=cmd_run)

    p_once = sub.add_parser("once", help="a single cycle")
    p_once.add_argument("--dry-run", action="store_true", help="don't send and don't save state")
    p_once.set_defaults(func=cmd_once)

    sub.add_parser("check", help="validates the configuration").set_defaults(func=cmd_check)

    p_st = sub.add_parser("stations", help="lists nearby ARPAE stations")
    p_st.add_argument("--radius", type=float, help="radius in km")
    p_st.add_argument("-v", "--verbose", action="store_true", help="show all channels")
    p_st.set_defaults(func=cmd_stations)

    sub.add_parser("telegram-test", help="tests sending a message").set_defaults(func=cmd_telegram_test)

    p_cid = sub.add_parser("chat-id", help="discover your chat_id")
    p_cid.add_argument("token", help="bot token")
    p_cid.set_defaults(func=cmd_chat_id)

    sub.add_parser("status", help="database status").set_defaults(func=cmd_status)
    sub.add_parser("rules-test", help="re-evaluates the rules without notifying").set_defaults(
        func=cmd_rules_test
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except config_module.ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except ExprError as exc:
        print(f"Error in a rule: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
