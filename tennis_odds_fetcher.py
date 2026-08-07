"""
Fetches tennis moneyline (match-winner) odds from OddsPapi (free tier).

Structurally similar to odds_fetcher.py (basketball), with two real
differences learned from building that one:

1. Tennis has no single "tournament" like WNBA — there are many
   concurrent tournaments across the tour at any given time. OddsPapi's
   own docs confirm sportId CAN be queried directly against /fixtures
   as long as it's paired with a date range (from/to, under 10 days
   apart) — no tournamentId required. That's what we do here.

2. Singles vs doubles: doubles matches have two names per side (e.g.
   "Player A/Player B"). We only want singles, so any fixture with a
   "/" in a participant name is filtered out.

Same lessons from the basketball build are already baked in here from
the start (rather than discovered live again): a widened-enough date
window, preferring non-finished fixtures when multiple matches exist
between the same two names, and a market-name filter that excludes
set/game sub-markets so we only ever get the full-match winner odds.
"""

import time
import datetime
import requests
from config import ODDSPAPI_API_KEY

BASE_URL = "https://api.oddspapi.io/v4"

# Tennis can have dozens of matches on a busy day across concurrent
# tournaments — far more than WNBA's handful of games. Each match needing
# its own /v4/odds call is exactly the pattern that caused a real
# rate-limit bug on the balldontlie side earlier. Pacing requests here
# proactively rather than waiting to hit the same failure mode live.
MIN_SECONDS_BETWEEN_REQUESTS = 1.0
_last_request_time = 0

_tennis_sport_id = None
_moneyline_market = None       # (market_id, outcome_a_id, outcome_b_id)
_fixtures_cache = {}           # date_str -> fixtures list
_odds_cache = {}                # fixture_id -> raw /v4/odds response


def _get(path, params, retries=2):
    global _last_request_time
    params = {**params, "apiKey": ODDSPAPI_API_KEY}

    for attempt in range(retries + 1):
        elapsed = time.time() - _last_request_time
        if elapsed < MIN_SECONDS_BETWEEN_REQUESTS:
            time.sleep(MIN_SECONDS_BETWEEN_REQUESTS - elapsed)

        resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=15)
        _last_request_time = time.time()

        if resp.status_code == 429 and attempt < retries:
            time.sleep(3 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()


def _get_tennis_sport_id():
    global _tennis_sport_id
    if _tennis_sport_id is not None:
        return _tennis_sport_id

    sports = _get("/sports", {"language": "en"})
    for sport in sports:
        if sport.get("sportName", "").lower() == "tennis":
            _tennis_sport_id = sport["sportId"]
            return _tennis_sport_id

    raise RuntimeError("Couldn't find 'Tennis' in OddsPapi's /sports list.")


def _get_moneyline_market():
    """
    Finds the tennis match-winner market (2 outcomes: player A / player
    B). Excludes set-handicap, game-handicap, and total-games sub-markets
    which use the same general shape but mean something completely
    different.
    """
    global _moneyline_market
    if _moneyline_market is not None:
        return _moneyline_market

    sport_id = _get_tennis_sport_id()
    markets = _get("/markets", {"language": "en"})

    exclude_keywords = ["set", "game", "handicap", "total", "over", "under", "aces", "double fault"]

    candidates = [
        m for m in markets
        if m.get("sportId") == sport_id
        and m.get("marketLength") == 2
        and any(kw in m.get("marketName", "").lower() for kw in ["winner", "match odds", "moneyline", "to win"])
        and not any(kw in m.get("marketName", "").lower() for kw in exclude_keywords)
    ]

    if not candidates:
        raise RuntimeError(
            "Couldn't find a 2-outcome tennis match-winner market in "
            "OddsPapi's /v4/markets list. Run this file directly to see "
            "all tennis markets and pick manually."
        )

    market = candidates[0]
    outcome_ids = [o["outcomeId"] for o in market["outcomes"]]
    _moneyline_market = (market["marketId"], outcome_ids[0], outcome_ids[1])
    return _moneyline_market


def _get_tennis_fixtures_for_date(date_str):
    """
    date_str: 'YYYY-MM-DD'. Returns SINGLES fixtures only (doubles
    filtered out) for a window from that day through the next day at
    noon UTC — same late-match/UTC-boundary safety margin used for WNBA.
    """
    if date_str in _fixtures_cache:
        return _fixtures_cache[date_str]

    sport_id = _get_tennis_sport_id()
    start = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    end = start + datetime.timedelta(days=1, hours=12)

    fixtures = _get("/fixtures", {
        "sportId": sport_id,
        "from": f"{date_str}T00:00:00Z",
        "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })

    singles_only = [
        f for f in fixtures
        if "/" not in f.get("participant1Name", "") and "/" not in f.get("participant2Name", "")
    ]

    _fixtures_cache[date_str] = singles_only
    return singles_only


def _names_match(a, b):
    a, b = a.lower(), b.lower()
    return a in b or b in a


def _find_fixture(player_a_name, player_b_name, date_str):
    """
    Returns (fixture, player_a_is_participant1) or (None, None).
    Prefers a non-finished match if multiple fixtures exist between the
    same two names in the query window (same reasoning as the WNBA fix).
    """
    fixtures = _get_tennis_fixtures_for_date(date_str)
    matches = []

    for f in fixtures:
        p1, p2 = f.get("participant1Name", ""), f.get("participant2Name", "")
        if _names_match(p1, player_a_name) and _names_match(p2, player_b_name):
            matches.append((f, True))
        elif _names_match(p1, player_b_name) and _names_match(p2, player_a_name):
            matches.append((f, False))

    if not matches:
        return None, None

    not_finished = [m for m in matches if m[0].get("statusName") != "Finished"]
    if not_finished:
        return not_finished[0]

    return matches[0]


def debug_fixture_status(player_a_name, player_b_name, date_str):
    """Same diagnostic pattern used for the basketball bot."""
    fixture, _ = _find_fixture(player_a_name, player_b_name, date_str)
    if not fixture:
        all_fixtures = _get_tennis_fixtures_for_date(date_str)
        names = [f"{f.get('participant1Name')} vs {f.get('participant2Name')}" for f in all_fixtures]
        return f"no fixture matched '{player_a_name}' vs '{player_b_name}' — OddsPapi tennis fixtures found: {names}"

    return (
        f"fixture matched (id={fixture.get('fixtureId')}), "
        f"status={fixture.get('statusName')}, hasOdds={fixture.get('hasOdds')}, "
        f"startTime={fixture.get('startTime')}"
    )


def _get_odds_for_fixture(fixture_id):
    if fixture_id in _odds_cache:
        return _odds_cache[fixture_id]
    data = _get("/odds", {"fixtureId": fixture_id})
    _odds_cache[fixture_id] = data
    return data


def get_match_odds(player_a_name, player_b_name, date_str):
    """
    Returns {"player_a_odds": float, "player_b_odds": float} or None if
    the fixture or odds couldn't be found/matched.
    """
    fixture, a_is_p1 = _find_fixture(player_a_name, player_b_name, date_str)
    if not fixture or not fixture.get("hasOdds"):
        return None

    market_id, outcome_a, outcome_b = _get_moneyline_market()
    a_outcome_id = outcome_a if a_is_p1 else outcome_b
    b_outcome_id = outcome_b if a_is_p1 else outcome_a

    odds_data = _get_odds_for_fixture(fixture["fixtureId"])

    best_a, best_b = None, None
    for book_slug, book_data in odds_data.get("bookmakerOdds", {}).items():
        market = book_data.get("markets", {}).get(str(market_id))
        if not market:
            continue
        outcomes = market.get("outcomes", {})

        a_price = outcomes.get(str(a_outcome_id), {}).get("players", {}).get("0", {}).get("price")
        b_price = outcomes.get(str(b_outcome_id), {}).get("players", {}).get("0", {}).get("price")

        if a_price is not None and (best_a is None or a_price > best_a):
            best_a = a_price
        if b_price is not None and (best_b is None or b_price > best_b):
            best_b = b_price

    if best_a is None and best_b is None:
        return None

    return {"player_a_odds": best_a, "player_b_odds": best_b}


if __name__ == "__main__":
    import json

    print("Tennis sportId:", _get_tennis_sport_id())

    print("\nSearching for the match-winner market definition...")
    try:
        market_id, oc_a, oc_b = _get_moneyline_market()
        print(f"Found market_id={market_id}, outcomes=({oc_a}, {oc_b})")
    except RuntimeError as e:
        print(f"NOT FOUND: {e}")

    today = datetime.date.today().isoformat()
    print(f"\nSingles fixtures today ({today}):")
    fixtures = _get_tennis_fixtures_for_date(today)
    print(f"Found {len(fixtures)} singles fixture(s).\n")

    for f in fixtures[:15]:  # tennis can have MANY matches on a given day, cap the printout
        print(
            f"  {f.get('participant1Name')} vs {f.get('participant2Name')} | "
            f"status: {f.get('statusName')} | hasOdds: {f.get('hasOdds')} | "
            f"start: {f.get('startTime')}"
        )
    if len(fixtures) > 15:
        print(f"  ...and {len(fixtures) - 15} more")

    live_fixture = next((f for f in fixtures if f.get("hasOdds")), None)
    if live_fixture:
        p1, p2 = live_fixture["participant1Name"], live_fixture["participant2Name"]
        print(f"\nPulling live odds for: {p1} vs {p2}")
        print(f"Raw fixture keys (check for a surface field here): {list(live_fixture.keys())}")
        result = get_match_odds(p1, p2, today)
        print("Result:", result)
    else:
        print("\nNo fixture with hasOdds=true right now.")
