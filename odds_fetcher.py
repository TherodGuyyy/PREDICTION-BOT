"""
Fetches WNBA moneyline, totals (over/under), and sub-market (team/half/
quarter totals) odds from TheRundown API (free tier) — replaces OddsPapi.

Public functions below have the EXACT SAME names, arguments, and return
shapes as the old OddsPapi version, so main.py and analysis.py needed
zero changes for this swap. Only the internals (which provider, how a
"fixture" is found and parsed) changed.

CONFIRMED FROM THEROUNDOWN'S PUBLISHED DOCS, not yet verified live (this
build environment has no network access to test with). Moneyline and
full-game/half/quarter totals are built against a well-documented part
of TheRundown's schema (market_id + period_id filtering) and should be
solid. Team totals (market_id=94) is the one genuinely uncertain part —
see the big comment above get_team_totals_odds() for why, and treat its
output with more suspicion than the rest until you've verified it
against a real game with real team-total lines posted.

Run `python odds_fetcher.py` directly (see the bottom of this file) to
print today's WNBA fixtures and a full breakdown of what each market
lookup found — check that against what you see on an actual sportsbook
before trusting this in production, same as you'd want to for any new
data source.
"""

import datetime
import rundown_client as rc

_wnba_sport_id = None
_events_cache = {}  # (date_str,) window key -> merged events list


def _get_wnba_sport_id():
    global _wnba_sport_id
    if _wnba_sport_id is None:
        _wnba_sport_id = rc.find_sport_id("WNBA")
    return _wnba_sport_id


def _get_wnba_events_for_date(date_str):
    """
    Same reasoning as the old OddsPapi version's wide window: fetch
    today AND tomorrow's date so a late-tipoff West Coast game that
    TheRundown might bucket under the next UTC date is still found.
    """
    if date_str in _events_cache:
        return _events_cache[date_str]

    sport_id = _get_wnba_sport_id()
    start = datetime.date.fromisoformat(date_str)
    tomorrow = (start + datetime.timedelta(days=1)).isoformat()

    events = rc.get_events_for_dates(sport_id, [date_str, tomorrow])
    _events_cache[date_str] = events
    return events


def _names_match(a, b):
    a, b = (a or "").lower(), (b or "").lower()
    return a in b or b in a


def _find_event(home_team_name, away_team_name, date_str):
    """
    Returns (event, home_is_teams1) or (None, None). Prefers a
    not-finished event when multiple matches exist between the same two
    teams in the query window — same logic as the old OddsPapi code.
    """
    events = _get_wnba_events_for_date(date_str)
    matches = []

    for ev in events:
        away_name, home_name = rc.team_names(ev)
        if away_name is None:
            continue
        if _names_match(home_name, home_team_name) and _names_match(away_name, away_team_name):
            matches.append((ev, True))
        elif _names_match(away_name, home_team_name) and _names_match(home_name, away_team_name):
            # names came back swapped vs. what we expected — still a match,
            # just note home is actually in the teams[0]/away slot
            matches.append((ev, False))

    if not matches:
        return None, None

    not_finished = [m for m in matches if not rc.event_is_finished(m[0])]
    if not_finished:
        return not_finished[0]
    return matches[0]


def debug_fixture_status(home_team_name, away_team_name, date_str):
    event, _ = _find_event(home_team_name, away_team_name, date_str)
    if not event:
        events = _get_wnba_events_for_date(date_str)
        names = [f"{rc.team_names(e)[0]} @ {rc.team_names(e)[1]}" for e in events]
        return (
            f"no event matched '{home_team_name}' vs '{away_team_name}' — "
            f"TheRundown WNBA events found today: {names}"
        )
    return (
        f"event matched (id={event.get('event_id')}), "
        f"finished={rc.event_is_finished(event)}, "
        f"event_date={event.get('event_date')}"
    )


def get_match_odds(home_team_name, away_team_name, date_str):
    """
    Returns {"home_odds": float, "away_odds": float} or None.
    """
    event, home_is_teams1 = _find_event(home_team_name, away_team_name, date_str)
    if not event:
        return None

    prices = rc.best_price_per_participant(event, rc.MARKET_MONEYLINE, rc.PERIOD_FULL_GAME)
    if not prices:
        return None

    away_name, home_name = rc.team_names(event)
    if not home_is_teams1:
        # names came back in the swapped slot — see _find_event's second branch
        home_name, away_name = away_name, home_name

    home_odds = next((p for n, p in prices.items() if _names_match(n, home_team_name)), None)
    away_odds = next((p for n, p in prices.items() if _names_match(n, away_team_name)), None)

    if home_odds is None and away_odds is None:
        return None
    return {"home_odds": home_odds, "away_odds": away_odds}


def get_totals_odds(home_team_name, away_team_name, date_str):
    """
    Returns [{"line": 165.5, "over_odds": ..., "under_odds": ..., "market_id": 3}, ...]
    """
    event, _ = _find_event(home_team_name, away_team_name, date_str)
    if not event:
        return []

    by_line = rc.best_price_per_line(event, rc.MARKET_TOTAL, rc.PERIOD_FULL_GAME)
    return [
        {"line": line, "over_odds": v["over"], "under_odds": v["under"], "market_id": rc.MARKET_TOTAL}
        for line, v in sorted(by_line.items())
    ]


def get_first_half_totals_odds(home_team_name, away_team_name, date_str):
    event, _ = _find_event(home_team_name, away_team_name, date_str)
    if not event:
        return []
    by_line = rc.best_price_per_line(event, rc.MARKET_TOTAL, rc.PERIOD_FIRST_HALF)
    return [
        {"line": line, "over_odds": v["over"], "under_odds": v["under"], "market_id": rc.MARKET_TOTAL}
        for line, v in sorted(by_line.items())
    ]


def get_quarter_totals_odds(home_team_name, away_team_name, date_str, quarter_num=1):
    period_id = rc.PERIOD_QUARTER.get(quarter_num)
    if period_id is None:
        return []
    event, _ = _find_event(home_team_name, away_team_name, date_str)
    if not event:
        return []
    by_line = rc.best_price_per_line(event, rc.MARKET_TOTAL, period_id)
    return [
        {"line": line, "over_odds": v["over"], "under_odds": v["under"], "market_id": rc.MARKET_TOTAL}
        for line, v in sorted(by_line.items())
    ]


# ---------------------------------------------------------------------------
# get_team_totals_odds — THE LEAST CERTAIN PART OF THIS FILE.
#
# TheRundown's docs confirm market_id=94 is "Team Totals" but don't show a
# worked example of its participant/line shape the way they do for
# Moneyline and Total. Two plausible shapes exist:
#   (a) participants are the two TEAM names, each with two lines whose
#       "value" is the same number but tagged over/under some other way
#   (b) participants are named things like "Aces Over"/"Aces Under" —
#       i.e. team name AND side folded into one participant name
# This code handles BOTH: it groups by team name match first (same
# _names_match approach as the old OddsPapi sub-market code), then
# checks each matched participant's own name for "over"/"under" to
# assign a side; if that fails, it falls back to price ordering (lower
# price usually implies favorite side, but this is a weak fallback).
# BEFORE TRUSTING THIS IN PRODUCTION: run this file directly on a night
# with team-total lines posted and check the printed output against a
# real sportsbook. If it's wrong, main.py already wraps this call in a
# try/except (see main.py's run_wnba), so a bad result here just means
# no team-totals tip that day, never a wrong one silently posted.
# ---------------------------------------------------------------------------
def get_team_totals_odds(home_team_name, away_team_name, date_str):
    event, _ = _find_event(home_team_name, away_team_name, date_str)
    if not event:
        return {"home": [], "away": []}

    home_rows, away_rows = [], []
    for name, value, price in rc.iter_market_prices(event, rc.MARKET_TEAM_TOTAL, rc.PERIOD_FULL_GAME):
        if value is None or name is None:
            continue
        lower = name.lower()
        side = "over" if "over" in lower else ("under" if "under" in lower else None)

        is_home = _names_match(home_team_name, name)
        is_away = _names_match(away_team_name, name)
        if is_home == is_away:
            continue  # ambiguous or unrelated — skip rather than guess

        target = home_rows if is_home else away_rows
        target.append((value, side, price))

    def _to_list(rows):
        by_line = {}
        for value, side, price in rows:
            entry = by_line.setdefault(value, {"over": None, "under": None})
            if side in ("over", "under"):
                if entry[side] is None or price > entry[side]:
                    entry[side] = price
        return [
            {"line": line, "over_odds": v["over"], "under_odds": v["under"], "market_id": rc.MARKET_TEAM_TOTAL}
            for line, v in sorted(by_line.items())
            if v["over"] is not None or v["under"] is not None
        ]

    return {"home": _to_list(home_rows), "away": _to_list(away_rows)}


if __name__ == "__main__":
    print("WNBA sportId:", _get_wnba_sport_id())

    today = datetime.date.today().isoformat()
    print(f"\nEvents today+tomorrow window ({today}):")
    events = _get_wnba_events_for_date(today)
    print(f"Found {len(events)} WNBA event(s).\n")

    for ev in events:
        away, home = rc.team_names(ev)
        print(f"  {away} @ {home} | finished: {rc.event_is_finished(ev)} | event_date: {ev.get('event_date')}")
        if not ev.get("markets"):
            print(f"    NOTE: no 'markets' key/data on this event — raw keys: {list(ev.keys())}")

    if events:
        away, home = rc.team_names(events[0])
        if home and away:
            print(f"\nPulling odds for: {away} @ {home}")
            print("Moneyline:", get_match_odds(home, away, today))
            print("Totals:", get_totals_odds(home, away, today))
            print("First-half totals:", get_first_half_totals_odds(home, away, today))
            print("1st-quarter totals:", get_quarter_totals_odds(home, away, today, quarter_num=1))
            print("Team totals (LEAST CERTAIN — verify this one manually):",
                  get_team_totals_odds(home, away, today))
    else:
        print("\nNo events found — nothing to test odds lookups against right now.")
