"""Entry point run by the GitHub Actions workflow.

One pass is: poll Telegram, check due alerts, persist state. With LOOP_MINUTES
set, that pass repeats inside a single job so polling can be finer grained than
GitHub's five minute cron floor allows.
"""
import os
import subprocess
import sys
import time
from datetime import timedelta

import config
from alerts import check_due_alerts, reschedule_orphans
from bot import poll_telegram
from db import Database, log, utcnow
from market_data import MarketData, MarketDataError
from telegram_api import Telegram, TelegramError

# State churns every tick (last_checked_at and friends). Committing that every
# time would bury the repo in binary commits, so routine churn is flushed on a
# timer and only meaningful events force an immediate commit.
COMMIT_INTERVAL_SECONDS = int(os.environ.get("COMMIT_INTERVAL_SECONDS", "600"))
PUSH_ATTEMPTS = 3


class StateStore:
    """Commits the SQLite file and log back to the repo so state survives."""

    def __init__(self):
        self.enabled = config.GIT_PUSH and self._in_git_repo()
        self.last_commit = time.time()
        if not self.enabled:
            log("Git persistence disabled (not a repo, or GIT_PUSH=0).")

    @staticmethod
    def _run(args, check=True):
        return subprocess.run(args, capture_output=True, text=True, check=check)

    def _in_git_repo(self):
        try:
            result = self._run(["git", "rev-parse", "--is-inside-work-tree"],
                               check=False)
            return result.returncode == 0 and result.stdout.strip() == "true"
        except (OSError, FileNotFoundError):
            return False

    def has_changes(self):
        if not self.enabled:
            return False
        result = self._run(["git", "status", "--porcelain", "--", config.STATE_DIR],
                           check=False)
        return bool(result.stdout.strip())

    def flush(self, reason, force=False):
        """Commit and push. Raises on failure so the run fails loudly."""
        if not self.enabled:
            return False
        due = force or (time.time() - self.last_commit) >= COMMIT_INTERVAL_SECONDS
        if not due or not self.has_changes():
            return False

        self._run(["git", "add", "--", config.STATE_DIR])
        commit = self._run(
            ["git", "commit", "-m", "state: %s [skip ci]" % reason], check=False)
        if commit.returncode != 0 and "nothing to commit" not in commit.stdout:
            raise RuntimeError("git commit failed: %s%s"
                               % (commit.stdout, commit.stderr))

        branch = self._run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        last_error = ""
        for attempt in range(1, PUSH_ATTEMPTS + 1):
            # Another run of this workflow may have pushed in the meantime.
            self._run(["git", "pull", "--rebase", "--autostash", "origin", branch],
                      check=False)
            push = self._run(["git", "push", "origin", branch], check=False)
            if push.returncode == 0:
                self.last_commit = time.time()
                log("State committed and pushed (%s)." % reason)
                return True
            last_error = push.stdout + push.stderr
            log("Push attempt %d/%d failed: %s" % (attempt, PUSH_ATTEMPTS,
                                                   last_error.strip()[:200]))
            time.sleep(3 * attempt)
        raise RuntimeError("could not push state after %d attempts: %s"
                           % (PUSH_ATTEMPTS, last_error.strip()[:500]))


def tick(db, tg, md, store):
    """One poll-and-check pass. Returns True if anything material happened."""
    material = False
    md.invalidate()

    handled = poll_telegram(db, tg, md, timeout=0)
    if handled:
        log("Handled %d Telegram update(s)." % handled)
        material = True

    try:
        fired = check_due_alerts(db, tg, md)
        if fired:
            log("Fired %d alert(s)." % fired)
            material = True
    except MarketDataError as exc:
        # Data problems are expected and transient; the next run retries.
        log("Market data unavailable this pass: %s" % exc)
    except TelegramError as exc:
        log("Telegram unavailable this pass: %s" % exc)

    store.flush("tick", force=material)
    return material


def sleep_while_polling(db, tg, md, seconds, deadline):
    """Idle by long polling Telegram so commands answer immediately.

    Long polling costs nothing and turns dead sleep time into responsiveness.
    """
    end = time.time() + seconds
    while time.time() < end:
        if deadline and utcnow() >= deadline:
            return
        chunk = int(min(25, max(1, end - time.time())))
        try:
            if poll_telegram(db, tg, md, timeout=chunk):
                return  # a message arrived; run a full tick now
        except TelegramError as exc:
            log("Polling error while idle: %s" % exc)
            time.sleep(min(10, chunk))


def run(loop_minutes):
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Add it as a repository secret.")
    if not config.TWELVEDATA_API_KEY:
        raise SystemExit("TWELVEDATA_API_KEY is not set. Add it as a repository secret.")

    db = Database()
    store = StateStore()
    try:
        tg = Telegram()
    except TelegramError as exc:
        raise SystemExit(str(exc))
    md = MarketData(db)

    reschedule_orphans(db)

    deadline = utcnow() + timedelta(minutes=loop_minutes) if loop_minutes > 0 else None
    log("Starting run. loop_minutes=%d tick=%ds active_alerts=%d credits_used=%d"
        % (loop_minutes, config.TICK_SECONDS, len(db.active_alerts()),
           db.credits_used_today()))

    try:
        while True:
            started = time.time()
            tick(db, tg, md, store)
            if deadline is None:
                break
            if utcnow() >= deadline:
                break
            remaining = config.TICK_SECONDS - (time.time() - started)
            if remaining > 0:
                sleep_while_polling(db, tg, md, remaining, deadline)
    finally:
        # Always land the state, even after an exception, so nothing is lost.
        try:
            store.flush("final", force=True)
        except RuntimeError as exc:
            log("FATAL: %s" % exc)
            raise
        db.close()
    log("Run complete.")


def main(argv):
    args = argv[1:]
    if args and args[0] == "probe":
        from market_data import probe
        db = Database()
        try:
            probe(db, args[1].upper() if len(args) > 1 else "EURUSD")
        finally:
            db.close()
        return 0
    if args and args[0] == "once":
        run(0)
        return 0
    run(config.LOOP_MINUTES)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
