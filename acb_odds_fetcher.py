"""
Fetches Spanish ACB (Liga Endesa) moneyline and totals odds from
TheRundown API (free tier) — same backend and same rundown_client.py as
odds_fetcher.py (WNBA) and ncaab_odds_fetcher.py, just pointed at ACB.

SPORT LOOKUP: unlike WNBA/NCAAB, ACB's exact name in TheRundown's
/sports list was never confirmed ahead of time (no live way to check
until this actually runs). _get_acb_sport_id() tries several plausible
names in order via rundown_client.find_sport_id_trying() — "ACB",
"Liga Endesa", "Liga ACB", "Spanish Basketball" — and raises a single
clear error listing everything it tried plus the full raw sports list
if NONE of them match. That error is the answer to "does TheRundown
even carry this league" — run `python acb_odds_fetcher.py` directly
(see __main__ block) to find out for real.

Public functions here have the SAME NAMES AND RETURN SHAPES as
odds_fetcher.py's WNBA versions, so main.py's ACB run function can reuse
the same calling pattern with zero surprises.

SCOPE: moneyline + full-game totals only, no team/half/quarter
sub-markets — ACB is a low-volume "quality leg" source, not a volume
one (see rollover-grand-audit-bot-spec-v2.md), so the sub-market
functions from the WNBA/NCAAB versions weren't worth carrying over for
a first pass. Add them later if ACB proves out and sub-market odds
turn out to be posted for it at all.
"""

import datetime
import rundown_client as rc

_acb_sport_id = None
_events_cache = {}  # date_str -> merged events list


def _get_acb_sport_id():
    global _acb_sport_id
    if _acb_sport_id is None:
        _acb_sport_id = rc.find_sport_id_trying("ACB", "Liga Endesa", "Liga ACB", "Spanish Basketball")
    return _acb_sport_id


def _get_acb_events_for_date(date_str):
    if date_str in _events_cache:
        return _events_cache[date_str]

    sport_id = _get_acb_sport_id()
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
    Same logic as odds_fetcher.py's _find_event. ACB's small 18-team
    league makes name collisions much less likely than NCAAB's 360+, so
    plain substring matching should be safe here without the extra
    caution that file needed.
    """
    events = _get_acb_events_for_date(date_str)
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
        events = _get_acb_events_for_date(date_str)
        names = [f"{rc.team_names(e)[0]} @ {rc.team_names(e)[1]}" for e in events]
        return (
            f"no event matched '{home_team_name}' vs '{away_team_name}' — "
            f"TheRundown ACB events found today: {names}"
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


if __name__ == "__main__":
    print("ACB sportId:", _get_acb_sport_id())

    today = datetime.date.today().isoformat()
    print(f"\nEvents today+tomorrow window ({today}):")
    events = _get_acb_events_for_date(today)
    print(f"Found {len(events)} ACB event(s).\n")

    for ev in events:
        away, home = rc.team_names(ev)
        print(f"  {away} @ {home} | finished: {rc.event_is_finished(ev)} | event_date: {ev.get('event_date')}")

    if events:
        target = next((ev for ev in events if not rc.event_is_finished(ev)), events[0])
        away, home = rc.team_names(target)
        if home and away:
            print(f"\nPulling odds for: {away} @ {home}")
            print("Moneyline:", get_match_odds(home, away, today))
            print("Totals:", get_totals_odds(home, away, today))
    else:
        print("\nNo events found today — check the sport-lookup line above first: if it errored, "
              "that's the real finding (TheRundown may not carry ACB at all, or use a name none of "
              "the tried candidates matched). If it resolved fine, this may just be a day off in the "
              "ACB schedule (18 teams, 4-9 games/week — not every day has fixtures).")
