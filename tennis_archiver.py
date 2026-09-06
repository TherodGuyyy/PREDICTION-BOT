"""
Builds a self-maintained archive of completed tennis match results, since
Sackmann's archive is confirmed to lag by more than a year (see
tennis_stats_fetcher.py). Uses TheRundown data already being fetched for
odds — no new API, no new quota — so this costs nothing extra to run.

Persisted to tennis_archive.json in the repo root, committed back to the
repo by the GitHub Actions workflow (same pattern as tips_log.json —
GitHub Actions runners are ephemeral, so nothing persists across runs
unless it's committed back).

IMPORTANT — READ BEFORE TRUSTING THIS: determining who WON a finished
match requires reading TheRundown's "score" object on the event, and the
EXACT field names in that object are not confirmed live (TheRundown's own
changelog confirms the object includes "event_status, display_clock,
game_period, and current scores" but doesn't give the literal field
names for "current scores"). Getting this wrong would be worse than not
building it at all — a self-built archive with silently WRONG winners
would poison every downstream form/H2H calculation that reads from it.
So _determine_winner() below tries the most plausible field name
patterns, and if NONE of them produce a confident, unambiguous result,
it logs the raw score object (once per run, not spammed) and skips that
match entirely rather than guessing. Run this for real and check the
'[tennis archive]' diagnostic lines — if matches are being skipped with
"couldn't determine winner", that raw score dump tells you exactly what
field names to add.
"""

import json
import os
import datetime

import rundown_client as rc
from tennis_odds_fetcher import _get_tennis_fixtures_for_date
from tennis_stats_fetcher import get_tournament_surface
from tennis_rankings import get_current_rank
from telegram_sender import send_alert

ARCHIVE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tennis_archive.json")

MILESTONE_MIN_PLAYERS = 10   # alert once this many distinct players...
MILESTONE_MIN_MATCHES = 5    # ...have at least this many archived matches each

_winner_diagnostic_logged_this_run = False


def _load_archive():
    if not os.path.exists(ARCHIVE_PATH):
        return {"matches": [], "milestone_alert_sent": False}
    try:
        with open(ARCHIVE_PATH, "r") as f:
            data = json.load(f)
        data.setdefault("matches", [])
        data.setdefault("milestone_alert_sent", False)
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [tennis archive] couldn't read {ARCHIVE_PATH} ({type(e).__name__}: {e}) — "
              f"starting a fresh archive rather than crashing. If this keeps happening, check "
              f"whether the file got corrupted by a previous run.")
        return {"matches": [], "milestone_alert_sent": False}


def _save_archive(archive):
    with open(ARCHIVE_PATH, "w") as f:
        json.dump(archive, f, indent=2)


def _determine_winner(fixture):
    """
    Returns 'player_a' (participant1Name), 'player_b' (participant2Name),
    or None if the winner can't be confidently determined. See this
    module's docstring for why "can't determine" is a real, expected
    outcome right now, not a bug to suppress.
    """
    global _winner_diagnostic_logged_this_run

    event = fixture.get("_raw_event", {})
    score = event.get("score") or {}

    # Pattern A: an explicit winner name/id field that matches one of our
    # two known participant names directly.
    for key in ("winner", "winner_name", "winner_id"):
        val = score.get(key)
        if val:
            val_str = str(val).lower()
            if val_str in fixture["participant1Name"].lower():
                return "player_a"
            if val_str in fixture["participant2Name"].lower():
                return "player_b"

    # Pattern B: numeric away/home scores (sets won) under a few plausible
    # key-name variants — away corresponds to participant1Name (player_a),
    # home to participant2Name (player_b), matching rundown_client's
    # documented teams[0]=away/teams[1]=home ordering.
    key_pairs = [
        ("score_away", "score_home"),
        ("away_score", "home_score"),
        ("away", "home"),
    ]
    for away_key, home_key in key_pairs:
        away_val, home_val = score.get(away_key), score.get(home_key)
        if away_val is None or home_val is None:
            continue
        try:
            away_val, home_val = int(away_val), int(home_val)
        except (TypeError, ValueError):
            continue
        if away_val == home_val:
            continue  # tied/incomplete — not a confident result
        return "player_a" if away_val > home_val else "player_b"

    # nothing matched confidently — log the raw structure ONCE per run so
    # a real run reveals the actual field names, instead of guessing
    if not _winner_diagnostic_logged_this_run and score:
        print(f"  [tennis archive] couldn't determine a winner from this finished match's score "
              f"data — here's the raw 'score' object so the real field names can be added: {score}")
        _winner_diagnostic_logged_this_run = True

    return None


def record_completed_matches():
    """
    Checks yesterday's and today's tennis fixtures for any that are now
    finished, and archives results for ones not already recorded (by
    fixture_id). Call this once per main.py run. Returns the number of
    NEW matches recorded this run (0 is normal and expected most runs).
    """
    archive = _load_archive()
    already_recorded_ids = {m["fixture_id"] for m in archive["matches"]}

    today = datetime.date.today()
    dates_to_check = [(today - datetime.timedelta(days=1)).isoformat(), today.isoformat()]

    newly_recorded = 0
    for date_str in dates_to_check:
        for fixture in _get_tennis_fixtures_for_date(date_str):
            fixture_id = fixture["fixtureId"]
            if fixture_id in already_recorded_ids:
                continue
            if fixture["statusName"] != "Finished":
                continue

            winner = _determine_winner(fixture)
            if winner is None:
                continue  # couldn't confidently determine — skip, don't guess

            player_a, player_b = fixture["participant1Name"], fixture["participant2Name"]
            surface = get_tournament_surface(fixture["tournamentName"]) or "Unknown"

            archive["matches"].append({
                "fixture_id": fixture_id,
                "date": date_str,
                "player_a": player_a,
                "player_b": player_b,
                "winner": winner,
                "surface": surface,
                "player_a_rank": get_current_rank(player_a, fixture["tour"]),
                "player_b_rank": get_current_rank(player_b, fixture["tour"]),
            })
            already_recorded_ids.add(fixture_id)
            newly_recorded += 1

    if newly_recorded:
        _save_archive(archive)
        print(f"  [tennis archive] recorded {newly_recorded} new completed match result(s). "
              f"Archive now has {len(archive['matches'])} total.")

    _check_and_send_milestone_alert(archive)
    return newly_recorded


def _player_match_counts(archive):
    counts = {}
    for m in archive["matches"]:
        counts[m["player_a"]] = counts.get(m["player_a"], 0) + 1
        counts[m["player_b"]] = counts.get(m["player_b"], 0) + 1
    return counts


def _check_and_send_milestone_alert(archive):
    """
    Sends a ONE-TIME Telegram alert the first run where at least
    MILESTONE_MIN_PLAYERS distinct players each have MILESTONE_MIN_MATCHES+
    archived matches — per the user's explicit ask. Uses a persisted flag
    so this fires exactly once, ever, not on every run afterward.
    """
    if archive.get("milestone_alert_sent"):
        return

    counts = _player_match_counts(archive)
    qualifying_players = [name for name, count in counts.items() if count >= MILESTONE_MIN_MATCHES]

    if len(qualifying_players) < MILESTONE_MIN_PLAYERS:
        return

    message = (
        f"🎾 *Tennis archive milestone*\n"
        f"{len(qualifying_players)} players now have {MILESTONE_MIN_MATCHES}+ matches logged "
        f"in the self-built results archive ({len(archive['matches'])} total matches recorded).\n"
        f"This is independent of Sackmann's data — it's what the bot has tracked itself from "
        f"TheRundown's completed match results."
    )
    send_alert(message)
    archive["milestone_alert_sent"] = True
    _save_archive(archive)
    print(f"  [tennis archive] MILESTONE reached and Telegram alert sent — "
          f"{len(qualifying_players)} players with {MILESTONE_MIN_MATCHES}+ matches logged.")


if __name__ == "__main__":
    recorded = record_completed_matches()
    archive = _load_archive()
    counts = _player_match_counts(archive)
    print(f"\nTotal matches in archive: {len(archive['matches'])}")
    print(f"Distinct players tracked: {len(counts)}")
    qualifying = sum(1 for c in counts.values() if c >= MILESTONE_MIN_MATCHES)
    print(f"Players with {MILESTONE_MIN_MATCHES}+ matches: {qualifying} (need {MILESTONE_MIN_PLAYERS} for the alert)")
