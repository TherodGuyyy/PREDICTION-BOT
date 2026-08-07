"""
Fetches recent tennis match history and current form from Jeff Sackmann's
free tennis_atp / tennis_wta CSV archives on GitHub.

IMPORTANT — read this before trusting it fully:
Unlike balldontlie (near-live), this data source updates periodically —
sometimes daily during majors, sometimes lagging by 1-2+ weeks in quieter
stretches. This code is honest about that: player_form_summary() reports
how many DAYS OLD the player's most recent match on record is, and
tennis_main.py surfaces that in its logs, so staleness is visible rather
than silently assumed away.

Credit: Jeff Sackmann, https://github.com/JeffSackmann/tennis_atp and
tennis_wta. Data used here under Creative Commons Attribution-
NonCommercial-ShareAlike — personal, non-commercial use.
"""

import csv
import io
import datetime
import requests
from config import ATP_MATCHES_URL, WTA_MATCHES_URL, TENNIS_MIN_MATCHES_FOR_ANALYSIS, TENNIS_SURFACE_MIN_MATCHES

# cached per run — one fetch per (tour, year) no matter how many players
# we look up against it
_matches_cache = {}


def _fetch_matches_csv(tour, year):
    """
    tour: 'atp' or 'wta'. year: int.
    Returns a list of dict rows (csv.DictReader output), or an empty list
    if that year's file doesn't exist yet (e.g. very early in a new year)
    or the fetch fails for any reason — callers should treat an empty
    list as "no data available", not crash.
    """
    cache_key = (tour, year)
    if cache_key in _matches_cache:
        return _matches_cache[cache_key]

    url_template = ATP_MATCHES_URL if tour == "atp" else WTA_MATCHES_URL
    url = url_template.format(year=year)

    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        rows = list(reader)
    except requests.RequestException:
        rows = []

    _matches_cache[cache_key] = rows
    return rows


def _names_match(a, b):
    a, b = a.lower().strip(), b.lower().strip()
    return a in b or b in a


def _parse_tourney_date(date_str):
    """Sackmann's dates are 'YYYYMMDD' strings. Returns a date object or None."""
    try:
        return datetime.datetime.strptime(date_str.strip(), "%Y%m%d").date()
    except (ValueError, AttributeError):
        return None


def get_player_recent_matches(player_name, tour, num_matches=15):
    """
    Returns this player's most recent matches (current year, falling back
    to also including last year's file if the current year alone doesn't
    have enough), most recent first. Each entry is normalized to:
    {"date": date, "surface": str, "won": bool, "player_rank": int|None,
     "opponent_name": str, "opponent_rank": int|None}
    """
    current_year = datetime.date.today().year
    all_rows = _fetch_matches_csv(tour, current_year)

    # if the current year doesn't have enough of this player's matches yet
    # (e.g. very early in the season), also pull last year's file so we're
    # not starving the model of data in January/February
    matches = [r for r in all_rows if _names_match(r.get("winner_name", ""), player_name)
               or _names_match(r.get("loser_name", ""), player_name)]

    if len(matches) < num_matches:
        prev_rows = _fetch_matches_csv(tour, current_year - 1)
        prev_matches = [r for r in prev_rows if _names_match(r.get("winner_name", ""), player_name)
                         or _names_match(r.get("loser_name", ""), player_name)]
        matches = matches + prev_matches

    normalized = []
    for r in matches:
        match_date = _parse_tourney_date(r.get("tourney_date", ""))
        if match_date is None:
            continue

        is_winner = _names_match(r.get("winner_name", ""), player_name)

        def _to_int(val):
            try:
                return int(val)
            except (ValueError, TypeError):
                return None

        normalized.append({
            "date": match_date,
            "surface": (r.get("surface") or "").strip(),
            "won": is_winner,
            "player_rank": _to_int(r.get("winner_rank") if is_winner else r.get("loser_rank")),
            "opponent_name": r.get("loser_name") if is_winner else r.get("winner_name"),
            "opponent_rank": _to_int(r.get("loser_rank") if is_winner else r.get("winner_rank")),
        })

    normalized.sort(key=lambda m: m["date"], reverse=True)
    return normalized[:num_matches]


def _surface_matches(match_surface, target_surface):
    """
    Loose surface comparison — the match data (Sackmann) and the live
    fixture data (OddsPapi) are two completely independent sources that
    may label surfaces differently (casing, "Hard" vs "Hard Court", etc).
    An exact-match comparison here would silently fail on any naming
    mismatch and quietly fall back to overall form every time, with no
    visible error — this normalizes both sides and matches on the core
    surface word so that failure mode can't happen silently.
    """
    if not match_surface or not target_surface:
        return False
    a = match_surface.strip().lower()
    b = target_surface.strip().lower()
    return a == b or a in b or b in a


def player_form_summary(player_name, tour, surface):
    """
    Returns a form summary for this player, or None if there isn't enough
    recent match data to trust (protects against small-sample noise, same
    philosophy as the WNBA min-games check).

    surface: the surface of the UPCOMING match we're analyzing (e.g.
    'Hard', 'Clay', 'Grass') — used to pull out surface-specific form.
    """
    matches = get_player_recent_matches(player_name, tour)
    if len(matches) < TENNIS_MIN_MATCHES_FOR_ANALYSIS:
        return None

    # current rank = rank listed in their single most recent match
    current_rank = matches[0]["player_rank"]
    if current_rank is None or current_rank <= 0:
        # try the next few matches in case the most recent one just has a
        # missing/invalid rank field (happens for wildcard/qualifier entries)
        current_rank = None
        for m in matches[1:4]:
            if m["player_rank"] is not None and m["player_rank"] > 0:
                current_rank = m["player_rank"]
                break
    if current_rank is None:
        return None  # no usable rank at all — not enough to analyze safely

    wins = sum(1 for m in matches if m["won"])
    overall_win_pct = wins / len(matches)

    surface_matches = [m for m in matches if _surface_matches(m["surface"], surface)]
    if len(surface_matches) >= TENNIS_SURFACE_MIN_MATCHES:
        surface_wins = sum(1 for m in surface_matches if m["won"])
        surface_win_pct = surface_wins / len(surface_matches)
        surface_sample_size = len(surface_matches)
    else:
        # not enough matches on this surface yet — fall back to overall
        # form rather than trusting a 1-2 match surface sample
        surface_win_pct = overall_win_pct
        surface_sample_size = len(surface_matches)

    most_recent_match_days_ago = (datetime.date.today() - matches[0]["date"]).days

    return {
        "matches_sampled": len(matches),
        "current_rank": current_rank,
        "overall_win_pct": overall_win_pct,
        "surface_win_pct": surface_win_pct,
        "surface_sample_size": surface_sample_size,
        "most_recent_match_days_ago": most_recent_match_days_ago,
    }


if __name__ == "__main__":
    # Quick manual test — checks recent match data for a well-known
    # player, without needing OddsPapi keys or running the full bot.
    # Run: python tennis_stats_fetcher.py
    test_player = "Carlos Alcaraz"
    test_tour = "atp"
    test_surface = "Hard"

    print(f"Fetching recent matches for {test_player} ({test_tour})...")
    matches = get_player_recent_matches(test_player, test_tour)
    print(f"Found {len(matches)} recent matches.")
    for m in matches[:5]:
        result = "won" if m["won"] else "lost"
        print(f"  {m['date']} | {m['surface']} | {result} vs {m['opponent_name']} "
              f"(rank {m['player_rank']} vs {m['opponent_rank']})")

    summary = player_form_summary(test_player, test_tour, test_surface)
    print(f"\nForm summary (surface={test_surface}):", summary)
    if summary:
        print(f"Most recent match on record was {summary['most_recent_match_days_ago']} days ago "
              f"— if that number looks large, the data may be lagging behind the live tour right now.")
