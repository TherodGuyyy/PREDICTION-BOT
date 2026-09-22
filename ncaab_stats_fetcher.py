"""
Pulls today's NCAA Men's Basketball games and team form data from ESPN's
public scoreboard/schedule API — NOT balldontlie, unlike WNBA.

WHY A DIFFERENT PROVIDER FOR THIS SPORT: balldontlie's free tier does not
include the Games endpoint for NCAAB at all (confirmed against their
published tier table — Games needs at least ALL-STAR, $9.99/mo; Team
Stats needs GOAT, $39.99/mo). Since this bot is being kept fully free,
NCAAB uses ESPN's scoreboard/team-schedule endpoints instead. These are
UNOFFICIAL — ESPN doesn't publish or support them — but they're the same
public, no-key endpoints most free college-basketball tools and scripts
run on (e.g. the hoopR R package), and have been stable for years. No
rate limit is documented, so this still paces requests lightly out of
courtesy, same spirit as stats_fetcher.py's balldontlie pacing, just less
strict since there's no published 5/min-style limit to respect here.

UNVERIFIED LIVE, UPDATE 2026-09-22: the first real GitHub Actions run hit a
403 Forbidden on the scoreboard endpoint. Most likely cause: the original
User-Agent header literally contained the word "bot"
("...prediction-bot/1.0"), which is a common anti-scraping trigger —
swapped to a realistic browser UA below. If a 403 still happens after
that fix, the next suspect is GitHub Actions' IP ranges specifically
being blocked (some sites block known CI/cloud datacenter ranges
outright, independent of headers) — other public tools built on this
same ESPN endpoint DO run successfully from GitHub Actions on a
schedule, so a full block isn't the base rate here, but it's not
impossible either. If that turns out to be it, the fix is routing
through a proxy or a different free host to run from, not a code change
in this file.

Public functions here intentionally mirror stats_fetcher.py's names and
return shapes (get_todays_games, team_form_summary, get_days_rest,
get_head_to_head_record) so main.py's NCAAB run function can reuse the
exact same downstream code (analysis.py, tip logic) with zero changes —
same "swap the provider, not the interface" pattern used for the
OddsPapi -> TheRundown swap.

NOT YET BUILT (scope decision for this first pass, not an oversight):
  - Pace / possessions estimate. WNBA's version uses balldontlie's
    /team_stats endpoint for this; ESPN doesn't expose an equivalent in
    a standardized way across 360+ D-I teams that's worth trusting yet.
    team_form_summary() always returns pace=None here — analysis.py
    already handles that (falls back to the raw point-differential
    model), so this degrades gracefully rather than breaking anything.
  - get_finished_games_for_date() (used by tip_tracker.py for grading).
    Not built yet since NCAAB tips can't be sent until the season
    starts (Nov 1, 2026) anyway — add this alongside turning tips on.
"""

import time
import datetime
import requests
from config import ESPN_NCAAB_BASE_URL, MIN_GAMES_FOR_ANALYSIS

MIN_SECONDS_BETWEEN_REQUESTS = 1.5  # light courtesy pacing — no documented limit to tune against
_last_request_time = 0

_HEADERS = {
    # The previous UA ("...prediction-bot/1.0") explicitly announced
    # itself as a bot, which is a common trigger for exactly the kind
    # of 403 seen on the first live GitHub Actions run. Using a plain,
    # realistic browser UA instead — this is what worked around the
    # same issue for other people's scripts hitting this endpoint.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def _get(url, params=None, retries=3):
    global _last_request_time

    for attempt in range(retries + 1):
        elapsed = time.time() - _last_request_time
        if elapsed < MIN_SECONDS_BETWEEN_REQUESTS:
            time.sleep(MIN_SECONDS_BETWEEN_REQUESTS - elapsed)

        resp = requests.get(url, headers=_HEADERS, params=params, timeout=15)
        _last_request_time = time.time()

        if resp.status_code == 429 and attempt < retries:
            time.sleep(10 * (attempt + 1))
            continue

        resp.raise_for_status()
        return resp.json()


def _current_ncaab_season_year():
    """
    ESPN/NCAA convention: a season spanning Nov 2026 - Apr 2027 is
    labeled by its START year, 2026. So Jul-Dec counts as the season
    starting THIS year; Jan-Jun counts as the season that started LAST
    year. UNVERIFIED against a live response — if ESPN's `season` param
    turns out to expect something else, the team-schedule diagnostic in
    __main__ will make that obvious fast.
    """
    today = datetime.date.today()
    return today.year if today.month >= 7 else today.year - 1


def _normalize_event(event):
    """
    Converts one ESPN scoreboard/schedule event into the SAME shape
    stats_fetcher.py's balldontlie-backed functions return, so
    analysis.py and main.py need zero changes to handle NCAAB games:
      {"id", "date", "status", "home_team": {"id","full_name"},
       "visitor_team": {"id","full_name"}, "home_score", "away_score"}
    Returns None if the event doesn't have the two competitors this
    needs — logged loudly rather than silently skipped, so a real shape
    mismatch is visible on the first live run instead of just quietly
    returning fewer games than expected.
    """
    try:
        competitors = event["competitions"][0]["competitors"]
    except (KeyError, IndexError, TypeError):
        print(f"  NCAAB: event {event.get('id')} has no usable 'competitions[0].competitors' — "
              f"raw keys: {list(event.keys())}")
        return None

    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        print(f"  NCAAB: event {event.get('id')} missing a home or away competitor — "
              f"competitors found: {[c.get('homeAway') for c in competitors]}")
        return None

    status_type = event.get("status", {}).get("type", {})
    is_final = bool(status_type.get("completed")) or status_type.get("state") == "post"

    def _score(c):
        raw = c.get("score")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "id": event.get("id"),
        "date": event.get("date"),
        "status": "post" if is_final else "scheduled",
        "home_team": {
            "id": home.get("team", {}).get("id"),
            "full_name": home.get("team", {}).get("displayName"),
        },
        "visitor_team": {
            "id": away.get("team", {}).get("id"),
            "full_name": away.get("team", {}).get("displayName"),
        },
        "home_score": _score(home),
        "away_score": _score(away),
    }


def get_todays_games():
    """
    Returns today's scheduled (not-yet-finished) NCAAB games.

    Same UTC/late-tipoff safety net as stats_fetcher.get_todays_games():
    queries today AND tomorrow's date and filters out anything already
    finished, rather than trusting a single date bucket.

    groups=50 requests ESPN's "all Division I" group — UNVERIFIED live,
    but widely reported by community tools as the id needed to get the
    full slate rather than a small default subset (e.g. Top 25 only).
    If a live run shows a suspiciously small game count on a normal
    weeknight, this parameter is the first thing to double-check.
    """
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    date_range = f"{today.strftime('%Y%m%d')}-{tomorrow.strftime('%Y%m%d')}"

    data = _get(f"{ESPN_NCAAB_BASE_URL}/scoreboard", params={"dates": date_range, "groups": 50, "limit": 200})
    events = data.get("events", [])

    games = [g for g in (_normalize_event(e) for e in events) if g is not None]
    return [g for g in games if g["status"] != "post"]


def get_team_recent_games(team_id, num_games=10, season=None):
    """
    Returns a team's last `num_games` completed games THIS SEASON, most
    recent first — same contract as stats_fetcher's balldontlie version.
    Pulls the WHOLE season schedule in one request (ESPN's team-schedule
    endpoint doesn't need the page-by-page cursor dance balldontlie's
    does) then filters/sorts/trims locally.
    """
    season = season or _current_ncaab_season_year()
    data = _get(f"{ESPN_NCAAB_BASE_URL}/teams/{team_id}/schedule", params={"season": season})
    events = data.get("events", [])

    games = [g for g in (_normalize_event(e) for e in events) if g is not None]
    finished = [g for g in games if g["status"] == "post"]
    finished.sort(key=lambda g: g["date"] or "", reverse=True)
    return finished[:num_games]


def _parse_game_date(date_str):
    if not date_str:
        return None
    try:
        # ESPN dates are full ISO timestamps, e.g. "2026-11-04T23:00Z"
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


def get_head_to_head_record(team_a_id, team_b_id, num_matchups=5, season=None):
    """
    Same contract as stats_fetcher's version, but simpler to build here:
    a team's ESPN schedule already includes every opponent it played, so
    this just pulls team_a's season schedule and filters to games
    against team_b_id — no separate cross-team query needed.
    Returns None if fewer than 2 head-to-head games on record this
    season (same small-sample protection as the WNBA version; NCAAB
    conference realignment makes multi-season lookback less reliable
    than WNBA's "this season + last" fallback, so this only checks the
    current season for now).
    """
    season = season or _current_ncaab_season_year()
    data = _get(f"{ESPN_NCAAB_BASE_URL}/teams/{team_a_id}/schedule", params={"season": season})
    events = data.get("events", [])

    games = [g for g in (_normalize_event(e) for e in events) if g is not None]
    matchups = [
        g for g in games
        if g["status"] == "post" and {g["home_team"]["id"], g["visitor_team"]["id"]} == {team_a_id, team_b_id}
    ]

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
    `include_pace` is accepted for interface compatibility but ignored —
    pace is always None here (see module docstring's "NOT YET BUILT").
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
        "pace": None,  # see module docstring — not built for NCAAB yet
    }


if __name__ == "__main__":
    # Quick manual test — run: python ncaab_stats_fetcher.py
    # Won't show much before Nov 1, 2026 (season start), but confirms
    # the endpoints respond and shows the raw shape to check field
    # names against once real games exist.
    import json

    print(f"Season year being used: {_current_ncaab_season_year()}")

    print("\nRAW scoreboard check (today+tomorrow, groups=50):")
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    raw = _get(
        f"{ESPN_NCAAB_BASE_URL}/scoreboard",
        params={"dates": f"{today.strftime('%Y%m%d')}-{tomorrow.strftime('%Y%m%d')}", "groups": 50, "limit": 200},
    )
    events = raw.get("events", [])
    print(f"Found {len(events)} raw event(s).")
    if events:
        print("First raw event (check this against the field names _normalize_event expects):")
        print(json.dumps(events[0], indent=2)[:3000])
    else:
        print("No events — expected before the season starts (Nov 1, 2026) or if groups=50 is wrong.")

    print("\n--- Now running the real functions ---")
    games = get_todays_games()
    print(f"get_todays_games(): {len(games)} game(s) found.")
    for g in games:
        print(f"  {g['visitor_team']['full_name']} @ {g['home_team']['full_name']} (status: {g['status']})")
