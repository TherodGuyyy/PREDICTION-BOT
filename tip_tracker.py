"""
Logs every WNBA tip the bot sends, then automatically grades past tips
against real game results on each run.

Why this exists: analysis.py has several constants that are explicitly
labeled "reasonable starting point, not tuned on real results yet"
(TOTAL_POINTS_STD_DEV, the win-probability weights, REST_WEIGHT, etc).
You can't responsibly tune those without a track record of predicted vs.
actual outcomes. This module builds that track record automatically,
at $0 cost, using data you already have access to (balldontlie).

Tennis tips are intentionally NOT logged/graded here — WNBA only, per
your instructions. Tennis tips just pass through main.py untouched.

Storage: a single JSON file (TIPS_LOG_PATH) committed back to the repo
by the GitHub Actions workflow after each run (see wnba-tips.yml) so
history survives between runs — GitHub Actions runners are ephemeral
and don't persist files on their own.
"""

import json
import os
import datetime
from stats_fetcher import get_finished_games_for_date

TIPS_LOG_PATH = os.path.join(os.path.dirname(__file__), "tips_log.json")

# if a tip's game is still "pending" after this many days, something went
# wrong finding a matching finished game (team name mismatch, date bucket
# issue, etc) — mark it unresolved rather than checking forever
MAX_DAYS_BEFORE_UNRESOLVED = 4


def _load_log():
    if not os.path.exists(TIPS_LOG_PATH):
        return []
    try:
        with open(TIPS_LOG_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # corrupted or unreadable log shouldn't crash the whole bot run —
        # start fresh rather than blocking tip-sending over a log file
        print("  WARNING: tips_log.json unreadable, starting a fresh log.")
        return []


def _save_log(entries):
    with open(TIPS_LOG_PATH, "w") as f:
        json.dump(entries, f, indent=2, default=str)


def log_tips(tips, game_date):
    """
    Appends today's sent WNBA tips (moneyline + totals) to the log as
    'pending'. Call this AFTER send_tips() so we only log what actually
    went out. Tennis tips are skipped — WNBA-only tracking.
    """
    if not tips:
        return

    entries = _load_log()
    logged_at = datetime.date.today().isoformat()

    for i, tip in enumerate(tips):
        if tip.get("type") not in ("moneyline", "totals"):
            continue  # tennis or anything else — not tracked here

        entry = dict(tip)
        entry["tip_id"] = f"{game_date}_{tip['type']}_{i}"
        entry["game_date"] = game_date
        entry["logged_at"] = logged_at
        entry["status"] = "pending"
        entry["actual_result"] = None
        entries.append(entry)

    _save_log(entries)
    print(f"  Logged {sum(1 for t in tips if t.get('type') in ('moneyline', 'totals'))} "
          f"WNBA tip(s) to tips_log.json for later grading.")


def _find_matching_game(games, team_a, team_b):
    for g in games:
        names = {g["home_team"]["full_name"], g["visitor_team"]["full_name"]}
        if {team_a, team_b} == names:
            return g
    return None


def _grade_moneyline(tip, game):
    home_is_tipped_side = game["home_team"]["full_name"] == tip["team"]
    tipped_score = game["home_score"] if home_is_tipped_side else game["away_score"]
    other_score = game["away_score"] if home_is_tipped_side else game["home_score"]
    won = tipped_score > other_score
    return ("won" if won else "lost"), f"{tipped_score}-{other_score}"


def _grade_totals(tip, game):
    actual_total = game["home_score"] + game["away_score"]
    line = tip["line"]
    if tip["side"] == "over":
        won = actual_total > line
    else:
        won = actual_total < line
    # a push (actual_total == line) is neither — flag it distinctly
    if actual_total == line:
        return "push", str(actual_total)
    return ("won" if won else "lost"), str(actual_total)


def grade_pending_tips():
    """
    Checks every 'pending' tip whose game_date has passed against real
    results, updates their status in place, and prints a running
    win/loss record. Call this at the START of each run, before
    analyzing today's games — so results are as fresh as possible by
    the time you check Telegram or the repo.
    """
    entries = _load_log()
    if not entries:
        return

    today = datetime.date.today()
    pending = [e for e in entries if e.get("status") == "pending"]
    if not pending:
        return

    print(f"\n[Grading] Checking {len(pending)} pending WNBA tip(s) against results...")

    # group pending tips by game_date so we only fetch each date once
    dates_needed = sorted({e["game_date"] for e in pending})
    games_by_date = {}
    for d in dates_needed:
        try:
            games_by_date[d] = get_finished_games_for_date(d)
        except Exception as e:
            print(f"  Couldn't fetch results for {d}: {e}")
            games_by_date[d] = []

    for entry in pending:
        game_date_str = entry["game_date"]
        try:
            game_date = datetime.date.fromisoformat(game_date_str)
        except ValueError:
            continue

        games = games_by_date.get(game_date_str, [])

        if entry["type"] == "moneyline":
            game = _find_matching_game(games, entry["team"], entry["opponent"])
            if game:
                status, actual = _grade_moneyline(entry, game)
                entry["status"] = status
                entry["actual_result"] = actual
                continue
        elif entry["type"] == "totals":
            # matchup is stored as "AWAY @ HOME"
            try:
                away_name, home_name = [s.strip() for s in entry["matchup"].split("@")]
            except (ValueError, KeyError):
                away_name, home_name = None, None
            game = _find_matching_game(games, away_name, home_name) if away_name else None
            if game:
                status, actual = _grade_totals(entry, game)
                entry["status"] = status
                entry["actual_result"] = actual
                continue

        # no matching finished game found yet
        days_old = (today - game_date).days
        if days_old > MAX_DAYS_BEFORE_UNRESOLVED:
            entry["status"] = "unresolved"

    _save_log(entries)

    graded = [e for e in entries if e.get("status") in ("won", "lost", "push")]
    wins = sum(1 for e in graded if e["status"] == "won")
    losses = sum(1 for e in graded if e["status"] == "lost")
    pushes = sum(1 for e in graded if e["status"] == "push")
    if wins + losses > 0:
        win_rate = wins / (wins + losses) * 100
        print(f"[Grading] Overall WNBA tip record so far: {wins}-{losses}"
              f"{f'-{pushes}p' if pushes else ''} ({win_rate:.1f}%)")
