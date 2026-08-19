"""
Logs every WNBA tip the bot sends, then automatically grades past tips
against real game results on each run.

Why this exists: analysis.py has several constants that are explicitly
labeled "reasonable starting point, not tuned on real results yet"
(TOTAL_POINTS_STD_DEV, the win-probability weights, REST_WEIGHT, etc).
You can't responsibly tune those without a track record of predicted vs.
actual outcomes. This module builds that track record automatically,
at $0 cost, using data you already have access to (balldontlie).

GRADABLE vs UNGRADABLE tip types, and why: moneyline, totals, and
team_totals can all be graded using final game/team scores, which
balldontlie's free tier provides. half_totals and quarter_totals CANNOT
be graded here — that would need actual first-half/quarter scores, which
require period-level box score data that's a paid-tier balldontlie
feature (same limitation documented in stats_fetcher.get_team_pace).
Rather than let those sit as "pending" forever or get silently
mislabeled, they're logged with a distinct "ungradable" status right
away — you'll still see every half/quarter tip that went out, just
without a win/loss verdict next to it.

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

# tip types with enough free data to check against a real result
GRADABLE_TYPES = ("moneyline", "totals", "team_totals")
# tip types logged for the record, but never gradable with free data
UNGRADABLE_TYPES = ("half_totals", "quarter_totals")

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
    Appends today's sent WNBA tips to the log. Gradable types start as
    'pending'; ungradable types (half/quarter totals) are logged
    immediately as 'ungradable' — see module docstring. Call this AFTER
    send_tips() so we only log what actually went out. Tennis tips are
    skipped — WNBA-only tracking.
    """
    if not tips:
        return

    entries = _load_log()
    logged_at = datetime.date.today().isoformat()
    logged_count = 0

    for i, tip in enumerate(tips):
        tip_type = tip.get("type")
        if tip_type not in GRADABLE_TYPES and tip_type not in UNGRADABLE_TYPES:
            continue  # tennis or anything else — not tracked here

        entry = dict(tip)
        entry["tip_id"] = f"{game_date}_{tip_type}_{i}"
        entry["game_date"] = game_date
        entry["logged_at"] = logged_at
        entry["status"] = "pending" if tip_type in GRADABLE_TYPES else "ungradable"
        entry["actual_result"] = None
        entries.append(entry)
        logged_count += 1

    _save_log(entries)
    print(f"  Logged {logged_count} WNBA tip(s) to tips_log.json for later grading "
          f"(some may be marked 'ungradable' — see tip_tracker.py's docstring).")


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


def _grade_team_totals(tip, game):
    """
    tip["team"] holds the specific team this total was about (see
    analysis.find_team_totals_value_tip) — use that, not tip["matchup"],
    to know which side's actual score to check the line against.
    """
    is_home = game["home_team"]["full_name"] == tip["team"]
    actual_score = game["home_score"] if is_home else game["away_score"]
    line = tip["line"]
    if tip["side"] == "over":
        won = actual_score > line
    else:
        won = actual_score < line
    if actual_score == line:
        return "push", str(actual_score)
    return ("won" if won else "lost"), str(actual_score)


def grade_pending_tips():
    """
    Checks every 'pending' tip whose game_date has passed against real
    results, updates their status in place, and prints a running
    win/loss record. Call this at the START of each run, before
    analyzing today's games — so results are as fresh as possible by
    the time you check Telegram or the repo. Tips already marked
    'ungradable' at log time (half/quarter totals) are skipped entirely.
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
        entry_type = entry.get("type")

        if entry_type == "moneyline":
            game = _find_matching_game(games, entry["team"], entry["opponent"])
            if game:
                status, actual = _grade_moneyline(entry, game)
                entry["status"] = status
                entry["actual_result"] = actual
                continue
        elif entry_type == "totals":
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
        elif entry_type == "team_totals":
            # matchup is just the team's own name here (see
            # find_team_totals_value_tip) — we still need BOTH team names
            # to find the game, so fall back to "team" + "opponent" if
            # present, else skip gracefully rather than guess
            team = entry.get("team")
            opponent = entry.get("opponent")
            game = _find_matching_game(games, team, opponent) if team and opponent else None
            if game:
                status, actual = _grade_team_totals(entry, game)
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
