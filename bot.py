"""Telegram polling, message parsing and command handling.

Parsing is plain keyword and regex extraction. Fields are pulled out
independently so word order never matters, and anything missing is asked for
by name rather than guessed at.
"""
import json
import re

import config
from db import iso, log, parse_iso, utcnow
from market_data import (BudgetExhausted, MarketData, MarketDataError,
                         adaptive_zone_interval, grid_next_close)
from telegram_api import TelegramError

CHOCH_KEYWORDS = [
    "change of character", "change in character", "changeofcharacter",
    "choch", "chock", "two candle", "2 candle", "2candle", "two candles",
    "2 candles", "double candle",
]
ZONE_KEYWORDS = ["zone", "level", "approaching", "approach"]

ABOVE_WORDS = ["above", "over", "bullish", "upside", "break up", "breakup", "up"]
BELOW_WORDS = ["below", "under", "bearish", "downside", "break down",
               "breakdown", "down"]

BUY_WORDS = ["buy", "demand", "support", "long", "bid"]
SELL_WORDS = ["sell", "supply", "resistance", "short", "offer"]

PENDING_TTL_SECONDS = 900

FIELD_PROMPTS = {
    "pair": "the currency pair, for example EURUSD",
    "price": "the price, written with a decimal point, for example 1.1620",
    "timeframe": "the timeframe, one of 1m 5m 15m 30m 1H 4H 1D",
    "alert_type": "the alert type, either change of character or zone",
    "direction": "the direction, either above or below",
}


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def normalize(text):
    """Lowercase, and glue a number to the unit that follows it."""
    text = (text or "").lower().strip()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(
        r"(\d+)\s+(m|mins?|minutes?|h|hrs?|hours?|d|days?)\b", r"\1\2", text)
    return text


def _find_pair(text):
    """Match against the whitelist rather than accepting any six letters."""
    best = None
    for pair in config.PAIRS:
        base, quote = config.PAIRS[pair]
        pattern = r"(?<![a-z])%s\s*[/\-]?\s*%s(?![a-z])" % (base.lower(), quote.lower())
        match = re.search(pattern, text)
        if match and (best is None or match.start() < best[1]):
            best = (pair, match.start(), match.end())
    if not best:
        return None, text
    return best[0], text[:best[1]] + " " + text[best[2]:]


def _find_timeframe(text):
    for alias in sorted(config.TIMEFRAME_ALIASES, key=len, reverse=True):
        pattern = r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(alias)
        match = re.search(pattern, text)
        if match:
            canonical = config.TIMEFRAME_ALIASES[alias]
            return canonical, text[:match.start()] + " " + text[match.end():]
    return None, text


def _strip_phrases(text, phrases):
    hits = []
    for phrase in sorted(phrases, key=len, reverse=True):
        pattern = r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(phrase)
        if re.search(pattern, text):
            hits.append(phrase)
            text = re.sub(pattern, " ", text)
    return hits, text


def parse_alert(text):
    """Extract every field independently. Returns a dict, never raises."""
    original = text
    text = normalize(text)
    result = {
        "pair": None, "timeframe": None, "alert_type": None, "direction": None,
        "side": None, "prices": [], "missing": [], "raw": original,
    }

    pair, text = _find_pair(text)
    result["pair"] = pair

    timeframe, text = _find_timeframe(text)
    result["timeframe"] = timeframe

    choch_hits, text = _strip_phrases(text, CHOCH_KEYWORDS)
    zone_hits, text = _strip_phrases(text, ZONE_KEYWORDS)
    # "choch above the level" mentions both; the more specific type wins.
    if choch_hits:
        result["alert_type"] = "choch"
    elif zone_hits:
        result["alert_type"] = "zone"

    above_hits, text = _strip_phrases(text, ABOVE_WORDS)
    below_hits, text = _strip_phrases(text, BELOW_WORDS)
    if above_hits and not below_hits:
        result["direction"] = "above"
    elif below_hits and not above_hits:
        result["direction"] = "below"

    buy_hits, text = _strip_phrases(text, BUY_WORDS)
    sell_hits, text = _strip_phrases(text, SELL_WORDS)
    if buy_hits and not sell_hits:
        result["side"] = "buy"
    elif sell_hits and not buy_hits:
        result["side"] = "sell"

    # A decimal point is required so timeframe digits and stray numbers can
    # never be mistaken for a price.
    result["prices"] = [float(p) for p in re.findall(r"\d+\.\d+", text)]

    missing = []
    if not result["pair"]:
        missing.append("pair")
    if not result["prices"]:
        missing.append("price")
    if not result["timeframe"]:
        missing.append("timeframe")
    if not result["alert_type"]:
        missing.append("alert_type")
    if result["alert_type"] == "choch" and not result["direction"]:
        missing.append("direction")
    result["missing"] = missing
    return result


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def fmt_price(pair, value):
    if value is None:
        return "-"
    return "%.3f" % value if pair.endswith("JPY") else "%.5f" % value


def describe_alert(row):
    pair, tf = row["pair"], row["timeframe"]
    if row["alert_type"] == "zone":
        side = (" " + row["side"]) if row["side"] else ""
        if row["price_low"] == row["price_high"]:
            body = "zone%s at %s" % (side, fmt_price(pair, row["price_low"]))
        else:
            body = "zone%s %s to %s" % (side, fmt_price(pair, row["price_low"]),
                                        fmt_price(pair, row["price_high"]))
    else:
        direction = "above" if row["alert_type"] == "choch_up" else "below"
        body = "change of character %s %s" % (direction, fmt_price(pair, row["price"]))
    return "%s %s %s %s" % (row["id"], pair, tf, body)


def alert_sentence(row, price=None):
    """The TTS payload. Plain sentences, no symbols, markdown or emoji."""
    spoken_tf = config.TIMEFRAMES[row["timeframe"]]["spoken"]
    pair = config.spoken_pair(row["pair"])
    if row["alert_type"] == "zone":
        side = (" " + row["side"]) if row["side"] else ""
        sentence = "%s %s%s level is approaching." % (pair, spoken_tf, side)
    else:
        sentence = "Change of character has happened in %s %s." % (pair, spoken_tf)
    if config.APPEND_PRICE_TO_ALERT and price is not None:
        sentence += " Price %s." % fmt_price(row["pair"], price)
    return sentence


HELP_TEXT = (
    "Send one message containing the pair, the price, the timeframe and the "
    "alert type. Order does not matter.\n\n"
    "Change of character:\n"
    "  EURUSD 15m choch above 1.1620\n"
    "  change of character GBPUSD below 1.2700 on the 1H\n\n"
    "Zone or level:\n"
    "  EURUSD 15m sell zone 1.1590 to 1.1600\n"
    "  GBPUSD 4H buy level 1.2700\n\n"
    "Prices must include a decimal point. A single zone price is widened by "
    "%s pips either side.\n\n"
    "Timeframes: 1m 5m 15m 30m 1H 4H 1D\n\n"
    "Commands:\n"
    "  /list shows your active alerts\n"
    "  /cancel 3 cancels alert 3\n"
    "  /reset 3 reactivates alert 3 after it has fired\n"
    "  /status shows the data budget\n"
    "  /help shows this message"
) % int(config.ZONE_PIP_BUFFER)

START_TEXT = (
    "Forex alert bot is running.\n\n"
    "It watches two things: a change of character, meaning two consecutive "
    "closed candles closing beyond a price you give, and a zone, meaning price "
    "trading into a range you give.\n\n" + HELP_TEXT
)


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def register_alert(db, tg, md, chat_id, parsed):
    """Turn a fully parsed message into a stored alert and confirm it."""
    pair = parsed["pair"]
    timeframe = parsed["timeframe"]
    prices = parsed["prices"]

    if parsed["alert_type"] == "choch":
        if len(prices) > 1:
            tg.send_safe(chat_id, "I found more than one price in that message. "
                                  "A change of character alert needs exactly one "
                                  "level. Please resend with a single price.")
            return None
        alert_type = "choch_up" if parsed["direction"] == "above" else "choch_down"
        alert_id = db.add_alert(
            chat_id, pair, timeframe, alert_type, price=prices[0],
            next_check_at=grid_next_close(timeframe))
        tg.send_safe(chat_id, (
            "Registered alert %d. %s %s change of character %s %s. I will tell "
            "you when two consecutive closed candles finish %s that level."
        ) % (alert_id, pair, timeframe, parsed["direction"],
             fmt_price(pair, prices[0]), parsed["direction"]))
        log("Registered alert %d for chat %s: %s %s %s %s"
            % (alert_id, chat_id, pair, timeframe, alert_type, prices[0]))
        return alert_id

    # zone
    if len(prices) >= 2:
        low, high = sorted(prices[:2])
    else:
        buffer_ = config.ZONE_PIP_BUFFER * config.pip_size(pair)
        low, high = prices[0] - buffer_, prices[0] + buffer_

    side = parsed["side"]
    inside_now = None
    try:
        series = md.series(pair, "1m", outputsize=5, max_age=0)
        last = series.bars[-1]
        if side is None:
            # A zone above current price is somewhere to sell into, below is
            # somewhere to buy from. Only used for the spoken wording.
            side = "sell" if low > last.close else ("buy" if high < last.close else None)
        inside_now = last.low <= high and last.high >= low
    except MarketDataError as exc:
        log("Could not read live price while registering zone: %s" % exc)

    alert_id = db.add_alert(
        chat_id, pair, timeframe, "zone", price_low=low, price_high=high,
        side=side, next_check_at=utcnow())
    if inside_now:
        # Arming only on a confirmed outside reading is what stops a zone that
        # already contains price from firing instantly and repeatedly.
        db.update_alert(alert_id, armed=0)

    label = ("at %s" % fmt_price(pair, low)) if low == high else (
        "%s to %s" % (fmt_price(pair, low), fmt_price(pair, high)))
    text = "Registered alert %d. %s %s %szone %s." % (
        alert_id, pair, timeframe, (side + " ") if side else "", label)
    if inside_now:
        text += (" Price is inside that zone right now, so this alert will fire "
                 "once price leaves the zone and comes back into it.")
    else:
        text += " I will tell you when price trades into it."
    tg.send_safe(chat_id, text)
    log("Registered alert %d for chat %s: %s %s zone %s-%s"
        % (alert_id, chat_id, pair, timeframe, low, high))
    return alert_id


def ask_for_missing(db, tg, chat_id, parsed, combined_text):
    understood = []
    if parsed["pair"]:
        understood.append("pair %s" % parsed["pair"])
    if parsed["timeframe"]:
        understood.append("timeframe %s" % parsed["timeframe"])
    if parsed["alert_type"]:
        understood.append("type %s" % (
            "change of character" if parsed["alert_type"] == "choch" else "zone"))
    if parsed["direction"]:
        understood.append("direction %s" % parsed["direction"])
    if parsed["prices"]:
        understood.append("price %s" % ", ".join(str(p) for p in parsed["prices"]))

    needed = [FIELD_PROMPTS[f] for f in parsed["missing"]]
    lines = []
    lines.append("I understood: %s." % ", ".join(understood) if understood
                 else "I could not read anything usable from that message.")
    lines.append("Still needed: %s." % "; ".join(needed))
    lines.append("Reply with just the missing part, or resend the whole alert.")
    tg.send_safe(chat_id, "\n".join(lines))
    db.set_pending(chat_id, json.dumps({"text": combined_text}), parsed["missing"])


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def handle_command(db, tg, md, chat_id, text):
    parts = text.split()
    command = parts[0].split("@")[0].lower()
    args = parts[1:]

    if command == "/start":
        tg.send_safe(chat_id, START_TEXT)
    elif command == "/help":
        tg.send_safe(chat_id, HELP_TEXT)
    elif command == "/list":
        rows = db.list_alerts(chat_id, status="active")
        if not rows:
            tg.send_safe(chat_id, "You have no active alerts.")
        else:
            body = "\n".join(describe_alert(r) for r in rows)
            tg.send_safe(chat_id, "Active alerts:\n" + body)
    elif command in ("/cancel", "/reset"):
        if not args or not args[0].isdigit():
            tg.send_safe(chat_id, "Give the alert number, for example %s 3. "
                                  "Use /list to see the numbers." % command)
            return
        alert_id = int(args[0])
        row = db.get_alert(alert_id, chat_id)
        if not row:
            tg.send_safe(chat_id, "No alert %d belongs to this chat." % alert_id)
            return
        if command == "/cancel":
            db.update_alert(alert_id, status="cancelled")
            tg.send_safe(chat_id, "Cancelled alert %d." % alert_id)
        else:
            db.update_alert(alert_id, status="active", armed=0, last_bar_ts=None,
                            triggered_at=None, fail_count=0,
                            next_check_at=iso(utcnow()))
            tg.send_safe(chat_id, "Reactivated alert %d. %s"
                         % (alert_id, describe_alert(db.get_alert(alert_id))))
    elif command == "/status":
        used = db.credits_used_today()
        active = db.list_alerts(chat_id, status="active")
        pairs = len({r["pair"] for r in db.active_alerts()})
        tg.send_safe(chat_id, (
            "Active alerts: %d\nData credits used today: %d of %d\n"
            "Zone polling interval: %d seconds"
        ) % (len(active), used, config.DAILY_CREDIT_BUDGET,
             adaptive_zone_interval(db, pairs)))
    else:
        tg.send_safe(chat_id, "I do not know that command. Send /help for usage.")


# --------------------------------------------------------------------------
# update handling
# --------------------------------------------------------------------------

def handle_message(db, tg, md, message):
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return

    if text.startswith("/"):
        db.clear_pending(chat_id)
        handle_command(db, tg, md, chat_id, text)
        return

    combined = text
    pending = db.get_pending(chat_id)
    if pending:
        created = parse_iso(pending["created_at"])
        fresh = created and (utcnow() - created).total_seconds() < PENDING_TTL_SECONDS
        if fresh:
            # Prefer a self-contained new attempt; only stitch the reply onto
            # the earlier message when it cannot stand alone.
            if parse_alert(text)["missing"]:
                try:
                    earlier = json.loads(pending["payload"]).get("text", "")
                except (ValueError, TypeError):
                    earlier = ""
                combined = (earlier + " " + text).strip()
        db.clear_pending(chat_id)

    parsed = parse_alert(combined)
    if parsed["missing"]:
        ask_for_missing(db, tg, chat_id, parsed, combined)
        return

    if len(parsed["prices"]) > 2:
        tg.send_safe(chat_id, "I found more than two prices in that message. "
                              "Please resend with one price, or two for a zone range.")
        return

    try:
        register_alert(db, tg, md, chat_id, parsed)
    except BudgetExhausted as exc:
        tg.send_safe(chat_id, "Registered nothing: the daily market data budget "
                              "is used up. Try again after midnight UTC.")
        log("Registration blocked: %s" % exc)


def poll_telegram(db, tg, md, timeout=0):
    """Drain pending updates, advancing the stored offset as we go."""
    offset = db.get_offset()
    try:
        updates = tg.get_updates(offset + 1 if offset else 0, timeout=timeout)
    except TelegramError as exc:
        log("getUpdates failed, will retry: %s" % exc)
        return 0

    handled = 0
    for update in updates or []:
        update_id = update.get("update_id")
        try:
            if "message" in update:
                handle_message(db, tg, md, update["message"])
            handled += 1
        except TelegramError as exc:
            log("Telegram error handling update %s: %s" % (update_id, exc))
        except MarketDataError as exc:
            log("Market data error handling update %s: %s" % (update_id, exc))
        except Exception as exc:  # never let one bad message stall the offset
            log("Unexpected error handling update %s: %r" % (update_id, exc))
        finally:
            # Advance regardless, so a message that always fails cannot wedge
            # the queue on every future run.
            if update_id is not None:
                db.set_offset(update_id)
    return handled
