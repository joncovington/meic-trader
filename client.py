"""
tastytrade sandbox session management.

Responsibilities
────────────────
• Authenticate via OAuth (SDK >= 12: Session(is_test=True)).
• Persist the session to disk so restarts reuse the existing token.
• Run a background keepalive that calls session.refresh() two minutes
  before the 15-minute session token expires.
• Expose get_session() / get_account() that retry once on any auth error,
  re-authenticating transparently.
• Provide a with_session() async context-manager decorator for callers that
  want automatic re-auth on httpx 401 responses.

Token lifecycle
───────────────
  session_token   : lives 15 min  (auto-refreshed by SDK on every request,
                                   and proactively by our keepalive task)
  refresh_token   : does not expire (TT_REFRESH env var)
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import AsyncIterator, Callable, TypeVar

import httpx
from tastytrade import Session
from tastytrade.account import Account

import config
from logger import log

# ── constants ────────────────────────────────────────────────────────────────

# Refresh the session token this many seconds before it expires.
_REFRESH_BUFFER_SECS = 120
# How often the keepalive loop wakes to check the expiry clock.
_KEEPALIVE_INTERVAL_SECS = 60
# Maximum re-auth attempts before giving up.
_MAX_REAUTH_ATTEMPTS = 3

# ── module-level singletons ───────────────────────────────────────────────────

_session:          Session | None = None
_account:          Account | None = None
_keepalive_task:   asyncio.Task | None = None
_auth_lock:        asyncio.Lock = asyncio.Lock()

T = TypeVar("T")

# ── session persistence ───────────────────────────────────────────────────────

def _cache_path() -> Path | None:
    if not config.SESSION_CACHE:
        return None
    return Path(config.SESSION_CACHE)


def _save_session(session: Session) -> None:
    path = _cache_path()
    if path is None:
        return
    try:
        data = session.serialize()
        path.write_text(json.dumps(data), encoding="utf-8")
        log.debug("Session token cached to %s", path)
    except Exception:
        log.warning("Could not save session cache.", exc_info=True)


def _load_cached_session() -> Session | None:
    path = _cache_path()
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        session = Session.deserialize(data)
        log.debug("Loaded cached session from %s", path)
        return session
    except Exception:
        log.warning("Cached session unreadable — will re-authenticate.", exc_info=True)
        return None


def _clear_session_cache() -> None:
    path = _cache_path()
    if path and path.exists():
        path.unlink(missing_ok=True)
        log.debug("Session cache cleared.")

# ── authentication ────────────────────────────────────────────────────────────

def _credentials_present() -> bool:
    return bool(config.TT_SECRET and config.TT_REFRESH)


async def _authenticate() -> Session:
    """Create a brand-new Session from OAuth credentials."""
    if not _credentials_present():
        raise RuntimeError(
            "Credentials not found in Windows Credential Manager.\n"
            "Run:  python meic.py secrets --sandbox  (or --live)"
        )
    is_sandbox = (config.MODE == "sandbox")
    log.info("Authenticating with tastytrade %s (OAuth)...", "sandbox" if is_sandbox else "live")
    loop = asyncio.get_event_loop()
    session: Session = await loop.run_in_executor(
        None,
        lambda: Session(
            config.TT_SECRET,
            config.TT_REFRESH,
            is_test=is_sandbox,
        ),
    )
    # Session() sets a dummy token; call refresh() to exchange credentials for a real token.
    refresh_result = session.refresh()
    if asyncio.iscoroutine(refresh_result):
        await refresh_result
    log.info("Authentication successful. Token expires at: %s",
             _fmt_expiry(session.session_expiration))
    _save_session(session)
    return session


async def _validate_session(session: Session) -> bool:
    """Refresh the session token; return True on success, False on any error."""
    try:
        refresh_result = session.refresh()
        if asyncio.iscoroutine(refresh_result):
            await refresh_result
        return True
    except Exception:
        log.debug("Session refresh failed during validation.", exc_info=True)
        return False


def _fmt_expiry(ts: float) -> str:
    if ts == 0.0:
        return "unknown"
    import datetime
    import pytz
    return datetime.datetime.fromtimestamp(ts, pytz.timezone("America/New_York")).strftime(
        "%H:%M:%S %Z"
    )

# ── keepalive background task ─────────────────────────────────────────────────

async def _keepalive_loop() -> None:
    """
    Wakes every minute and proactively refreshes the session token when it
    is within _REFRESH_BUFFER_SECS of expiry.  Saves the updated token to disk.
    """
    log.debug("Keepalive task started.")
    while True:
        await asyncio.sleep(_KEEPALIVE_INTERVAL_SECS)
        sess = _session
        if sess is None:
            continue
        secs_left = sess.session_expiration - time.time()
        if secs_left <= _REFRESH_BUFFER_SECS:
            log.info(
                "Session token expires in %.0fs — refreshing proactively...", secs_left
            )
            try:
                refresh_result = sess.refresh()
                if asyncio.iscoroutine(refresh_result):
                    await refresh_result
                log.info(
                    "Token refreshed. New expiry: %s",
                    _fmt_expiry(sess.session_expiration),
                )
                _save_session(sess)
            except Exception:
                log.warning("Proactive token refresh failed.", exc_info=True)

# ── public API ────────────────────────────────────────────────────────────────

async def get_session(*, force_reauth: bool = False) -> Session:
    """
    Return a live, validated Session.

    • On first call: tries the on-disk cache, validates it, falls back to
      full re-authentication if the cache is stale or absent.
    • Starts the keepalive background task if not already running.
    • force_reauth=True skips the cache and re-authenticates immediately.
    """
    global _session, _keepalive_task

    async with _auth_lock:
        if force_reauth:
            log.info("Forced re-authentication requested.")
            _clear_session_cache()
            _session = None

        if _session is not None:
            return _session

        # Try cached session first
        if not force_reauth:
            cached = _load_cached_session()
            if cached is not None:
                log.info("Validating cached session...")
                if await _validate_session(cached):
                    log.info(
                        "Cached session is valid (expires %s).",
                        _fmt_expiry(cached.session_expiration),
                    )
                    _session = cached
                else:
                    log.info("Cached session rejected — re-authenticating.")
                    _clear_session_cache()

        if _session is None:
            _session = await _authenticate()

        # Start keepalive if needed
        if _keepalive_task is None or _keepalive_task.done():
            _keepalive_task = asyncio.create_task(
                _keepalive_loop(), name="session-keepalive"
            )

    return _session


async def get_account() -> Account:
    """Return the configured (or auto-selected) Account, cached after first call."""
    global _account
    if _account is not None:
        return _account

    session = await get_session()
    result = Account.get_accounts(session)
    accounts: list[Account] = await result if asyncio.iscoroutine(result) else result

    if not accounts:
        raise RuntimeError("No accounts found on this sandbox session.")

    if config.ACCOUNT_NUMBER:
        matched = [a for a in accounts if a.account_number == config.ACCOUNT_NUMBER]
        if not matched:
            raise RuntimeError(
                f"Account {config.ACCOUNT_NUMBER} not found. "
                f"Available: {[a.account_number for a in accounts]}"
            )
        _account = matched[0]
    else:
        _account = accounts[0]
        if len(accounts) > 1:
            log.warning(
                "Multiple accounts found %s — using %s. "
                "Set TASTYTRADE_ACCOUNT_NUMBER to pin one.",
                [a.account_number for a in accounts],
                _account.account_number,
            )

    log.info("Using account: %s", _account.account_number)
    return _account


# ── re-auth decorator ─────────────────────────────────────────────────────────

@asynccontextmanager
async def with_session() -> AsyncIterator[Session]:
    """
    Async context manager that yields a live session.
    On httpx.HTTPStatusError with status 401, re-authenticates once and retries.

    Usage:
        async with with_session() as sess:
            result = account.some_api_call(sess, ...)
    """
    for attempt in range(1, _MAX_REAUTH_ATTEMPTS + 1):
        sess = await get_session(force_reauth=(attempt > 1))
        try:
            yield sess
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401 and attempt < _MAX_REAUTH_ATTEMPTS:
                log.warning(
                    "Received 401 on attempt %d/%d — re-authenticating.",
                    attempt,
                    _MAX_REAUTH_ATTEMPTS,
                )
                global _session, _account
                _session = None
                _account = None  # account token tied to session
                continue
            raise


def reauth_on_401(async_fn: Callable[..., T]) -> Callable[..., T]:
    """
    Decorator version of with_session() for functions that call get_session()
    internally.  Catches 401 errors and retries with a fresh session.

    Usage:
        @reauth_on_401
        async def my_api_call():
            sess = await get_session()
            ...
    """
    @wraps(async_fn)
    async def wrapper(*args, **kwargs):
        for attempt in range(1, _MAX_REAUTH_ATTEMPTS + 1):
            if attempt > 1:
                await get_session(force_reauth=True)
            try:
                return await async_fn(*args, **kwargs)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401 and attempt < _MAX_REAUTH_ATTEMPTS:
                    log.warning(
                        "401 on %s attempt %d/%d — re-authenticating.",
                        async_fn.__name__, attempt, _MAX_REAUTH_ATTEMPTS,
                    )
                    global _session, _account
                    _session = None
                    _account = None
                    continue
                raise
    return wrapper  # type: ignore[return-value]


async def shutdown() -> None:
    """Cancel the keepalive task on clean shutdown."""
    global _keepalive_task
    if _keepalive_task and not _keepalive_task.done():
        _keepalive_task.cancel()
        try:
            await _keepalive_task
        except asyncio.CancelledError:
            pass
        log.debug("Keepalive task stopped.")
