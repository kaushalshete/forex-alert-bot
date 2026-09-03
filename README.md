# Telegram Forex Alert Bot

Two kinds of forex alert, delivered to Telegram as plain spoken sentences so a
phone-side automation app can read them aloud. Runs entirely on GitHub Actions.
No VM, no hosting, no dependencies beyond the Python standard library.

**Change of character** — fires when two consecutive closed candles on a
timeframe you choose close beyond a price level you give, having closed on the
other side of it immediately before.

**Zone or level** — fires when price trades into a range you give, having been
outside it beforehand.

---

## Read this before you set it up

**The repository must be public.** GitHub Actions bills every job rounded *up*
to the nearest minute, so even a four-second job costs a full minute. A
five-minute schedule is 8,640 billed minutes a month against a 2,000 minute
private-repo allowance. Public repositories get unlimited free minutes, which is
the only way this runs for free.

That means **your alert levels, pairs and Telegram chat ID are publicly
readable** in `state/alerts.db`. Your bot token and API key are *not* — they live
in encrypted repository secrets and never touch the code. If the alert data
itself is sensitive to you, this design is not right for you; move the state to a
private database instead.

**GitHub's cron cannot run every minute.** Five minutes is the floor, and
scheduled runs are best-effort: they routinely fire late and are sometimes
dropped entirely under load. This project works around that rather than pretending
otherwise:

- The cron entry is a *watchdog*. Each job then ticks internally every 60 seconds
  for 50 minutes, so a late trigger just extends coverage instead of skipping
  checks. Set `LOOP_MINUTES: '0'` in the workflow for a plain single-pass run every
  five minutes if you would rather stay closer to normal Actions usage.
- Condition checks scan backwards over a window of candles rather than only the
  newest one, so a break that happened during a coverage gap is still caught on the
  next run.

**Scheduled workflows are disabled after 60 days of repository inactivity**, and
the bot's own commits do not reliably reset that clock. If alerts go quiet for no
reason, check the Actions tab and press *Run workflow*.

---

## Setup

### 1. Create the Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, choose a display name and a username ending in `bot`.
3. BotFather replies with a token like `123456789:AAE...`. Keep it; it goes into
   a GitHub secret in step 3, not into any file.
4. Open a chat with your new bot and send it `/start` — a bot cannot message you
   until you have messaged it first.

### 2. Get a Twelve Data API key

1. Sign up at [twelvedata.com](https://twelvedata.com/pricing) and pick the free plan.
2. Copy the API key from the dashboard.

Free tier limits are **800 API credits per day** and **8 requests per minute**.
The bot tracks its own daily spend and throttles itself to stay inside both.

### 3. Add both as repository secrets

In your repository: **Settings → Secrets and variables → Actions → New repository
secret**. Add exactly these two names:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TWELVEDATA_API_KEY` | the key from Twelve Data |

Paste them into the GitHub web form only. Do not put them in a file, a commit, or
a chat message — anything committed to a public repo is permanently exposed even
if you delete it afterwards.

### 4. Push and let it run

```bash
git init
git add .
git commit -m "Forex alert bot"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

Then open **Actions**, enable workflows if prompted, and press *Run workflow* on
**Forex alerts** to start immediately rather than waiting for the next cron tick.
From then on it is automatic.

### 5. Confirm the candle boundaries (optional but recommended)

Twelve Data's 4H and 1D boundary conventions are not documented in a way worth
trusting, and this bot deliberately derives them from the timestamps the API
returns rather than assuming a grid. To see what your account actually gets:

```bash
TWELVEDATA_API_KEY=your-key python main.py probe EURUSD
```

It prints the observed bar interval, recent bar open times, the newest bar it
considers closed, and the next expected close, for every timeframe. Costs 7
credits.

---

## Using the bot

Send one message containing the pair, the price, the timeframe and the alert
type. Order does not matter, and casing does not matter.

```
EURUSD 15m choch above 1.1620
change of character GBPUSD below 1.2700 on the 1H
usdjpy 4hr two candle close above 150.20

EURUSD 15m sell zone 1.1590 to 1.1600
GBPUSD 4H buy level 1.2700
```

**Prices must contain a decimal point.** That is what lets the parser tell
`1.1620` apart from the `15` in `15m` without guessing.

A zone given as one price is widened by 5 pips either side (0.05 for JPY pairs).
Two prices are read as an explicit range.

If something is missing, the bot tells you what it understood and what it still
needs, then remembers the rest for 15 minutes — so a one-word `below` reply is
enough to finish an alert. It never guesses a direction.

### Commands

| Command | Effect |
|---|---|
| `/start` | Welcome and message format |
| `/help` | Usage examples |
| `/list` | Your active alerts, with their numbers |
| `/cancel 3` | Cancel alert 3 |
| `/reset 3` | Reactivate alert 3 after it has fired |
| `/status` | Alerts active, data credits used today, current zone polling interval |

### What the alerts sound like

Alert messages are plain sentences with no markdown, symbols or emoji, and the
currency codes are spaced so text-to-speech reads them properly:

```
Change of character has happened in EUR USD 15 minute.
GBP USD 15 minute sell level is approaching.
```

Confirmations and command replies are ordinary readable text. If your automation
app reads every message from the bot, filter it on the phrases
`Change of character` and `level is approaching` so it only speaks the alerts.

Set `APPEND_PRICE_TO_ALERT: '1'` in the workflow to append `Price 1.16200.` to
the spoken sentence. It is off by default because it makes the sentence worse to
listen to.

---

## How a run works

1. Read the SQLite state committed in `state/alerts.db`.
2. Poll Telegram with `getUpdates`, using an offset stored in the database so no
   message is ever processed twice.
3. Register any new alerts and reply to any commands.
4. For each alert whose `next_check_at` has passed, fetch candles and evaluate.
   Alerts sharing a pair and timeframe share one request.
5. Send any triggered alerts, mark them `triggered`, and recompute
   `next_check_at` for the rest from the provider's own bar timestamps.
6. Commit the database and log back to the repo.

Nothing is held in memory between runs. If the state commit fails, the run fails
loudly rather than silently losing alerts.

### Scheduling

Change-of-character alerts are checked just after each candle close on their own
timeframe, so a 4H alert costs six requests a day, not one a minute.

Zone alerts are watched on **1-minute candles** regardless of the timeframe you
named, and each request returns every bar since the last check. That means a
wick into your zone between polls is still caught — polling less often costs
detection *latency*, not accuracy. The interval adapts to the credits left in the
day and the number of pairs being watched, between 60 and 900 seconds. Pin it
with `ZONE_POLL_SECONDS` if you would rather it were fixed.

Roughly: one pair with a zone alert polled every 5 minutes costs about 290
credits a day. Two or three active pairs fit inside the free tier comfortably;
much more than that and the interval will stretch on its own.

---

## Configuration

All set in `env:` in `.github/workflows/run.yml`.

| Variable | Default | Meaning |
|---|---|---|
| `LOOP_MINUTES` | `50` | Minutes to keep ticking inside one job. `0` = single pass |
| `TICK_SECONDS` | `60` | Seconds between passes inside a job |
| `DAILY_CREDIT_BUDGET` | `700` | Twelve Data credits to spend per UTC day (free tier is 800) |
| `REQUESTS_PER_MINUTE` | `7` | Self-throttle, below the free tier's 8 |
| `CANDLE_LAG_SECONDS` | `20` | Grace period before treating a candle as closed |
| `ZONE_POLL_SECONDS` | `0` | Zone poll interval; `0` derives it from the budget |
| `ZONE_PIP_BUFFER` | `5` | Pips either side of a single-price zone |
| `APPEND_PRICE_TO_ALERT` | `0` | `1` appends the price to the spoken sentence |
| `COMMIT_INTERVAL_SECONDS` | `600` | How often routine state churn is committed |

Triggers, registrations and cancellations are committed immediately regardless;
the interval only batches the routine `last_checked_at` churn so the repository
does not fill with binary commits.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Entry point: poll, check, persist, loop |
| `bot.py` | Telegram polling, message parsing, commands |
| `alerts.py` | Condition logic for both alert types |
| `market_data.py` | Twelve Data client, candle-close timing, rate limiting |
| `db.py` | SQLite schema and access |
| `config.py` | Pair whitelist, timeframes, all tunables |
| `test_alerts.py` | Offline tests. No network or API key needed |
| `state/alerts.db` | Committed state: alerts, offset, credit usage, trigger log |
| `state/alerts.log` | Plain-text log of triggers and errors |

## Running locally

```bash
python test_alerts.py                    # 90 checks, no network

export TELEGRAM_BOT_TOKEN=...
export TWELVEDATA_API_KEY=...
export GIT_PUSH=0                        # don't commit while testing
python main.py once                      # one pass
python main.py probe EURUSD              # inspect candle conventions
```

## Maintenance

The state file is committed on every material change, so the repository grows
slowly but forever. If `git clone` gets sluggish after a year or so, squash the
history:

```bash
git checkout --orphan fresh && git add -A && git commit -m "Reset history"
git branch -D main && git branch -m main && git push -f origin main
```

## Known limits

- **Alert data is public.** See the warning at the top.
- **Delivery is not guaranteed.** GitHub can drop a scheduled run; the backward
  scan recovers missed breaks on the next run, but a trigger can arrive late.
- **A near-continuous job is outside the spirit of free Actions.** Set
  `LOOP_MINUTES: '0'` if you would rather stay conservative, at the cost of
  five-minute-or-worse granularity.
- **Free-tier data is thin.** Roughly two or three actively watched pairs before
  zone polling starts stretching.
- **Weekends and holidays.** The forex market is closed; the provider returns
  stale bars and nothing fires. This is correct, not a fault.
- **One-shot alerts.** Each fires once and stops. Use `/reset <id>` to rearm.
