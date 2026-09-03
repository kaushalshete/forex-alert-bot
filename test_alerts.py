"""Offline tests for parsing and condition logic.

No network and no API key: candles are synthesised so the CHoCH and zone rules
can be checked deterministically. Run with: python test_alerts.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="fx-test-"))
os.environ["GIT_PUSH"] = "0"

import alerts as alerts_mod  # noqa: E402
import config  # noqa: E402
from bot import alert_sentence, fmt_price, parse_alert  # noqa: E402
from db import Database, iso  # noqa: E402
from market_data import Bar, Series, grid_next_close  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def eq(label, actual, expected):
    check(label, actual == expected, "(got %r, want %r)" % (actual, expected))


NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)


def make_series(pair, timeframe, closes, highs=None, lows=None, end=None):
    """Build bars ending well before NOW so every one counts as closed."""
    step = config.TIMEFRAMES[timeframe]["seconds"]
    end = end or (NOW - timedelta(seconds=step * 2))
    bars = []
    for i, close in enumerate(closes):
        ts = end - timedelta(seconds=step * (len(closes) - 1 - i))
        high = highs[i] if highs else close + 0.0005
        low = lows[i] if lows else close - 0.0005
        bars.append(Bar(ts, close, high, low, close))
    return Series(pair, timeframe, bars)


def alert(**kw):
    row = {
        "id": 1, "chat_id": 99, "pair": "EURUSD", "timeframe": "15m",
        "alert_type": "choch_up", "price": None, "price_low": None,
        "price_high": None, "side": None, "status": "active", "armed": 0,
        "last_bar_ts": None, "created_at": iso(NOW - timedelta(days=1)),
        "next_check_at": iso(NOW), "last_checked_at": None,
        "triggered_at": None, "fail_count": 0,
    }
    row.update(kw)
    return row


# --------------------------------------------------------------------------

def test_parsing():
    print("\nMessage parsing")

    p = parse_alert("EURUSD 15m choch above 1.1620")
    eq("pair", p["pair"], "EURUSD")
    eq("timeframe", p["timeframe"], "15m")
    eq("type", p["alert_type"], "choch")
    eq("direction", p["direction"], "above")
    eq("price", p["prices"], [1.1620])
    eq("nothing missing", p["missing"], [])

    p = parse_alert("change of character below 1.2700 on the 1 hour for GBP/USD")
    eq("word order free: pair", p["pair"], "GBPUSD")
    eq("word order free: timeframe", p["timeframe"], "1h")
    eq("word order free: direction", p["direction"], "below")
    eq("word order free: price", p["prices"], [1.27])

    p = parse_alert("usdjpy 4hr two candle close above 150.20")
    eq("jpy pair", p["pair"], "USDJPY")
    eq("4h alias", p["timeframe"], "4h")
    eq("2 candle keyword", p["alert_type"], "choch")
    eq("jpy price", p["prices"], [150.20])

    p = parse_alert("eurusd 15min sell zone 1.1590 to 1.1600")
    eq("zone type", p["alert_type"], "zone")
    eq("zone side", p["side"], "sell")
    eq("zone range", p["prices"], [1.1590, 1.1600])

    p = parse_alert("GBPUSD daily buy level 1.2700")
    eq("daily alias", p["timeframe"], "1d")
    eq("level keyword is a zone", p["alert_type"], "zone")
    eq("buy side", p["side"], "buy")

    p = parse_alert("EURUSD 15m choch 1.1620")
    eq("ambiguous direction is reported, not guessed", p["missing"], ["direction"])

    p = parse_alert("choch above 1.1620 15m")
    eq("missing pair", p["missing"], ["pair"])

    p = parse_alert("EURUSD 15m choch above")
    eq("missing price", p["missing"], ["price"])

    p = parse_alert("EURUSD choch above 1.1620")
    eq("missing timeframe", p["missing"], ["timeframe"])

    p = parse_alert("EURUSD 15m 1.1620")
    eq("missing alert type", p["missing"], ["alert_type"])

    p = parse_alert("XYZABC 15m choch above 1.1620")
    eq("unknown pair rejected by whitelist", p["pair"], None)

    p = parse_alert("EURUSD 15m choch above 1.1620")
    eq("timeframe digits never read as a price", p["prices"], [1.1620])

    p = parse_alert("today eurusd 15m zone 1.1600")
    eq("'today' does not match the day timeframe", p["timeframe"], "15m")


def test_choch():
    print("\nChange of character")
    level = 1.1620

    a = alert(alert_type="choch_up", price=level)
    s = make_series("EURUSD", "15m", [1.1600, 1.1610, 1.1625, 1.1630])
    fired, newest = alerts_mod.evaluate_choch(a, s, NOW)
    check("bullish fires on two closes above after one at or below",
          fired is not None and fired.close == 1.1630)

    s = make_series("EURUSD", "15m", [1.1600, 1.1610, 1.1625, 1.1615])
    fired, _ = alerts_mod.evaluate_choch(a, s, NOW)
    check("no fire when only one candle closes above", fired is None)

    s = make_series("EURUSD", "15m", [1.1630, 1.1640, 1.1650, 1.1660])
    fired, _ = alerts_mod.evaluate_choch(a, s, NOW)
    check("no fire when price was already above (no break)", fired is None)

    b = alert(alert_type="choch_down", price=level)
    s = make_series("EURUSD", "15m", [1.1640, 1.1630, 1.1615, 1.1610])
    fired, _ = alerts_mod.evaluate_choch(b, s, NOW)
    check("bearish fires on two closes below", fired is not None)

    s = make_series("EURUSD", "15m", [1.1640, 1.1630, 1.1615, 1.1625])
    fired, _ = alerts_mod.evaluate_choch(b, s, NOW)
    check("bearish does not fire on a single close below", fired is None)

    # A break older than the alert must be ignored.
    s = make_series("EURUSD", "15m", [1.1600, 1.1610, 1.1625, 1.1630])
    c = alert(alert_type="choch_up", price=level,
              last_bar_ts=iso(s.bars[-1].ts))
    fired, _ = alerts_mod.evaluate_choch(c, s, NOW)
    check("already evaluated bars never re-fire", fired is None)

    # A break several bars back is still caught after a missed run.
    s = make_series("EURUSD", "15m",
                    [1.1600, 1.1610, 1.1625, 1.1630, 1.1635, 1.1640, 1.1645])
    d = alert(alert_type="choch_up", price=level)
    fired, _ = alerts_mod.evaluate_choch(d, s, NOW)
    check("break inside a coverage gap is recovered",
          fired is not None and fired.close == 1.1630)

    s = make_series("EURUSD", "15m", [1.1600, 1.1610])
    fired, _ = alerts_mod.evaluate_choch(a, s, NOW)
    check("fewer than three closed bars is a no-op", fired is None)

    # The forming bar must never be treated as closed. Here the newest bar
    # opened 30s ago, so the one before it closed 30s ago: past the lag cushion.
    step = config.TIMEFRAMES["15m"]["seconds"]
    s = make_series("EURUSD", "15m", [1.1600, 1.1610, 1.1625, 1.1630],
                    end=NOW - timedelta(seconds=30))
    eq("newest bar excluded while still forming", len(s.closed_bars(NOW)), 3)

    # A bar that closed inside the lag cushion is not read yet either.
    s = make_series("EURUSD", "15m", [1.1600, 1.1610, 1.1625, 1.1630],
                    end=NOW - timedelta(seconds=5))
    eq("a bar that just closed waits for the provider to publish",
       len(s.closed_bars(NOW)), 2)
    check("observed interval derived from data", s.interval_seconds == step)


def test_zone():
    print("\nZone")
    low, high = 1.1590, 1.1600

    a = alert(alert_type="zone", price_low=low, price_high=high, armed=0)
    s = make_series("EURUSD", "1m", [1.1650, 1.1640, 1.1630],
                    highs=[1.1655, 1.1645, 1.1635], lows=[1.1645, 1.1635, 1.1625])
    fired, newest, armed = alerts_mod.evaluate_zone(a, s, NOW)
    check("outside the zone does not fire", fired is None)
    eq("outside the zone arms the alert", armed, 1)

    a = alert(alert_type="zone", price_low=low, price_high=high, armed=1)
    s = make_series("EURUSD", "1m", [1.1620, 1.1610, 1.1595],
                    highs=[1.1625, 1.1615, 1.1600], lows=[1.1615, 1.1605, 1.1590])
    fired, _, _ = alerts_mod.evaluate_zone(a, s, NOW)
    check("armed alert fires on entry into the zone", fired is not None)

    a = alert(alert_type="zone", price_low=low, price_high=high, armed=0)
    s = make_series("EURUSD", "1m", [1.1595, 1.1596, 1.1597],
                    highs=[1.1600, 1.1601, 1.1602], lows=[1.1590, 1.1591, 1.1592])
    fired, _, armed = alerts_mod.evaluate_zone(a, s, NOW)
    check("an unarmed alert already inside the zone stays quiet", fired is None)
    eq("and stays unarmed", armed, 0)

    # A wick through the zone between polls must still count.
    a = alert(alert_type="zone", price_low=low, price_high=high, armed=1)
    s = make_series("EURUSD", "1m", [1.1640, 1.1630, 1.1640],
                    highs=[1.1645, 1.1635, 1.1645], lows=[1.1635, 1.1598, 1.1635])
    fired, _, _ = alerts_mod.evaluate_zone(a, s, NOW)
    check("a wick into the zone fires even though no bar closed inside",
          fired is not None)

    a = alert(alert_type="zone", price_low=low, price_high=high, armed=1,
              last_bar_ts=iso(s.bars[-1].ts))
    fired, _, _ = alerts_mod.evaluate_zone(a, s, NOW)
    check("bars already evaluated do not re-fire", fired is None)


def test_sentences():
    print("\nSpoken alert text")
    s = alert_sentence(alert(alert_type="choch_up", price=1.1620,
                             pair="EURUSD", timeframe="15m"))
    eq("choch sentence", s, "Change of character has happened in EUR USD 15 minute.")

    s = alert_sentence(alert(alert_type="zone", pair="GBPUSD", timeframe="15m",
                             side="sell", price_low=1.27, price_high=1.2705))
    eq("zone sentence", s, "GBP USD 15 minute sell level is approaching.")

    s = alert_sentence(alert(alert_type="zone", pair="GBPUSD", timeframe="4h",
                             side=None, price_low=1.27, price_high=1.2705))
    eq("zone sentence without a side", s,
       "GBP USD 4 hour level is approaching.")

    s = alert_sentence(alert(alert_type="choch_down", pair="USDJPY",
                             timeframe="1d", price=150.2))
    eq("daily is spoken as daily", s,
       "Change of character has happened in USD JPY daily.")

    for text in [alert_sentence(alert(alert_type="choch_up", price=1.16)),
                 alert_sentence(alert(alert_type="zone", side="buy",
                                      price_low=1.1, price_high=1.2))]:
        check("no symbols in %r" % text,
              all(ch.isalnum() or ch in " ." for ch in text))

    eq("price formatting, 5 dp", fmt_price("EURUSD", 1.162), "1.16200")
    eq("price formatting, 3 dp for JPY", fmt_price("USDJPY", 150.2), "150.200")


def test_database():
    print("\nDatabase")
    path = os.path.join(os.environ["STATE_DIR"], "test.db")
    if os.path.exists(path):
        os.remove(path)
    db = Database(path)

    aid = db.add_alert(42, "EURUSD", "15m", "choch_up", price=1.162,
                       next_check_at=NOW - timedelta(minutes=1))
    check("alert stored", db.get_alert(aid) is not None)
    eq("scoped by chat", db.get_alert(aid, 43), None)
    eq("listed as active", len(db.list_alerts(42, "active")), 1)
    eq("due for checking", len(db.due_alerts(NOW)), 1)

    db.update_alert(aid, status="triggered")
    eq("triggered alerts drop out of the active list",
       len(db.list_alerts(42, "active")), 0)
    db.update_alert(aid, status="active", next_check_at=iso(NOW))
    eq("reset restores it", len(db.list_alerts(42, "active")), 1)

    db.set_offset(5001)
    eq("offset persisted", db.get_offset(), 5001)
    db.add_credits(3)
    eq("credits counted", db.credits_used_today(), 3)

    db.set_pending(42, '{"text": "eurusd 15m choch 1.16"}', ["direction"])
    check("pending stored", db.get_pending(42) is not None)
    db.clear_pending(42)
    eq("pending cleared", db.get_pending(42), None)

    zid = db.add_alert(42, "GBPUSD", "15m", "zone", price_low=1.27,
                       price_high=1.2705, side="sell", next_check_at=NOW)
    row = db.get_alert(zid)
    eq("zone bounds stored", (row["price_low"], row["price_high"]), (1.27, 1.2705))
    eq("choch row keeps a null zone", db.get_alert(aid)["price_low"], None)
    db.close()


def test_scheduling():
    print("\nScheduling")
    boundary = grid_next_close("15m", datetime(2026, 9, 3, 12, 7, 0, tzinfo=timezone.utc))
    eq("15m grid boundary", boundary.strftime("%H:%M:%S"),
       "12:15:%02d" % config.CANDLE_LAG_SECONDS)

    boundary = grid_next_close("1h", datetime(2026, 9, 3, 12, 59, 0, tzinfo=timezone.utc))
    eq("1h grid boundary", boundary.strftime("%H:%M:%S"),
       "13:00:%02d" % config.CANDLE_LAG_SECONDS)

    # Real scheduling reads the provider's own timestamps rather than a grid.
    s = make_series("EURUSD", "4h", [1.16, 1.161, 1.162, 1.163],
                    end=NOW - timedelta(hours=1))
    expected = s.bars[-1].ts + timedelta(seconds=s.interval_seconds
                                         + config.CANDLE_LAG_SECONDS)
    eq("next close derived from the newest bar", s.next_close_at(NOW), expected)

    # A provider lagging behind must not produce a boundary in the past.
    s = make_series("EURUSD", "15m", [1.16, 1.161, 1.162, 1.163],
                    end=NOW - timedelta(hours=3))
    check("stale data still yields a future boundary", s.next_close_at(NOW) > NOW)


class FakeTelegram:
    """Records outgoing messages instead of calling the Bot API."""

    def __init__(self):
        self.sent = []

    def send_safe(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True

    send_message = send_safe

    def last(self):
        return self.sent[-1][1] if self.sent else ""


class FakeMarketData:
    """Serves canned candles keyed by pair and timeframe."""

    def __init__(self, canned):
        self.canned = canned
        self.requests = []

    def series(self, pair, timeframe, outputsize=30, max_age=None):
        self.requests.append((pair, timeframe))
        return self.canned[(pair, timeframe)]

    def invalidate(self):
        pass


def test_end_to_end():
    print("\nEnd to end")
    path = os.path.join(os.environ["STATE_DIR"], "e2e.db")
    if os.path.exists(path):
        os.remove(path)
    db = Database(path)
    tg = FakeTelegram()

    import bot as bot_mod

    # Registering a CHoCH needs no market data at all.
    md = FakeMarketData({})
    bot_mod.handle_message(db, tg, md, {
        "chat": {"id": 7}, "text": "EURUSD 15m choch above 1.1620"})
    rows = db.list_alerts(7, "active")
    eq("one alert registered", len(rows), 1)
    check("confirmation mentions the level", "1.16200" in tg.last())
    eq("no market data spent registering a choch", md.requests, [])

    # A message missing the direction is asked about, not guessed at.
    bot_mod.handle_message(db, tg, md, {
        "chat": {"id": 7}, "text": "GBPUSD 1h choch 1.2700"})
    check("clarification requested", "above or below" in tg.last())
    eq("nothing registered yet", len(db.list_alerts(7, "active")), 1)
    check("pending recorded", db.get_pending(7) is not None)

    # The one word reply is stitched onto the earlier message.
    bot_mod.handle_message(db, tg, md, {"chat": {"id": 7}, "text": "below"})
    rows = db.list_alerts(7, "active")
    eq("clarified alert registered", len(rows), 2)
    gbp = [r for r in rows if r["pair"] == "GBPUSD"][0]
    eq("direction taken from the reply", gbp["alert_type"], "choch_down")
    eq("price carried over from the first message", gbp["price"], 1.27)

    # Now make EURUSD break its level and check the alert fires.
    db.update_alert(rows[0]["id"], next_check_at=iso(NOW - timedelta(minutes=1)))
    db.update_alert(gbp["id"], next_check_at=iso(NOW + timedelta(days=1)))
    md = FakeMarketData({
        ("EURUSD", "15m"): make_series(
            "EURUSD", "15m", [1.1600, 1.1610, 1.1625, 1.1630]),
    })
    fired = alerts_mod.check_due_alerts(db, tg, md, NOW)
    eq("one alert fired", fired, 1)
    eq("spoken sentence delivered", tg.last(),
       "Change of character has happened in EUR USD 15 minute.")
    eq("marked triggered", db.get_alert(rows[0]["id"])["status"], "triggered")
    eq("only the due alert consumed data", md.requests, [("EURUSD", "15m")])

    # It must not fire a second time.
    before = len(tg.sent)
    alerts_mod.check_due_alerts(db, tg, md, NOW)
    eq("a triggered alert stays quiet", len(tg.sent), before)

    # /list, /reset and /cancel.
    bot_mod.handle_message(db, tg, md, {"chat": {"id": 7}, "text": "/list"})
    check("list shows the remaining active alert", "GBPUSD" in tg.last())
    bot_mod.handle_message(db, tg, md, {
        "chat": {"id": 7}, "text": "/reset %d" % rows[0]["id"]})
    eq("reset reactivates", db.get_alert(rows[0]["id"])["status"], "active")
    eq("reset clears the evaluated bar marker",
       db.get_alert(rows[0]["id"])["last_bar_ts"], None)
    bot_mod.handle_message(db, tg, md, {
        "chat": {"id": 7}, "text": "/cancel %d" % rows[0]["id"]})
    eq("cancel deactivates", db.get_alert(rows[0]["id"])["status"], "cancelled")
    bot_mod.handle_message(db, tg, md, {
        "chat": {"id": 7}, "text": "/cancel 999"})
    check("cancelling someone else's alert is refused",
          "No alert 999" in tg.last())

    # Two alerts sharing a chart must cost only one request.
    md = FakeMarketData({
        ("EURUSD", "15m"): make_series(
            "EURUSD", "15m", [1.1600, 1.1610, 1.1615, 1.1618]),
    })
    for level in (1.1650, 1.1660):
        db.add_alert(7, "EURUSD", "15m", "choch_up", price=level,
                     next_check_at=NOW - timedelta(minutes=1))
    alerts_mod.check_due_alerts(db, tg, md, NOW)
    eq("requests are grouped by pair and timeframe", len(md.requests), 1)
    db.close()


def main():
    test_parsing()
    test_choch()
    test_zone()
    test_sentences()
    test_database()
    test_scheduling()
    test_end_to_end()
    print("\n%s" % ("All checks passed." if not FAILURES
                    else "%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES))))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
