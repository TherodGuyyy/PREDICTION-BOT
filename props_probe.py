"""
Prop-lines probe: does YOUR TheRundown key return NBA player-prop lines?

Why this exists: TheRundown's docs list the NBA prop markets (points,
rebounds, assists, threes, PRA ...) but do not say whether the free tier
includes them. Rather than build a props model on a guess, run this once
and read the verdict.

Run it (GitHub Actions -> "Props Probe" -> Run workflow), ideally a few
hours before NBA tip-off (roughly 17:00-22:00 UTC): books post props late,
so a run at 08:00 UTC can say "no props posted" even on a paid plan.

Verdicts (logged as event "probe_verdict"):
    PROPS_AVAILABLE   prop markets came back with real lines -> free tier works
    NO_PROPS_POSTED   games exist but no prop lines yet -> inconclusive, rerun later
    NO_GAMES          no NBA games in the window -> inconclusive, rerun on a game day
    ACCESS_DENIED     401/402/403 -> key rejected or plan does not include this

Design notes (kept deliberately small and testable):
  * The HTTP layer is a class with an injectable session and sleep function,
    so tests run with fakes and never touch the network.
  * Network calls retry with exponential backoff (1s, 2s, 4s) on timeouts,
    connection errors, 429 and 5xx. Auth/plan errors are NOT retried.
  * The API key is read from the THERUNDOWN_API_KEY environment variable only.
  * The probe logs a truncated copy of the first raw prop market. Prop payload
    shapes are undocumented for us, and that sample is what lets the real
    props module be written against facts instead of guesses.
"""

import datetime
import json
import os
import sys
import time

import requests

from structured_logging import get_logger

log = get_logger("props_probe")

BASE_URL = "https://therundown.io/api/v2"
NBA_FALLBACK_SPORT_ID = 4  # per TheRundown docs; used only if /sports lookup fails
MAX_RETRIES = 3
RETRY_STATUSES = (429, 500, 502, 503, 504)
ACCESS_STATUSES = (401, 402, 403)
SAMPLE_CHAR_LIMIT = 1500

# market_id -> friendly name, from TheRundown's player-props guide
PROP_MARKETS = {
    29: "points",
    35: "rebounds",
    39: "assists",
    38: "threes_made",
    93: "points_rebounds_assists",
    99: "points_assists",
    297: "points_rebounds",
    298: "rebounds_assists",
    98: "blocks",
    33: "turnovers",
}

VERDICT_PROPS = "PROPS_AVAILABLE"
VERDICT_NO_PROPS = "NO_PROPS_POSTED"
VERDICT_NO_GAMES = "NO_GAMES"
VERDICT_DENIED = "ACCESS_DENIED"


class ProbeConfigError(RuntimeError):
    """Raised when the API key is missing; nothing was sent."""


class ProbeAccessError(RuntimeError):
    """Raised on 401/402/403: key rejected, or plan lacks this feature."""

    def __init__(self, status, path, body):
        self.status = status
        self.path = path
        self.body = body
        super().__init__(f"HTTP {status} on {path}")


class ProbeResponseError(RuntimeError):
    """Raised when a 2xx response is not valid JSON."""


class RundownHttp:
    """Minimal authenticated GET client with retries and injectable I/O."""

    def __init__(self, api_key, session=None, sleep=time.sleep):
        if not api_key:
            raise ProbeConfigError(
                "THERUNDOWN_API_KEY is not set. Add it as a GitHub Actions secret "
                "(Settings -> Secrets and variables -> Actions)."
            )
        self._api_key = api_key
        self._session = session or requests.Session()
        self._sleep = sleep

    def get_json(self, path, params=None):
        headers = {"X-TheRundown-Key": self._api_key}
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._session.get(
                    f"{BASE_URL}{path}", params=params, headers=headers, timeout=20
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt < MAX_RETRIES:
                    delay = 2 ** attempt
                    log.warning(
                        "request_retry",
                        extra={"ctx": {"path": path, "attempt": attempt + 1,
                                       "delay_s": delay, "error": type(exc).__name__}},
                    )
                    self._sleep(delay)
                    continue
                raise

            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                delay = 2 ** attempt
                log.warning(
                    "request_retry",
                    extra={"ctx": {"path": path, "attempt": attempt + 1,
                                   "delay_s": delay, "status": resp.status_code}},
                )
                self._sleep(delay)
                continue

            if resp.status_code in ACCESS_STATUSES:
                raise ProbeAccessError(resp.status_code, path, (resp.text or "")[:300])

            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError as exc:
                raise ProbeResponseError(f"Non-JSON response from {path}") from exc

        raise RuntimeError("unreachable: retry loop exited without return or raise")


def find_nba_sport_id(client):
    """
    Exact-name match on 'NBA'. A substring match would wrongly hit 'WNBA'
    (the string 'nba' is inside 'wnba'), so equality is required.
    """
    try:
        data = client.get_json("/sports")
    except requests.HTTPError as exc:
        log.warning("sports_lookup_failed_using_fallback",
                    extra={"ctx": {"error": str(exc), "fallback_id": NBA_FALLBACK_SPORT_ID}})
        return NBA_FALLBACK_SPORT_ID

    entries = data if isinstance(data, list) else data.get("sports", data.get("data", []))
    for entry in entries or []:
        name = (entry.get("name") or entry.get("sport_name") or "").strip().lower()
        sport_id = entry.get("id", entry.get("sport_id"))
        if name == "nba" and sport_id is not None:
            return sport_id

    log.warning("nba_not_found_in_sports_list_using_fallback",
                extra={"ctx": {"fallback_id": NBA_FALLBACK_SPORT_ID}})
    return NBA_FALLBACK_SPORT_ID


def summarise_events(events):
    """
    Pure function: reduces raw events to counts per prop market, plus one
    truncated raw sample of the first prop market found.
    """
    per_market = {}
    events_with_props = 0
    sample = None

    for event in events:
        has_props = False
        for market in event.get("markets", []):
            market_id = market.get("market_id")
            if market_id not in PROP_MARKETS:
                continue
            participants = market.get("participants") or []
            line_count = 0
            players = set()
            books = set()
            for participant in participants:
                players.add(participant.get("name"))
                for line in participant.get("lines", []):
                    prices = line.get("prices") or {}
                    if prices:
                        line_count += 1
                        books.update(prices.keys())
            if line_count == 0:
                continue
            has_props = True
            stats = per_market.setdefault(
                PROP_MARKETS[market_id],
                {"events": 0, "participant_names": 0, "lines": 0, "books": set()},
            )
            stats["events"] += 1
            stats["participant_names"] += len(players)
            stats["lines"] += line_count
            stats["books"].update(books)
            if sample is None:
                sample = json.dumps(market, default=str)[:SAMPLE_CHAR_LIMIT]
        if has_props:
            events_with_props += 1

    return {
        "events_total": len(events),
        "events_with_props": events_with_props,
        "per_market": {
            name: {**s, "books": len(s["books"])} for name, s in per_market.items()
        },
        "raw_sample_first_prop_market": sample,
    }


def decide_verdict(summary):
    if summary["events_total"] == 0:
        return VERDICT_NO_GAMES
    if summary["events_with_props"] == 0:
        return VERDICT_NO_PROPS
    return VERDICT_PROPS


def run_probe(client, dates):
    """Fetches NBA events for each date and returns a combined report."""
    sport_id = find_nba_sport_id(client)
    market_ids = ",".join(str(m) for m in PROP_MARKETS)

    events = []
    seen = set()
    for date_str in dates:
        data = client.get_json(
            f"/sports/{sport_id}/events/{date_str}", {"market_ids": market_ids}
        )
        for event in (data.get("events", []) if isinstance(data, dict) else []):
            event_id = event.get("event_id")
            if event_id is not None and event_id in seen:
                continue
            seen.add(event_id)
            events.append(event)

    summary = summarise_events(events)
    summary["verdict"] = decide_verdict(summary)
    summary["sport_id"] = sport_id
    summary["dates"] = list(dates)
    return summary


def _write_step_summary(report):
    """Writes a readable markdown verdict to the Actions run page, if available."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        "## NBA prop-lines probe",
        f"**Verdict: {report['verdict']}**",
        f"- Dates checked (UTC): {', '.join(report['dates'])}",
        f"- NBA events found: {report['events_total']}",
        f"- Events with prop lines: {report['events_with_props']}",
    ]
    for name, stats in report["per_market"].items():
        lines.append(f"- {name}: {stats['lines']} lines, {stats['books']} books")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        log.warning("step_summary_write_failed", extra={"ctx": {"error": str(exc)}})


def main(env=None, today=None):
    env = os.environ if env is None else env
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    dates = [today.isoformat(), (today + datetime.timedelta(days=1)).isoformat()]

    try:
        client = RundownHttp(env.get("THERUNDOWN_API_KEY"))
    except ProbeConfigError as exc:
        log.error("probe_config_error", extra={"ctx": {"error": str(exc)}})
        return 2

    log.info("probe_started", extra={"ctx": {"dates": dates}})
    try:
        report = run_probe(client, dates)
    except ProbeAccessError as exc:
        log.error("probe_verdict", extra={"ctx": {
            "verdict": VERDICT_DENIED, "status": exc.status,
            "path": exc.path, "body": exc.body}})
        return 1
    except (requests.RequestException, ProbeResponseError) as exc:
        log.error("probe_failed", extra={"ctx": {"error": str(exc)}})
        return 1

    log.info("probe_verdict", extra={"ctx": report})
    _write_step_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
