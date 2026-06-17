"""
Time-based scheduler.  Fires coroutines at specified ET times on market days.
Checks every 15 seconds; fires each time-slot exactly once per day.
"""
import asyncio
from datetime import datetime, date

import pytz

import config
from logger import log

ET = pytz.timezone("America/New_York")

# NYSE holidays (update annually)
NYSE_HOLIDAYS_2025 = {
    date(2025, 1, 1),
    date(2025, 1, 20),
    date(2025, 2, 17),
    date(2025, 4, 18),
    date(2025, 5, 26),
    date(2025, 6, 19),
    date(2025, 7, 4),
    date(2025, 9, 1),
    date(2025, 11, 27),
    date(2025, 12, 25),
}

NYSE_HOLIDAYS_2026 = {
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 8, 31),
    date(2026, 11, 26),
    date(2026, 12, 25),
}

NYSE_HOLIDAYS = NYSE_HOLIDAYS_2025 | NYSE_HOLIDAYS_2026


def _is_market_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


EOD_TIME = "16:05"


async def run_scheduler(entry_coro_factory) -> None:
    """
    entry_coro_factory(label: str) should return an awaitable coroutine.
    Also fires run_eod_report() at EOD_TIME on market days.
    Runs until interrupted (Ctrl-C / SIGINT).
    """
    from report import run_eod_report

    fired_today: set[str] = set()
    last_reset_date: date | None = None

    log.info("Scheduler started. Entry times (ET): %s", config.ENTRY_TIMES_ET)

    while True:
        now = datetime.now(ET)
        today = now.date()

        # Reset fired set at midnight
        if today != last_reset_date:
            fired_today.clear()
            last_reset_date = today
            if _is_market_day(today):
                log.info(
                    "Market day %s — watching for entries at %s ET (EOD at %s).",
                    today, config.ENTRY_TIMES_ET, EOD_TIME,
                )
            else:
                log.info("Non-market day %s — no entries will fire.", today)

        if _is_market_day(today):
            current_time = now.strftime("%H:%M")
            for slot in config.ENTRY_TIMES_ET:
                if current_time == slot and slot not in fired_today:
                    fired_today.add(slot)
                    label = f"Entry-{slot}"
                    log.info("Firing %s", label)
                    asyncio.create_task(entry_coro_factory(label))

            if current_time == EOD_TIME and EOD_TIME not in fired_today:
                fired_today.add(EOD_TIME)
                log.info("Firing EOD report.")
                asyncio.create_task(run_eod_report())

        await asyncio.sleep(15)
