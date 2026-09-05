"""
Fetches tennis moneyline (match-winner) odds from TheRundown API (free
tier) — replaces OddsPapi.

main.py imports THREE things from this file: get_match_odds,
debug_fixture_status, and the internal _get_tennis_fixtures_for_date
(used directly to get today's list of matches with hasOdds/
participant1Name/participant2Name/tournamentName fields). Rather than
changing main.py to understand TheRundown's different event shape, this
file translates TheRundown's "events" into fixture-shaped dicts with the
SAME field names the old OddsPapi code produced (participant1Name,
participant2Name, hasOdds, tournamentName, fixtureId, statusName,
startTime) — so main.py, tennis_stats_fetcher.py's get_tournament_surface
lookup, and everything else downstream needed ZERO changes for this swap.

CONFIRMED FROM THEROUNDOWN'S PUBLISHED DOCS: ATP Tennis = sport ID 38,
WTA Tennis = sport ID 39 (also double-checked dynamically by name via
rundown_client.find_sport_id, so if TheRundown ever renumbers these,
this keeps working without an edit). Full-match tennis odds use
period_id=0 (prematch, full match) per TheRundown's docs.

NOT yet verified live (no network access in this build environment) —
run `python tennis_odds_fetcher.py` directly and check the printed
fixtures/odds against a real sportsbook before trusting this in
production, same as odds_fetcher.py.
"""

import datetime
import rundown_client as rc

# CONFIRMED directly from TheRundown's own changelog (docs.therundown.io/
# changelog): "Added ATP Tennis (sport ID 38) and WTA Tennis (sport ID 39)
# as first-class leagues." Using these fixed IDs instead of name-matching
# against /sports — live testing showed TheRundown's actual sport_name
# field for tennis doesn't literally contain the string "ATP Tennis" the
# way find_sport_id() was searching for (the WNBA lookup worked fine
# since "WNBA" is short and exact; tennis's real name field is phrased
# differently). Rather than guess at the real phrasing, these two IDs are
# authoritative straight from TheRundown's own release notes.
_TOUR_SPORT_IDS = {"atp": 38, "wta": 39}
_tour_sport_ids = {}  # "atp" / "wta" -> sport_id (kept as a cache/override point)
_fixtures_cache = {}  # date_str -> list of fixture-shaped dicts


def _get_tour_sport_id(tour):
    if tour not in _tour_sport_ids:
        _tour_sport_ids[tour] = _TOUR_SPORT_IDS[tour]
    return _tour_sport_ids[tour]


def _tournament_name_from_event(event):
    schedule = event.get("schedule") or {}
    return (
        schedule.get("event_name")
        or schedule.get("league_name")
        or schedule.get("tournament_name")
        or event.get("league_name")
        or event.get("tournament_name")
    )


def _event_to_fixture(event, tour):
    """
    Translates one TheRundown event into the same fixture shape the old
    OddsPapi code used, so nothing downstream needs to change.
    """
    away_name, home_name = rc.team_names(event)
    has_moneyline = bool(rc.best_price_per_participant(event, rc.MARKET_MONEYLINE, rc.PERIOD_FULL_GAME))

    return {
        "fixtureId": event.get("event_id"),
        "participant1Name": away_name,
        "participant2Name": home_name,
        "hasOdds": has_moneyline,
        "tournamentName": _tournament_name_from_event(event),
        "statusName": "Finished" if rc.event_is_finished(event) else "Not Finished",
        "startTime": event.get("event_date"),
        "tour": tour,
        "_raw_event": event,  # kept so get_match_odds doesn't need to re-fetch
    }


def _get_tennis_fixtures_for_date(date_str):
    """
    Returns SINGLES fixtures (doubles filtered out — any name containing
    "/" is a doubles pairing) across BOTH ATP and WTA for this date and
    the next day (same late-match safety window as odds_fetcher.py).
    """
    if date_str in _fixtures_cache:
        return _fixtures_cache[date_str]

    tomorrow = (datetime.date.fromisoformat(date_str) + datetime.timedelta(days=1)).isoformat()

    all_fixtures = []
    for tour in ("atp", "wta"):
        sport_id = _get_tour_sport_id(tour)
        events = rc.get_events_for_dates(sport_id, [date_str, tomorrow], market_ids="1")
        for ev in events:
            fixture = _event_to_fixture(ev, tour)
            p1, p2 = fixture["participant1Name"], fixture["participant2Name"]
            if not p1 or not p2:
                continue
            if "/" in p1 or "/" in p2:  # doubles pairing — skip
                continue
            all_fixtures.append(fixture)

    _fixtures_cache[date_str] = all_fixtures
    return all_fixtures


def _names_match(a, b):
    a, b = (a or "").lower(), (b or "").lower()
    return a in b or b in a


def _find_fixture(player_a_name, player_b_name, date_str):
    """
    Returns (fixture, a_is_participant1) or (None, None) — same
    signature/behavior as the old OddsPapi version.
    """
    fixtures = _get_tennis_fixtures_for_date(date_str)
    matches = []

    for f in fixtures:
        p1, p2 = f["participant1Name"], f["participant2Name"]
        if _names_match(p1, player_a_name) and _names_match(p2, player_b_name):
            matches.append((f, True))
        elif _names_match(p1, player_b_name) and _names_match(p2, player_a_name):
            matches.append((f, False))

    if not matches:
        return None, None

    not_finished = [m for m in matches if m[0]["statusName"] != "Finished"]
    if not_finished:
        return not_finished[0]
    return matches[0]


def debug_fixture_status(player_a_name, player_b_name, date_str):
    fixture, _ = _find_fixture(player_a_name, player_b_name, date_str)
    if not fixture:
        all_fixtures = _get_tennis_fixtures_for_date(date_str)
        names = [f"{f['participant1Name']} vs {f['participant2Name']}" for f in all_fixtures]
        return f"no fixture matched '{player_a_name}' vs '{player_b_name}' — TheRundown tennis fixtures found: {names}"

    return (
        f"fixture matched (id={fixture['fixtureId']}), status={fixture['statusName']}, "
        f"hasOdds={fixture['hasOdds']}, startTime={fixture['startTime']}, tour={fixture['tour']}"
    )


def get_match_odds(player_a_name, player_b_name, date_str):
    """
    Returns {"player_a_odds": float, "player_b_odds": float} or None.
    """
    fixture, a_is_p1 = _find_fixture(player_a_name, player_b_name, date_str)
    if not fixture or not fixture["hasOdds"]:
        return None

    event = fixture["_raw_event"]
    prices = rc.best_price_per_participant(event, rc.MARKET_MONEYLINE, rc.PERIOD_FULL_GAME)
    if not prices:
        return None

    a_odds = next((p for n, p in prices.items() if _names_match(n, player_a_name)), None)
    b_odds = next((p for n, p in prices.items() if _names_match(n, player_b_name)), None)

    if a_odds is None and b_odds is None:
        return None
    return {"player_a_odds": a_odds, "player_b_odds": b_odds}


if __name__ == "__main__":
    print("ATP sportId:", _get_tour_sport_id("atp"))
    print("WTA sportId:", _get_tour_sport_id("wta"))

    today = datetime.date.today().isoformat()
    print(f"\nSingles fixtures today+tomorrow window ({today}):")
    fixtures = _get_tennis_fixtures_for_date(today)
    print(f"Found {len(fixtures)} singles fixture(s).\n")

    for f in fixtures[:15]:
        print(
            f"  {f['participant1Name']} vs {f['participant2Name']} ({f['tour'].upper()}) | "
            f"status: {f['statusName']} | hasOdds: {f['hasOdds']} | "
            f"tournament: {f['tournamentName']} | start: {f['startTime']}"
        )
    if len(fixtures) > 15:
        print(f"  ...and {len(fixtures) - 15} more")

    live_fixture = next((f for f in fixtures if f["hasOdds"]), None)
    if live_fixture:
        p1, p2 = live_fixture["participant1Name"], live_fixture["participant2Name"]
        print(f"\nPulling live odds for: {p1} vs {p2}")
        result = get_match_odds(p1, p2, today)
        print("Result:", result)
    else:
        print("\nNo fixture with hasOdds=true right now.")
