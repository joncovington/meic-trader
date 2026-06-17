"""
Order construction, placement, fill monitoring, and stop-limit placement.

Iron condor = 4-leg SELL order (credit).
After IC fills:
  - Place a STOP_LIMIT BUY order on the put spread  (2 legs, GTC)
  - Place a STOP_LIMIT BUY order on the call spread (2 legs, GTC)

Stop trigger = 90% of IC credit (rounded to nearest $0.01)
Stop limit   = 95% of IC credit (rounded to nearest $0.01)
"""
import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from tastytrade.order import (
    NewOrder, Leg, OrderAction, OrderType, OrderTimeInForce,
    PriceEffect, InstrumentType,
)
from tastytrade.account import Account

import config
from chain import SelectedStrikes
from client import get_session, get_account, reauth_on_401
from logger import log


PENNY = Decimal("0.01")


def _round_penny(value: Decimal) -> Decimal:
    return value.quantize(PENNY, rounding=ROUND_HALF_UP)


def _mid_price(bid: Decimal, ask: Decimal) -> Decimal:
    return _round_penny((bid + ask) / 2)


async def _get_quote(session, streamer_symbol: str) -> tuple[Decimal, Decimal]:
    """Return (bid, ask) for a single option by streaming a Quote event."""
    from tastytrade.streamer import DXLinkStreamer
    from tastytrade.dxfeed import Quote, EventType

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(EventType.QUOTE, [streamer_symbol])
        quote: Quote = await asyncio.wait_for(streamer.get_event(Quote), timeout=15)
    return Decimal(str(quote.bid_price)), Decimal(str(quote.ask_price))


async def _ic_mid_price(strikes: SelectedStrikes) -> Decimal:
    """
    Compute mid price for the iron condor as a net credit.
    IC mid = (short_put_mid - long_put_mid) + (short_call_mid - long_call_mid)
    """
    session = await get_session()
    legs = [
        strikes.short_put,
        strikes.long_put,
        strikes.short_call,
        strikes.long_call,
    ]
    bids_asks: list[tuple[Decimal, Decimal]] = []
    for opt in legs:
        b, a = await _get_quote(session, opt.streamer_symbol)
        bids_asks.append((b, a))

    sp_mid = _mid_price(*bids_asks[0])
    lp_mid = _mid_price(*bids_asks[1])
    sc_mid = _mid_price(*bids_asks[2])
    lc_mid = _mid_price(*bids_asks[3])

    ic_credit = _round_penny((sp_mid - lp_mid) + (sc_mid - lc_mid))
    log.info(
        "IC mid prices — short put: %.2f, long put: %.2f, short call: %.2f, long call: %.2f → net credit: %.2f",
        sp_mid, lp_mid, sc_mid, lc_mid, ic_credit,
    )
    return ic_credit


def _build_ic_order(strikes: SelectedStrikes, credit: Decimal) -> NewOrder:
    def leg(opt, action: OrderAction) -> Leg:
        return Leg(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=opt.symbol,
            action=action,
            quantity=config.QUANTITY,
        )

    return NewOrder(
        time_in_force=OrderTimeInForce.DAY,
        order_type=OrderType.LIMIT,
        legs=[
            leg(strikes.short_put,  OrderAction.SELL_TO_OPEN),
            leg(strikes.long_put,   OrderAction.BUY_TO_OPEN),
            leg(strikes.short_call, OrderAction.SELL_TO_OPEN),
            leg(strikes.long_call,  OrderAction.BUY_TO_OPEN),
        ],
        price=credit,
        price_effect=PriceEffect.CREDIT,
    )


def _build_spread_stop_limit(
    short_opt, long_opt,
    stop_trigger: Decimal,
    stop_limit:   Decimal,
    label: str,
) -> NewOrder:
    """Build a GTC stop-limit closing order for a 2-leg spread."""
    log.info(
        "%s stop-limit: trigger=%.2f, limit=%.2f", label, stop_trigger, stop_limit
    )

    def leg(opt, action: OrderAction) -> Leg:
        return Leg(
            instrument_type=InstrumentType.EQUITY_OPTION,
            symbol=opt.symbol,
            action=action,
            quantity=config.QUANTITY,
        )

    return NewOrder(
        time_in_force=OrderTimeInForce.GTC,
        order_type=OrderType.STOP_LIMIT,
        stop_trigger=stop_trigger,
        price=stop_limit,
        price_effect=PriceEffect.DEBIT,
        legs=[
            leg(short_opt, OrderAction.BUY_TO_CLOSE),
            leg(long_opt,  OrderAction.SELL_TO_CLOSE),
        ],
    )


async def _place_order_async(account: Account, session, order: NewOrder):
    """Place an order using the async SDK method."""
    return await account.place_order(session, order, dry_run=False)


async def _poll_for_fill(account: Account, session, order_id: str) -> Decimal | None:
    """
    Poll until the order is filled or timeout.
    Returns the fill price (net credit per contract) or None on timeout/cancel.
    """
    elapsed = 0
    while elapsed < config.FILL_TIMEOUT:
        await asyncio.sleep(config.FILL_POLL_INTERVAL)
        elapsed += config.FILL_POLL_INTERVAL
        order = await account.get_order(session, order_id)
        status = order.status
        log.debug("Order %s status: %s", order_id, status)

        if status == "Filled":
            fill_price = Decimal(str(order.price))
            log.info("Order %s FILLED at %.2f credit.", order_id, fill_price)
            return fill_price

        if status in ("Cancelled", "Rejected", "Expired"):
            log.warning("Order %s ended with status: %s", order_id, status)
            return None

    log.warning("Order %s not filled after %ds — cancelling.", order_id, config.FILL_TIMEOUT)
    await account.cancel_order(session, order_id)
    return None


@dataclass
class EntryResult:
    entry_label: str
    ic_credit: Decimal
    ic_order_id: str
    put_stop_order_id: str
    call_stop_order_id: str
    strikes: SelectedStrikes


def _live_guard_fail(label: str, reason: str) -> None:
    """Log and print a bold red notice for a failed live safety guard."""
    from rich.console import Console
    log.warning("[%s] LIVE GUARD FAILED — %s — skipping entry.", label, reason)
    Console().print(f"[bold red]\n  LIVE GUARD FAILED [{label}]: {reason} — entry skipped.\n[/bold red]")


async def _run_live_guards(entry_label: str, ic_credit: Decimal, strikes: SelectedStrikes) -> bool:
    """
    Run pre-flight safety checks for live mode.
    Returns True if all guards pass; logs+prints and returns False on any failure.
    """
    # Guard 1: must be a credit
    if ic_credit <= 0:
        _live_guard_fail(entry_label, f"IC mid is a debit ({ic_credit:+.2f})")
        return False

    # Guard 2: credit within configured range
    if ic_credit < config.MIN_CREDIT or ic_credit > config.MAX_CREDIT:
        _live_guard_fail(
            entry_label,
            f"credit ${ic_credit:.2f} outside range "
            f"[${config.MIN_CREDIT:.2f}, ${config.MAX_CREDIT:.2f}]",
        )
        return False

    # Guard 3: buying power
    if config.BP_CHECK_ENABLED:
        session = await get_session()
        account = await get_account()
        balances = await account.get_balances(session)
        available_bp = Decimal(str(getattr(balances, "derivative_buying_power", 0) or 0))
        margin_req = (config.WING_WIDTH * 100) - ic_credit
        bp_needed  = margin_req * config.BP_BUFFER
        if bp_needed > available_bp:
            _live_guard_fail(
                entry_label,
                f"BP needed ${bp_needed:,.2f} > available ${available_bp:,.2f}",
            )
            return False

    return True


@reauth_on_401
async def place_ic_entry(entry_label: str, strikes: SelectedStrikes) -> EntryResult | None:
    """
    Full entry flow:
      1. Compute IC mid price
      2. (live only) Run pre-flight safety guards
      3. Place limit IC order
      4. Wait for fill
      5. Place stop-limit orders for put and call spreads
    Returns EntryResult on success, None if order never filled or guard fails.
    """
    session = await get_session()
    account = await get_account()

    # --- 1. Mid price ---
    ic_credit = await _ic_mid_price(strikes)
    if ic_credit <= 0 and config.MODE != "live":
        log.error("[%s] IC mid credit is %.2f — skipping entry.", entry_label, ic_credit)
        return None

    # --- 2. Live safety guards ---
    if config.MODE == "live":
        if not await _run_live_guards(entry_label, ic_credit, strikes):
            return None

    # --- 3. Place IC order ---
    ic_order = _build_ic_order(strikes, ic_credit)
    log.info("[%s] Placing IC order at %.2f credit...", entry_label, ic_credit)
    response = await _place_order_async(account, session, ic_order)
    order_id = str(response.order.id)
    log.info("[%s] IC order placed: id=%s", entry_label, order_id)

    # --- 4. Wait for fill ---
    fill_price = await _poll_for_fill(account, session, order_id)
    if fill_price is None:
        return None

    # --- 5. Compute stop-limit levels ---
    stop_trigger = _round_penny(fill_price * config.STOP_TRIGGER_RATIO)
    stop_limit   = _round_penny(fill_price * config.STOP_LIMIT_RATIO)

    # --- 6. Place put spread stop-limit ---
    put_stop_order = _build_spread_stop_limit(
        strikes.short_put, strikes.long_put,
        stop_trigger, stop_limit,
        label=f"[{entry_label}] PUT",
    )
    put_resp = await _place_order_async(account, session, put_stop_order)
    put_stop_id = str(put_resp.order.id)
    log.info("[%s] Put spread stop-limit placed: id=%s", entry_label, put_stop_id)

    # --- 7. Place call spread stop-limit ---
    call_stop_order = _build_spread_stop_limit(
        strikes.short_call, strikes.long_call,
        stop_trigger, stop_limit,
        label=f"[{entry_label}] CALL",
    )
    call_resp = await _place_order_async(account, session, call_stop_order)
    call_stop_id = str(call_resp.order.id)
    log.info("[%s] Call spread stop-limit placed: id=%s", entry_label, call_stop_id)

    return EntryResult(
        entry_label=entry_label,
        ic_credit=fill_price,
        ic_order_id=order_id,
        put_stop_order_id=put_stop_id,
        call_stop_order_id=call_stop_id,
        strikes=strikes,
    )
