"""
MEIC strategy orchestration.
Each entry:  select strikes → place IC → wait for fill → place stop-limits.
All 4 entries run concurrently once triggered.
"""
import asyncio
from datetime import datetime

import pytz

from chain import select_strikes
from orders import place_ic_entry, EntryResult
from logger import log

ET = pytz.timezone("America/New_York")


async def run_entry(entry_label: str) -> EntryResult | None:
    log.info("=== Entry %s starting ===", entry_label)
    try:
        strikes = await select_strikes()
        result  = await place_ic_entry(entry_label, strikes)
        if result:
            log.info(
                "=== Entry %s complete: credit=%.2f, put_stop=%s, call_stop=%s ===",
                entry_label,
                result.ic_credit,
                result.put_stop_order_id,
                result.call_stop_order_id,
            )
        else:
            log.warning("=== Entry %s did not complete (order not filled) ===", entry_label)
        return result
    except Exception:
        log.exception("Unhandled error in entry %s", entry_label)
        return None
