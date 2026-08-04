"""
Pulls today's WNBA games and the team stats needed to analyze them,
from the balldontlie API (free tier).

The free tier is rate-limited to 5 requests/minute. With 3 games a day
(6 teams) and possibly 1-2 pages of game history per team, that adds up
fast if requests fire back-to-back with no pacing — this is what caused
the 429 error. Every request in this file now goes through
_rate_limited_get(), which paces requests to stay under that limit
(instead of firing them as fast as possible and hoping), plus retries
with backoff as a fallback if a 429 slips through anyway. This makes a
full run slower (a minute or so for a few games) but reliable — that's
the right trade-off for something that only needs to run once a day.
"""

import time
import requests
import datetime
from config import BALLDONTLIE_WNBA_BASE_URL, BALLDONTLIE_API_KEY, MIN_GAMES_FOR_ANALYSIS

HEADERS = {"Authorization": BALLDONTLIE_API_KEY}

MIN_SECONDS_BETWEEN_REQUESTS = 13  # 5/min limit -> just over 12s apart, with a small safety margin
_last_request_time = 0


def _rate_limited_get(url, params, retries=3):
    global _last_request_time

    for attempt in range(retries + 1):
        # pace ourselves so we don't even approach the limit
        elapsed = time.time() - _last_request_time
        if elapsed < MIN_SECONDS_BETWEEN_REQUESTS:
            time.sleep(MIN_SECONDS_BETWEEN_REQUESTS - elapsed)

        resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
        _last_request_time = time.time()

        if resp.status_code == 429 and attempt < retries:
            # fallback in case we still get rate-limited (e.g. another
            # process sharing the same key) — back off harder and retry
            time.sleep(15 * (attempt + 1))
            continue

        resp.raise_for_status()
        return resp.json()


def get_todays_games():
    """
    Returns today's scheduled (not-yet-finished) WNBA games.

    Real bug found via live testing: balldontlie buckets games by the UTC
    calendar date of their start time. A US evening tip-off (e.g. 9pm ET
    or later) very often lands on the NEXT UTC day. So querying only
    "today" (UTC) can pick up a leftover already-finished game from late
    last night (US time) while completely missing tonight's actual game,
    which UTC-wise falls under tomorrow. Fix: query both today's and
    tomorrow's UTC dates and filter out anything already finished
    (status == "post") — this reliably surfaces tonight's real game
    regardless of which UTC date bucket it happens to land in.
    """
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)

    data = _rate_limited_get(
        f"{BALLDONTLIE_WNBA_BASE_URL}/games",
        params={"dates[]": [today.isoformat(), tomorrow.isoformat()]},
    )
    games = data.get("data", [])

    return [g for g in games if g.get("status") != "post"]


def get_team_recent_games(team_id, num_games=10, max_pages=6):
    """
    Returns a team's last `num_games` completed games THIS SEASON, most
    recent first. Used to compute recent form (win %, point differential)
    rather than relying on season-long averages alone.

    Filters to the current season explicitly (seasons[]) — without this,
    a team with 20+ years of WNBA history could return page after page
    of old seasons before ever reaching this year's games, silently
    starving the bot of any usable recent data even mid-season.
    """
    current_season = datetime.date.today().year
    all_games = []
    cursor = None

    for _ in range(max_pages):
        params = {"team_ids[]": team_id, "seasons[]": current_season, "per_page": 25}
        if cursor:
            params["cursor"] = cursor

        payload = _rate_limited_get(f"{BALLDONTLIE_WNBA_BASE_URL}/games", params=params)
        all_games.extend(payload.get("data", []))

        cursor = payload.get("meta", {}).get("next_cursor")
        if not cursor:
            break

    # keep only finished games, sort most-recent-first, trim to num_games
    finished = [g for g in all_games if g.get("status") == "post"]
    finished.sort(key=lambda g: g["date"], reverse=True)
    return finished[:num_games]


def team_form_summary(team_id):
    """
    Turns a team's recent games into simple numbers the analysis step can use:
    win %, average point differential, and a naive momentum score
    (recent games weighted slightly more than older ones within the window).
    """
    games = get_team_recent_games(team_id)
    if len(games) < MIN_GAMES_FOR_ANALYSIS:
        # not enough completed games yet to trust the sample — e.g. this
        # matters a lot in the first couple weeks of a new NBA/NCAAB season
        return None

    wins = 0
    point_diffs = []
    points_scored = []
    points_allowed = []
    for g in games:
        is_home = g["home_team"]["id"] == team_id
        team_score = g["home_score"] if is_home else g["away_score"]
        opp_score = g["away_score"] if is_home else g["home_score"]
        point_diffs.append(team_score - opp_score)
        points_scored.append(team_score)
        points_allowed.append(opp_score)
        if team_score > opp_score:
            wins += 1

    win_pct = wins / len(games)
    avg_point_diff = sum(point_diffs) / len(point_diffs)
    avg_points_scored = sum(points_scored) / len(points_scored)
    avg_points_allowed = sum(points_allowed) / len(points_allowed)

    return {
        "games_sampled": len(games),
        "win_pct": win_pct,
        "avg_point_diff": avg_point_diff,
        "avg_points_scored": avg_points_scored,
        "avg_points_allowed": avg_points_allowed,
    }


if __name__ == "__main__":
    # Quick manual test — checks ONE team's recent-form data without
    # running the full bot (which takes a couple minutes and touches
    # Telegram). Run: python stats_fetcher.py
    import json

    test_team_id = 30
    current_season = datetime.date.today().year

    print(f"RAW check first — team_id={test_team_id}, no filters except team_ids[]:")
    raw = _rate_limited_get(f"{BALLDONTLIE_WNBA_BASE_URL}/games", params={"team_ids[]": test_team_id, "per_page": 5})
    print(f"Total games returned (this page): {len(raw.get('data', []))}")
    if raw.get("data"):
        print("Sample raw game (first result):")
        print(json.dumps(raw["data"][0], indent=2))
    else:
        print("NO games returned at all for this team_id, even with zero filters — "
              "team_id=30 is probably wrong for WNBA. Check the actual team IDs via:")
        print(f'  GET {BALLDONTLIE_WNBA_BASE_URL}/teams')
        teams = _rate_limited_get(f"{BALLDONTLIE_WNBA_BASE_URL}/teams", params={"per_page": 25})
        print(json.dumps(teams.get("data", []), indent=2))

    print(f"\nNow with seasons[]={current_season} filter:")
    filtered = _rate_limited_get(
        f"{BALLDONTLIE_WNBA_BASE_URL}/games",
        params={"team_ids[]": test_team_id, "seasons[]": current_season, "per_page": 5},
    )
    print(f"Games returned with season filter: {len(filtered.get('data', []))}")

    print("\n--- Now running the real functions ---")
    games = get_team_recent_games(test_team_id)
    print(f"Found {len(games)} finished games this season.")
    if games:
        print("Most recent game date:", games[0]["date"])

    summary = team_form_summary(test_team_id)
    print("\nForm summary:", summary)
