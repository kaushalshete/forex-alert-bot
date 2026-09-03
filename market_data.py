"""Twelve Data integration and candle-close timing.

Candle boundary conventions differ per provider, and for 4h/1d they are not
documented in a way worth trusting. So nothing here assumes a boundary: the
bar interval and the next close are both derived from the timestamps the API
actually returns. Run `python main.py probe` once you have a key to see the
conventions your account gets.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config
from db import log, utcnow

BASE_URL = "https://api.twelvedata.com"
USER_AGENT = "forex-alert-bot/1.0"


class MarketDataError(Exception):
    """Recoverable data problem. Retry on the next run rather than crashing."""


class RateLimited(MarketDataError):
    pass


class BudgetExhausted(MarketDataError):
    pass


class Bar:
    __slots__ = ("ts", "open", "high", "low", "close")

    def __init__(self, ts, o, h, low, c):
        self.ts = ts        # bar OPEN time, UTC
        self.open = o
        self.high = h
        self.low = low
        self.close = c

    def __repr__(self):
        return "Bar(%s o=%s h=%s l=%s c=%s)" % (
            self.ts.isoformat(), self.open, self.high, self.low, self.close)


class Series:
    """A window of bars plus the interval observed in the data itself."""

    def __init__(self, pair, timeframe, bars):
        self.pair = pair
        self.timeframe = timeframe
        self.bars = bars  # ascending by ts
        self.interval_seconds = _observed_interval(bars, timeframe)

    def closed_bars(self, now=None, lag=None):
        """Bars whose close boundary is safely in the past.

        The newest bar the API returns is usually still forming, and providers
        publish a few seconds late, hence the lag cushion.
        """
        now = now or utcnow()
        lag = config.CANDLE_LAG_SECONDS if lag is None else lag
        cutoff = now - timedelta(seconds=lag)
        return [b for b in self.bars
                if b.ts + timedelta(seconds=self.interval_seconds) <= cutoff]

    def next_close_at(self, now=None):
        """When the bar currently forming is expected to close."""
        now = now or utcnow()
        if not self.bars:
            return now + timedelta(seconds=config.TICK_SECONDS)
        step = timedelta(seconds=self.interval_seconds)
        boundary = self.bars[-1].ts + step
        # If the newest bar already closed, the provider is behind; step forward
        # from its boundary until we land in the future.
        guard = 0
        while boundary <= now and guard < 500:
            boundary += step
            guard += 1
        return boundary + timedelta(seconds=config.CANDLE_LAG_SECONDS)


def _observed_interval(bars, timeframe):
    """Median gap between consecutive bars, falling back to the configured value.

    Median rather than mean so weekend gaps and session breaks do not skew it.
    """
    configured = config.TIMEFRAMES[timeframe]["seconds"]
    if len(bars) < 3:
        return configured
    gaps = sorted((bars[i + 1].ts - bars[i].ts).total_seconds()
                  for i in range(len(bars) - 1))
    median = gaps[len(gaps) // 2]
    if median <= 0:
        return configured
    return int(median)


def _parse_dt(text):
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise MarketDataError("unrecognised datetime from provider: %r" % text)


class MarketData:
    def __init__(self, database, api_key=None):
        self.db = database
        self.api_key = api_key or config.TWELVEDATA_API_KEY
        self._cache = {}       # (pair, timeframe) -> (fetched_at, Series)
        self._request_times = []

    # ---------- budget and throttling ----------

    def credits_left(self):
        return max(0, config.DAILY_CREDIT_BUDGET - self.db.credits_used_today())

    def _throttle(self):
        """Stay under the free tier's per-minute request cap."""
        window = 60.0
        now = time.time()
        self._request_times = [t for t in self._request_times if now - t < window]
        if len(self._request_times) >= config.REQUESTS_PER_MINUTE:
            wait = window - (now - self._request_times[0]) + 0.5
            if wait > 0:
                log("Rate limit cushion: sleeping %.1fs" % wait)
                time.sleep(wait)
            now = time.time()
            self._request_times = [t for t in self._request_times if now - t < window]
        self._request_times.append(time.time())

    def _request(self, path, params):
        if not self.api_key:
            raise MarketDataError("TWELVEDATA_API_KEY is not set")
        if self.credits_left() <= 0:
            raise BudgetExhausted(
                "daily credit budget of %d exhausted" % config.DAILY_CREDIT_BUDGET)

        params = dict(params, apikey=self.api_key)
        url = "%s/%s?%s" % (BASE_URL, path, urllib.parse.urlencode(params))
        self._throttle()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 429:
                raise RateLimited("HTTP 429 from Twelve Data: %s" % body)
            raise MarketDataError("HTTP %s from Twelve Data: %s" % (exc.code, body))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MarketDataError("network error calling Twelve Data: %s" % exc)
        except json.JSONDecodeError as exc:
            raise MarketDataError("non-JSON response from Twelve Data: %s" % exc)

        self.db.add_credits(1)

        if isinstance(payload, dict) and payload.get("status") == "error":
            code = payload.get("code")
            message = payload.get("message", "unknown error")
            if code in (429, 430):
                raise RateLimited("Twelve Data rate limit: %s" % message)
            raise MarketDataError("Twelve Data error %s: %s" % (code, message))
        return payload

    # ---------- series ----------

    def series(self, pair, timeframe, outputsize=30, max_age=None):
        """Fetch (or reuse) a candle window for one pair and timeframe.

        The cache is what keeps several alerts on the same pair and timeframe
        from each burning a credit.
        """
        key = (pair, timeframe)
        max_age = config.TICK_SECONDS if max_age is None else max_age
        cached = self._cache.get(key)
        if cached and (time.time() - cached[0]) < max_age and len(cached[1].bars) >= outputsize:
            return cached[1]

        payload = self._request("time_series", {
            "symbol": config.td_symbol(pair),
            "interval": config.TIMEFRAMES[timeframe]["td"],
            "outputsize": max(outputsize, 5),
            "timezone": "UTC",
            "order": "ASC",
            "format": "JSON",
        })

        values = payload.get("values") if isinstance(payload, dict) else None
        if not values:
            raise MarketDataError("no candles returned for %s %s" % (pair, timeframe))

        bars = []
        for row in values:
            try:
                bars.append(Bar(
                    _parse_dt(row["datetime"]),
                    float(row["open"]), float(row["high"]),
                    float(row["low"]), float(row["close"])))
            except (KeyError, TypeError, ValueError) as exc:
                raise MarketDataError("malformed candle for %s %s: %s"
                                      % (pair, timeframe, exc))
        bars.sort(key=lambda b: b.ts)

        series = Series(pair, timeframe, bars)
        self._cache[key] = (time.time(), series)
        return series

    def invalidate(self):
        self._cache.clear()


def grid_next_close(timeframe, now=None):
    """Fallback boundary on a plain UTC grid, used only before any data exists.

    Real scheduling comes from Series.next_close_at, which reads the provider's
    own timestamps instead of guessing.
    """
    now = now or utcnow()
    step = config.TIMEFRAMES[timeframe]["seconds"]
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = int((now - epoch).total_seconds())
    boundary = epoch + timedelta(seconds=((elapsed // step) + 1) * step)
    return boundary + timedelta(seconds=config.CANDLE_LAG_SECONDS)


def adaptive_zone_interval(database, active_pairs):
    """Spread the remaining daily credits across the pairs that need watching.

    Zone alerts read the 1 minute series, so one request covers every tick that
    happened since the last one. Polling less often costs detection latency, not
    accuracy, which is the right thing to trade away when credits are scarce.
    """
    if config.ZONE_POLL_SECONDS > 0:
        return config.ZONE_POLL_SECONDS
    pairs = max(1, active_pairs)
    left = max(1, config.DAILY_CREDIT_BUDGET - database.credits_used_today())
    now = utcnow()
    end_of_day = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    seconds_left = max(60.0, (end_of_day - now).total_seconds())
    per_pair = left / pairs
    interval = seconds_left / max(1.0, per_pair)
    return int(max(config.ZONE_POLL_MIN_SECONDS,
                   min(config.ZONE_POLL_MAX_SECONDS, interval)))


def probe(database, pair="EURUSD"):
    """Print what the provider actually does, so boundaries can be verified."""
    md = MarketData(database)
    print("Probing %s against Twelve Data (UTC now = %s)\n" % (pair, utcnow().isoformat()))
    for tf in config.TIMEFRAMES:
        try:
            series = md.series(pair, tf, outputsize=6, max_age=0)
        except MarketDataError as exc:
            print("%-4s ERROR %s" % (tf, exc))
            continue
        stamps = ", ".join(b.ts.strftime("%Y-%m-%d %H:%M") for b in series.bars[-4:])
        closed = series.closed_bars()
        print("%-4s observed interval %6ds  configured %6ds" % (
            tf, series.interval_seconds, config.TIMEFRAMES[tf]["seconds"]))
        print("     bar opens : %s" % stamps)
        print("     newest closed bar : %s" % (
            closed[-1].ts.strftime("%Y-%m-%d %H:%M") if closed else "none"))
        print("     next close expected : %s" % series.next_close_at().isoformat())
    print("\nCredits used today: %d of %d"
          % (database.credits_used_today(), config.DAILY_CREDIT_BUDGET))
