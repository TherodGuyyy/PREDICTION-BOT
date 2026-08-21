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
import datetime
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
_all_markets_raw = None           # RAW /v4/markets response, fetched ONCE per run and
                                   # reused by every market finder below (moneyline,
                                   # totals, team totals, half totals, quarter totals).
                                   # Each finder used to independently re-fetch the full
                                   # markets catalog — harmless in isolation, but on a
                                   # rate-limited free tier, 5 separate full-catalog
                                   # fetches (one per finder) instead of 1 shared one is
                                   # exactly the kind of thing that burns through a
                                   # request budget fast and triggers 429s partway
                                   # through a run.


def _get(path, params, retries=3):
    """
    retries=3 (up from 2) and now retries on THREE failure modes, not
    just 429:
      - 429 (rate limited) — retried, as before
      - 5xx (OddsPapi's own server erroring) — these are usually
        transient blips on their end, not something wrong with our
        request, so worth riding out with backoff rather than giving up
        on the whole game after one bad response
      - network-level failures (read timeouts, connection resets) — these
        never even reach the status-code check below, since they raise
        before requests.get() returns a response at all. A single slow
        response used to take the whole game down immediately; now it
        gets the same backoff-and-retry treatment as the others.
    A persistent, non-transient outage on OddsPapi's side will still
    fail after exhausting retries — this doesn't paper over a real
    outage, just stops one transient hiccup from skipping a whole game.
    """
    params = {**params, "apiKey": ODDSPAPI_API_KEY}
    for attempt in range(retries + 1):
        try:
            resp = requests.get(f"{BASE_URL}{path}", params=params, timeout=20)
        except requests.exceptions.RequestException:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise

        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2 * (attempt + 1))
            continue

        resp.raise_for_status()
        return resp.json()


def _get_all_basketball_markets():
    """
    Fetches OddsPapi's full /v4/markets catalog ONCE per run and caches
    it — every market finder (moneyline, totals, team totals, half
    totals, quarter totals) filters from this single cached list instead
    of each hitting /v4/markets independently. This is the fix for a
    real issue: those 5 finders used to each fetch the whole catalog
    separately, which on a rate-limited free tier could burn through
    the request budget before a run even got through its first game.
    """
    global _all_markets_raw
    if _all_markets_raw is None:
        _all_markets_raw = _get("/markets", {"language": "en"})
    return _all_markets_raw


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
    markets = _get_all_basketball_markets()

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
    markets = _get_all_basketball_markets()

    # keywords that indicate this is NOT a full-game total (a quarter, a
    # half, a single team's total, or a player prop) — these all use the
    # same marketType="totals" classification but with wildly different,
    # much smaller line values, which is exactly what caused a quarter or
    # half total (~49.5) to get treated as if it were a full-game total.
    exclude_keywords = [
        "quarter", "1st", "2nd", "3rd", "4th", "first", "second", "third", "fourth",
        "half", "period", "team", "player", "points by",
    ]

    results = []
    for m in markets:
        if m.get("sportId") != sport_id:
            continue
        if m.get("marketType") != "totals":
            continue
        name = m.get("marketName", "").lower()
        if "over" not in name and "under" not in name and "total" not in name:
            continue
        if any(kw in name for kw in exclude_keywords):
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


# ---------------------------------------------------------------------------
# Sub-markets: individual team totals, first-half totals, quarter totals.
#
# IMPORTANT CAVEAT: the moneyline and full-game-totals finders above were
# built and confirmed against LIVE OddsPapi responses (per the module
# docstring). The functions below follow the exact same pattern, but this
# environment can't reach api.oddspapi.io directly to verify them the same
# way — so the keyword matching here is a best-effort guess at how
# OddsPapi names these sub-markets, not a confirmed fact. Run this file
# directly (`python odds_fetcher.py`) and check the printed sub-market
# lists against a real game before trusting these in production — if the
# keywords below don't match OddsPapi's actual naming, these will just
# silently find nothing (fail safe — no odds means no tip, never a wrong
# tip) rather than error out, but you also won't get the sub-market tips
# you're expecting until the keyword lists are corrected to match.
# ---------------------------------------------------------------------------

def _get_period_totals_markets(include_keywords, exclude_keywords=None):
    """
    Generic finder for a totals sub-market: returns markets whose name
    contains ANY of include_keywords and NONE of exclude_keywords.
    Includes "market_name" in each result (unlike the main totals finder)
    so callers matching a specific TEAM can inspect the raw name.
    """
    sport_id = _get_basketball_sport_id()
    markets = _get_all_basketball_markets()
    exclude_keywords = exclude_keywords or []

    results = []
    for m in markets:
        if m.get("sportId") != sport_id:
            continue
        if m.get("marketType") != "totals":
            continue
        name = m.get("marketName", "").lower()
        if "over" not in name and "under" not in name and "total" not in name:
            continue
        if not any(kw in name for kw in include_keywords):
            continue
        if any(kw in name for kw in exclude_keywords):
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
            "market_name": m.get("marketName", ""),
        })
    return results


_half_totals_markets = None


def _get_first_half_totals_markets():
    global _half_totals_markets
    if _half_totals_markets is None:
        _half_totals_markets = _get_period_totals_markets(
            include_keywords=["1st half", "first half"],
            exclude_keywords=["team", "quarter"],
        )
    return _half_totals_markets


_quarter_totals_markets = {}  # keyed by quarter_num


def _get_quarter_totals_markets(quarter_num=1):
    if quarter_num not in _quarter_totals_markets:
        keywords_by_quarter = {
            1: ["1st quarter", "first quarter", "q1"],
            2: ["2nd quarter", "second quarter", "q2"],
            3: ["3rd quarter", "third quarter", "q3"],
            4: ["4th quarter", "fourth quarter", "q4"],
        }
        include = keywords_by_quarter.get(quarter_num)
        if not include:
            return []
        _quarter_totals_markets[quarter_num] = _get_period_totals_markets(
            include_keywords=include,
            exclude_keywords=["team", "half"],
        )
    return _quarter_totals_markets[quarter_num]


_team_totals_markets = None


def _get_team_totals_markets():
    global _team_totals_markets
    if _team_totals_markets is None:
        _team_totals_markets = _get_period_totals_markets(
            include_keywords=["team total", "team points"],
            exclude_keywords=["half", "quarter"],
        )
    return _team_totals_markets


def _best_odds_by_line(fixture_id, markets):
    """
    Shared aggregation: given a fixture and a list of market defs (each
    with market_id/line/over_id/under_id), returns the best (highest)
    over/under price per distinct line across all bookmakers. Same core
    logic as get_totals_odds, factored out so the sub-market functions
    below don't each reimplement it.
    """
    if not markets:
        return {}

    odds_data = _get_odds_for_fixture(fixture_id)
    best_by_line = {}

    for book_slug, book_data in odds_data.get("bookmakerOdds", {}).items():
        book_markets = book_data.get("markets", {})
        for tm in markets:
            market = book_markets.get(str(tm["market_id"]))
            if not market:
                continue
            outcomes = market.get("outcomes", {})

            over_price = outcomes.get(str(tm["over_id"]), {}).get("players", {}).get("0", {}).get("price")
            under_price = outcomes.get(str(tm["under_id"]), {}).get("players", {}).get("0", {}).get("price")
            if over_price is None and under_price is None:
                continue

            line = tm["line"]
            key = (line, tm["market_id"])  # keep markets distinct even if lines happen to collide
            entry = best_by_line.setdefault(key, {
                "line": line, "over_odds": None, "under_odds": None,
                "market_id": tm["market_id"], "market_name": tm.get("market_name", ""),
            })
            if over_price is not None and (entry["over_odds"] is None or over_price > entry["over_odds"]):
                entry["over_odds"] = over_price
            if under_price is not None and (entry["under_odds"] is None or under_price > entry["under_odds"]):
                entry["under_odds"] = under_price

    return best_by_line


def get_first_half_totals_odds(home_team_name, away_team_name, date_str):
    """
    Returns first-half totals lines, same shape as get_totals_odds():
    [{"line": 82.5, "over_odds": 1.9, "under_odds": 1.9, "market_id": ...}, ...]
    Empty list if the fixture/odds aren't found, or (see module-level
    caveat above) if this WNBA odds feed doesn't label half markets the
    way this code expects yet.
    """
    fixture, _ = _find_fixture(home_team_name, away_team_name, date_str)
    if not fixture or not fixture.get("hasOdds"):
        return []
    markets = _get_first_half_totals_markets()
    best_by_line = _best_odds_by_line(fixture["fixtureId"], markets)
    return [
        {"line": v["line"], "over_odds": v["over_odds"], "under_odds": v["under_odds"], "market_id": v["market_id"]}
        for v in sorted(best_by_line.values(), key=lambda v: (v["line"] is None, v["line"]))
        if v["line"] is not None
    ]


def get_quarter_totals_odds(home_team_name, away_team_name, date_str, quarter_num=1):
    """
    Returns totals lines for a single quarter (default: 1st), same shape
    as get_totals_odds(). Same empty-list-on-not-found behavior as
    get_first_half_totals_odds.
    """
    fixture, _ = _find_fixture(home_team_name, away_team_name, date_str)
    if not fixture or not fixture.get("hasOdds"):
        return []
    markets = _get_quarter_totals_markets(quarter_num)
    best_by_line = _best_odds_by_line(fixture["fixtureId"], markets)
    return [
        {"line": v["line"], "over_odds": v["over_odds"], "under_odds": v["under_odds"], "market_id": v["market_id"]}
        for v in sorted(best_by_line.values(), key=lambda v: (v["line"] is None, v["line"]))
        if v["line"] is not None
    ]


def get_team_totals_odds(home_team_name, away_team_name, date_str):
    """
    Returns individual-team totals lines, split by team:
    {"home": [{"line", "over_odds", "under_odds", "market_id"}, ...],
     "away": [...]}
    A market is assigned to a team by checking whether that team's name
    appears in the market's raw name (e.g. "Las Vegas Aces Total Points")
    — if a market can't be confidently matched to either team, it's
    skipped rather than guessed at.
    """
    fixture, _ = _find_fixture(home_team_name, away_team_name, date_str)
    if not fixture or not fixture.get("hasOdds"):
        return {"home": [], "away": []}

    markets = _get_team_totals_markets()
    if not markets:
        return {"home": [], "away": []}

    home_markets, away_markets = [], []
    for m in markets:
        name = m.get("market_name", "")
        home_match = _names_match(home_team_name, name)
        away_match = _names_match(away_team_name, name)
        if home_match and not away_match:
            home_markets.append(m)
        elif away_match and not home_match:
            away_markets.append(m)
        # if both or neither match (ambiguous / unrelated market), skip it —
        # never guess which team a total belongs to

    home_by_line = _best_odds_by_line(fixture["fixtureId"], home_markets)
    away_by_line = _best_odds_by_line(fixture["fixtureId"], away_markets)

    def _to_list(by_line):
        return [
            {"line": v["line"], "over_odds": v["over_odds"], "under_odds": v["under_odds"], "market_id": v["market_id"]}
            for v in sorted(by_line.values(), key=lambda v: (v["line"] is None, v["line"]))
            if v["line"] is not None
        ]

    return {"home": _to_list(home_by_line), "away": _to_list(away_by_line)}


def _get_wnba_fixtures_for_date(date_str):
    """
    date_str: 'YYYY-MM-DD'. Returns fixtures for that day for WNBA.

    IMPORTANT: the query window is deliberately wider than just that one
    UTC calendar day. WNBA games often tip off late enough in US time
    (e.g. 9-10pm ET, or even later for West Coast teams) that they cross
    into the NEXT UTC day. A strict "today 00:00 to 23:59 UTC" window can
    completely miss tonight's actual game, while still matching an
    already-finished OLDER meeting between the same two teams that
    happens to fall inside that narrow window — which is exactly the
    bug this fixed (a "Finished" fixture from ~9pm the previous US
    evening was getting matched instead of tonight's upcoming game).
    So we query from today 00:00 UTC through tomorrow 12:00 UTC, which
    comfortably covers even the latest West Coast tip-offs.
    """
    if date_str in _fixtures_cache:
        return _fixtures_cache[date_str]

    tournament_id = _get_wnba_tournament_id()
    start = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    end = start + datetime.timedelta(days=1, hours=12)

    fixtures = _get("/fixtures", {
        "tournamentId": tournament_id,
        "from": f"{date_str}T00:00:00Z",
        "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    _fixtures_cache[date_str] = fixtures
    return fixtures


def _names_match(a, b):
    a, b = a.lower(), b.lower()
    return a in b or b in a


def _find_fixture(home_team_name, away_team_name, date_str):
    """
    Returns (fixture, home_is_participant1) or (None, None).

    Because the query window (see _get_wnba_fixtures_for_date) is wider
    than one calendar day, it's possible to find MORE than one past
    meeting between the same two teams — an old finished game plus
    tonight's upcoming one. When that happens, we deliberately prefer a
    fixture that hasn't finished yet (Pre-Game/Live) over a Finished
    one, since a finished game can never have current odds anyway and
    tonight's game is what we actually care about.
    """
    fixtures = _get_wnba_fixtures_for_date(date_str)
    matches = []

    for f in fixtures:
        p1, p2 = f.get("participant1Name", ""), f.get("participant2Name", "")
        if _names_match(p1, home_team_name) and _names_match(p2, away_team_name):
            matches.append((f, True))
        elif _names_match(p1, away_team_name) and _names_match(p2, home_team_name):
            matches.append((f, False))

    if not matches:
        return None, None

    # prefer a non-finished fixture if one exists among the matches
    not_finished = [m for m in matches if m[0].get("statusName") != "Finished"]
    if not_finished:
        return not_finished[0]

    return matches[0]


def debug_fixture_status(home_team_name, away_team_name, date_str):
    """
    Returns a short human-readable diagnostic string explaining exactly
    why a game's odds might be missing — used to make GitHub Actions
    logs self-explanatory without needing to run anything locally.
    """
    fixture, _ = _find_fixture(home_team_name, away_team_name, date_str)
    if not fixture:
        all_fixtures = _get_wnba_fixtures_for_date(date_str)
        names = [f"{f.get('participant1Name')} vs {f.get('participant2Name')}" for f in all_fixtures]
        return (
            f"no fixture matched '{home_team_name}' vs '{away_team_name}' — "
            f"OddsPapi fixtures found today: {names}"
        )

    return (
        f"fixture matched (id={fixture.get('fixtureId')}), "
        f"status={fixture.get('statusName')}, hasOdds={fixture.get('hasOdds')}, "
        f"startTime={fixture.get('startTime')}"
    )


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

    # best_by_line: line -> {"over_odds": ..., "under_odds": ..., "market_id": ...}
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
            entry = best_by_line.setdefault(line, {"over_odds": None, "under_odds": None, "market_id": tm["market_id"]})
            if over_price is not None and (entry["over_odds"] is None or over_price > entry["over_odds"]):
                entry["over_odds"] = over_price
            if under_price is not None and (entry["under_odds"] is None or under_price > entry["under_odds"]):
                entry["under_odds"] = under_price

    return [
        {"line": line, "over_odds": v["over_odds"], "under_odds": v["under_odds"], "market_id": v["market_id"]}
        for line, v in sorted(best_by_line.items())
    ]


if __name__ == "__main__":
    import json

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
    print(f"Found {len(totals)} full-game totals line(s):")
    for t in totals:
        print(f"  line {t['line']}  (market_id={t['market_id']}, over_id={t['over_id']}, under_id={t['under_id']})")

    print("\nSearching for SUB-MARKET definitions (team / half / quarter totals)...")
    print("NOTE: these keyword matches are unverified against live OddsPapi data —")
    print("if a section below is empty but you expect that market to exist for")
    print("today's games, the keyword lists in odds_fetcher.py need adjusting.")

    half_markets = _get_first_half_totals_markets()
    print(f"\nFirst-half totals: found {len(half_markets)} market(s)")
    for m in half_markets:
        print(f"  '{m['market_name']}'  line={m['line']}  market_id={m['market_id']}")

    q1_markets = _get_quarter_totals_markets(1)
    print(f"\n1st-quarter totals: found {len(q1_markets)} market(s)")
    for m in q1_markets:
        print(f"  '{m['market_name']}'  line={m['line']}  market_id={m['market_id']}")

    team_markets = _get_team_totals_markets()
    print(f"\nIndividual team totals: found {len(team_markets)} market(s)")
    for m in team_markets:
        print(f"  '{m['market_name']}'  line={m['line']}  market_id={m['market_id']}")

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

        print("\nSub-market results (may be empty — see note above):")
        print("Team totals:", get_team_totals_odds(p1, p2, today))
        print("First-half totals:", get_first_half_totals_odds(p1, p2, today))
        print("1st-quarter totals:", get_quarter_totals_odds(p1, p2, today, quarter_num=1))
    else:
        print("\nNo fixture with hasOdds=true right now — try again closer to game time.")
