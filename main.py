"""
MEIC Trader — internal main module.

Entry point is meic.py.  This module exposes _startup() and _main() for use by
the CLI.  It is not meant to be run directly.
"""
import asyncio
import signal
import sys
import time

from rich.console import Console
from rich.panel import Panel

import client
import config
from client import get_session, get_account, shutdown as shutdown_session
from database import init_db
from scheduler import run_scheduler
from logger import log

console = Console()

_MAX_STARTUP_ATTEMPTS = 3
_STARTUP_RETRY_DELAY  = 5


async def _connect_once() -> tuple:
    """Single attempt to authenticate and fetch account + balances. Returns (session, account, balances)."""
    loop = asyncio.get_event_loop()
    session  = await get_session(force_reauth=True)
    account  = await get_account()
    balances = await loop.run_in_executor(None, lambda: account.get_balances(session))
    return session, account, balances


async def _startup() -> tuple:
    """
    Attempt connection up to _MAX_STARTUP_ATTEMPTS times.
    Prints a rich Panel banner on success or final failure.
    Exits the process on final failure.
    """
    mode_label = "Sandbox" if config.MODE == "sandbox" else "LIVE"
    profile_label = config.ACTIVE_PROFILE
    exp_warn = "  [bold yellow]⚠ EXPERIMENTAL PROFILE[/bold yellow]\n" if config.EXPERIMENTAL else ""
    live_warn = "  [bold yellow]⚠ LIVE MODE — ORDERS WILL BE SUBMITTED[/bold yellow]\n" if config.MODE == "live" else ""

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_STARTUP_ATTEMPTS + 1):
        try:
            session, account, balances = await _connect_once()

            nlv = getattr(balances, "net_liquidating_value",   None)
            bp  = getattr(balances, "derivative_buying_power", None)
            nlv_str = f"${float(nlv):,.2f}" if nlv is not None else "—"
            bp_str  = f"${float(bp):,.2f}"  if bp  is not None else "—"
            expiry  = client._fmt_expiry(session.session_expiration)
            acct_no = account.account_number
            acct_type = getattr(account, "account_type_name", "")

            body = (
                f"{live_warn}{exp_warn}"
                f"  [green]✓  Connected[/green]\n"
                f"  Account   [bold]{acct_no}[/bold]  ({acct_type})\n"
                f"  Balance   NLV {nlv_str}  │  BP {bp_str}\n"
                f"  Token     expires {expiry}\n"
                f"  Profile   {profile_label}  │  Mode: {mode_label}"
            )
            console.print(Panel(body, title=f"MEIC Trader  ·  {mode_label} Connection", border_style="green"))
            log.info(
                "Ready. Account: %s | Token expires: %s | Profile: %s",
                acct_no, expiry, profile_label,
            )
            return session, account, balances

        except Exception as exc:
            last_exc = exc
            log.warning("Startup attempt %d/%d failed: %s", attempt, _MAX_STARTUP_ATTEMPTS, exc)
            if attempt < _MAX_STARTUP_ATTEMPTS:
                console.print(
                    f"[yellow]  Connection attempt {attempt}/{_MAX_STARTUP_ATTEMPTS} failed — "
                    f"retrying in {_STARTUP_RETRY_DELAY}s…[/yellow]"
                )
                await asyncio.sleep(_STARTUP_RETRY_DELAY)

    body = f"  [red]✗  Authentication failed[/red]\n  {last_exc}"
    console.print(Panel(body, title=f"MEIC Trader  ·  {mode_label} Connection", border_style="red"))
    sys.exit(1)


async def _main() -> None:
    init_db()
    await _startup()

    from strategy import run_entry
    await run_scheduler(run_entry)


def main(mode: str = "sandbox", profile: str | None = None) -> None:
    config.init(mode=mode, profile_override=profile)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop():
        log.info("Shutdown signal received.")
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    log.info("MEIC Trader starting up... Mode=%s Profile=%s", mode, profile or config.ACTIVE_PROFILE)

    try:
        loop.run_until_complete(_main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutdown requested.")
    finally:
        loop.run_until_complete(shutdown_session())
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        log.info("MEIC Trader stopped.")
