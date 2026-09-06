"""
Fetches CURRENT ATP/WTA singles rankings from Wikipedia's "Current tennis
rankings" page — a genuinely free, no-key, weekly-updated source. Built
to fill a real gap: Sackmann's per-match rank data (used elsewhere in
this codebase) is only as current as his match archives, which are
confirmed to lag by more than a year at times (see tennis_stats_fetcher.py's
TENNIS_DATA_LOOKBACK_YEARS). Wikipedia's rankings page is updated weekly
regardless of that lag, so it's a separate, more current source
specifically for "what is this player's rank RIGHT NOW."

CONFIRMED LIVE (fetched directly, not just documentation): the page at
https://en.wikipedia.org/wiki/Current_tennis_rankings has separate tables
for "ATP rankings (singles)", "Singles race rankings", "ATP rankings
(doubles)", "Doubles race rankings", and the same four for WTA — eight
tables total. Player names sometimes include a trailing " (CTY)" country
code and sometimes don't (e.g. "Jannik Sinner (ITA)" vs "Daniil Medvedev"
with no code shown for some players) — both are handled below.

NOT yet verified against the exact HTML structure requests+BeautifulSoup
will actually see (this environment could only fetch a pre-processed
version of the page, not the raw HTML the live REST API returns). This
uses BeautifulSoup against Wikipedia's rendered-HTML REST endpoint
specifically because rendered HTML is far more forgiving to parse than
raw wikitext (templates/links are already expanded to plain text) — but
the exact way each table is labeled (a caption? a preceding heading? a
preceding paragraph?) isn't confirmed live. Run `python tennis_rankings.py`
directly and check the printed output against the real page before
trusting this — same verify-live approach used throughout this project.
"""

import re
import requests
from bs4 import BeautifulSoup

RANKINGS_URL = "https://en.wikipedia.org/api/rest_v1/page/html/Current_tennis_rankings"

# Wikimedia's API blocks requests with no identifying User-Agent (or a
# generic default one) with a 403 — confirmed live. Their etiquette
# policy asks for a descriptive header identifying the client and a
# contact point; this is the fix, not a workaround for something we did
# wrong.
_REQUEST_HEADERS = {
    "User-Agent": "PredictionBotTennisRankings/1.0 (personal project; free-tier tennis tips bot)"
}

_rankings_cache = {}  # "atp" / "wta" -> {player_name_lower: rank_int}

# a table caption/heading counts as the singles-rankings table for a tour
# only if it mentions the tour and "singles", but NOT "race" (the race
# table is a separate, different ranking system) and NOT "doubles"
_SINGLES_INCLUDE = ["singles"]
_SINGLES_EXCLUDE = ["race", "doubles"]

_COUNTRY_CODE_SUFFIX = re.compile(r"\s*\([A-Z]{2,3}\)\s*$")


def _clean_player_name(cell_text):
    """
    Strips a trailing " (ITA)"-style country code if present, strips
    footnote markers/checkmarks, and collapses whitespace. Returns None
    if what's left is empty (a malformed row, a header repeated mid-table,
    etc. — better to skip a bad row than store garbage).
    """
    text = cell_text.replace("✓", "").strip()
    text = _COUNTRY_CODE_SUFFIX.sub("", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text or None


def _table_is_singles_rankings(caption_text, tour_label):
    caption_lower = (caption_text or "").lower()
    if tour_label.lower() not in caption_lower:
        return False
    if not any(word in caption_lower for word in _SINGLES_INCLUDE):
        return False
    if any(word in caption_lower for word in _SINGLES_EXCLUDE):
        return False
    return True


def _find_preceding_caption(table_tag):
    """
    Wikipedia doesn't reliably wrap each table in its own <caption> tag —
    the "ATP rankings (singles) as of DATE" label is often just plain
    text in the paragraph/heading immediately before the table. Walks
    backward through previous siblings to find the nearest non-empty
    text, which should be that label.
    """
    node = table_tag.find_previous_sibling()
    while node is not None:
        text = node.get_text(strip=True)
        if text:
            return text
        node = node.find_previous_sibling()
    return ""


def _parse_rankings_table(table_tag):
    """
    Returns {player_name_lower: rank_int} from one wikitable. Expects a
    header row (No. / Player / Points / Move) followed by data rows.
    Skips any row that doesn't parse cleanly rather than guessing.
    """
    rankings = {}
    rows = table_tag.find_all("tr")

    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        # skip header rows (th cells, or first cell isn't a number/"=")
        rank_text = cells[0].get_text(strip=True)
        if not rank_text or (not rank_text.isdigit() and rank_text != "="):
            continue

        player_name = _clean_player_name(cells[1].get_text(" ", strip=True))
        if not player_name:
            continue

        if rank_text == "=":
            # a tied rank shares the previous row's number — same
            # convention used throughout this Wikipedia page for ties
            rank = rankings.get("_last_rank")
            if rank is None:
                continue
        else:
            rank = int(rank_text)
            rankings["_last_rank"] = rank

        rankings[player_name.lower()] = rank

    rankings.pop("_last_rank", None)
    return rankings


def _fetch_rankings(tour):
    """
    tour: 'atp' or 'wta'. Returns {player_name_lower: rank_int}, or an
    empty dict (never raises) if the fetch or parsing fails — callers
    should treat an empty dict as "ranking unavailable this run", the
    same fail-safe philosophy used everywhere else in this codebase.
    """
    if tour in _rankings_cache:
        return _rankings_cache[tour]

    tour_label = "ATP" if tour == "atp" else "WTA"

    try:
        resp = requests.get(RANKINGS_URL, headers=_REQUEST_HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [tennis rankings] network error fetching Wikipedia rankings ({type(e).__name__}: {e}) "
              f"— treating as unavailable this run.")
        _rankings_cache[tour] = {}
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")

    for table in tables:
        caption = _find_preceding_caption(table)
        if _table_is_singles_rankings(caption, tour_label):
            rankings = _parse_rankings_table(table)
            if rankings:
                _rankings_cache[tour] = rankings
                return rankings
            else:
                print(f"  [tennis rankings] found a table captioned '{caption}' matching {tour_label} "
                      f"singles, but parsed ZERO rankings from it — table structure may not match what "
                      f"this code expects.")

    print(f"  [tennis rankings] couldn't find a '{tour_label} rankings (singles)' table among "
          f"{len(tables)} table(s) on the page — captions/headings may be labeled differently than "
          f"expected. Run this file directly to see all captions found.")
    _rankings_cache[tour] = {}
    return {}


def get_current_rank(player_name, tour):
    """
    Returns this player's current numeric rank from Wikipedia's weekly-
    updated rankings page, or None if not found/unavailable. Matches by
    exact lowercased name first, then falls back to substring matching
    (handles minor formatting differences, e.g. accented characters
    rendered differently between sources).
    """
    rankings = _fetch_rankings(tour)
    if not rankings:
        return None

    target = player_name.strip().lower()
    if target in rankings:
        return rankings[target]

    for name, rank in rankings.items():
        if name in target or target in name:
            return rank

    return None


if __name__ == "__main__":
    for tour in ("atp", "wta"):
        rankings = _fetch_rankings(tour)
        print(f"\n{tour.upper()} singles rankings found: {len(rankings)} player(s)")
        for name, rank in sorted(rankings.items(), key=lambda kv: kv[1])[:10]:
            print(f"  #{rank}: {name.title()}")
