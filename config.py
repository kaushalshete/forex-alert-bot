"""Static configuration and env-driven settings.

Every tunable lives here so the workflow can override behaviour with plain
environment variables instead of code edits.
"""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


STATE_DIR = os.environ.get("STATE_DIR", "state")
DB_PATH = os.path.join(STATE_DIR, "alerts.db")
LOG_PATH = os.path.join(STATE_DIR, "alerts.log")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY", "").strip()

# 0 = single pass then exit (polite mode, relies purely on the 5 minute cron).
# >0 = keep ticking inside one job for this many minutes, giving 1 minute
# granularity that GitHub's cron scheduler cannot provide on its own.
LOOP_MINUTES = _int("LOOP_MINUTES", 0)
TICK_SECONDS = _int("TICK_SECONDS", 60)

# Twelve Data free tier: 800 credits/day, 8 requests/minute. Stay under both.
DAILY_CREDIT_BUDGET = _int("DAILY_CREDIT_BUDGET", 700)
REQUESTS_PER_MINUTE = _int("REQUESTS_PER_MINUTE", 7)

# Providers publish a bar a few seconds after the boundary; don't read too eagerly.
CANDLE_LAG_SECONDS = _int("CANDLE_LAG_SECONDS", 20)

# 0 = derive the zone polling interval from the remaining daily credit budget.
ZONE_POLL_SECONDS = _int("ZONE_POLL_SECONDS", 0)
ZONE_POLL_MIN_SECONDS = _int("ZONE_POLL_MIN_SECONDS", 60)
ZONE_POLL_MAX_SECONDS = _int("ZONE_POLL_MAX_SECONDS", 900)

# Single-price zone alerts expand by this many pips either side.
ZONE_PIP_BUFFER = _float("ZONE_PIP_BUFFER", 5.0)

# Off by default: the alert text is read aloud by TTS, and digits/symbols
# tacked on the end make for an ugly spoken sentence.
APPEND_PRICE_TO_ALERT = os.environ.get("APPEND_PRICE_TO_ALERT", "0") == "1"

GIT_PUSH = os.environ.get("GIT_PUSH", "1") == "1"

# The whitelist of tradable symbols, written the way Twelve Data names them.
# Nothing outside this list can be parsed out of a Telegram message or polled.
ALLOWED_PAIRS = [
    # Majors, metals and crypto.
    "EUR/USD", "GBP/USD", "AUD/USD", "NZD/USD",
    "USD/JPY", "USD/CAD", "USD/CHF",
    "XAU/USD", "XAG/USD", "BTC/USD",
    # Crosses.
    "EUR/GBP", "EUR/JPY", "EUR/CHF", "EUR/AUD", "EUR/CAD", "EUR/NZD",
    "GBP/JPY", "GBP/CHF", "GBP/AUD", "GBP/CAD", "GBP/NZD",
    "AUD/JPY", "AUD/CHF", "AUD/CAD", "AUD/NZD",
    "NZD/JPY", "NZD/CHF", "NZD/CAD",
    "CAD/JPY", "CAD/CHF", "CHF/JPY",
]

# Internally a pair is the slashless key ("EURUSD"); the slash only comes back
# when we talk to Twelve Data. Both views are derived from ALLOWED_PAIRS so the
# whitelist is edited in exactly one place.
PAIRS = {}
for _symbol in ALLOWED_PAIRS:
    _base, _quote = _symbol.split("/")
    PAIRS[_base + _quote] = (_base, _quote)

# A "pip" only means the usual thing for currencies. Metals and crypto move in
# far bigger increments, so ZONE_PIP_BUFFER would be meaningless without these.
PIP_SIZES = {
    "XAUUSD": 0.1,
    "XAGUSD": 0.01,
    "BTCUSD": 1.0,
}

TIMEFRAMES = {
    "1m":  {"td": "1min",  "seconds": 60,    "spoken": "1 minute"},
    "5m":  {"td": "5min",  "seconds": 300,   "spoken": "5 minute"},
    "15m": {"td": "15min", "seconds": 900,   "spoken": "15 minute"},
    "30m": {"td": "30min", "seconds": 1800,  "spoken": "30 minute"},
    "1h":  {"td": "1h",    "seconds": 3600,  "spoken": "1 hour"},
    "4h":  {"td": "4h",    "seconds": 14400, "spoken": "4 hour"},
    "1d":  {"td": "1day",  "seconds": 86400, "spoken": "daily"},
}

# Spoken forms and loose user input both map onto the canonical keys above.
TIMEFRAME_ALIASES = {
    "1m": "1m", "1min": "1m", "1minute": "1m", "m1": "1m", "1minutes": "1m",
    "5m": "5m", "5min": "5m", "5minute": "5m", "m5": "5m", "5minutes": "5m",
    "15m": "15m", "15min": "15m", "15minute": "15m", "m15": "15m", "15minutes": "15m",
    "30m": "30m", "30min": "30m", "30minute": "30m", "m30": "30m", "30minutes": "30m",
    "1h": "1h", "1hr": "1h", "1hour": "1h", "h1": "1h", "60m": "1h", "60min": "1h",
    "hourly": "1h", "1hours": "1h",
    "4h": "4h", "4hr": "4h", "4hour": "4h", "h4": "4h", "4hours": "4h", "240m": "4h",
    "1d": "1d", "1day": "1d", "d1": "1d", "daily": "1d", "day": "1d", "1days": "1d",
}


def pip_size(pair):
    """JPY crosses quote to 2 decimals, everything else to 4.

    Metals and crypto are the exceptions listed in PIP_SIZES.
    """
    if pair in PIP_SIZES:
        return PIP_SIZES[pair]
    return 0.01 if pair.endswith("JPY") else 0.0001


def spoken_pair(pair):
    """EURUSD -> 'EUR USD'. Separating the codes stops TTS reading gibberish."""
    base, quote = PAIRS.get(pair, (pair[:3], pair[3:]))
    return "%s %s" % (base, quote)


def td_symbol(pair):
    base, quote = PAIRS.get(pair, (pair[:3], pair[3:]))
    return "%s/%s" % (base, quote)
