"""
NBA odds from TheRundown (free tier: moneyline + full-game totals from
DraftKings, FanDuel and BetMGM). Same backend and helper module as the
WNBA/NCAAB odds fetchers, pointed at the NBA.

TWO THINGS THAT WERE EASY TO GET WRONG, and are handled here:

1. SPORT LOOKUP. A plain substring search for "nba" also matches "WNBA"
   (the letters nba are inside wnba), and could silently return the WNBA's
   id. This file requires the sport name to be exactly "NBA".

2. TEAM NAME MATCHING. balldontlie says "Los Angeles Clippers"; an odds
   feed may say "LA Clippers". Plain "is one name inside the other" fails
   there. So two names also match when their LAST WORD matches ("Clippers"
   = "Clippers"). Every NBA nickname is unique, so this is safe.

Prices from rundown_client are already converted to decimal odds.
"""

import datetime

import requests

import rundown_client as rc
from structured_logging import get_logger

log = get_logger("nba_odds")

NBA_FALLBACK_SPORT_ID = 4  # per TheRundown docs; the probe on 2026-11-10 saw sport 4 = NBA
_nba_sport_id = None
_events_cache = {}  # date_str -> events


def _norm(name):
    return " ".join((name or "").lower().replace(".", "").split())


def names_match(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    return a.split()[-1] == b.split()[-1]


def _get_nba_sport_id():
    global _nba_sport_id
    if _nba_sport_id is not None:
        return _nba_sport_id
    try:
        data = rc._get_all_sports()
    except (rc.RundownAuthError, requests.RequestException) as exc:
        log.warning("nba_sport_lookup_failed_using_fallback",
                    extra={"ctx": {"error": str(exc), "fallback_id": NBA_FALLBACK_SPORT_ID}})
        _nba_sport_id = NBA_FALLBACK_SPORT_ID
        return _nba_sport_id

    entries = data if isinstance(data, list) else data.get("sports", data.get("data", []))
    for entry in entries or []:
        name = _norm(entry.get("name") or entry.get("sport_name"))
        sport_id = entry.get("id", entry.get("sport_id"))
        if name == "nba" and sport_id is not None:
            _nba_sport_id = sport_id
            return _nba_sport_id

    log.warning("nba_not_in_sports_list_using_fallback",
                extra={"ctx": {"fallback_id": NBA_FALLBACK_SPORT_ID}})
    _nba_sport_id = NBA_FALLBACK_SPORT_ID
    return _nba_sport_id


def _events_for_date(date_str):
    if date_str not in _events_cache:
        tomorrow = (datetime.date.fromisoformat(date_str) + datetime.timedelta(days=1)).isoformat()
        _events_cache[date_str] = rc.get_events_for_dates(_get_nba_sport_id(), [date_str, tomorrow])
        log.info("nba_odds_events_loaded", extra={"ctx": {
            "date": date_str, "events": len(_events_cache[date_str])}})
    return _events_cache[date_str]


def find_event(events, home_name, away_name):
    """Pure: returns the matching event, or None. Prefers events that have not finished."""
    matches = []
    for ev in events:
        ev_away, ev_home = rc.team_names(ev)
        if ev_away is None:
            continue
        if names_match(ev_home, home_name) and names_match(ev_away, away_name):
            matches.append(ev)
    if not matches:
        return None
    open_events = [e for e in matches if not rc.event_is_finished(e)]
    return (open_events or matches)[0]


def debug_fixture_status(home_name, away_name, date_str):
    events = _events_for_date(date_str)
    if find_event(events, home_name, away_name) is None:
        seen = [f"{rc.team_names(e)[0]} @ {rc.team_names(e)[1]}" for e in events]
        return f"no event matched '{away_name} @ {home_name}'. Odds feed had: {seen}"
    return "event matched"


def get_match_odds(home_name, away_name, date_str):
    event = find_event(_events_for_date(date_str), home_name, away_name)
    if not event:
        return None
    prices = rc.best_price_per_participant(event, rc.MARKET_MONEYLINE, rc.PERIOD_FULL_GAME)
    home_odds = next((p for n, p in prices.items() if names_match(n, home_name)), None)
    away_odds = next((p for n, p in prices.items() if names_match(n, away_name)), None)
    if home_odds is None and away_odds is None:
        return None
    return {"home_odds": home_odds, "away_odds": away_odds}


def get_totals_odds(home_name, away_name, date_str):
    event = find_event(_events_for_date(date_str), home_name, away_name)
    if not event:
        return []
    by_line = rc.best_price_per_line(event, rc.MARKET_TOTAL, rc.PERIOD_FULL_GAME)
    return [
        {"line": line, "over_odds": v["over"], "under_odds": v["under"], "market_id": rc.MARKET_TOTAL}
        for line, v in sorted(by_line.items())
    ]
