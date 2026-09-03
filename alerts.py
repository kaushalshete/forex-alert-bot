"""Condition evaluation for the two alert types."""
from datetime import timedelta

import config
from bot import alert_sentence
from db import iso, log, parse_iso, utcnow
from market_data import (BudgetExhausted, MarketDataError,
                         adaptive_zone_interval, grid_next_close)

# Enough history that a run skipped by a late or dropped cron trigger still
# has the bars needed to spot a break that happened in the gap.
CHOCH_LOOKBACK_BARS = 30
ZONE_MAX_BARS = 90
MAX_BACKOFF_SECONDS = 1800


def _floor_ts(alert):
    """Ignore anything that happened before the alert existed or was last seen."""
    seen = parse_iso(alert["last_bar_ts"])
    if seen:
        return seen
    return parse_iso(alert["created_at"])


def evaluate_choch(alert, series, now=None):
    """Return (fired_bar, newest_closed_ts).

    Bullish fires when two consecutive closed candles finish above the level and
    the candle immediately before them finished at or below it. Bearish mirrors
    that. Scanning every completed pattern in the window, rather than only the
    newest one, is what makes a missed run recoverable.
    """
    now = now or utcnow()
    bars = series.closed_bars(now)
    if len(bars) < 3:
        return None, (bars[-1].ts if bars else None)

    level = alert["price"]
    bullish = alert["alert_type"] == "choch_up"
    floor = _floor_ts(alert)

    fired = None
    for i in range(2, len(bars)):
        before, first, second = bars[i - 2], bars[i - 1], bars[i]
        if floor and second.ts <= floor:
            continue
        if bullish:
            broke = (before.close <= level and first.close > level
                     and second.close > level)
        else:
            broke = (before.close >= level and first.close < level
                     and second.close < level)
        if broke:
            fired = second
            break
    return fired, bars[-1].ts


def evaluate_zone(alert, series, now=None):
    """Return (fired_bar, newest_closed_ts, armed).

    A bar counts as inside when its high/low range overlaps the zone, so a wick
    through the zone between polls is still caught. `armed` carries the "was
    outside on the previous check" state across runs.
    """
    now = now or utcnow()
    bars = series.closed_bars(now)
    if not bars:
        return None, None, alert["armed"]

    low, high = alert["price_low"], alert["price_high"]
    armed = bool(alert["armed"])
    floor = _floor_ts(alert)

    fired = None
    for bar in bars:
        if floor and bar.ts <= floor:
            continue
        inside = bar.low <= high and bar.high >= low
        if inside:
            if armed and fired is None:
                fired = bar
        else:
            armed = True
    return fired, bars[-1].ts, int(armed)


def _data_timeframe(alert):
    # Zones are watched on 1 minute bars whatever timeframe the user named, so
    # a touch between polls is never missed. One request still covers the whole
    # gap, so the finer resolution costs no extra credits.
    return "1m" if alert["alert_type"] == "zone" else alert["timeframe"]


def _reschedule_failure(db, alert, exc):
    fail_count = alert["fail_count"] + 1
    delay = min(MAX_BACKOFF_SECONDS, 60 * (2 ** min(fail_count, 5)))
    db.update_alert(alert["id"], fail_count=fail_count,
                    next_check_at=iso(utcnow() + timedelta(seconds=delay)),
                    last_checked_at=iso(utcnow()))
    log("Alert %d check failed (%d in a row), retrying in %ds: %s"
        % (alert["id"], fail_count, delay, exc))


def check_due_alerts(db, tg, md, now=None):
    """Evaluate every alert whose next_check_at has passed. Returns count fired."""
    now = now or utcnow()
    due = db.due_alerts(now)
    if not due:
        return 0

    active_pairs = len({r["pair"] for r in db.active_alerts()})
    zone_interval = adaptive_zone_interval(db, active_pairs)

    # Group by pair and data timeframe so alerts sharing a chart share a request.
    groups = {}
    for alert in due:
        groups.setdefault((alert["pair"], _data_timeframe(alert)), []).append(alert)

    fired_total = 0
    for (pair, timeframe), alerts in sorted(groups.items()):
        outputsize = (min(ZONE_MAX_BARS, zone_interval // 60 + 10)
                      if timeframe == "1m" else CHOCH_LOOKBACK_BARS)
        try:
            series = md.series(pair, timeframe, outputsize=outputsize)
        except BudgetExhausted as exc:
            _pause_until_midnight(db, tg, alerts, exc)
            continue
        except MarketDataError as exc:
            for alert in alerts:
                _reschedule_failure(db, alert, exc)
            continue

        for alert in alerts:
            fired_total += _apply(db, tg, alert, series, zone_interval, now)
    return fired_total


def _apply(db, tg, alert, series, zone_interval, now):
    if alert["alert_type"] == "zone":
        fired, newest_ts, armed = evaluate_zone(alert, series, now)
        next_at = now + timedelta(seconds=zone_interval)
        updates = {"armed": armed}
    else:
        fired, newest_ts = evaluate_choch(alert, series, now)
        next_at = series.next_close_at(now)
        updates = {}

    updates["last_checked_at"] = iso(now)
    updates["fail_count"] = 0
    if newest_ts:
        updates["last_bar_ts"] = iso(newest_ts)

    if not fired:
        updates["next_check_at"] = iso(next_at)
        db.update_alert(alert["id"], **updates)
        return 0

    sentence = alert_sentence(alert, fired.close)
    delivered = tg.send_safe(alert["chat_id"], sentence)
    if not delivered:
        # Leave it active and retry shortly; an alert that never reached the
        # user must not be quietly marked as done.
        updates["next_check_at"] = iso(now + timedelta(seconds=60))
        db.update_alert(alert["id"], **updates)
        return 0

    updates["status"] = "triggered"
    updates["triggered_at"] = iso(now)
    updates["next_check_at"] = iso(now + timedelta(days=3650))
    db.update_alert(alert["id"], **updates)
    db.log_trigger(alert, sentence, fired.close)
    return 1


def _pause_until_midnight(db, tg, alerts, exc):
    """Out of data credits: stop retrying until the daily quota resets."""
    now = utcnow()
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    notified = set()
    for alert in alerts:
        db.update_alert(alert["id"], next_check_at=iso(midnight),
                        last_checked_at=iso(now))
        if alert["chat_id"] not in notified:
            tg.send_safe(alert["chat_id"], (
                "Market data budget for today is used up, so alert checks are "
                "paused until midnight UTC. Reduce the number of active alerts "
                "or raise the daily budget to avoid this."))
            notified.add(alert["chat_id"])
    log("Data budget exhausted, %d alerts paused until %s: %s"
        % (len(alerts), midnight.isoformat(), exc))


def reschedule_orphans(db):
    """Give any active alert a sane next check time if one is missing."""
    for alert in db.active_alerts():
        if not alert["next_check_at"]:
            tf = _data_timeframe(alert)
            db.update_alert(alert["id"], next_check_at=iso(grid_next_close(tf)))
