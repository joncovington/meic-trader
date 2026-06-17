"""
Runtime configuration.

Credentials are read from Windows Credential Manager via keyring (DPAPI).
Strategy settings are loaded from profiles/<name>.json.
The active profile name comes from settings.json; --profile CLI arg overrides it.
"""
import datetime
import json
from decimal import Decimal
from pathlib import Path

# ── paths ─────────────────────────────────────────────────────────────────────

_BASE_DIR     = Path(__file__).parent
PROFILES_DIR  = _BASE_DIR / "profiles"
SETTINGS_FILE = _BASE_DIR / "settings.json"
SESSION_CACHE = str(_BASE_DIR / "session_cache.json")
DB_PATH       = str(_BASE_DIR / "trades.db")
REPORTS_DIR   = _BASE_DIR / "reports"

# ── mode (set by meic.py CLI before any other module imports config values) ───

MODE: str = "sandbox"   # "sandbox" | "live"

# ── credentials (populated by load_credentials()) ────────────────────────────

TT_SECRET:      str = ""
TT_REFRESH:     str = ""
ACCOUNT_NUMBER: str = ""

# ── strategy constants (populated by load_profile()) ─────────────────────────

SYMBOL:             str     = "XSP"
TARGET_DELTA:       Decimal = Decimal("0.15")
WING_WIDTH:         Decimal = Decimal("3.00")
ENTRY_TIMES_ET:     list    = ["10:55", "11:25", "12:25", "13:35"]
QUANTITY:           int     = 1
EXPERIMENTAL:       bool    = False
STOP_TRIGGER_RATIO: Decimal = Decimal("0.90")
STOP_LIMIT_RATIO:   Decimal = Decimal("0.95")
MIN_CREDIT:         Decimal = Decimal("0.50")
MAX_CREDIT:         Decimal = Decimal("5.00")
BP_CHECK_ENABLED:   bool    = True
BP_BUFFER:          Decimal = Decimal("1.25")

# ── active profile name ───────────────────────────────────────────────────────

ACTIVE_PROFILE: str = "default"


def _keyring_prefix() -> str:
    return "meic_trader_sandbox" if MODE == "sandbox" else "meic_trader_live"


def load_credentials() -> None:
    """Read credentials from Windows Credential Manager into module-level vars."""
    global TT_SECRET, TT_REFRESH
    import keyring
    prefix = _keyring_prefix()
    TT_SECRET  = keyring.get_password(f"{prefix}_secret", "client_secret") or ""
    TT_REFRESH = keyring.get_password(f"{prefix}_token",  "refresh_token") or ""

    if not TT_SECRET or not TT_REFRESH:
        mode_label = "sandbox" if MODE == "sandbox" else "live"
        raise RuntimeError(
            f"{mode_label.capitalize()} credentials not found in Windows Credential Manager.\n"
            f"Run:  python meic.py secrets --{mode_label}"
        )


def get_active_profile_name(override: str | None = None) -> str:
    """Return the active profile name from settings.json, or override if provided."""
    if override:
        return override
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return data.get("active_profile", "default")
        except Exception:
            pass
    return "default"


def load_profile(name: str | None = None) -> dict:
    """Load and return raw profile dict from profiles/<name>.json."""
    global ACTIVE_PROFILE
    profile_name = get_active_profile_name(name)
    ACTIVE_PROFILE = profile_name
    path = PROFILES_DIR / f"{profile_name}.json"
    if not path.exists():
        raise RuntimeError(
            f"Profile '{profile_name}' not found at {path}.\n"
            "Run:  python meic.py config new"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def apply_profile(profile: dict) -> None:
    """Populate module-level strategy constants from a loaded profile dict."""
    global SYMBOL, TARGET_DELTA, WING_WIDTH, ENTRY_TIMES_ET, QUANTITY
    global EXPERIMENTAL, STOP_TRIGGER_RATIO, STOP_LIMIT_RATIO
    global MIN_CREDIT, MAX_CREDIT, BP_CHECK_ENABLED, BP_BUFFER

    SYMBOL             = profile["symbol"]
    TARGET_DELTA       = Decimal(str(profile["delta"]))
    WING_WIDTH         = Decimal(str(profile["wing_width"]))
    ENTRY_TIMES_ET     = profile["entry_times"]
    QUANTITY           = int(profile["quantity"])
    EXPERIMENTAL       = bool(profile.get("experimental", False))
    STOP_TRIGGER_RATIO = Decimal(str(profile.get("stop_trigger_ratio", "0.90")))
    STOP_LIMIT_RATIO   = Decimal(str(profile.get("stop_limit_ratio",   "0.95")))
    MIN_CREDIT         = Decimal(str(profile.get("min_credit",  "0.50")))
    MAX_CREDIT         = Decimal(str(profile.get("max_credit",  "5.00")))
    BP_CHECK_ENABLED   = bool(profile.get("bp_check_enabled", True))
    BP_BUFFER          = Decimal(str(profile.get("bp_buffer", "1.25")))


def init(mode: str = "sandbox", profile_override: str | None = None) -> None:
    """
    Full config initialisation: set mode, load credentials, load + apply profile.
    Called once by meic.py before anything else.
    """
    global MODE
    MODE = mode
    load_credentials()
    profile = load_profile(profile_override)
    apply_profile(profile)


# ── profile file helpers ──────────────────────────────────────────────────────

_PROFILE_DEFAULTS = {
    "symbol":             "XSP",
    "delta":              0.15,
    "wing_width":         3.0,
    "entry_times":        ["10:55", "11:25", "12:25", "13:35"],
    "quantity":           1,
    "experimental":       False,
    "min_credit":         0.50,
    "max_credit":         5.00,
    "bp_check_enabled":   True,
    "bp_buffer":          1.25,
    "stop_trigger_ratio": 0.90,
    "stop_limit_ratio":   0.95,
}


_MARKET_OPEN  = datetime.time(9, 30)
_MARKET_CLOSE = datetime.time(15, 55)


def validate_entry_times(times: list) -> str | None:
    """
    Return an error string if times is invalid, None if OK.
    Rules (always enforced, including experimental mode):
      - 1–8 entries
      - no duplicates
      - each must be a valid HH:MM
      - each must be within 09:30–15:55 ET
    """
    if not (1 <= len(times) <= 8):
        return f"entry_times must have 1–8 entries (got {len(times)})"
    if len(times) != len(set(times)):
        return "entry_times must not contain duplicates"
    for t in times:
        try:
            h, m = map(int, str(t).split(":"))
            et = datetime.time(h, m)
        except Exception:
            return f"'{t}' is not a valid HH:MM time"
        if not (_MARKET_OPEN <= et <= _MARKET_CLOSE):
            return f"'{t}' is outside market hours (09:30–15:55 ET)"
    return None


def save_profile(name: str, data: dict) -> None:
    PROFILES_DIR.mkdir(exist_ok=True)
    path = PROFILES_DIR / f"{name}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_settings(active_profile: str) -> None:
    SETTINGS_FILE.write_text(
        json.dumps({"active_profile": active_profile}, indent=2),
        encoding="utf-8",
    )


def list_profiles() -> list[str]:
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def default_profile_data() -> dict:
    return dict(_PROFILE_DEFAULTS)
