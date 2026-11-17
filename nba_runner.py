"""
Runs the NBA part of the daily job. This is the "glue": it asks the data
modules for games, forms and odds, hands them to the pure model in
nba_model.py, and collects the results. It contains no maths of its own.

Every outside dependency (games, form, odds) is passed in as a function,
so tests can run the whole flow with fake data and no internet. When
nothing is passed in, the real fetchers are used.

Failure policy ("fail gracefully"): a problem with NBA data must never stop
the WNBA/NCAAB/ACB/tennis parts. Each failure is logged as JSON with a
clear event name, then the run carries on with whatever it has.

Returns two lists:
  tips         value tips (odds-based) - same shape the other sports use
  predictions  one model-view dict per analysed game (for the daily digest;
               also logged now, and stored in the prediction ledger later)
"""

import datetime
import time

import requests

import rundown_client as rc
from nba_model import NBA_MAX_TIPS_PER_DAY, build_prediction, find_all_tips, format_digest
from nba_stats_fetcher import NbaDataError
from structured_logging import get_logger

log = get_logger("nba_runner")

# Things that can go wrong on ONE game because a feed returned odd data.
# Listed explicitly (not a blanket except) so real bugs are not hidden.
_BAD_DATA = (KeyError, TypeError, ValueError, ZeroDivisionError)
_ODDS_FAILURES = (rc.RundownAuthError, requests.RequestException)


def _default_deps():
    import nba_odds_fetcher as odds
    import nba_stats_fetcher as stats
    return {
        "get_games": stats.get_todays_games,
        "get_form": stats.team_form_summary,
        "get_markets": odds.get_game_markets,
        "debug_status": odds.debug_fixture_status,
    }


def _game_date(game, fallback):
    try:
        return datetime.date.fromisoformat(game["date"])
    except (KeyError, TypeError, ValueError):
        return fallback


def run_nba(today, deps=None):
    d = _default_deps()
    d.update(deps or {})
    today_date = datetime.date.fromisoformat(today)

    log.info("nba_run_started", extra={"ctx": {"date": today}})
    try:
        games = d["get_games"]()
    except NbaDataError as exc:
        log.error("nba_games_unavailable", extra={"ctx": {"error": str(exc)}})
        return [], []

    if not games:
        log.info("nba_no_games", extra={"ctx": {"date": today}})
        return [], []

    tips, predictions = [], []
    for game in games:
        home, away = game["home_team"], game["visitor_team"]
        matchup = f"{away['full_name']} @ {home['full_name']}"
        try:
            as_of = _game_date(game, today_date)
            home_form = d["get_form"](home["id"], as_of)
            away_form = d["get_form"](away["id"], as_of)
            if not home_form or not away_form:
                log.info("nba_skip_not_enough_games", extra={"ctx": {"matchup": matchup}})
                continue

            markets = _safe_markets(d["get_markets"], home["full_name"], away["full_name"], today)
            if not markets:
                log.warning("nba_no_odds_found", extra={"ctx": {
                    "matchup": matchup, "detail": _safe_status(d["debug_status"], home, away, today)}})
            elif set(markets) != {"full"} and not home_form.get("q_scored"):
                log.info("nba_no_quarter_data_for_period_markets", extra={"ctx": {"matchup": matchup}})

            prediction = build_prediction(game, home_form, away_form, markets.get("full"),
                                          periods_available=list(markets))
            predictions.append(prediction)
            log.info("nba_prediction", extra={"ctx": prediction})

            for tip in find_all_tips(game, home_form, away_form, markets):
                tip["game_id"] = game["id"]  # lets the ledger tie the tip to its game
                log.info("nba_tip_found", extra={"ctx": tip})
                tips.append(tip)

        except NbaDataError as exc:
            log.error("nba_game_data_error", extra={"ctx": {"matchup": matchup, "error": str(exc)}})
        except _BAD_DATA as exc:
            log.error("nba_game_bad_data", extra={"ctx": {
                "matchup": matchup, "error_type": type(exc).__name__, "error": str(exc)}})

    log.info("nba_run_finished", extra={"ctx": {
        "games": len(games), "predictions": len(predictions), "tips": len(tips)}})
    return tips, predictions


def _safe_markets(fetch, home_name, away_name, today):
    """Odds are a bonus: if the feed fails we still publish the model's own view."""
    try:
        return fetch(home_name, away_name, today) or {}
    except _ODDS_FAILURES as exc:
        log.warning("nba_odds_unavailable", extra={"ctx": {
            "home": home_name, "away": away_name, "error": str(exc)}})
        return {}


def _safe_status(debug_status, home, away, today):
    try:
        return debug_status(home["full_name"], away["full_name"], today)
    except _ODDS_FAILURES as exc:
        return f"status lookup failed: {exc}"


def select_tips(tips, cap=NBA_MAX_TIPS_PER_DAY):
    """Best first (by win probability, edge breaks ties), capped like the other sports."""
    ranked = sorted(tips, key=lambda t: (t["our_estimated_prob"], t["edge"]), reverse=True)
    return ranked[:cap]


def send_digest(predictions, date_str, send=None, sleep=time.sleep):
    """Sends the daily 'model view' message(s). Sends nothing if there are no predictions."""
    if not predictions:
        return 0
    if send is None:
        from telegram_sender import send_alert
        send = send_alert
    messages = format_digest(predictions, date_str)
    for i, message in enumerate(messages):
        if i:
            sleep(1.5)  # Telegram rate limit: pace consecutive messages
        send(message)
    log.info("nba_digest_sent", extra={"ctx": {"messages": len(messages), "games": len(predictions)}})
    return len(messages)
