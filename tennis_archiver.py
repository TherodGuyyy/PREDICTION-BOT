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
from tennis_stats_fetcher import get_tournament_surface, get_player_recent_matches, get_head_to_head
from tennis_rankings import get_current_rank
from telegram_sender import send_alert
from config import TENNIS_H2H_MIN_MATCHUPS

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


def _archived_match_to_normalized(match, player_name):
    """
    Converts one archive entry into the SAME shape
    tennis_stats_fetcher.get_player_recent_matches() produces, from the
    given player's perspective, so the two sources can be merged and
    treated identically by player_form_summary().
    """
    is_a = _names_match(match["player_a"], player_name)
    won = (match["winner"] == "player_a") if is_a else (match["winner"] == "player_b")
    opponent = match["player_b"] if is_a else match["player_a"]
    player_rank = match["player_a_rank"] if is_a else match["player_b_rank"]
    opponent_rank = match["player_b_rank"] if is_a else match["player_a_rank"]

    return {
        "date": datetime.date.fromisoformat(match["date"]),
        "surface": match["surface"],
        "won": won,
        "player_rank": player_rank,
        "opponent_name": opponent,
        "opponent_rank": opponent_rank,
    }


def _names_match(a, b):
    a, b = (a or "").lower(), (b or "").lower()
    return a in b or b in a


def get_player_archived_matches(player_name):
    """
    Returns this player's matches from the self-built archive (NOT
    Sackmann's), most-recent-first, in the same normalized shape
    get_player_recent_matches() uses.
    """
    archive = _load_archive()
    matches = [
        _archived_match_to_normalized(m, player_name)
        for m in archive["matches"]
        if _names_match(m["player_a"], player_name) or _names_match(m["player_b"], player_name)
    ]
    matches.sort(key=lambda m: m["date"], reverse=True)
    return matches


def get_merged_recent_matches(player_name, tour, num_matches=15):
    """
    Combines Sackmann's archive (via tennis_stats_fetcher, which can lag
    or intermittently fail — see its own diagnostics) with this bot's
    self-built results archive (guaranteed current, but starts from
    empty and grows over time), most-recent-first, deduplicated by
    (date, opponent) so a match that somehow appears in both sources
    isn't double-counted.

    This is the RECOMMENDED way to get a player's match history now —
    pass the result into tennis_stats_fetcher.player_form_summary(...,
    matches=this_result) instead of letting it call Sackmann alone.
    """
    sackmann_matches = get_player_recent_matches(player_name, tour, num_matches=num_matches)
    own_matches = get_player_archived_matches(player_name)

    seen = set()
    merged = []
    for m in sackmann_matches + own_matches:
        key = (m["date"], m["opponent_name"].lower() if m["opponent_name"] else None)
        if key in seen:
            continue
        seen.add(key)
        merged.append(m)

    merged.sort(key=lambda m: m["date"], reverse=True)
    return merged[:num_matches]


def get_merged_head_to_head(player_a, player_b, tour, num_matchups=5):
    """
    FIXES A REAL BUG found 2026-09-24: main.py's tennis section was
    calling tennis_stats_fetcher.get_head_to_head() directly, which
    ONLY reads Sackmann's CSVs — the same ones confirmed 404ing for
    every year, including past ones (see tennis_stats_fetcher.py's own
    diagnostics). That call was silently returning None for every
    single tennis matchup, always. Not a blocker for tips (h2h is only
    printed as supplementary info, never gates a tip either way), but
    it meant head-to-head context has effectively never worked for
    tennis. This is the fix — same merge concept as
    get_merged_recent_matches, just for h2h specifically.

    NOT a straight merge/sum with Sackmann's count: Sackmann's
    get_head_to_head() returns an aggregate (meetings_total,
    meetings_a_won) with no per-match IDs to de-duplicate against this
    archive's own records. Summing the two could double-count a match
    that exists in both once Sackmann's fetch is eventually fixed. So
    this PREFERS the self-built archive when it already has enough
    matchups on its own, and only falls back to Sackmann's result
    otherwise — same "pick one source, don't blend" approach
    get_head_to_head_record() uses for NCAAB.
    """
    archive = _load_archive()
    matchups = [
        m for m in archive["matches"]
        if (_names_match(m["player_a"], player_a) and _names_match(m["player_b"], player_b))
        or (_names_match(m["player_a"], player_b) and _names_match(m["player_b"], player_a))
    ]

    if len(matchups) >= TENNIS_H2H_MIN_MATCHUPS:
        matchups.sort(key=lambda m: m["date"], reverse=True)
        matchups = matchups[:num_matchups]
        a_wins = sum(
            1 for m in matchups
            if (m["winner"] == "player_a") == _names_match(m["player_a"], player_a)
        )
        return {"matchups_found": len(matchups), "player_a_win_pct": a_wins / len(matchups)}

    # not enough in the self-built archive yet — fall back to Sackmann,
    # which will keep returning None until its own 404 issue is fixed,
    # but this way the merged function is correct the moment either
    # source has enough data, with no code changes needed later
    return get_head_to_head(player_a, player_b, tour)


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
