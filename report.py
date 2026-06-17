"""
Report engine: IC entry data model, API reconstruction, rich tables, CSV writer.
"""
import asyncio
import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Sequence

import pytz
from rich.console import Console
from rich.table import Table
from rich import box

import config
from logger import log

ET = pytz.timezone("America/New_York")
console = Console()


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class IcEntry:
    trade_date:         date
    entry_time:         str       # "HH:MM" ET
    expiration:         date
    put_strike:         Decimal
    call_strike:        Decimal
    wing_width:         Decimal
    put_credit:         Decimal   # per-contract dollars (×100 already applied)
    call_credit:        Decimal
    net_credit:         Decimal
    put_status:         str       # "expired" | "STOPPED HH:MM" | "open"
    call_status:        str
    put_stop_cost:      Decimal
    call_stop_cost:     Decimal
    pnl:                Decimal
    fees:               Decimal
    ic_order_id:        str
    put_stop_order_id:  str | None
    call_stop_order_id: str | None


@dataclass
class DaySummary:
    date:      date
    entries:   int
    wins:      int
    win_rate:  float
    gross_pnl: Decimal
    fees:      Decimal
    net_pnl:   Decimal


# ── P&L helpers ───────────────────────────────────────────────────────────────

def _calc_pnl(entry: IcEntry) -> Decimal:
    put_pnl  = entry.put_credit  - entry.put_stop_cost
    call_pnl = entry.call_credit - entry.call_stop_cost
    return put_pnl + call_pnl


def _summarise(entries: Sequence[IcEntry], d: date) -> DaySummary:
    wins      = sum(1 for e in entries if e.pnl > 0)
    gross     = sum((e.pnl for e in entries), Decimal("0"))
    fees      = sum((e.fees for e in entries), Decimal("0"))
    win_rate  = (wins / len(entries) * 100) if entries else 0.0
    return DaySummary(
        date=d,
        entries=len(entries),
        wins=wins,
        win_rate=win_rate,
        gross_pnl=gross,
        fees=fees,
        net_pnl=gross - fees,
    )


# ── API reconstruction (today only) ──────────────────────────────────────────

async def _reconstruct_today(symbol: str) -> list[IcEntry]:
    """
    Rebuild today's IcEntry list from the live tastytrade API.
    Uses order history + transaction history; does NOT require DB.
    """
    from client import get_session, get_account
    session = await get_session()
    account = await get_account()
    today   = datetime.now(ET).date()

    log.info("Reconstructing today's entries from API (%s)...", today)

    orders = await account.get_order_history(
        session,
        start_date=today,
        end_date=today,
        underlying_symbol=symbol,
    )

    transactions = await account.get_history(
        session,
        start_date=today,
        end_date=today,
        underlying_symbol=symbol,
    )

    # Index transactions by order_id for fast fee lookup
    tx_by_order: dict[str, list] = {}
    for tx in transactions:
        oid = getattr(tx, "order_id", None)
        if oid:
            tx_by_order.setdefault(str(oid), []).append(tx)

    # Separate IC opening orders (4-leg, Filled, has SELL_TO_OPEN) from stop orders
    ic_orders   = []
    stop_orders = []
    for o in orders:
        legs = list(o.legs or [])
        actions = {getattr(leg, "action", "").upper() for leg in legs}
        if len(legs) == 4 and "SELL_TO_OPEN" in actions and o.status == "Filled":
            ic_orders.append(o)
        elif len(legs) == 2 and "BUY_TO_CLOSE" in actions and o.order_type in ("Stop Limit", "STOP_LIMIT"):
            stop_orders.append(o)

    # Build symbol-set index for stop matching
    def _symbols(o) -> frozenset:
        return frozenset(getattr(leg, "symbol", "") for leg in (o.legs or []))

    ic_symbol_sets = {str(o.id): _symbols(o) for o in ic_orders}

    # Match stop orders to their parent IC order
    stops_by_ic: dict[str, list] = {}
    for stop in stop_orders:
        stop_syms = _symbols(stop)
        for ic_id, ic_syms in ic_symbol_sets.items():
            if stop_syms & ic_syms:  # overlap means they share options
                stops_by_ic.setdefault(ic_id, []).append(stop)
                break

    entries: list[IcEntry] = []
    for ic in ic_orders:
        ic_id  = str(ic.id)
        legs   = list(ic.legs or [])

        # Determine put/call legs and credits from fills
        put_short = call_short = put_long = call_long = None
        for leg in legs:
            sym    = getattr(leg, "symbol", "")
            action = getattr(leg, "action", "").upper()
            # OCC symbol: last char is P or C
            is_put = sym.endswith("P") or ("P" in sym[-2:])
            if action == "SELL_TO_OPEN":
                if is_put:
                    put_short = leg
                else:
                    call_short = leg
            else:
                if is_put:
                    put_long = leg
                else:
                    call_long = leg

        if not all([put_short, call_short, put_long, call_long]):
            log.warning("Could not identify all legs for IC order %s — skipping.", ic_id)
            continue

        def _fill_price(leg) -> Decimal:
            return Decimal(str(getattr(leg, "average_open_price", 0) or 0))

        put_credit  = (_fill_price(put_short)  - _fill_price(put_long))  * 100
        call_credit = (_fill_price(call_short) - _fill_price(call_long)) * 100
        net_credit  = put_credit + call_credit

        # Fees from transaction records
        fees = Decimal("0")
        for tx in tx_by_order.get(ic_id, []):
            fees += (
                Decimal(str(getattr(tx, "commission",       0) or 0)) +
                Decimal(str(getattr(tx, "regulatory_fees",  0) or 0)) +
                Decimal(str(getattr(tx, "clearing_fees",    0) or 0))
            )

        # Match stop orders for put and call spreads
        put_stop = call_stop = None
        for stop in stops_by_ic.get(ic_id, []):
            syms = _symbols(stop)
            if getattr(put_short, "symbol", "") in syms:
                put_stop = stop
            elif getattr(call_short, "symbol", "") in syms:
                call_stop = stop

        def _stop_status(stop) -> str:
            if stop is None:
                return "expired"
            st = (stop.status or "").lower()
            if st == "filled":
                ts = getattr(stop, "terminal_at", None)
                if ts:
                    try:
                        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                        hhmm = dt.astimezone(ET).strftime("%H:%M")
                        return f"STOPPED {hhmm}"
                    except Exception:
                        pass
                return "STOPPED --:--"
            if st in ("cancelled", "rejected", "expired", "removed"):
                return "expired"
            return "open"

        def _stop_cost(stop) -> Decimal:
            if stop is None or (stop.status or "").lower() != "filled":
                return Decimal("0")
            return Decimal(str(stop.price or 0)) * 100

        put_status  = _stop_status(put_stop)
        call_status = _stop_status(call_stop)
        put_stop_cost  = _stop_cost(put_stop)
        call_stop_cost = _stop_cost(call_stop)
        pnl = (put_credit - put_stop_cost) + (call_credit - call_stop_cost)

        # Entry time from IC order placed_at
        placed_at = getattr(ic, "placed_at", None)
        if placed_at:
            try:
                dt = datetime.fromisoformat(str(placed_at).replace("Z", "+00:00"))
                entry_time = dt.astimezone(ET).strftime("%H:%M")
            except Exception:
                entry_time = "--:--"
        else:
            entry_time = "--:--"

        # Expiration from first leg symbol (OCC format: XSPYYMMDDP/C...)
        exp_date = today
        sym = getattr(put_short, "symbol", "")
        try:
            # OCC symbol: underlying + YYMMDD + P/C + strike
            # Find 6-digit date substring
            import re
            m = re.search(r"(\d{6})[PC]", sym)
            if m:
                exp_date = datetime.strptime(m.group(1), "%y%m%d").date()
        except Exception:
            pass

        put_strike  = Decimal(str(getattr(put_short,  "strike_price", 0) or 0))
        call_strike = Decimal(str(getattr(call_short, "strike_price", 0) or 0))
        wing = Decimal(str(config.WING_WIDTH))

        entries.append(IcEntry(
            trade_date=today,
            entry_time=entry_time,
            expiration=exp_date,
            put_strike=put_strike,
            call_strike=call_strike,
            wing_width=wing,
            put_credit=put_credit,
            call_credit=call_credit,
            net_credit=net_credit,
            put_status=put_status,
            call_status=call_status,
            put_stop_cost=put_stop_cost,
            call_stop_cost=call_stop_cost,
            pnl=pnl,
            fees=fees,
            ic_order_id=ic_id,
            put_stop_order_id=str(put_stop.id) if put_stop else None,
            call_stop_order_id=str(call_stop.id) if call_stop else None,
        ))

    entries.sort(key=lambda e: e.entry_time)
    log.info("Reconstructed %d entries for %s.", len(entries), today)
    return entries


# ── public build function ─────────────────────────────────────────────────────

async def build_report(
    start_date: date,
    end_date: date,
    symbol: str,
    mode: str,
    experimental: bool = False,
) -> list[IcEntry]:
    """
    Return list[IcEntry] for the date range.
    If the range is today-only, reconstruct from API; otherwise read from DB.
    """
    from database import get_entries
    today = datetime.now(ET).date()

    if start_date == today == end_date:
        return await _reconstruct_today(symbol)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: get_entries(start_date, end_date, mode, experimental),
    )


# ── display ───────────────────────────────────────────────────────────────────

def _status_badge(status: str) -> str:
    if status == "expired":
        return "[white on navy] expired [/white on navy]"
    if status.startswith("STOPPED"):
        return f"[white on red] {status} [/white on red]"
    return f"[dim]{status}[/dim]"


def _pnl_str(pnl: Decimal) -> str:
    val = f"${abs(pnl):,.2f}"
    if pnl >= 0:
        return f"[green]{val}[/green]"
    return f"[bold red]-{val}[/bold red]"


def print_daily_table(entries: list[IcEntry], report_date: date) -> None:
    today = datetime.now(ET).date()
    title = "Today's Trades" if report_date == today else f"Trades: {report_date}"
    console.print(f"\n[bold cyan]{title}[/bold cyan]\n")

    t = Table(box=box.SIMPLE_HEAVY, show_footer=False, pad_edge=False)
    t.add_column("TIME",        style="dim",   no_wrap=True)
    t.add_column("WIDTH",       justify="right")
    t.add_column("PUT STRIKE",  justify="right")
    t.add_column("CALL STRIKE", justify="right")
    t.add_column("PUT $",       justify="right")
    t.add_column("CALL $",      justify="right")
    t.add_column("NET CREDIT",  justify="right")
    t.add_column("PUT STATUS",  no_wrap=True)
    t.add_column("CALL STATUS", no_wrap=True)
    t.add_column("P&L",         justify="right")

    total_pnl = Decimal("0")
    for e in entries:
        total_pnl += e.pnl
        t.add_row(
            e.entry_time,
            str(int(e.wing_width)),
            str(int(e.put_strike)),
            str(int(e.call_strike)),
            f"${e.put_credit:,.2f}",
            f"${e.call_credit:,.2f}",
            f"${e.net_credit:,.2f}",
            _status_badge(e.put_status),
            _status_badge(e.call_status),
            _pnl_str(e.pnl),
        )

    console.print(t)
    console.print(
        f"  [bold]DAILY TOTAL[/bold]  {_pnl_str(total_pnl)}\n"
    )


def print_summary_table(summaries: list[DaySummary], label: str) -> None:
    console.print(f"\n[bold cyan]{label}[/bold cyan]\n")

    t = Table(box=box.SIMPLE_HEAVY, show_footer=False, pad_edge=False)
    t.add_column("DATE",      no_wrap=True)
    t.add_column("ENTRIES",   justify="right")
    t.add_column("WINS",      justify="right")
    t.add_column("WIN RATE",  justify="right")
    t.add_column("GROSS P&L", justify="right")
    t.add_column("FEES",      justify="right")
    t.add_column("NET P&L",   justify="right")

    total_entries = total_wins = 0
    total_gross = total_fees = total_net = Decimal("0")

    for s in summaries:
        if s.win_rate >= 60:
            wr_color = "green"
        elif s.win_rate >= 40:
            wr_color = "yellow"
        else:
            wr_color = "red"

        t.add_row(
            str(s.date),
            str(s.entries),
            str(s.wins),
            f"[{wr_color}]{s.win_rate:.1f}%[/{wr_color}]",
            f"${s.gross_pnl:,.2f}",
            f"${s.fees:,.2f}",
            _pnl_str(s.net_pnl),
        )
        total_entries += s.entries
        total_wins    += s.wins
        total_gross   += s.gross_pnl
        total_fees    += s.fees
        total_net     += s.net_pnl

    overall_wr = (total_wins / total_entries * 100) if total_entries else 0.0
    if overall_wr >= 60:
        wr_col = "green"
    elif overall_wr >= 40:
        wr_col = "yellow"
    else:
        wr_col = "red"

    console.print(t)
    console.print(
        f"  [bold]TOTAL[/bold]  {total_entries} entries  "
        f"{total_wins} wins  [{wr_col}]{overall_wr:.1f}%[/{wr_col}]  "
        f"gross {_pnl_str(total_gross)}  fees ${total_fees:,.2f}  net {_pnl_str(total_net)}\n"
    )


# ── CSV writer ────────────────────────────────────────────────────────────────

def write_csv(entries: list[IcEntry], summaries: list[DaySummary], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "trade_date", "entry_time", "expiration",
            "put_strike", "call_strike", "wing_width",
            "put_credit", "call_credit", "net_credit",
            "put_status", "call_status",
            "put_stop_cost", "call_stop_cost",
            "pnl", "fees", "ic_order_id",
        ])
        for e in entries:
            writer.writerow([
                e.trade_date, e.entry_time, e.expiration,
                e.put_strike, e.call_strike, e.wing_width,
                e.put_credit, e.call_credit, e.net_credit,
                e.put_status, e.call_status,
                e.put_stop_cost, e.call_stop_cost,
                e.pnl, e.fees, e.ic_order_id,
            ])

        if summaries:
            writer.writerow([])
            writer.writerow(["date", "entries", "wins", "win_rate_%", "gross_pnl", "fees", "net_pnl"])
            for s in summaries:
                writer.writerow([
                    s.date, s.entries, s.wins,
                    f"{s.win_rate:.1f}",
                    s.gross_pnl, s.fees, s.net_pnl,
                ])

    log.info("CSV written to %s", path)


# ── EOD auto-report ───────────────────────────────────────────────────────────

async def run_eod_report() -> None:
    """
    Called by scheduler at 16:05 ET:
      1. Reconstruct today's entries from API
      2. Upsert into DB
      3. Print daily table
      4. Write CSV
    """
    from database import upsert_entry
    today   = datetime.now(ET).date()
    mode    = config.MODE
    exp     = config.EXPERIMENTAL
    symbol  = config.SYMBOL

    entries = await _reconstruct_today(symbol)
    if not entries:
        log.info("EOD report: no entries found for %s.", today)
        return

    loop = asyncio.get_event_loop()
    for entry in entries:
        await loop.run_in_executor(None, lambda e=entry: upsert_entry(e, mode, exp))

    print_daily_table(entries, today)

    csv_path = config.REPORTS_DIR / f"{today}.csv"
    summary  = _summarise(entries, today)
    write_csv(entries, [summary], csv_path)
