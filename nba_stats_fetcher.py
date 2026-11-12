"""
NBA data layer: today's games and each team's recent form, from
balldontlie's FREE tier (teams / players / games endpoints, 5 requests per
minute, same key the WNBA part of this bot already uses).

WHAT THIS FILE DOES, IN PLAIN WORDS
  1. Asks balldontlie for tonight's NBA games.
  2. Asks it ONCE for every finished game of the last ~5 weeks.
  3. Works out each team's recent form (wins, points scored/allowed,
     days since last game) from that single download.

WHY ONE DOWNLOAD INSTEAD OF ONE PER TEAM: the free tier allows only 5
requests a minute. Asking per team (30 teams) would take ~6 minutes.
Downloading the whole recent window once costs about 3 requests total.

NBA vs WNBA DIFFERENCES HANDLED HERE (this is why NBA is not just a copy
of the WNBA code):
  * Score fields are home_team_score / visitor_team_score (WNBA API uses
    home_score / away_score).
  * Finished games have status_state == "final" (WNBA used status "post").
  * "season" is the START year of the season (2026 means 2026-27).
  * Preseason games are excluded by starting the window on opening night
    (REGULAR_SEASON_START). Update that date each October.

Design notes: the HTTP client takes its session, sleep and clock as
arguments so tests never touch the network or really wait. Errors are
specific (NbaAccessError, NbaDataError) so the runner can skip NBA with a
clear log line instead of crashing the whole bot.
"""

import datetime
import os
import time

import requests

from config import BALLDONTLIE_API_KEY, BALLDONTLIE_BASE_URL, MIN_GAMES_FOR_ANALYSIS
from structured_logging import get_logger

log = get_logger("nba_stats")

# NBA 2026-27 regular season opened Tuesday 2026-10-20. Update every October.
# Can also be overridden with an NBA_SEASON_START environment variable (YYYY-MM-DD).
REGULAR_SEASON_START = datetime.date(2026, 10, 20)
HISTORY_DAYS = 35            # how far back to download finished games
FORM_GAMES = 10              # a team's "recent form" = its last 10 finished games
UPCOMING_WINDOW_HOURS = 24   # only analyse games starting within the next 24h
MIN_SECONDS_BETWEEN_REQUESTS = 13.0   # free tier: 5/min, so just over 12s apart
MAX_RETRIES = 3
RETRY_STATUSES = (429, 500, 502, 503, 504)
ACCESS_STATUSES = (401, 403)
MAX_PAGES = 10
UPCOMING_STATES = ("scheduled", "")   # skip in-progress, postponed, cancelled


class NbaDataError(RuntimeError):
    """Balldontlie answered badly (bad JSON, repeated failures)."""


class NbaAccessError(NbaDataError):
    """Key missing/rejected (401/403) - NBA games should be free, so check the key."""


class BalldontlieClient:
    """Authenticated GET with pacing + exponential backoff. I/O is injectable."""

    def __init__(self, api_key, base_url=BALLDONTLIE_BASE_URL, session=None,
                 sleep=time.sleep, clock=time.monotonic,
                 min_interval=MIN_SECONDS_BETWEEN_REQUESTS):
        if not api_key or api_key == "PASTE_YOUR_KEY_HERE":
            raise NbaAccessError(
                "BALLDONTLIE_API_KEY is not set. Add it as a GitHub Actions secret "
                "(it is the same key the WNBA part already uses)."
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._min_interval = min_interval
        self._last_request_at = None

    def _pace(self):
        if self._last_request_at is None:
            return
        wait = self._min_interval - (self._clock() - self._last_request_at)
        if wait > 0:
            self._sleep(wait)

    @staticmethod
    def _backoff(status, attempt):
        # a 429 means we hit the per-minute limit: wait out a good part of the minute
        base = 15 if status == 429 else 2
        return base * (2 ** attempt)

    def get(self, path, params=None):
        url = f"{self._base_url}{path}"
        headers = {"Authorization": self._api_key}  # balldontlie: no "Bearer" prefix

        for attempt in range(MAX_RETRIES + 1):
            self._pace()
            try:
                resp = self._session.get(url, params=params, headers=headers, timeout=20)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self._last_request_at = self._clock()
                if attempt < MAX_RETRIES:
                    delay = self._backoff(None, attempt)
                    log.warning("nba_request_retry", extra={"ctx": {
                        "path": path, "attempt": attempt + 1, "delay_s": delay,
                        "error": type(exc).__name__}})
                    self._sleep(delay)
                    continue
                raise NbaDataError(f"Network failure calling {path}: {type(exc).__name__}") from exc
            self._last_request_at = self._clock()

            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                delay = self._backoff(resp.status_code, attempt)
                log.warning("nba_request_retry", extra={"ctx": {
                    "path": path, "attempt": attempt + 1, "delay_s": delay,
                    "status": resp.status_code}})
                self._sleep(delay)
                continue

            if resp.status_code in ACCESS_STATUSES:
                raise NbaAccessError(
                    f"HTTP {resp.status_code} on {path}. NBA games are on the free tier, "
                    "so most likely the BALLDONTLIE_API_KEY secret is wrong or missing."
                )
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                raise NbaDataError(f"HTTP {resp.status_code} on {path}") from exc
            try:
                return resp.json()
            except ValueError as exc:
                raise NbaDataError(f"Non-JSON response from {path}") from exc

        raise NbaDataError(f"Gave up on {path} after {MAX_RETRIES + 1} attempts")

    def get_all_pages(self, path, params):
        """Follows balldontlie's cursor pagination, up to MAX_PAGES pages."""
        rows = []
        params = dict(params)
        for _ in range(MAX_PAGES):
            payload = self.get(path, params)
            rows.extend(payload.get("data", []))
            cursor = (payload.get("meta") or {}).get("next_cursor")
            if not cursor:
                return rows
            params["cursor"] = cursor
        log.warning("nba_pagination_cap_hit", extra={"ctx": {"path": path, "max_pages": MAX_PAGES}})
        return rows


def normalize_game(raw):
    """
    Converts one balldontlie NBA game into the shape the rest of the bot
    expects. Returns None if team ids are missing.
    """
    home = raw.get("home_team") or {}
    away = raw.get("visitor_team") or {}
    if home.get("id") is None or away.get("id") is None:
        return None

    state = (raw.get("status_state") or "").lower()
    finished = state == "final" or (not state and raw.get("status") == "Final")

    return {
        "id": raw.get("id"),
        "date": (raw.get("date") or "")[:10],
        "datetime": raw.get("datetime"),
        "state": state,
        "status": "post" if finished else "scheduled",
        "home_team": {"id": home["id"], "full_name": home.get("full_name") or home.get("name")},
        "visitor_team": {"id": away["id"], "full_name": away.get("full_name") or away.get("name")},
        "home_score": raw.get("home_team_score"),
        "away_score": raw.get("visitor_team_score"),
    }


def _parse_date(date_str):
    try:
        return datetime.date.fromisoformat((date_str or "")[:10])
    except ValueError:
        return None


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def is_upcoming(game, now, window_hours=UPCOMING_WINDOW_HOURS):
    """
    True if the game has not started and starts within the window.
    The window matters because balldontlie's date buckets can put
    TOMORROW's slate in a 'today + tomorrow' query; only tonight's games
    (within 24h) should be analysed with today's form and odds.
    """
    if game["state"] not in UPCOMING_STATES:
        return False
    start = _parse_datetime(game.get("datetime"))
    if start is None:
        return True  # no timestamp: trust the date query
    return now <= start <= now + datetime.timedelta(hours=window_hours)


def compute_team_form(finished_games, team_id, as_of_date,
                      num_games=FORM_GAMES, min_games=MIN_GAMES_FOR_ANALYSIS):
    """
    Pure function. Returns the team's form over its last `num_games`
    finished games, or None if there are fewer than `min_games` usable
    games (small-sample protection, same philosophy as the WNBA code).

    days_since_last_game: 1 means the team played YESTERDAY (back-to-back).
    """
    mine = [g for g in finished_games
            if team_id in (g["home_team"]["id"], g["visitor_team"]["id"])
            and g["home_score"] is not None and g["away_score"] is not None]
    mine.sort(key=lambda g: (g["date"], g.get("datetime") or ""), reverse=True)
    mine = mine[:num_games]
    if len(mine) < min_games:
        return None

    wins, scored, allowed = 0, [], []
    for g in mine:
        is_home = g["home_team"]["id"] == team_id
        own = g["home_score"] if is_home else g["away_score"]
        opp = g["away_score"] if is_home else g["home_score"]
        scored.append(own)
        allowed.append(opp)
        wins += 1 if own > opp else 0

    last_date = _parse_date(mine[0]["date"])
    days_since = (as_of_date - last_date).days if last_date and as_of_date else None
    if days_since is not None and days_since < 0:
        days_since = None

    n = len(mine)
    return {
        "games_sampled": n,
        "win_pct": wins / n,
        "avg_points_scored": sum(scored) / n,
        "avg_points_allowed": sum(allowed) / n,
        "avg_point_diff": (sum(scored) - sum(allowed)) / n,
        "days_since_last_game": days_since,
    }


class NbaStats:
    """Fetches games once, then answers form questions from memory."""

    def __init__(self, client, today=None, now=None):
        self._client = client
        self._today = today or datetime.date.today()
        self._now = now or datetime.datetime.now(datetime.timezone.utc)
        self._history = None

    def _season_start(self):
        override = os.environ.get("NBA_SEASON_START")
        parsed = _parse_date(override) if override else None
        return parsed or REGULAR_SEASON_START

    def todays_games(self):
        tomorrow = self._today + datetime.timedelta(days=1)
        raw = self._client.get_all_pages("/games", {
            "dates[]": [self._today.isoformat(), tomorrow.isoformat()], "per_page": 100})
        games = [g for g in (normalize_game(r) for r in raw) if g]
        upcoming = [g for g in games if is_upcoming(g, self._now)]
        log.info("nba_games_found", extra={"ctx": {
            "returned": len(games), "upcoming": len(upcoming)}})
        return upcoming

    def _load_history(self):
        if self._history is None:
            start = max(self._today - datetime.timedelta(days=HISTORY_DAYS), self._season_start())
            raw = self._client.get_all_pages("/games", {
                "start_date": start.isoformat(), "end_date": self._today.isoformat(),
                "per_page": 100})
            games = [g for g in (normalize_game(r) for r in raw) if g]
            self._history = [g for g in games if g["status"] == "post"]
            log.info("nba_history_loaded", extra={"ctx": {
                "from": start.isoformat(), "finished_games": len(self._history)}})
        return self._history

    def team_form(self, team_id, as_of_date=None):
        return compute_team_form(self._load_history(), team_id, as_of_date or self._today)


_default_stats = None


def _stats():
    global _default_stats
    if _default_stats is None:
        _default_stats = NbaStats(BalldontlieClient(BALLDONTLIE_API_KEY))
    return _default_stats


def get_todays_games():
    return _stats().todays_games()


def team_form_summary(team_id, as_of_date=None):
    return _stats().team_form(team_id, as_of_date)


if __name__ == "__main__":
    # Manual check: python nba_stats_fetcher.py  (logs what it found, as JSON lines)
    try:
        found = get_todays_games()
    except NbaDataError as exc:
        log.error("nba_check_failed", extra={"ctx": {"error": str(exc)}})
    else:
        for g in found:
            home, away = g["home_team"], g["visitor_team"]
            log.info("nba_game", extra={"ctx": {
                "matchup": f"{away['full_name']} @ {home['full_name']}",
                "home_form": team_form_summary(home["id"]),
                "away_form": team_form_summary(away["id"])}})
