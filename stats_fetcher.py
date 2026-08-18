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


def get_finished_games_for_date(date_str):
    """
    Returns all FINISHED WNBA games for a specific past date (YYYY-MM-DD).
    Used by tip_tracker.py to grade yesterday's (or older) tips against
    actual results — separate from get_todays_games(), which is scoped
    to today/tomorrow and deliberately excludes finished games.
    """
    data = _rate_limited_get(
        f"{BALLDONTLIE_WNBA_BASE_URL}/games",
        params={"dates[]": [date_str]},
    )
    games = data.get("data", [])
    return [g for g in games if g.get("status") == "post"]


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


def _parse_game_date(date_str):
    """
    balldontlie game dates come back as either 'YYYY-MM-DD' or a full
    ISO timestamp depending on endpoint — handle both rather than
    assuming one format and silently failing rest-day math on the other.
    """
    if not date_str:
        return None
    try:
        return datetime.date.fromisoformat(date_str[:10])
    except (ValueError, TypeError):
        return None


def get_days_rest(team_id, as_of_date, recent_games=None):
    """
    Days between a team's most recent COMPLETED game and as_of_date
    (the date of the upcoming game we're analyzing). Returns None if we
    don't have a usable prior game on record — callers should treat
    that as "unknown", not as "fully rested".

    as_of_date: a datetime.date.
    recent_games: pass in an already-fetched get_team_recent_games()
    result to avoid a second API call when the caller already has it.
    """
    games = recent_games if recent_games is not None else get_team_recent_games(team_id)
    if not games:
        return None

    last_game_date = _parse_game_date(games[0].get("date"))
    if last_game_date is None:
        return None

    days = (as_of_date - last_game_date).days
    if days < 0:
        # shouldn't happen (would mean the "recent" game is in the
        # future relative to as_of_date) — treat as unknown rather than
        # reporting a nonsensical negative rest value
        return None
    return days


def get_head_to_head_record(team_a_id, team_b_id, num_matchups=5, max_pages=4):
    """
    Looks at the two teams' shared game history (this season + last, same
    reasoning as get_team_recent_games falling back a year) and returns
    team_a's win % in matchups specifically against team_b.

    Returns None if there aren't at least 2 head-to-head games on record
    — same small-sample protection philosophy as MIN_GAMES_FOR_ANALYSIS.
    A single head-to-head game is closer to noise than signal.
    """
    current_season = datetime.date.today().year
    matchups = []

    for season in (current_season, current_season - 1):
        cursor = None
        for _ in range(max_pages):
            params = {
                "team_ids[]": [team_a_id, team_b_id],
                "seasons[]": season,
                "per_page": 25,
            }
            if cursor:
                params["cursor"] = cursor

            payload = _rate_limited_get(f"{BALLDONTLIE_WNBA_BASE_URL}/games", params=params)
            page_games = payload.get("data", [])

            # team_ids[] with two ids returns games involving EITHER team —
            # filter down to games where BOTH teams played each other
            for g in page_games:
                ids_in_game = {g["home_team"]["id"], g["visitor_team"]["id"]}
                if ids_in_game == {team_a_id, team_b_id} and g.get("status") == "post":
                    matchups.append(g)

            cursor = payload.get("meta", {}).get("next_cursor")
            if not cursor:
                break

        if len(matchups) >= num_matchups:
            break

    if len(matchups) < 2:
        return None

    matchups.sort(key=lambda g: g["date"], reverse=True)
    matchups = matchups[:num_matchups]

    a_wins = 0
    for g in matchups:
        a_is_home = g["home_team"]["id"] == team_a_id
        a_score = g["home_score"] if a_is_home else g["away_score"]
        b_score = g["away_score"] if a_is_home else g["home_score"]
        if a_score > b_score:
            a_wins += 1

    return {
        "matchups_found": len(matchups),
        "team_a_win_pct": a_wins / len(matchups),
    }


def get_team_pace(team_id, num_games=10, recent_games=None):
    """
    Estimates the team's average possessions per game over its last
    `num_games` completed games, using the standard simplified pace
    formula: POSS ≈ FGA + 0.44*FTA + TOV - OREB, summed across all
    players on the team for each game, then averaged.

    Returns None (not a crash) if:
      - the /stats endpoint isn't available on this API tier (401/403),
      - or the response doesn't have the fields this formula needs.
    Both cases are logged loudly so a silent tier limitation doesn't
    quietly masquerade as "team just has no pace data". Callers
    (team_form_summary) must treat None as "unknown", same rule as
    days_rest — never assume a default pace.

    This makes one /stats call PER GAME (not per team), on top of the
    existing games call — meaningfully more API usage than the rest of
    this file. Given the free tier's 5 req/min pacing already in
    _rate_limited_get, expect this to add real time to a run.
    """
    games = recent_games if recent_games is not None else get_team_recent_games(team_id, num_games=num_games)
    if not games:
        return None

    possessions_per_game = []

    for g in games:
        game_id = g.get("id")
        if game_id is None:
            continue

        try:
            payload = _rate_limited_get(
                f"{BALLDONTLIE_WNBA_BASE_URL}/stats",
                params={"game_ids[]": game_id, "team_ids[]": team_id, "per_page": 25},
            )
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            print(f"  PACE: /stats endpoint returned {status} for game {game_id} — "
                  f"this WNBA tier may not include box-score stats. Skipping pace adjustment.")
            return None

        player_rows = payload.get("data", [])
        if not player_rows:
            # no player rows for this game/team combo — skip this game
            # rather than treating it as zero possessions
            continue

        try:
            fga = sum(r.get("fga") or 0 for r in player_rows)
            fta = sum(r.get("fta") or 0 for r in player_rows)
            tov = sum(r.get("turnover") or 0 for r in player_rows)
            oreb = sum(r.get("oreb") or 0 for r in player_rows)
        except (TypeError, AttributeError):
            print(f"  PACE: unexpected /stats response shape for game {game_id} — skipping pace adjustment.")
            return None

        game_poss = fga + (0.44 * fta) + tov - oreb
        if game_poss > 0:
            possessions_per_game.append(game_poss)

    if len(possessions_per_game) < MIN_GAMES_FOR_ANALYSIS:
        # not enough usable games to trust a pace average — same
        # small-sample guard as team_form_summary
        return None

    return sum(possessions_per_game) / len(possessions_per_game)


def team_form_summary(team_id, as_of_date=None, include_pace=True):
    """
    Turns a team's recent games into simple numbers the analysis step can use:
    win %, average point differential, a naive momentum score
    (recent games weighted slightly more than older ones within the window),
    days of rest heading into the game being analyzed, and (if available)
    estimated pace.

    as_of_date: date of the upcoming game (defaults to today) — used only
    to compute days_rest; doesn't affect which games count as "recent".
    include_pace: set False to skip the extra /stats calls entirely
    (useful for a quick smoke test, or if you've confirmed your tier
    doesn't support it and don't want the wasted requests every run).
    """
    if as_of_date is None:
        as_of_date = datetime.date.today()

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
    days_rest = get_days_rest(team_id, as_of_date, recent_games=games)

    pace = None
    if include_pace:
        pace = get_team_pace(team_id, recent_games=games)

    return {
        "games_sampled": len(games),
        "win_pct": win_pct,
        "avg_point_diff": avg_point_diff,
        "avg_points_scored": avg_points_scored,
        "avg_points_allowed": avg_points_allowed,
        "days_rest": days_rest,  # None if unknown — analysis.py must handle that
        "pace": pace,            # None if unavailable — analysis.py must handle that
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
