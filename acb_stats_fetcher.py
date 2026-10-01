"""
Pulls ACB (Spanish Liga Endesa) games and team form from Highlightly's
Basketball API — a THIRD provider, distinct from balldontlie (WNBA) and
TheRundown (NCAAB, tennis odds).
 
WHY A THIRD PROVIDER: TheRundown's /sports list was checked live and
confirmed to carry ZERO Spanish basketball coverage — not under any
name (see the full raw list from that run, kept in this project's chat
history: NBA, WNBA, NCAA Men's Basketball, and non-US soccer leagues,
but no ACB, Liga Endesa, or Euroleague). Digging into why turned up a
real structural reason, not just an oversight: Liga ACB has an
EXCLUSIVE data-rights partnership with Genius Sports, enforced at ACB
venues — which is almost certainly why neither TheRundown nor The Odds
API bothers carrying it; a real data license costs money for a market
this niche. Highlightly's Basketball API explicitly lists ACB among its
340+ covered leagues (alongside Euroleague, BBL, LNB, etc.) on its free
Basic plan (100 requests/day, no card required).
 
NO ODDS FROM THIS PROVIDER, BY DESIGN: this module intentionally has no
odds/edge counterpart. ACB predictions are pure model output — a
predicted winner + confidence, a predicted total, a predicted half
split — gated only by how confident the model is, with no market
comparison at all. Check acb_prediction.py for that logic and its own
docstring for the reasoning.
 
SETUP REQUIRED: unlike balldontlie/TheRundown (already-configured keys
in this project), Highlightly needs its OWN account and key. Sign up
free at https://highlightly.net/login (or via RapidAPI), Basic/Free
plan, then add the key as a GitHub repo secret named exactly
HIGHLIGHTLY_API_KEY. _get() raises a clear, specific error with these
instructions if the key is missing.
 
VERIFIED AGAINST HIGHLIGHTLY'S PUBLISHED DOCS (basketball-api/documentation
on highlightly.net) — NOT yet run against a real live response, since
that needs the API key only the account holder has. Confidence here is
much higher than the ESPN/TheRundown guesses earlier in this project:
Highlightly's docs give exact field names AND a full example response
for every endpoint used below (not just a schema), so this is built
against confirmed shapes, not inferred ones. Still worth running
`python acb_stats_fetcher.py` directly once the key is set (see the
__main__ block) to confirm live before trusting it for real predictions.
 
RATE LIMIT IS DAILY, NOT PER-MINUTE: the free plan caps at 100
requests/DAY total, not a per-minute rate. That changes the design
goal from "pace requests" (balldontlie's problem) to "minimize total
call COUNT per run" — this module makes exactly one call each for
finding the league, today's matches, and per-team last-five-games/
head-to-head (no day-by-day lookback loop like ncaab_stats_fetcher.py
needed for TheRundown — Highlightly's last-five-games endpoint returns
exactly what's needed in one call). Even so, with ~3 calls per game
analyzed (2x last-five-games + 1x head-to-head), a busy day with many
ACB fixtures plus multiple runs/day could approach the 100/day ceiling
— worth watching in practice, not just assuming it's fine.
"""
 
import os
import time
import datetime
import requests
 
HIGHLIGHTLY_BASE_URL = "https://basketball.highlightly.net"
MIN_GAMES_FOR_ANALYSIS = 5  # matches config.py's WNBA/NCAAB threshold — Highlightly's
                             # last-five-games endpoint caps at 5 anyway, so this is
                             # really just an "all 5 must be present" check here
 
_league_cache = None       # (league_id, season), found once per run
_last_five_cache = {}      # team_id -> list of normalized games, found once per run
_h2h_cache = {}            # frozenset({team_a_id, team_b_id}) -> h2h result
 
 
def _get_api_key():
    key = os.environ.get("HIGHLIGHTLY_API_KEY", "")
    if not key or key.upper() in ("YOUR_API_KEY", "PLACEHOLDER", "CHANGE_ME", ""):
        raise RuntimeError(
            "HIGHLIGHTLY_API_KEY is missing or still a placeholder — no request was sent. "
            "Sign up free at https://highlightly.net/login (Basic/Free plan, 100 requests/day, "
            "no card needed) or via RapidAPI, then add your key as a GitHub repo secret named "
            "EXACTLY 'HIGHLIGHTLY_API_KEY' (Settings -> Secrets and variables -> Actions)."
        )
    return key
 
 
def _get(path, params=None, retries=2):
    for attempt in range(retries + 1):
        resp = requests.get(
            f"{HIGHLIGHTLY_BASE_URL}{path}",
            params=params,
            headers={"x-rapidapi-key": _get_api_key()},
            timeout=15,
        )
        if resp.status_code == 429 and attempt < retries:
            # 429 on a 100/day cap likely means the day's budget is already
            # gone — a short retry won't help much, but two attempts is
            # cheap insurance against a transient blip
            time.sleep(5 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
 
 
def _get_acb_league():
    """
    Finds ACB's (league_id, season) by listing Spain's leagues and
    scanning names for "acb" or "endesa" — more robust than guessing
    Highlightly's exact leagueName spelling, since we get to look at
    the real list rather than assume a filter string matches it
    server-side. Picks the MOST RECENT season in that league's
    `seasons` list, which should be the current one if Highlightly's
    catalog is up to date. Cached for the rest of this run.
    """
    global _league_cache
    if _league_cache is not None:
        return _league_cache
 
    data = _get("/leagues", params={"countryCode": "ES", "limit": 100})
    leagues = data.get("data", [])
 
    acb = next(
        (l for l in leagues if "acb" in l.get("name", "").lower() or "endesa" in l.get("name", "").lower()),
        None,
    )
    if acb is None:
        names = [l.get("name") for l in leagues]
        raise RuntimeError(
            f"No league matching 'acb' or 'endesa' found among Spain's leagues on Highlightly. "
            f"Full list of Spanish league names returned: {names}"
        )
 
    seasons = [s.get("season") for s in acb.get("seasons", []) if s.get("season") is not None]
    if not seasons:
        raise RuntimeError(f"Found ACB (league_id={acb['id']}) but it has no seasons listed at all.")
 
    season = max(seasons)
    _league_cache = (acb["id"], season)
    return _league_cache
 
 
def _parse_score(score_str):
    """'105 - 104' -> (105, 104). Returns (None, None) if not parseable
    (e.g. a game that hasn't started yet has no score string)."""
    if not score_str:
        return None, None
    try:
        home_str, away_str = score_str.split(" - ")
        return int(home_str.strip()), int(away_str.strip())
    except (ValueError, AttributeError):
        return None, None
 
 
def _normalize_match(m):
    """
    Converts one Highlightly match into the SAME shape stats_fetcher.py's
    balldontlie-backed functions return, PLUS two extra fields not
    present in the WNBA/NCAAB versions:
      - "quarters": each quarter's home/away split — lets
        acb_prediction.py build a real half-split model instead of the
        flat-proportion fallback the other sports are stuck with.
      - "league_id": CONFIRMED NECESSARY FROM A LIVE RUN 2026-10-01 —
        Highlightly's /last-five-games has NO league filter at all (its
        own docs confirm this: it returns a team's last 5 finished
        games across EVERY competition that team plays in). A real run
        showed Barcelona's "last 5" mixing genuine ACB opponents with
        EuroLeague ones (Dubai, Anadolu Efes Istanbul) — several ACB
        clubs play both competitions. league_id lets
        get_team_recent_games()/get_head_to_head_record() filter back
        down to ACB-only games client-side, since the API can't do it
        server-side.
      {"id", "date", "status", "home_team": {"id","full_name"},
       "visitor_team": {"id","full_name"}, "home_score", "away_score",
       "league_id", "quarters": {"q1": (h,a), ...}}
    """
    state = m.get("state", {})
    description = state.get("description", "")
    is_finished = description in ("Finished", "Finished after over time")
 
    score = state.get("score", {})
    home_score, away_score = _parse_score(score.get("current"))
 
    quarters = {}
    for q in ("q1", "q2", "q3", "q4"):
        h, a = _parse_score(score.get(q))
        quarters[q] = (h, a)
 
    return {
        "id": m.get("id"),
        "date": m.get("date"),
        "status": "post" if is_finished else "scheduled",
        "home_team": {"id": m.get("homeTeam", {}).get("id"), "full_name": m.get("homeTeam", {}).get("name")},
        "visitor_team": {"id": m.get("awayTeam", {}).get("id"), "full_name": m.get("awayTeam", {}).get("name")},
        "home_score": home_score,
        "away_score": away_score,
        "league_id": m.get("league", {}).get("id"),
        "quarters": quarters,
    }
 
 
def get_todays_games():
    """Returns today's scheduled (not-yet-finished) ACB games."""
    league_id, season = _get_acb_league()
    today = datetime.date.today().isoformat()
 
    data = _get("/matches", params={"leagueId": league_id, "season": season, "date": today})
    matches = data.get("data", [])
 
    games = [_normalize_match(m) for m in matches]
    return [g for g in games if g["status"] != "post"]
 
 
def get_team_recent_games(team_id, num_games=5):
    """
    Returns the team's last 5 finished ACB games — note "ACB", not just
    "finished games". Highlightly's /last-five-games has no league
    filter at all (confirmed from its own docs AND a live run that
    showed Barcelona's raw "last 5" mixing real ACB opponents with
    EuroLeague ones, since several ACB clubs play both competitions).
    This filters the raw response down to league_id == ACB's own,
    using each match's own embedded league info (the one piece of
    per-game league scoping the API does provide, even though the
    endpoint itself doesn't accept a league filter as a parameter).
 
    REAL CONSEQUENCE OF THIS, not a bug to "fix" further: for a club
    that also plays EuroLeague, filtering can leave FEWER than 5 ACB
    games in their most recent 5 games overall (e.g. 3 ACB + 2
    EuroLeague in their last 5 total), which may make
    team_form_summary() correctly return None for that club more often
    during multi-competition stretches than it would for an ACB-only
    club. That's the honest, correct behavior given what the free tier
    actually offers — better to skip a prediction than build one on a
    too-small or contaminated sample. Cached per run, pre-filter.
    """
    if team_id in _last_five_cache:
        return _last_five_cache[team_id][:num_games]
 
    acb_league_id, _ = _get_acb_league()
    raw = _get("/last-five-games", params={"teamId": team_id})
    games = [_normalize_match(m) for m in raw]
    games = [g for g in games if g["status"] == "post" and g["league_id"] == acb_league_id]
    games.sort(key=lambda g: g["date"] or "", reverse=True)
 
    _last_five_cache[team_id] = games
    return games[:num_games]
 
 
def get_head_to_head_record(team_a_id, team_b_id, num_matchups=5):
    """
    Same contract as the other sports' versions. Highlightly's
    head-2-head endpoint already returns the last 10 meetings directly
    (no lookback loop needed, unlike NCAAB's TheRundown-based version).
    Same ACB-only filter as get_team_recent_games(), for the same
    reason: two clubs that both play EuroLeague could have met there
    too, and a EuroLeague meeting isn't necessarily representative of
    how they play each other in ACB specifically (different rotations,
    different stakes). Returns None if fewer than 2 ACB matchups on
    record — same small-sample protection used everywhere else in this
    project.
    """
    key = frozenset({team_a_id, team_b_id})
    if key in _h2h_cache:
        return _h2h_cache[key]
 
    acb_league_id, _ = _get_acb_league()
    raw = _get("/head-2-head", params={"teamIdOne": team_a_id, "teamIdTwo": team_b_id})
    matchups = [_normalize_match(m) for m in raw]
    matchups = [m for m in matchups if m["status"] == "post" and m["league_id"] == acb_league_id]
 
    if len(matchups) < 2:
        _h2h_cache[key] = None
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
 
    result = {"matchups_found": len(matchups), "team_a_win_pct": a_wins / len(matchups)}
    _h2h_cache[key] = result
    return result
 
 
def team_form_summary(team_id, as_of_date=None, include_pace=True):
    """
    Same contract/return shape as stats_fetcher.team_form_summary(),
    PLUS four ACB-only fields (avg_first_half_points_scored/allowed,
    avg_second_half_points_scored/allowed) computed from REAL
    quarter-by-quarter data — not available for WNBA/NCAAB, and what
    lets acb_prediction.py build a genuine half-split model instead of
    a flat 50/50 guess. `include_pace` accepted for interface
    compatibility but ignored — pace isn't built for ACB either (same
    scope decision as NCAAB; Highlightly's Team Statistics endpoint
    could support this later, not built for this first pass).
    """
    games = get_team_recent_games(team_id)
    if len(games) < MIN_GAMES_FOR_ANALYSIS:
        return None
 
    wins = 0
    point_diffs, points_scored, points_allowed = [], [], []
    first_half_scored, first_half_allowed = [], []
    second_half_scored, second_half_allowed = [], []
 
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
 
        q = g["quarters"]
        q1h, q1a = q.get("q1", (None, None))
        q2h, q2a = q.get("q2", (None, None))
        q3h, q3a = q.get("q3", (None, None))
        q4h, q4a = q.get("q4", (None, None))
        if None not in (q1h, q1a, q2h, q2a):
            team_1h = (q1h + q2h) if is_home else (q1a + q2a)
            opp_1h = (q1a + q2a) if is_home else (q1h + q2h)
            first_half_scored.append(team_1h)
            first_half_allowed.append(opp_1h)
        if None not in (q3h, q3a, q4h, q4a):
            team_2h = (q3h + q4h) if is_home else (q3a + q4a)
            opp_2h = (q3a + q4a) if is_home else (q3h + q4h)
            second_half_scored.append(team_2h)
            second_half_allowed.append(opp_2h)
 
    if len(point_diffs) < MIN_GAMES_FOR_ANALYSIS:
        return None
 
    def _avg(values):
        return sum(values) / len(values) if values else None
 
    return {
        "games_sampled": len(point_diffs),
        "win_pct": wins / len(point_diffs),
        "avg_point_diff": sum(point_diffs) / len(point_diffs),
        "avg_points_scored": sum(points_scored) / len(points_scored),
        "avg_points_allowed": sum(points_allowed) / len(points_allowed),
        "days_rest": None,  # not built yet — see module docstring; add if it turns out to matter for ACB
        "pace": None,       # not built for ACB — see docstring
        # ACB-only, real quarter data (falls back to None if any game was missing quarter data):
        "avg_first_half_points_scored": _avg(first_half_scored),
        "avg_first_half_points_allowed": _avg(first_half_allowed),
        "avg_second_half_points_scored": _avg(second_half_scored),
        "avg_second_half_points_allowed": _avg(second_half_allowed),
    }
 
 
if __name__ == "__main__":
    # Quick manual test — run: python acb_stats_fetcher.py
    # Requires HIGHLIGHTLY_API_KEY to be set (see module docstring).
    league_id, season = _get_acb_league()
    print(f"ACB league_id={league_id}, season={season}")
 
    games = get_todays_games()
    print(f"\nget_todays_games(): {len(games)} game(s) found today.")
    for g in games:
        print(f"  {g['visitor_team']['full_name']} @ {g['home_team']['full_name']} (status: {g['status']})")
 
    # "0 games today" is genuinely ambiguous on its own — ACB doesn't play
    # every day (mostly weekend rounds), so an empty day could be entirely
    # normal OR could mean league_id/season is subtly wrong and this always
    # returns nothing. Checking a KNOWN date (the season-opening weekend)
    # resolves that ambiguity unconditionally, regardless of what day this
    # happens to be run on.
    print("\n--- Sanity check against the known season-opening weekend (2026-09-26/27) ---")
    opening_weekend = _get("/matches", params={"leagueId": league_id, "season": season, "date": "2026-09-26"})
    opening_matches = opening_weekend.get("data", [])
    print(f"Matches found on 2026-09-26: {len(opening_matches)}")
    if not opening_matches:
        opening_weekend_2 = _get("/matches", params={"leagueId": league_id, "season": season, "date": "2026-09-27"})
        opening_matches = opening_weekend_2.get("data", [])
        print(f"Matches found on 2026-09-27: {len(opening_matches)}")
    if opening_matches:
        print("CONFIRMED: league_id/season combo is correct — real jornada-1 games found. "
              "An empty 'today' is therefore a genuine off-day, not a config problem.")
        opening_games = []
        for m in opening_matches[:3]:
            g = _normalize_match(m)
            opening_games.append(g)
            print(f"  {g['visitor_team']['full_name']} @ {g['home_team']['full_name']} "
                  f"(ids {g['visitor_team']['id']}/{g['home_team']['id']}) "
                  f"-> {g['home_score']}-{g['away_score']} (status: {g['status']})")
 
        # /last-five-games and /head-2-head have NEVER been hit live yet —
        # "today" being empty meant that section of this script never ran.
        # Test them directly against real team IDs now, rather than assume
        # they'll work whenever the next real game day comes around.
        print("\n--- Testing /last-five-games and /head-2-head against real team IDs ---")
        test_team = opening_games[0]["home_team"]
        print(f"get_team_recent_games({test_team['id']}) [{test_team['full_name']}]:")
        recent = get_team_recent_games(test_team["id"])
        print(f"  {len(recent)} finished game(s) found (expect FEWER than 5 right now — "
              f"jornada 1 was the season opener, so no team can have 5 games played yet; "
              f"this is just confirming the endpoint itself responds sanely, not testing "
              f"the full 5-game model yet).")
        for g in recent:
            print(f"    {g['visitor_team']['full_name']} @ {g['home_team']['full_name']} "
                  f"-> {g['home_score']}-{g['away_score']} | quarters: {g['quarters']}")
 
        team_a, team_b = opening_games[0]["home_team"], opening_games[0]["visitor_team"]
        print(f"\nget_head_to_head_record({team_a['id']}, {team_b['id']}) "
              f"[{team_a['full_name']} vs {team_b['full_name']}]:")
        h2h = get_head_to_head_record(team_a["id"], team_b["id"])
        print(f"  {h2h}")
        print("  (None here is EXPECTED right now — needs 2+ past meetings, and these two "
              "have likely only just played their first meeting of the new season, if that "
              "was even their first ever meeting on record with this provider.)")
    else:
        print("WARNING: zero matches found even on the known season-opening weekend. "
              "This points to season or league_id being wrong, NOT just an off-day — "
              "worth checking the /leagues raw response (the acb dict found above) for "
              "its exact 'seasons' list and confirming 2026 is really in it.")
 
    if games:
        home = games[0]["home_team"]
        print(f"\nPulling form for {home['full_name']} (id={home['id']})...")
        recent = get_team_recent_games(home["id"])
        print(f"  {len(recent)} recent finished game(s) found.")
        form = team_form_summary(home["id"])
        print(f"  team_form_summary(): {form}")
    else:
        print("\nNo games today — check the league_id/season above look right, and that ACB "
              "actually has fixtures today (18 teams, 4-9 games/week — not every day has games).")
 
