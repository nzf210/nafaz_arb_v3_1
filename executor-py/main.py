"""Production entrypoint for executor-py: ccxt ENV initialization, Redis consumer loop, watchdog, and Postgres persistence."""

import argparse
import logging
import os
import sys
import time
from typing import Any, Dict, Optional

import redis

from consumer import SignalConsumer
from db import PostgresStore
from execution import DeltaNeutralExecutor
from watchdog import RiskWatchdog, TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("true", "1", "yes", "on")


def _env_bool_optional(name: str) -> Optional[bool]:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return None
    return val.strip().lower() in ("true", "1", "yes", "on")


def _env_float(name: str, default: Optional[float] = None) -> Optional[float]:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return float(val)


def build_ccxt_exchanges(load_markets: bool = True) -> Dict[str, Any]:
    """Initialize ccxt exchange clients from ENV credentials (`OKX_*`, `BYBIT_*`, `MEXC_*`)."""
    import ccxt

    exchanges: Dict[str, Any] = {}
    specs = {
        "okx": {
            "cls": ccxt.okx,
            "apiKey": os.getenv("OKX_API_KEY", ""),
            "secret": os.getenv("OKX_API_SECRET", ""),
            "password": os.getenv("OKX_PASSPHRASE", ""),
        },
        "bybit": {
            "cls": ccxt.bybit,
            "apiKey": os.getenv("BYBIT_API_KEY", ""),
            "secret": os.getenv("BYBIT_API_SECRET", ""),
        },
        "mexc": {
            "cls": ccxt.mexc,
            "apiKey": os.getenv("MEXC_API_KEY", ""),
            "secret": os.getenv("MEXC_API_SECRET", ""),
        },
    }
    sandbox = _env_bool("CEX_SANDBOX", False)
    for name, cfg in specs.items():
        cls = cfg.pop("cls")
        params = {k: v for k, v in cfg.items() if v}
        params["enableRateLimit"] = True
        client = cls(params)
        if sandbox and hasattr(client, "set_sandbox_mode"):
            client.set_sandbox_mode(True)
        if load_markets and hasattr(client, "load_markets"):
            try:
                client.load_markets()
            except Exception as exc:
                logger.warning("Failed to preload markets for %s: %s", name, exc)
        exchanges[name] = client
    return exchanges


def run_preflight(redis_client: Any, db_store: Optional[PostgresStore], dry_run: bool) -> bool:
    """Verify Redis, Postgres, and CEX credentials before going live."""
    ok = True
    try:
        redis_client.ping()
        logger.info("[PREFLIGHT] Redis ping: OK")
    except Exception:
        logger.exception("[PREFLIGHT] Redis ping FAILED")
        ok = False

    if db_store is not None:
        try:
            db_store.ensure_schema()
            settings = db_store.load_profile_settings()
            logger.info("[PREFLIGHT] PostgreSQL schema & settings: OK (%d profiles)", len(settings))
        except Exception:
            logger.exception("[PREFLIGHT] PostgreSQL FAILED")
            ok = False

    if not dry_run:
        has_any_key = any(
            os.getenv(k) for k in ("OKX_API_KEY", "BYBIT_API_KEY", "MEXC_API_KEY")
        )
        if not has_any_key:
            logger.error("[PREFLIGHT] Live mode (DRY_RUN=false) requires at least one CEX API key set")
            ok = False
    return ok


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="nafaz_arb v3.1 Executor & Watchdog")
    parser.add_argument("--preflight", action="store_true", help="Run connectivity & readiness checks then exit")
    args = parser.parse_args(argv)

    redis_url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    db_url = os.getenv("DATABASE_URL", "")
    dry_run = _env_bool("DRY_RUN", True)
    canary_cap = _env_float("CANARY_MAX_NOTIONAL_USDT", 20.0)
    watchdog_interval_sec = float(os.getenv("WATCHDOG_INTERVAL_SEC", "15"))

    redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
    db_store = PostgresStore(dsn=db_url) if db_url else None

    if args.preflight:
        sys.exit(0 if run_preflight(redis_client, db_store, dry_run) else 1)

    if db_store is not None:
        db_store.ensure_schema()
        db_store.sync_env_overrides(
            dry_run=_env_bool_optional("DRY_RUN"),
            canary_max_notional_usdt=_env_float("CANARY_MAX_NOTIONAL_USDT"),
        )

    exchanges = {} if dry_run else build_ccxt_exchanges(load_markets=True)
    notifier = TelegramNotifier(dry_run=dry_run)
    executor = DeltaNeutralExecutor(exchanges=exchanges, notifier=notifier.send)
    consumer = SignalConsumer(
        redis_client=redis_client,
        executor=executor,
        default_dry_run=dry_run,
        canary_max_notional_usdt=canary_cap,
        db_store=db_store,
    )
    watchdog = RiskWatchdog(
        redis_client=redis_client,
        exchanges=exchanges,
        notifier=notifier,
        unwind_callback=lambda pos: executor.unwind_route(pos, dry_run=dry_run),
        db_store=db_store,
    )

    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe("core:signals")
    logger.info(
        "Executor & Watchdog started (dry_run=%s, canary_cap=%s, db_enabled=%s)",
        dry_run,
        canary_cap,
        bool(db_store),
    )

    last_watchdog_ts = 0.0
    while True:
        msg = pubsub.get_message(timeout=1.0)
        if msg and msg.get("data"):
            res = consumer.process_signal(msg["data"])
            logger.info("Signal processed: %s", res)

        now = time.time()
        if now - last_watchdog_ts >= watchdog_interval_sec:
            watchdog.reconcile_positions()
            watchdog.check_margin_ratios()
            watchdog.check_adl_indicators()
            watchdog.check_exit_conditions()
            last_watchdog_ts = now


if __name__ == "__main__":
    main()
