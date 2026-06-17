"""
meic.py — single CLI entry point for the MEIC Trader.

Commands:
  run      Start the trading bot and scheduler
  auth     Test credentials and print the connection banner
  report   Display P&L/win-rate tables for various timeframes
  secrets  Manage credentials in Windows Credential Manager
  config   Manage strategy profiles
"""
import argparse
import asyncio
import getpass
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich import box

console = Console()
ET = pytz.timezone("America/New_York")


# ═══════════════════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _today_et() -> date:
    return datetime.now(ET).date()


def _week_range(ref: date) -> tuple[date, date]:
    monday = ref - timedelta(days=ref.weekday())
    return monday, monday + timedelta(days=6)


def _month_range(ref: date) -> tuple[date, date]:
    first = ref.replace(day=1)
    if ref.month == 12:
        last = date(ref.year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(ref.year, ref.month + 1, 1) - timedelta(days=1)
    return first, last


def _year_range(ref: date) -> tuple[date, date]:
    return date(ref.year, 1, 1), date(ref.year, 12, 31)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_\-]", "", name.lower().strip())


# ═══════════════════════════════════════════════════════════════════════════════
# run
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_run(args: argparse.Namespace) -> None:
    from main import main as _main
    _main(mode=args.mode, profile=getattr(args, "profile", None))


# ═══════════════════════════════════════════════════════════════════════════════
# auth
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_auth(args: argparse.Namespace) -> None:
    import config
    config.init(mode=args.mode, profile_override=getattr(args, "profile", None))

    async def _run():
        from main import _startup
        await _startup()

    asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# report
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_report(args: argparse.Namespace) -> None:
    import config as cfg
    # Credentials needed only for today's live API reconstruction
    try:
        cfg.init(mode=args.mode, profile_override=getattr(args, "profile", None))
    except RuntimeError as exc:
        # Missing credentials only matters if we're querying today
        pass

    subcommand = args.report_cmd
    today = _today_et()

    if subcommand == "daily":
        ref = date.fromisoformat(args.date) if args.date else today
        start = end = ref
        label = "Today's Trades" if ref == today else f"Trades: {ref}"
    elif subcommand == "weekly":
        ref = date.fromisoformat(args.date) if args.date else today
        start, end = _week_range(ref)
        label = f"Weekly Summary: {start} – {end}"
    elif subcommand == "monthly":
        ref = date.fromisoformat(args.date) if args.date else today
        start, end = _month_range(ref)
        label = f"Monthly Summary: {start.strftime('%b %Y')}"
    elif subcommand == "yearly":
        ref = date.fromisoformat(args.date) if args.date else today
        start, end = _year_range(ref)
        label = f"Yearly Summary: {ref.year}"
    elif subcommand == "all-time":
        start = date(2000, 1, 1)
        end   = today
        label = "All-Time Summary"
    elif subcommand == "custom":
        start = date.fromisoformat(args.from_date)
        end   = date.fromisoformat(args.to_date)
        label = f"Custom Summary: {start} – {end}"
    else:
        console.print(f"[red]Unknown report subcommand: {subcommand}[/red]")
        sys.exit(1)

    experimental = getattr(args, "experimental", False)

    async def _run():
        from report import build_report, print_daily_table, print_summary_table, write_csv, _summarise
        from database import get_entries

        entries = await build_report(
            start_date=start,
            end_date=end,
            symbol=cfg.SYMBOL,
            mode=args.mode,
            experimental=experimental,
        )

        if subcommand == "daily":
            from collections import defaultdict
            by_date: dict[date, list] = defaultdict(list)
            for e in entries:
                by_date[e.trade_date].append(e)
            for d in sorted(by_date):
                print_daily_table(by_date[d], d)
        else:
            from collections import defaultdict
            by_date: dict[date, list] = defaultdict(list)
            for e in entries:
                by_date[e.trade_date].append(e)
            summaries = [_summarise(by_date[d], d) for d in sorted(by_date)]
            print_summary_table(summaries, label)

        # CSV output
        if not getattr(args, "no_csv", False):
            csv_path = Path(getattr(args, "csv", None) or (cfg.REPORTS_DIR / f"{subcommand}_{start}_{end}.csv"))
            from report import write_csv, _summarise
            from collections import defaultdict
            by_date2: dict[date, list] = defaultdict(list)
            for e in entries:
                by_date2[e.trade_date].append(e)
            summaries2 = [_summarise(by_date2[d], d) for d in sorted(by_date2)]
            write_csv(entries, summaries2, Path(csv_path))

    asyncio.run(_run())


# ═══════════════════════════════════════════════════════════════════════════════
# secrets
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_secrets(args: argparse.Namespace) -> None:
    if args.secrets_show:
        _secrets_show()
    elif args.secrets_delete:
        _secrets_delete(args.mode)
    else:
        _secrets_store(args.mode)


def _service(mode: str) -> str:
    return "meic_trader_sandbox" if mode == "sandbox" else "meic_trader_live"


def _secrets_show() -> None:
    import keyring
    for mode in ("sandbox", "live"):
        svc = _service(mode)
        secret  = keyring.get_password(svc, "client_secret")
        refresh = keyring.get_password(svc, "refresh_token")
        if secret and refresh:
            console.print(f"[green]✓[/green]  {mode.capitalize()} credentials stored in Credential Manager")
        else:
            console.print(f"[red]✗[/red]  {mode.capitalize()} credentials NOT found")


def _secrets_delete(mode: str) -> None:
    import keyring
    svc = _service(mode)
    keyring.delete_password(svc, "client_secret")
    keyring.delete_password(svc, "refresh_token")
    console.print(f"[yellow]Deleted {mode} credentials from Credential Manager.[/yellow]")


def _secrets_store(mode: str) -> None:
    import keyring
    from tastytrade import Session

    label = "sandbox" if mode == "sandbox" else "live"
    console.print(f"\nStoring [bold]{label}[/bold] credentials in Windows Credential Manager.\n")

    secret  = getpass.getpass(f"Enter {label} client secret: ")
    refresh = getpass.getpass(f"Enter {label} refresh token: ")

    if not secret or not refresh:
        console.print("[red]Credentials cannot be empty.[/red]")
        sys.exit(1)

    svc = _service(mode)
    keyring.set_password(svc, "client_secret", secret)
    keyring.set_password(svc, "refresh_token", refresh)
    console.print("[green]Credentials saved to Windows Credential Manager.[/green]\n")

    # Verify
    console.print("Verifying credentials...")
    try:
        session = Session(secret, refresh, is_test=(mode == "sandbox"))
        ok = session.validate()
        if ok:
            from tastytrade.account import Account
            accounts = Account.get_accounts(session)
            acct_no = accounts[0].account_number if accounts else "—"
            import client as _client
            expiry = _client._fmt_expiry(session.session_expiration)
            console.print(f"[green]✓  Login successful — account {acct_no}, token expires {expiry}[/green]")
        else:
            raise RuntimeError("session.validate() returned False")
    except Exception as exc:
        console.print(f"[red]✗  Verification failed: {exc}[/red]")
        console.print("[yellow]Removing invalid credentials from Credential Manager…[/yellow]")
        keyring.delete_password(svc, "client_secret")
        keyring.delete_password(svc, "refresh_token")
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# config
# ═══════════════════════════════════════════════════════════════════════════════

def cmd_config(args: argparse.Namespace) -> None:
    subcmd = args.config_cmd

    if subcmd == "list":
        _config_list()
    elif subcmd == "show":
        _config_show(getattr(args, "profile", None))
    elif subcmd == "new":
        _config_new(args.profile)
    elif subcmd == "switch":
        _config_switch(args.profile)
    elif subcmd == "delete":
        _config_delete(args.profile)
    elif subcmd == "set":
        _config_set(args)
    else:
        console.print(f"[red]Unknown config subcommand: {subcmd}[/red]")
        sys.exit(1)


def _config_list() -> None:
    from config import list_profiles, get_active_profile_name
    names = list_profiles()
    active = get_active_profile_name()
    if not names:
        console.print("[yellow]No profiles found. Run: python meic.py config new[/yellow]")
        return
    for n in names:
        marker = "[green]●[/green]" if n == active else " "
        console.print(f"  {marker} {n}")


def _config_show(profile_name: str | None) -> None:
    import config as cfg
    from config import get_active_profile_name, PROFILES_DIR
    name = profile_name or get_active_profile_name()
    path = PROFILES_DIR / f"{name}.json"
    if not path.exists():
        console.print(f"[red]Profile '{name}' not found.[/red]")
        sys.exit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    t = Table(title=f"Profile: {name}", box=box.ROUNDED)
    t.add_column("Setting", style="dim")
    t.add_column("Value")
    for k, v in data.items():
        t.add_row(k, str(v))
    console.print(t)


def _validate_field(key: str, value, experimental: bool) -> str | None:
    """Return error string if invalid, None if OK."""
    if key == "delta":
        v = float(value)
        if not experimental and not (0.05 <= v <= 0.30):
            return f"delta must be 0.05–0.30 (got {v}). Use experimental mode for other values."
    if key == "wing_width":
        v = float(value)
        if not experimental and not (1 <= v <= 10):
            return f"wing_width must be 1–10 (got {v}). Use experimental mode for other values."
    return None


def _interactive_profile_tui(name: str, defaults: dict) -> dict:
    """Run the interactive TUI to collect profile settings. Returns filled dict."""
    from config import default_profile_data
    d = dict(defaults)

    console.print(f"\n[bold]Creating profile: {name}[/bold]\n")

    d["symbol"]             = Prompt.ask("Symbol",                  default=str(d.get("symbol", "XSP")))
    d["delta"]              = float(Prompt.ask("Target delta",       default=str(d.get("delta", 0.15))))
    d["wing_width"]         = float(Prompt.ask("Wing width",         default=str(d.get("wing_width", 3.0))))
    entry_default           = ",".join(d.get("entry_times", ["10:55","11:25","12:25","13:35"]))
    entry_str               = Prompt.ask("Entry times (comma-separated HH:MM)", default=entry_default)
    d["entry_times"]        = [t.strip() for t in entry_str.split(",")]
    d["quantity"]           = int(Prompt.ask("Quantity (contracts)", default=str(d.get("quantity", 1))))
    d["stop_trigger_ratio"] = float(Prompt.ask("Stop trigger ratio", default=str(d.get("stop_trigger_ratio", 0.90))))
    d["stop_limit_ratio"]   = float(Prompt.ask("Stop limit ratio",   default=str(d.get("stop_limit_ratio", 0.95))))
    d["min_credit"]         = float(Prompt.ask("Min credit ($)",     default=str(d.get("min_credit", 0.50))))
    d["max_credit"]         = float(Prompt.ask("Max credit ($)",     default=str(d.get("max_credit", 5.00))))
    d["bp_check_enabled"]   = Confirm.ask("Enable BP check?",        default=d.get("bp_check_enabled", True))
    d["bp_buffer"]          = float(Prompt.ask("BP buffer",          default=str(d.get("bp_buffer", 1.25))))
    d["experimental"]       = Confirm.ask("Experimental mode?",      default=bool(d.get("experimental", False)))

    # Validate
    for field in ("delta", "wing_width"):
        err = _validate_field(field, d[field], d["experimental"])
        if err:
            if d["experimental"]:
                console.print(f"[yellow]Warning: {err}[/yellow]")
            else:
                console.print(f"[red]Error: {err}[/red]")
                sys.exit(1)

    return d


def _config_new(profile_name: str | None) -> None:
    from config import default_profile_data, save_profile, save_settings, get_active_profile_name, PROFILES_DIR

    raw_name = profile_name or Prompt.ask("Profile name", default="default")
    name = _slug(raw_name)
    if not name:
        console.print("[red]Invalid profile name.[/red]")
        sys.exit(1)

    path = PROFILES_DIR / f"{name}.json"
    defaults = json.loads(path.read_text(encoding="utf-8")) if path.exists() else default_profile_data()
    data = _interactive_profile_tui(name, defaults)
    save_profile(name, data)
    console.print(f"\n[green]✓  Profile '{name}' saved.[/green]")

    # Set as active if it's the first profile or user confirms
    existing = get_active_profile_name()
    if existing == "default" and name != "default":
        if Confirm.ask(f"Set '{name}' as the active profile?", default=False):
            save_settings(name)
            console.print(f"[green]Active profile set to '{name}'.[/green]")
    elif not (PROFILES_DIR / f"{existing}.json").exists():
        save_settings(name)


def _config_switch(profile_name: str) -> None:
    from config import save_settings, PROFILES_DIR
    path = PROFILES_DIR / f"{profile_name}.json"
    if not path.exists():
        console.print(f"[red]Profile '{profile_name}' not found.[/red]")
        sys.exit(1)
    save_settings(profile_name)
    console.print(f"[green]Active profile switched to '{profile_name}'.[/green]")


def _config_delete(profile_name: str) -> None:
    from config import PROFILES_DIR, get_active_profile_name, save_settings
    path = PROFILES_DIR / f"{profile_name}.json"
    if not path.exists():
        console.print(f"[red]Profile '{profile_name}' not found.[/red]")
        sys.exit(1)
    if not Confirm.ask(f"Delete profile '{profile_name}'?", default=False):
        console.print("Cancelled.")
        return
    path.unlink()
    console.print(f"[yellow]Profile '{profile_name}' deleted.[/yellow]")
    if get_active_profile_name() == profile_name:
        save_settings("default")
        console.print("[yellow]Active profile reset to 'default'.[/yellow]")


def _config_set(args: argparse.Namespace) -> None:
    from config import get_active_profile_name, PROFILES_DIR, save_profile, default_profile_data
    name = getattr(args, "profile", None) or get_active_profile_name()
    path = PROFILES_DIR / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else default_profile_data()

    changed = False
    if args.delta is not None:
        err = _validate_field("delta", args.delta, data.get("experimental", False))
        if err:
            console.print(f"[red]Error: {err}[/red]")
            sys.exit(1)
        data["delta"] = args.delta
        changed = True
    if args.width is not None:
        err = _validate_field("wing_width", args.width, data.get("experimental", False))
        if err:
            console.print(f"[red]Error: {err}[/red]")
            sys.exit(1)
        data["wing_width"] = args.width
        changed = True
    if args.symbol is not None:
        data["symbol"] = args.symbol
        changed = True
    if args.quantity is not None:
        data["quantity"] = args.quantity
        changed = True
    if args.min_credit is not None:
        data["min_credit"] = args.min_credit
        changed = True
    if args.max_credit is not None:
        data["max_credit"] = args.max_credit
        changed = True
    if args.bp_buffer is not None:
        data["bp_buffer"] = args.bp_buffer
        changed = True

    if changed:
        save_profile(name, data)
        console.print(f"[green]Profile '{name}' updated.[/green]")
    else:
        console.print("[yellow]No changes specified.[/yellow]")


# ═══════════════════════════════════════════════════════════════════════════════
# first-run TUI (no profiles exist)
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_profile_exists() -> None:
    """If no profiles exist, run the interactive TUI to create the default profile."""
    from config import list_profiles, default_profile_data, save_profile, save_settings, PROFILES_DIR
    if list_profiles():
        return
    console.print("\n[bold yellow]No configuration found. Let's set up a profile.[/bold yellow]")
    PROFILES_DIR.mkdir(exist_ok=True)
    data = _interactive_profile_tui("default", default_profile_data())
    save_profile("default", data)
    save_settings("default")
    console.print("[green]✓  Configuration saved.[/green]\n")


# ═══════════════════════════════════════════════════════════════════════════════
# argument parser
# ═══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="meic", description="MEIC Trader CLI")
    sub = p.add_subparsers(dest="command", required=True)

    # Shared: --sandbox / --live and --profile
    def _add_mode(parser):
        grp = parser.add_mutually_exclusive_group()
        grp.add_argument("--sandbox", dest="mode", action="store_const", const="sandbox",
                         help="Use sandbox (default)")
        grp.add_argument("--live",    dest="mode", action="store_const", const="live",
                         help="Use live account (orders submitted to market)")
        parser.set_defaults(mode="sandbox")
        parser.add_argument("--profile", default=None, help="Strategy profile name")

    # run
    run_p = sub.add_parser("run", help="Start the trading bot")
    _add_mode(run_p)
    run_p.set_defaults(func=cmd_run)

    # auth
    auth_p = sub.add_parser("auth", help="Test login and show connection banner")
    _add_mode(auth_p)
    auth_p.set_defaults(func=cmd_auth)

    # report
    rep_p  = sub.add_parser("report", help="Display trade reports")
    _add_mode(rep_p)
    rep_p.add_argument("--experimental", action="store_true", help="Query experimental_ic_entries table")
    rep_p.add_argument("--no-csv", action="store_true")
    rep_p.add_argument("--csv", default=None, metavar="PATH")
    rep_sub = rep_p.add_subparsers(dest="report_cmd", required=True)
    rep_p.set_defaults(func=cmd_report)

    for name in ("daily", "weekly", "monthly", "yearly"):
        sp = rep_sub.add_parser(name)
        sp.add_argument("--date", default=None, metavar="YYYY-MM-DD")

    rep_sub.add_parser("all-time")

    custom_p = rep_sub.add_parser("custom")
    custom_p.add_argument("--from", dest="from_date", required=True, metavar="YYYY-MM-DD")
    custom_p.add_argument("--to",   dest="to_date",   required=True, metavar="YYYY-MM-DD")

    # secrets — defaults to sandbox; --live switches to live credentials
    sec_p = sub.add_parser("secrets", help="Manage credentials in Windows Credential Manager")
    sec_p.set_defaults(func=cmd_secrets, mode="sandbox", secrets_show=False, secrets_delete=False)
    sec_p.add_argument("--live",   dest="mode", action="store_const", const="live",
                       help="Target live credentials instead of sandbox")
    sec_p.add_argument("--show",   dest="secrets_show",   action="store_true",
                       help="Show which credentials are stored (never prints values)")
    sec_p.add_argument("--delete", dest="secrets_delete", action="store_true",
                       help="Remove stored credentials")

    # config
    cfg_p = sub.add_parser("config", help="Manage strategy profiles")
    cfg_p.add_argument("--profile", default=None)
    cfg_sub = cfg_p.add_subparsers(dest="config_cmd", required=True)
    cfg_p.set_defaults(func=cmd_config)

    cfg_sub.add_parser("list")
    cfg_sub.add_parser("show")

    new_p = cfg_sub.add_parser("new")
    new_p.add_argument("--profile", default=None)

    sw_p = cfg_sub.add_parser("switch")
    sw_p.add_argument("--profile", required=True)

    del_p = cfg_sub.add_parser("delete")
    del_p.add_argument("--profile", required=True)

    set_p = cfg_sub.add_parser("set")
    set_p.add_argument("--delta",      type=float, default=None)
    set_p.add_argument("--width",      type=float, default=None)
    set_p.add_argument("--symbol",     type=str,   default=None)
    set_p.add_argument("--quantity",   type=int,   default=None)
    set_p.add_argument("--min-credit", dest="min_credit", type=float, default=None)
    set_p.add_argument("--max-credit", dest="max_credit", type=float, default=None)
    set_p.add_argument("--bp-buffer",  dest="bp_buffer",  type=float, default=None)
    set_p.add_argument("--profile",    default=None)

    return p


# ═══════════════════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_parser()
    args = parser.parse_args(argv)

    # For run/auth, ensure at least one profile exists before loading config
    if args.command in ("run", "auth"):
        _ensure_profile_exists()

    args.func(args)


if __name__ == "__main__":
    main()
