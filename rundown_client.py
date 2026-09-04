"""
Shared low-level client for TheRundown API (v2) — the free-tier odds
provider that replaced OddsPapi.

WHY THIS FILE EXISTS: odds_fetcher.py (WNBA) and tennis_odds_fetcher.py
both need the same underlying things — authenticated HTTP GET with
retries, looking up a sport's numeric ID by name, and turning a raw
event's markets into a simple (participant, line_value, price) list.
Factoring that into one shared module means a fix to the retry logic or
the market-parsing logic only has to happen once, not twice.

CONFIRMED FROM THEROUNDOWN'S PUBLISHED DOCS (docs.therundown.io) — NOT yet
verified against a live response, since this environment has no network
access to test with. Everything below is built to match the documented
schema as closely as possible, but the FIRST real run should be watched
closely: run `python odds_fetcher.py` or `python tennis_odds_fetcher.py`
directly (see the __main__ block in each) and check the printed output
against what you see for real. If field names below turn out to be
slightly off, the diagnostic output is designed to make that obvious
fast, the same way the old OddsPapi code flagged its own unverified
sub-market guesses.

Key documented facts this is built against:
  - Base URL: https://therundown.io/api/v2
  - Auth: header "X-TheRundown-Key: <key>" (or ?key=<key> as a fallback)
  - GET /sports and GET /affiliates are public (free, no auth required,
    don't count against your quota) — used to look up sport IDs by name
    rather than hardcoding them, same philosophy the old OddsPapi code
    used for "find WNBA in /tournaments" instead of a hardcoded ID.
  - GET /sports/{sportID}/events/{date} is the main odds endpoint.
    market_ids defaults to "1,2,3" (Moneyline, Spread, Total) if not
    passed explicitly.
  - Response shape: {"events": [{"event_id", "sport_id", "event_date",
    "teams": [away, home], "schedule": {...}, "score": {...},
    "markets": [{"market_id", "period_id", "name", "participants": [
    {"name", "lines": [{"value", "prices": {"<affiliate_id>": {"price"}}}]}
    ]}]}]}
  - Prematch period IDs: 0=Full Game, 1=1st Half, 2=2nd Half,
    3=1st Quarter, 4=2nd Quarter, 5=3rd Quarter, 6=4th Quarter.
  - Off-board sentinel: a price of exactly 0.0001 means "no real price
    right now" (line temporarily pulled) — must be treated as missing,
    not as a real (tiny) decimal odds value.
  - Market IDs used here: 1=Moneyline, 3=Total, 94=Team Totals.
"""

import time
import requests
from config import THERUNDOWN_API_KEY

BASE_URL = "https://therundown.io/api/v2"

# TheRundown's free tier is a generous daily quota (20k data points/day)
# rather than a tight per-minute rate limit like OddsPapi's, but pacing
# requests a little is still cheap insurance against a burst 429.
MIN_SECONDS_BETWEEN_REQUESTS = 0.5
_last_request_time = 0

OFF_BOARD_SENTINEL = 0.0001

# Prematch period IDs — see module docstring / TheRundown's Period IDs
# reference (docs.therundown.io/reference/periods).
PERIOD_FULL_GAME = 0
PERIOD_FIRST_HALF = 1
PERIOD_SECOND_HALF = 2
PERIOD_QUARTER = {1: 3, 2: 4, 3: 5, 4: 6}

MARKET_MONEYLINE = 1
MARKET_TOTAL = 3
MARKET_TEAM_TOTAL = 94

_sports_cache = None  # raw /sports response, fetched once and reused


def _get(path, params=None, retries=3):
    """
    GET against TheRundown, with the same three-failure-mode retry
    behavior the OddsPapi client used (429, 5xx, network-level errors) —
    that logic wasn't specific to OddsPapi, it's just sound practice for
    any free-tier API, so it's preserved here as-is.
    """
    global _last_request_time
    params = dict(params or {})
    headers = {"X-TheRundown-Key": THERUNDOWN_API_KEY}

    for attempt in range(retries + 1):
        elapsed = time.time() - _last_request_time
        if elapsed < MIN_SECONDS_BETWEEN_REQUESTS:
            time.sleep(MIN_SECONDS_BETWEEN_REQUESTS - elapsed)

        try:
            resp = requests.get(f"{BASE_URL}{path}", params=params, headers=headers, timeout=20)
        except requests.exceptions.RequestException:
            _last_request_time = time.time()
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise

        _last_request_time = time.time()

        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2 * (attempt + 1))
            continue

        resp.raise_for_status()
        return resp.json()


def _get_all_sports():
    global _sports_cache
    if _sports_cache is None:
        _sports_cache = _get("/sports")
    return _sports_cache


def find_sport_id(name_contains):
    """
    Looks up a sport's numeric ID by matching `name_contains` (case-
    insensitive) against whatever name field the /sports response uses.
    Defensive about the exact field names since this hasn't been
    verified against a live response — tries the most likely field
    names for both the list container and each entry's id/name fields,
    and raises a loud, specific error (including the raw first entry)
    if nothing matches, rather than silently returning a wrong ID.
    """
    data = _get_all_sports()

    # the list might be the raw response, or nested under "sports"
    entries = data if isinstance(data, list) else data.get("sports", data.get("data", []))

    if not entries:
        raise RuntimeError(
            f"TheRundown's /sports response didn't look like a list of sports. "
            f"Raw response: {data}"
        )

    target = name_contains.lower()
    for entry in entries:
        name = (entry.get("name") or entry.get("sport_name") or "").lower()
        if target in name:
            sport_id = entry.get("id") or entry.get("sport_id")
            if sport_id is not None:
                return sport_id

    raise RuntimeError(
        f"Couldn't find a sport matching '{name_contains}' in TheRundown's /sports list. "
        f"First entry for reference (check field names against this): "
        f"{entries[0] if entries else 'EMPTY LIST'}"
    )


def get_events_for_dates(sport_id, date_strs, market_ids="1,3,94", extra_params=None):
    """
    Fetches events for a sport across one or more dates (YYYY-MM-DD) and
    merges them, deduplicated by event_id. Fetching more than one date is
    the TheRundown equivalent of the old OddsPapi code's "widen the query
    window" trick — a late-night match can be filed under tomorrow's UTC
    date depending on how TheRundown buckets it, so when in doubt this
    calls with today AND tomorrow and lets the name-matching find the
    right one, same end result as the old wider from/to window.
    """
    params = {"market_ids": market_ids, "main_line": "true"}
    if extra_params:
        params.update(extra_params)

    seen_ids = set()
    merged = []
    for date_str in date_strs:
        data = _get(f"/sports/{sport_id}/events/{date_str}", params)
        events = data.get("events", []) if isinstance(data, dict) else []
        for ev in events:
            eid = ev.get("event_id")
            if eid is not None and eid in seen_ids:
                continue
            if eid is not None:
                seen_ids.add(eid)
            merged.append(ev)

    return merged


def event_is_finished(event):
    """
    Best-effort finished-check. Field location for event status isn't
    confirmed live — tries the most likely spots. Defaults to "not
    finished" if no status field is found at all, since this only
    affects which of several same-named-team fixtures gets preferred
    when there's more than one match, not whether odds are found at all.
    """
    status = (
        event.get("status")
        or event.get("score", {}).get("event_status")
        or event.get("score", {}).get("status")
        or ""
    )
    return "FINAL" in str(status).upper()


def team_names(event):
    """
    Returns (away_name, home_name) per TheRundown's documented ordering
    (teams[0] = away, teams[1] = home). Returns (None, None) if the
    event doesn't have a usable 2-entry teams list.
    """
    teams = event.get("teams") or []
    if len(teams) < 2:
        return None, None
    return teams[0].get("name"), teams[1].get("name")


def iter_market_prices(event, market_id, period_id=None):
    """
    Yields (participant_name, line_value, price) for every price row in
    `event` matching market_id (and period_id, if given). Skips off-
    board prices (the 0.0001 sentinel) automatically.
    """
    for market in event.get("markets", []):
        if market.get("market_id") != market_id:
            continue
        if period_id is not None and market.get("period_id") not in (period_id, None):
            continue

        for participant in market.get("participants", []):
            name = participant.get("name")
            for line in participant.get("lines", []):
                value = line.get("value")
                for price_obj in (line.get("prices") or {}).values():
                    price = price_obj.get("price") if isinstance(price_obj, dict) else price_obj
                    if price is None or price == OFF_BOARD_SENTINEL:
                        continue
                    yield name, value, price


def best_price_per_participant(event, market_id, period_id=None):
    """
    Returns {participant_name: best_price} — the highest (best-for-
    bettor) decimal price found across all sportsbooks for each
    participant in this market. Used for moneyline, where there's one
    line per side and we just want the best available price.
    """
    best = {}
    for name, _value, price in iter_market_prices(event, market_id, period_id):
        if name not in best or price > best[name]:
            best[name] = price
    return best


def best_price_per_line(event, market_id, period_id=None):
    """
    Returns {line_value: {"over": best_price_or_None, "under": best_price_or_None}}
    for a market whose participants are named 'Over'/'Under' sharing a
    common numeric line (the Total market, in prematch full-game or a
    half/quarter period). This is the well-documented case — the
    Moneyline/Total markets and period_id filtering are confirmed
    against TheRundown's published docs, unlike the team-totals case
    below.
    """
    by_line = {}
    for name, value, price in iter_market_prices(event, market_id, period_id):
        if value is None:
            continue
        side = "over" if "over" in (name or "").lower() else (
            "under" if "under" in (name or "").lower() else None
        )
        if side is None:
            continue
        entry = by_line.setdefault(value, {"over": None, "under": None})
        if entry[side] is None or price > entry[side]:
            entry[side] = price
    return by_line
