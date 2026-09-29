"""
Pulls today's Spanish ACB (Liga Endesa) games and team form data from
TheRundown — same provider and same approach as ncaab_stats_fetcher.py
after ITS switch away from ESPN, applied here from the start since
there was never a balldontlie or ESPN option for a European league.

SPORT LOOKUP: same uncertainty as acb_odds_fetcher.py — ACB's exact
name in TheRundown's /sports list isn't confirmed. Uses the same
find_sport_id_trying() with several candidate names; see that file's
docstring for details, and its __main__ block is the fastest way to
find out for real whether TheRundown carries this league at all.

SCHEDULE DIFFERENCE FROM NCAAB: ACB is an 18-team league playing
roughly 4-9 games a week total (vs NCAAB's 360+ teams playing 40-70
games a NIGHT), so a single team plays much less often. lookback_days
defaults to 90 here instead of NCAAB's 45, since a team's last 10 games
can span 2-3x as many calendar days.

Public functions here have the SAME NAMES AND RETURN SHAPES as
stats_fetcher.py's (WNBA/balldontlie) and ncaab_stats_fetcher.py's
(NCAAB/TheRundown) versions, so nothing in main.py or analysis.py needs
sport-specific handling beyond calling the right module.

NOT BUILT (same scope decision as NCAAB): pace/possessions estimate —
team_form_summary() always returns pace=None, and analysis.py already
handles that gracefully. get_finished_games_for_date() (for tip_tracker
grading) also isn't built yet — add it once ACB tips are confirmed
working end-to-end.
"""

import datetime
import rundown_client as rc
from config import MIN_GAMES_FOR_ANALYSIS

_acb_sport_id = None
_single_date_cache = {}  # exact date string -> events list, shared across every team's lookback


def _get_acb_sport_id():
    global _acb_sport_id
    if _acb_sport_id is None:
        _acb_sport_id = rc.find_sport_id_trying("ACB", "Liga Endesa", "Liga ACB", "Spanish Basketball")
    return _acb_sport_id


def _get_events_for_single_date(date_str):
    if date_str not in _single_date_cache:
        _single_date_cache[date_str] = rc.get_events_for_dates(_get_acb_sport_id(), [date_str])
    return _single_date_cache[date_str]


def _normalize_event(event):
    """
    Converts one TheRundown event into the SAME shape stats_fetcher.py's
    balldontlie-backed functions return:
      {"id", "date", "status", "home_team": {"id","full_name"},
       "visitor_team": {"id","full_name"}, "home_score", "away_score"}
    Returns None if the event doesn't have usable team names — logged
    loudly rather than silently skipped, so a real shape mismatch shows
    up on the first live run instead of just quietly losing games.
    """
    away_name, home_name = rc.team_names(event)
    if not away_name or not home_name:
        print(f"  ACB: event {event.get('event_id')} has no usable team names — "
              f"raw keys: {list(event.keys())}")
        return None

    away_id, home_id = rc.team_ids(event)
    away_score, home_score = rc.event_scores(event)

    return {
        "id": event.get("event_id"),
        "date": event.get("event_date"),
        "status": "post" if rc.event_is_finished(event) else "scheduled",
        "home_team": {"id": home_id, "full_name": home_name},
        "visitor_team": {"id": away_id, "full_name": away_name},
        "home_score": home_score,
        "away_score": away_score,
    }


def get_todays_games():
    """
    Returns today's scheduled (not-yet-finished) ACB games. Same
    today+tomorrow window as WNBA/tennis use, for the same reason (a
    late tip-off shouldn't fall into the wrong UTC-date bucket and get
    missed).
    """
    today = datetime.date.today().isoformat()
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

    events = rc.get_events_for_dates(_get_acb_sport_id(), [today, tomorrow])
    games = [g for g in (_normalize_event(e) for e in events) if g is not None]
    return [g for g in games if g["status"] != "post"]


def get_team_recent_games(team_id, num_games=10, lookback_days=90):
    """
    Walks backward day-by-day from today (up to `lookback_days`),
    collecting this team's FINISHED games, most recent first, until
    `num_games` is reached or the lookback window runs out. See the
    module docstring for why this is a lookback walk instead of a
    single team-schedule call, and why it's still cheap across a full
    run despite that.
    """
    collected = []
    day = datetime.date.today()

    for i in range(lookback_days):
        if len(collected) >= num_games:
            break
        date_str = (day - datetime.timedelta(days=i)).isoformat()
        events = _get_events_for_single_date(date_str)

        for event in events:
            away_id, home_id = rc.team_ids(event)
            if team_id not in (away_id, home_id):
                continue
            if not rc.event_is_finished(event):
                continue
            normalized = _normalize_event(event)
            if normalized:
                collected.append(normalized)

    collected.sort(key=lambda g: g["date"] or "", reverse=True)
    return collected[:num_games]


def _parse_game_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.date.fromisoformat(date_str[:10])
    except (ValueError, TypeError):
        return None


def get_days_rest(team_id, as_of_date, recent_games=None):
    """Same contract/logic as stats_fetcher.get_days_rest — see there for full reasoning."""
    games = recent_games if recent_games is not None else get_team_recent_games(team_id)
    if not games:
        return None

    last_game_date = _parse_game_date(games[0].get("date"))
    if last_game_date is None:
        return None

    days = (as_of_date - last_game_date).days
    return days if days >= 0 else None


def get_head_to_head_record(team_a_id, team_b_id, num_matchups=5, lookback_days=150):
    """
    Same contract as before. Wider lookback (150 days ~= a full ACB
    season) than get_team_recent_games's default, since head-to-head
    matchups between two specific teams are rare — most D-I pairs play
    at most once or twice a season, so a short lookback would almost
    always come up empty even when a real matchup happened earlier in
    the year. Returns None if fewer than 2 matchups found, same
    small-sample protection as elsewhere in this project.
    """
    matchups = []
    day = datetime.date.today()

    for i in range(lookback_days):
        date_str = (day - datetime.timedelta(days=i)).isoformat()
        events = _get_events_for_single_date(date_str)

        for event in events:
            away_id, home_id = rc.team_ids(event)
            if {away_id, home_id} != {team_a_id, team_b_id}:
                continue
            if not rc.event_is_finished(event):
                continue
            normalized = _normalize_event(event)
            if normalized:
                matchups.append(normalized)

    if len(matchups) < 2:
        return None

    matchups.sort(key=lambda g: g["date"] or "", reverse=True)
    matchups = matchups[:num_matchups]

    a_wins = 0
    for g in matchups:
        a_is_home = g["home_team"]["id"] == team_a_id
        a_score = g["home_score"] if a_is_home else g["away_score"]
        b_score = g["away_score"] if a_is_home else g["home_score"]
        if a_score is not None and b_score is not None and a_score > b_score:
            a_wins += 1

    return {
        "matchups_found": len(matchups),
        "team_a_win_pct": a_wins / len(matchups),
    }


def team_form_summary(team_id, as_of_date=None, include_pace=True):
    """
    Same contract/return shape as stats_fetcher.team_form_summary().
    `include_pace` accepted for interface compatibility but ignored —
    pace is always None here (see module docstring's "STILL NOT BUILT").
    """
    if as_of_date is None:
        as_of_date = datetime.date.today()

    games = get_team_recent_games(team_id)
    if len(games) < MIN_GAMES_FOR_ANALYSIS:
        return None

    wins = 0
    point_diffs, points_scored, points_allowed = [], [], []
    for g in games:
        is_home = g["home_team"]["id"] == team_id
        team_score = g["home_score"] if is_home else g["away_score"]
        opp_score = g["away_score"] if is_home else g["home_score"]
        if team_score is None or opp_score is None:
            continue
        point_diffs.append(team_score - opp_score)
        points_scored.append(team_score)
        points_allowed.append(opp_score)
        if team_score > opp_score:
            wins += 1

    if len(point_diffs) < MIN_GAMES_FOR_ANALYSIS:
        # scores missing on too many games to trust the sample, even
        # though the raw game count passed the earlier check
        return None

    return {
        "games_sampled": len(point_diffs),
        "win_pct": wins / len(point_diffs),
        "avg_point_diff": sum(point_diffs) / len(point_diffs),
        "avg_points_scored": sum(points_scored) / len(points_scored),
        "avg_points_allowed": sum(points_allowed) / len(points_allowed),
        "days_rest": get_days_rest(team_id, as_of_date, recent_games=games),
        "pace": None,  # see module docstring — not built for ACB yet
    }


if __name__ == "__main__":
    # Quick manual test — run: python acb_stats_fetcher.py
    # ACB is already in season (started Sept 26-27, 2026), so this
    # should actually show real games/results, unlike NCAAB's version
    # of this block which won't until Nov 1.
    # TheRundown responds and shows the raw shape to check field names
    # against once real games exist.
    import json

    print("ACB sportId:", _get_acb_sport_id())

    today = datetime.date.today().isoformat()
    print(f"\nRAW events check for {today}:")
    raw_events = _get_events_for_single_date(today)
    print(f"Found {len(raw_events)} raw event(s).")
    if raw_events:
        print("First raw event (check team_ids/event_scores in rundown_client.py against this):")
        print(json.dumps(raw_events[0], indent=2)[:3000])
    else:
        print("No events — check the sport-lookup line above: if it errored, that's the real "
              "finding. If it resolved fine, ACB is already in season, so an empty day just means "
              "no fixtures on this particular date (18 teams, 4-9 games/week).")

    print("\n--- Now running the real functions ---")
    games = get_todays_games()
    print(f"get_todays_games(): {len(games)} game(s) found.")
    for g in games:
        print(f"  {g['visitor_team']['full_name']} @ {g['home_team']['full_name']} (status: {g['status']})")
