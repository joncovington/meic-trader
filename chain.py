"""
Fetches the XSP option chain and selects strikes by delta.

Delta selection rule:
  - For the short put:  pick the put whose |delta| is <= TARGET_DELTA,
                        choosing the strike with the highest |delta| (closest to target).
  - For the short call: pick the call whose delta is <= TARGET_DELTA,
                        choosing the strike with the highest delta (closest to target).
  - Long put:  short_put_strike  - WING_WIDTH
  - Long call: short_call_strike + WING_WIDTH
"""
import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import pytz
from tastytrade.instruments import Option, NestedOptionChain
from tastytrade.streamer import DXLinkStreamer
from tastytrade.dxfeed import Greeks, EventType

import config
from client import get_session, reauth_on_401
from logger import log

ET = pytz.timezone("America/New_York")


@dataclass
class SelectedStrikes:
    expiration: date
    short_put:  Option
    long_put:   Option
    short_call: Option
    long_call:  Option
    # Greeks at time of selection
    short_put_delta:  Decimal
    short_call_delta: Decimal


def _today_et() -> date:
    return datetime.now(ET).date()


def _select_expiration(chain: NestedOptionChain) -> date:
    """Return today's 0DTE expiration if it exists, else the nearest future one."""
    today = _today_et()
    expirations = sorted(chain.expirations, key=lambda e: e.expiration_date)
    for exp in expirations:
        if exp.expiration_date >= today:
            log.info("Selected expiration: %s", exp.expiration_date)
            return exp.expiration_date
    raise RuntimeError(f"No valid expirations found in {config.SYMBOL} chain.")


async def _stream_greeks(session, symbols: list[str]) -> dict[str, Greeks]:
    """Subscribe to Greeks events and collect one snapshot per symbol."""
    collected: dict[str, Greeks] = {}
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(EventType.GREEKS, symbols)
        pending = set(symbols)
        while pending:
            greek = await asyncio.wait_for(streamer.get_event(Greeks), timeout=30)
            if greek.event_symbol in pending:
                collected[greek.event_symbol] = greek
                pending.discard(greek.event_symbol)
    return collected


def _best_delta_strike(
    options: list[Option],
    greeks_map: dict[str, Greeks],
    option_type: str,  # "P" or "C"
) -> tuple[Option, Decimal]:
    """
    Return (option, abs_delta) for the strike closest to TARGET_DELTA
    without exceeding it.
    """
    target = config.TARGET_DELTA
    best: tuple[Option, Decimal] | None = None

    for opt in options:
        if opt.option_type != option_type:
            continue
        g = greeks_map.get(opt.streamer_symbol)
        if g is None or g.delta is None:
            continue
        raw_delta = Decimal(str(g.delta))
        abs_delta = abs(raw_delta)
        if abs_delta > target:
            continue  # over the target — skip
        if best is None or abs_delta > best[1]:
            best = (opt, abs_delta)

    if best is None:
        raise RuntimeError(
            f"No {option_type} strike found with delta <= {target} for {config.SYMBOL}."
        )
    return best


def _find_wing(options: list[Option], strike: Decimal, option_type: str) -> Option:
    """Find the option at exactly (strike - WING_WIDTH) for puts or (strike + WING_WIDTH) for calls."""
    if option_type == "P":
        target_strike = strike - config.WING_WIDTH
    else:
        target_strike = strike + config.WING_WIDTH

    for opt in options:
        if opt.option_type == option_type and Decimal(str(opt.strike_price)) == target_strike:
            return opt

    raise RuntimeError(
        f"No {option_type} wing found at strike {target_strike} for {config.SYMBOL}."
    )


@reauth_on_401
async def select_strikes() -> SelectedStrikes:
    session = await get_session()
    loop = asyncio.get_event_loop()

    log.info("Fetching %s option chain...", config.SYMBOL)
    chain: NestedOptionChain = await loop.run_in_executor(
        None, lambda: NestedOptionChain.get_chain(session, config.SYMBOL)
    )

    expiration_date = _select_expiration(chain)

    # Collect all options for the selected expiration
    all_options: list[Option] = await loop.run_in_executor(
        None,
        lambda: Option.get_option_chain(session, config.SYMBOL, expiration_date),
    )

    if not all_options:
        raise RuntimeError(f"No options returned for {config.SYMBOL} {expiration_date}.")

    log.info("Retrieved %d options for %s %s. Streaming Greeks...", len(all_options), config.SYMBOL, expiration_date)

    symbols = [o.streamer_symbol for o in all_options]
    greeks_map = await _stream_greeks(session, symbols)
    log.info("Greeks received for %d/%d symbols.", len(greeks_map), len(symbols))

    short_put_opt, sp_delta  = _best_delta_strike(all_options, greeks_map, "P")
    short_call_opt, sc_delta = _best_delta_strike(all_options, greeks_map, "C")

    short_put_strike  = Decimal(str(short_put_opt.strike_price))
    short_call_strike = Decimal(str(short_call_opt.strike_price))

    long_put_opt  = _find_wing(all_options, short_put_strike,  "P")
    long_call_opt = _find_wing(all_options, short_call_strike, "C")

    log.info(
        "Strikes selected: Put spread %s/%s (Δ=%.3f), Call spread %s/%s (Δ=%.3f)",
        short_put_strike,
        Decimal(str(long_put_opt.strike_price)),
        sp_delta,
        short_call_strike,
        Decimal(str(long_call_opt.strike_price)),
        sc_delta,
    )

    return SelectedStrikes(
        expiration=expiration_date,
        short_put=short_put_opt,
        long_put=long_put_opt,
        short_call=short_call_opt,
        long_call=long_call_opt,
        short_put_delta=sp_delta,
        short_call_delta=sc_delta,
    )
