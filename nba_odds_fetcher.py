"""
NBA odds from TheRundown (free tier: DraftKings, FanDuel and BetMGM).
Same backend and helper module as the WNBA/NCAAB odds fetchers, pointed at
the NBA, and extended to read EVERY period: full game, 1st/2nd half and
the four quarters, for three markets: winner (moneyline), handicap
(spread) and total points.

Whether the FREE plan actually includes half/quarter lines is not
documented, so nothing here assumes it. Whatever comes back is used;
whatever is missing is skipped. The runner logs which periods were found.

THINGS THAT WERE EASY TO GET WRONG, and are handled here:

1. SPORT LOOKUP. A plain substring search for "nba" also matches "WNBA"
   (the letters nba are inside wnba). This file requires the name to be
   exactly "NBA".

2. TEAM NAME MATCHING. balldontlie says "Los Angeles Clippers"; an odds
   feed may say "LA Clippers". So two names also match when their LAST
   WORD matches ("Clippers"). Every NBA nickname is unique, so this is safe.

3. PERIOD LEAKAGE. The shared rundown_client helper treats a market with
   no period tag as belonging to EVERY period. For the NBA that could show
   a full-game line as a "1st half" line and produce a false tip. Here a
   market with no period tag counts as full game only, never as a half or
   quarter.

4. THREE-WAY MARKETS. Some books price a quarter winner with a "tie"
   option. Our model is two-way, so any winner market with more than two
   choices is skipped.

Prices from rundown_client are converted to decimal odds already.
"""

import datetime

import requests

import rundown_client as rc
from structured_logging import get_logger

log = get_logger("nba_odds")

NBA_FALLBACK_SPORT_ID = 4  # per TheRundown docs; the probe on 2026-11-10 saw sport 4 = NBA
MARKET_SPREAD = 2          # TheRundown: 1 moneyline, 2 spread (handicap), 3 total
MARKET_IDS = "1,2,3"
PERIOD_KEYS = {0: "full", 1: "h1", 2: "h2", 3: "q1", 4: "q2", 5: "q3", 6: "q4"}

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
        _events_cache[date_str] = rc.get_events_for_dates(
            _get_nba_sport_id(), [date_str, tomorrow], market_ids=MARKET_IDS)
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


# ---------------------------------------------------------------- reading prices (pure)

def _to_float(raw):
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def iter_period_prices(event, market_id, period_id):
    """
    Yields (participant_name, line_value, decimal_price) for one market in
    ONE period. A market with no period tag counts as the full game only.
    """
    for market in event.get("markets", []):
        if market.get("market_id") != market_id:
            continue
        tagged = market.get("period_id")
        if (0 if tagged is None else tagged) != period_id:
            continue
        for participant in market.get("participants", []):
            name = participant.get("name")
            for line in participant.get("lines", []):
                value = _to_float(line.get("value"))
                for price_obj in (line.get("prices") or {}).values():
                    price = price_obj.get("price") if isinstance(price_obj, dict) else price_obj
                    if price is None or price == rc.OFF_BOARD_SENTINEL:
                        continue
                    decimal_price = rc._american_to_decimal(price)
                    if decimal_price is not None:
                        yield name, value, decimal_price


def _side_of(name, home_name, away_name):
    if names_match(name, home_name):
        return "home"
    if names_match(name, away_name):
        return "away"
    return None


def read_winner_market(event, period_id, home_name, away_name):
    """{"home_odds", "away_odds"} (best price across books) or None. Skips three-way markets."""
    best, names = {}, set()
    for name, _value, price in iter_period_prices(event, rc.MARKET_MONEYLINE, period_id):
        names.add(_norm(name))
        side = _side_of(name, home_name, away_name)
        if side and price > best.get(side, 0):
            best[side] = price
    if len(names) > 2 or not best:
        return None
    return {"home_odds": best.get("home"), "away_odds": best.get("away")}


def read_handicap_market(event, period_id, home_name, away_name):
    """[{"side", "line", "odds"}]: best price per (team, handicap line)."""
    best = {}
    for name, value, price in iter_period_prices(event, MARKET_SPREAD, period_id):
        side = _side_of(name, home_name, away_name)
        if side is None or value is None:
            continue
        if price > best.get((side, value), 0):
            best[(side, value)] = price
    return [{"side": s, "line": v, "odds": o} for (s, v), o in sorted(best.items())]


def read_totals_market(event, period_id):
    """[{"line", "over_odds", "under_odds", "market_id"}] sorted by line."""
    by_line = {}
    for name, value, price in iter_period_prices(event, rc.MARKET_TOTAL, period_id):
        text = (name or "").lower()
        side = "over" if "over" in text else ("under" if "under" in text else None)
        if side is None or value is None:
            continue
        entry = by_line.setdefault(value, {"over": None, "under": None})
        if entry[side] is None or price > entry[side]:
            entry[side] = price
    return [{"line": line, "over_odds": v["over"], "under_odds": v["under"], "market_id": rc.MARKET_TOTAL}
            for line, v in sorted(by_line.items())]


def read_all_periods(event, home_name, away_name):
    """{period_key: {"moneyline", "spread", "totals"}} for every period that has any usable market."""
    markets = {}
    for period_id, key in PERIOD_KEYS.items():
        entry = {
            "moneyline": read_winner_market(event, period_id, home_name, away_name),
            "spread": read_handicap_market(event, period_id, home_name, away_name),
            "totals": read_totals_market(event, period_id),
        }
        if entry["moneyline"] or entry["spread"] or entry["totals"]:
            markets[key] = entry
    return markets


def get_game_markets(home_name, away_name, date_str):
    """Everything the odds feed offers for this game, grouped by period. {} if the game isn't found."""
    event = find_event(_events_for_date(date_str), home_name, away_name)
    if not event:
        return {}
    markets = read_all_periods(event, home_name, away_name)
    log.info("nba_markets_found", extra={"ctx": {
        "matchup": f"{away_name} @ {home_name}",
        "periods": {k: [name for name, present in
                        (("winner", v["moneyline"]), ("handicap", v["spread"]), ("total", v["totals"])) if present]
                    for k, v in markets.items()}}})
    return markets
