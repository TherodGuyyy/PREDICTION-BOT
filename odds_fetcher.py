"""
Fetches WNBA moneyline AND totals (over/under) odds from OddsPapi (free
tier), built against their actual published API reference
(oddspapi.io/en/docs/...).

Key facts about their schema, confirmed via live testing:
  - /v4/fixtures requires tournamentId (or participantId, or a date
    range) — sportId alone is rejected with a 400.
  - /v4/odds does NOT include market names, only numeric market/outcome
    IDs — so market definitions (including, for totals, the actual
    point line via the "handicap" field) are looked up once via
    /v4/markets and cached.
  - Each specific totals LINE (e.g. 160.5, 165.5) is its own separate
    market with its own marketId — there's no single generic "totals"
    market. Different bookmakers may offer different lines for the
    same game.
  - Prices come back in "price" as decimal odds directly.
  - One /v4/odds call per fixture returns ALL markets that bookmaker
    prices for that game in one response — so moneyline and totals are
    both pulled from a SINGLE odds fetch per fixture, cached by
    fixture ID, not fetched separately. This matters for staying
    inside the free tier's request limits.
"""

import time
import math
import requests
from config import ODDSPAPI_API_KEY

BASE_URL = "https://api.oddspapi.io/v4"

# cached per run — see module docstring for why this matters on a free tier
_basketball_sport_id = None
_wnba_tournament_id = None
_moneyline_market = None          # (market_id, outcome_a_id, outcome_b_id)
_totals_markets = None            # list of {market_id, handicap, over_id, under_id}
_fixtures_cache = {}              # date_str -> fixtures list
_odds_cache = {}                  # fixture_id -> raw /v4/odds response


def _get(path, params, retries=2):
    params = {**params, "apiKey": ODDSPAPI_API_KEY}
    for attempt in range(retries + 1):
        resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=15)
        if resp.status_code == 429 and attempt < retries:
            time.sleep(2 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()


def _get_basketball_sport_id():
    global _basketball_sport_id
    if _basketball_sport_id is not None:
        return _basketball_sport_id

    sports = _get("/sports", {"language": "en"})
    for sport in sports:
        if sport.get("sportName", "").lower() == "basketball":
            _basketball_sport_id = sport["sportId"]
            return _basketball_sport_id

    raise RuntimeError("Couldn't find 'Basketball' in OddsPapi's /sports list.")


def _get_wnba_tournament_id():
    global _wnba_tournament_id
    if _wnba_tournament_id is not None:
        return _wnba_tournament_id

    sport_id = _get_basketball_sport_id()
    tournaments = _get("/tournaments", {"sportId": sport_id, "language": "en"})

    for t in tournaments:
        if "wnba" in t.get("tournamentName", "").lower():
            _wnba_tournament_id = t["tournamentId"]
            return _wnba_tournament_id

    raise RuntimeError(
        "Couldn't find a WNBA tournament in OddsPapi's /tournaments list for "
        "sportId=basketball. Run this file directly to print the full list."
    )


def _get_moneyline_market():
    """
    Finds the basketball market for a straight match-winner bet (2
    outcomes, no draw possible in basketball). Returns
    (market_id, outcome_a_id, outcome_b_id).
    """
    global _moneyline_market
    if _moneyline_market is not None:
        return _moneyline_market

    sport_id = _get_basketball_sport_id()
    markets = _get("/markets", {"language": "en"})

    candidates = [
        m for m in markets
        if m.get("sportId") == sport_id
        and m.get("marketLength") == 2
        and any(kw in m.get("marketName", "").lower()
                for kw in ["moneyline", "winner", "match result", "match odds", "to win"])
        and not any(kw in m.get("marketName", "").lower() for kw in ["over", "under", "total"])
    ]

    if not candidates:
        raise RuntimeError(
            "Couldn't find a 2-outcome basketball moneyline market in "
            "OddsPapi's /v4/markets list. Run this file directly to see all "
            "basketball markets and pick manually."
        )

    market = candidates[0]
    outcome_ids = [o["outcomeId"] for o in market["outcomes"]]
    _moneyline_market = (market["marketId"], outcome_ids[0], outcome_ids[1])
    return _moneyline_market


def _get_totals_markets():
    """
    Finds ALL basketball totals (over/under) markets — one per distinct
    line, e.g. 160.5, 165.5, 170.5. Returns a list of dicts:
    [{"market_id": ..., "line": 165.5, "over_id": ..., "under_id": ...}, ...]
    """
    global _totals_markets
    if _totals_markets is not None:
        return _totals_markets

    sport_id = _get_basketball_sport_id()
    markets = _get("/markets", {"language": "en"})

    results = []
    for m in markets:
        if m.get("sportId") != sport_id:
            continue
        if m.get("marketType") != "totals":
            continue
        name = m.get("marketName", "").lower()
        if "over" not in name and "under" not in name and "total" not in name:
            continue

        outcomes = m.get("outcomes", [])
        over_id = next((o["outcomeId"] for o in outcomes if o.get("outcomeName", "").lower() == "over"), None)
        under_id = next((o["outcomeId"] for o in outcomes if o.get("outcomeName", "").lower() == "under"), None)
        if over_id is None or under_id is None:
            continue

        results.append({
            "market_id": m["marketId"],
            "line": m.get("handicap"),
            "over_id": over_id,
            "under_id": under_id,
        })

    _totals_markets = results
    return _totals_markets


def _get_wnba_fixtures_for_date(date_str):
    """date_str: 'YYYY-MM-DD'. Returns fixtures on that day for WNBA."""
    if date_str in _fixtures_cache:
        return _fixtures_cache[date_str]

    tournament_id = _get_wnba_tournament_id()
    fixtures = _get("/fixtures", {
        "tournamentId": tournament_id,
        "from": f"{date_str}T00:00:00Z",
        "to": f"{date_str}T23:59:59Z",
    })
    _fixtures_cache[date_str] = fixtures
    return fixtures


def _names_match(a, b):
    a, b = a.lower(), b.lower()
    return a in b or b in a


def _find_fixture(home_team_name, away_team_name, date_str):
    """Returns (fixture, home_is_participant1) or (None, None)."""
    fixtures = _get_wnba_fixtures_for_date(date_str)

    for f in fixtures:
        p1, p2 = f.get("participant1Name", ""), f.get("participant2Name", "")
        if _names_match(p1, home_team_name) and _names_match(p2, away_team_name):
            return f, True
        if _names_match(p1, away_team_name) and _names_match(p2, home_team_name):
            return f, False

    return None, None


def _get_odds_for_fixture(fixture_id):
    """
    Fetches (and caches) the FULL odds response for a fixture — this
    includes every market a bookmaker prices for that game, so both
    moneyline and totals extraction reuse this single call instead of
    each fetching odds separately.
    """
    if fixture_id in _odds_cache:
        return _odds_cache[fixture_id]

    data = _get("/odds", {"fixtureId": fixture_id})
    _odds_cache[fixture_id] = data
    return data


def get_match_odds(home_team_name, away_team_name, date_str):
    """
    Returns {"home_odds": float, "away_odds": float} or None if the
    fixture or odds couldn't be found/matched.
    """
    fixture, home_is_p1 = _find_fixture(home_team_name, away_team_name, date_str)
    if not fixture or not fixture.get("hasOdds"):
        return None

    market_id, outcome_a, outcome_b = _get_moneyline_market()
    home_outcome_id = outcome_a if home_is_p1 else outcome_b
    away_outcome_id = outcome_b if home_is_p1 else outcome_a

    odds_data = _get_odds_for_fixture(fixture["fixtureId"])

    best_home, best_away = None, None
    for book_slug, book_data in odds_data.get("bookmakerOdds", {}).items():
        market = book_data.get("markets", {}).get(str(market_id))
        if not market:
            continue
        outcomes = market.get("outcomes", {})

        home_price = outcomes.get(str(home_outcome_id), {}).get("players", {}).get("0", {}).get("price")
        away_price = outcomes.get(str(away_outcome_id), {}).get("players", {}).get("0", {}).get("price")

        if home_price is not None and (best_home is None or home_price > best_home):
            best_home = home_price
        if away_price is not None and (best_away is None or away_price > best_away):
            best_away = away_price

    if best_home is None and best_away is None:
        return None

    return {"home_odds": best_home, "away_odds": best_away}


def get_totals_odds(home_team_name, away_team_name, date_str):
    """
    Returns a list of available totals lines for this game, each with
    the best (highest) over/under price found across bookmakers:
    [{"line": 165.5, "over_odds": 1.85, "under_odds": 1.95}, ...]
    Returns an empty list if the fixture/odds aren't found, or no
    bookmaker prices any totals line for this game.
    """
    fixture, _ = _find_fixture(home_team_name, away_team_name, date_str)
    if not fixture or not fixture.get("hasOdds"):
        return []

    totals_markets = _get_totals_markets()
    if not totals_markets:
        return []

    odds_data = _get_odds_for_fixture(fixture["fixtureId"])

    # best_by_line: line -> {"over_odds": ..., "under_odds": ...}
    best_by_line = {}

    for book_slug, book_data in odds_data.get("bookmakerOdds", {}).items():
        markets = book_data.get("markets", {})
        for tm in totals_markets:
            market = markets.get(str(tm["market_id"]))
            if not market:
                continue
            outcomes = market.get("outcomes", {})

            over_price = outcomes.get(str(tm["over_id"]), {}).get("players", {}).get("0", {}).get("price")
            under_price = outcomes.get(str(tm["under_id"]), {}).get("players", {}).get("0", {}).get("price")

            if over_price is None and under_price is None:
                continue

            line = tm["line"]
            entry = best_by_line.setdefault(line, {"over_odds": None, "under_odds": None})
            if over_price is not None and (entry["over_odds"] is None or over_price > entry["over_odds"]):
                entry["over_odds"] = over_price
            if under_price is not None and (entry["under_odds"] is None or under_price > entry["under_odds"]):
                entry["under_odds"] = under_price

    return [
        {"line": line, "over_odds": v["over_odds"], "under_odds": v["under_odds"]}
        for line, v in sorted(best_by_line.items())
    ]


if __name__ == "__main__":
    import json
    import datetime

    print("Basketball sportId:", _get_basketball_sport_id())
    print("WNBA tournamentId:", _get_wnba_tournament_id())

    print("\nSearching for the moneyline market definition...")
    try:
        market_id, oc_a, oc_b = _get_moneyline_market()
        print(f"Found market_id={market_id}, outcomes=({oc_a}, {oc_b})")
    except RuntimeError as e:
        print(f"NOT FOUND: {e}")

    print("\nSearching for totals (over/under) market definitions...")
    totals = _get_totals_markets()
    print(f"Found {len(totals)} totals line(s):")
    for t in totals:
        print(f"  line {t['line']}  (market_id={t['market_id']}, over_id={t['over_id']}, under_id={t['under_id']})")

    today = datetime.date.today().isoformat()
    print(f"\nFixtures today ({today}):")
    fixtures = _get_wnba_fixtures_for_date(today)
    print(f"Found {len(fixtures)} WNBA fixture(s).\n")

    for f in fixtures:
        print(
            f"  {f.get('participant1Name')} vs {f.get('participant2Name')} | "
            f"status: {f.get('statusName')} | hasOdds: {f.get('hasOdds')} | "
            f"start: {f.get('startTime')}"
        )

    live_fixture = next((f for f in fixtures if f.get("hasOdds")), None)
    if live_fixture:
        p1, p2 = live_fixture["participant1Name"], live_fixture["participant2Name"]
        print(f"\nPulling live odds for: {p1} vs {p2}")

        ml = get_match_odds(p1, p2, today)
        print("Moneyline result:", ml)

        totals_odds = get_totals_odds(p1, p2, today)
        print("Totals result:", totals_odds)
    else:
        print("\nNo fixture with hasOdds=true right now — try again closer to game time.")
