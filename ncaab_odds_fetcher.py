"""
Fetches NCAA Men's Basketball moneyline, totals, and sub-market (team/
half/quarter totals) odds from TheRundown API (free tier) — same backend
and same rundown_client.py as odds_fetcher.py (WNBA), just pointed at a
different sport_id. Kept as its own file rather than parameterizing
odds_fetcher.py directly, matching this project's existing pattern of
one fetcher file per sport (see tennis_odds_fetcher.py) — easier to read
and to safely change one sport without risking another.

Public functions here have the EXACT SAME names and return shapes as
odds_fetcher.py's WNBA versions, so main.py's NCAAB run function can
reuse the same calling pattern with zero surprises.

SPORT LOOKUP NOTE: TheRundown's /sports list has BOTH "NCAA Football"
and "NCAA Men's Basketball" (or possibly "NCAA Basketball" — their own
docs use both spellings in different places). A plain find_sport_id
("NCAA") would risk matching football instead, since that lookup
returns on the first substring match and football's sport_id is lower.
must_also_contain=["basketball"] (added to rundown_client.py alongside
this file) requires BOTH substrings, so this is safe regardless of
which of the two exact spellings TheRundown's live response uses.

UNVERIFIED LIVE, same caveat as odds_fetcher.py: built against
TheRundown's published docs, not a live response. Run this file
directly (see __main__ block) once there are real NCAAB fixtures to
check the output against a real sportsbook, same as you'd want for any
new data source.
"""

import datetime
import rundown_client as rc

_ncaab_sport_id = None
_events_cache = {}  # date_str -> merged events list


def _get_ncaab_sport_id():
    global _ncaab_sport_id
    if _ncaab_sport_id is None:
        _ncaab_sport_id = rc.find_sport_id("NCAA", must_also_contain=["basketball"])
    return _ncaab_sport_id


def _get_ncaab_events_for_date(date_str):
    if date_str in _events_cache:
        return _events_cache[date_str]

    sport_id = _get_ncaab_sport_id()
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
    Same logic as odds_fetcher.py's _find_event. Worth flagging for
    NCAAB specifically: with 360+ teams (vs WNBA's ~13), the odds of two
    similarly-named schools causing a false substring match are higher
    than they were for WNBA — e.g. multiple "State" schools, or a short
    name that's a substring of an unrelated longer one. If fixtures
    start getting mismatched, this is the first place to tighten
    (require a closer match than plain substring containment).
    """
    events = _get_ncaab_events_for_date(date_str)
    matches = []

    for ev in events:
        away_name, home_name = rc.team_names(ev)
        if away_name is None:
            continue
        if _names_match(home_name, home_team_name) and _names_match(away_name, away_team_name):
            matches.append((ev, True))
        elif _names_match(away_name, home_team_name) and _names_match(home_name, away_team_name):
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
        events = _get_ncaab_events_for_date(date_str)
        names = [f"{rc.team_names(e)[0]} @ {rc.team_names(e)[1]}" for e in events]
        return (
            f"no event matched '{home_team_name}' vs '{away_team_name}' — "
            f"TheRundown NCAAB events found today: {names}"
        )
    return (
        f"event matched (id={event.get('event_id')}), "
        f"finished={rc.event_is_finished(event)}, "
        f"event_date={event.get('event_date')}"
    )


def get_match_odds(home_team_name, away_team_name, date_str):
    event, home_is_teams1 = _find_event(home_team_name, away_team_name, date_str)
    if not event:
        return None

    prices = rc.best_price_per_participant(event, rc.MARKET_MONEYLINE, rc.PERIOD_FULL_GAME)
    if not prices:
        return None

    away_name, home_name = rc.team_names(event)
    if not home_is_teams1:
        home_name, away_name = away_name, home_name

    home_odds = next((p for n, p in prices.items() if _names_match(n, home_team_name)), None)
    away_odds = next((p for n, p in prices.items() if _names_match(n, away_team_name)), None)

    if home_odds is None and away_odds is None:
        return None
    return {"home_odds": home_odds, "away_odds": away_odds}


def get_totals_odds(home_team_name, away_team_name, date_str):
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
    # NOTE: college basketball is played in HALVES, not quarters. This
    # function exists only for interface parity with odds_fetcher.py —
    # main.py's NCAAB run does NOT call this (see main.py comment).
    # Left in rather than deleted in case TheRundown's period_id scheme
    # ever adds a college-specific split; currently always returns [].
    return []


def get_team_totals_odds(home_team_name, away_team_name, date_str):
    """Same LEAST-CERTAIN caveat as odds_fetcher.get_team_totals_odds() — see that docstring."""
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
            continue

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
    print("NCAAB sportId:", _get_ncaab_sport_id())

    today = datetime.date.today().isoformat()
    print(f"\nEvents today+tomorrow window ({today}):")
    events = _get_ncaab_events_for_date(today)
    print(f"Found {len(events)} NCAAB event(s).\n")

    for ev in events:
        away, home = rc.team_names(ev)
        print(f"  {away} @ {home} | finished: {rc.event_is_finished(ev)} | event_date: {ev.get('event_date')}")

    if events:
        away, home = rc.team_names(events[0])
        if home and away:
            print(f"\nPulling odds for: {away} @ {home}")
            print("Moneyline:", get_match_odds(home, away, today))
            print("Totals:", get_totals_odds(home, away, today))
            print("First-half totals:", get_first_half_totals_odds(home, away, today))
            print("Team totals (LEAST CERTAIN — verify manually):", get_team_totals_odds(home, away, today))
    else:
        print("\nNo events found — expected before the season starts (Nov 1, 2026).")
