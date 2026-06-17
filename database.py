"""
SQLite persistence for IC trade entries.

Three tables:
  sandbox_ic_entries      — sandbox paper-trading records
  live_ic_entries         — live brokerage records
  experimental_ic_entries — any profile with experimental=True (isolated from P&L history)

All functions are synchronous; wrap with run_in_executor when calling from async code.
"""
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Generator

import config

_TABLES = ("sandbox_ic_entries", "live_ic_entries", "experimental_ic_entries")

_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date         TEXT    NOT NULL,
    entry_time         TEXT    NOT NULL,
    expiration         TEXT    NOT NULL,
    put_strike         REAL    NOT NULL,
    call_strike        REAL    NOT NULL,
    wing_width         REAL    NOT NULL,
    put_credit         REAL    NOT NULL,
    call_credit        REAL    NOT NULL,
    net_credit         REAL    NOT NULL,
    put_status         TEXT    NOT NULL,
    call_status        TEXT    NOT NULL,
    put_stop_cost      REAL    NOT NULL DEFAULT 0,
    call_stop_cost     REAL    NOT NULL DEFAULT 0,
    pnl                REAL    NOT NULL,
    fees               REAL    NOT NULL DEFAULT 0,
    ic_order_id        TEXT    NOT NULL UNIQUE,
    put_stop_order_id  TEXT,
    call_stop_order_id TEXT,
    profile            TEXT    NOT NULL DEFAULT 'default',
    simulated          INTEGER NOT NULL DEFAULT 1
);
"""


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    path = Path(config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        for table in _TABLES:
            con.execute(_DDL.format(table=table))


def _table_for(mode: str, experimental: bool) -> str:
    if experimental:
        return "experimental_ic_entries"
    return "sandbox_ic_entries" if mode == "sandbox" else "live_ic_entries"


def _entry_to_row(entry) -> dict:
    """Convert an IcEntry dataclass to a plain dict suitable for SQL insertion."""
    from report import IcEntry  # local import to avoid circular dependency
    d = asdict(entry) if hasattr(entry, "__dataclass_fields__") else dict(entry)
    # Convert date/Decimal to primitives
    for k, v in d.items():
        if isinstance(v, date):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            d[k] = float(v)
    d.setdefault("profile", config.ACTIVE_PROFILE)
    d.setdefault("simulated", 0)
    return d


def upsert_entry(entry, mode: str, experimental: bool) -> None:
    table = _table_for(mode, experimental)
    row = _entry_to_row(entry)
    cols = [
        "trade_date", "entry_time", "expiration",
        "put_strike", "call_strike", "wing_width",
        "put_credit", "call_credit", "net_credit",
        "put_status", "call_status",
        "put_stop_cost", "call_stop_cost",
        "pnl", "fees",
        "ic_order_id", "put_stop_order_id", "call_stop_order_id",
        "profile", "simulated",
    ]
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(
        f"{c} = excluded.{c}"
        for c in cols
        if c != "ic_order_id"
    )
    sql = f"""
        INSERT INTO {table} ({', '.join(cols)})
        VALUES ({placeholders})
        ON CONFLICT(ic_order_id) DO UPDATE SET {updates}
    """
    with _conn() as con:
        con.execute(sql, {c: row.get(c) for c in cols})


def get_entries(
    start_date: date,
    end_date: date,
    mode: str,
    experimental: bool = False,
) -> list:
    """Return a list of IcEntry dataclasses for the given date range."""
    from report import IcEntry
    table = _table_for(mode, experimental)
    sql = f"""
        SELECT * FROM {table}
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date, entry_time
    """
    with _conn() as con:
        rows = con.execute(sql, (start_date.isoformat(), end_date.isoformat())).fetchall()

    results = []
    for r in rows:
        results.append(IcEntry(
            trade_date=date.fromisoformat(r["trade_date"]),
            entry_time=r["entry_time"],
            expiration=date.fromisoformat(r["expiration"]),
            put_strike=Decimal(str(r["put_strike"])),
            call_strike=Decimal(str(r["call_strike"])),
            wing_width=Decimal(str(r["wing_width"])),
            put_credit=Decimal(str(r["put_credit"])),
            call_credit=Decimal(str(r["call_credit"])),
            net_credit=Decimal(str(r["net_credit"])),
            put_status=r["put_status"],
            call_status=r["call_status"],
            put_stop_cost=Decimal(str(r["put_stop_cost"])),
            call_stop_cost=Decimal(str(r["call_stop_cost"])),
            pnl=Decimal(str(r["pnl"])),
            fees=Decimal(str(r["fees"])),
            ic_order_id=r["ic_order_id"],
            put_stop_order_id=r["put_stop_order_id"],
            call_stop_order_id=r["call_stop_order_id"],
        ))
    return results
