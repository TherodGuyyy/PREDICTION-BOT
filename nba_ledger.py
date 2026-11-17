"""
The NBA prediction ledger: remember every forecast, grade it later, learn from it.

HOW IT WORKS (plain English)
  1. Each day, before tip-off, every forecast the bot makes is saved here:
       - who we think wins (full game, each half, each quarter) and how sure we are
       - the predicted score for each of those periods
       - every value tip we found, and whether it was actually sent to Telegram
     We also save the inputs behind the forecast (rest days, recent form).
  2. The next day, finished games are matched to saved forecasts and graded:
       - winner calls and tips are HIT, MISS or PUSH
       - score forecasts are scored by how far off they were (in points)
  3. For every miss, simple rules attach "likely contributing factors"
     (back-to-back, overtime, one team scoring far below expectation, ...).
     These are hints worth checking, not proven causes.
  4. A running track record shows whether "70% sure" really wins about 70%
     of the time (calibration), plus the average error of the score forecasts.

STORAGE
  One JSON file (nba_ledger.json) committed back to the repo by the daily
  GitHub Actions run, same as the tennis archive. The storage is behind a
  tiny "store" object (load/save), so moving to a real database later means
  writing one new store class, not touching the logic below.

A forecast is saved while its game is still pending and is never changed
after the game has been graded.
"""

import datetime
import json
import os

from nba_model import PERIODS
from structured_logging import get_logger

log = get_logger("nba_ledger")

LEDGER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nba_ledger.json")
SCHEMA_VERSION = 1
UNRESOLVED_AFTER_DAYS = 6          # a game we still cannot find after this long is given up on
CLOSE_GAME_POINTS = 4              # a loss by this few points (or fewer) is mostly luck
BLOWOUT_POINTS = 15
BIG_SCORING_GAP = 0.10             # a team scoring 10%+ away from our forecast is worth noting
BUCKETS = ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01))
MAX_REPORT_CHARS = 3500

_BAD_DATA = (KeyError, TypeError, ValueError, ZeroDivisionError)


class LedgerError(Exception):
    """The ledger file could not be read or written."""


# ---------------------------------------------------------------- storage

class JsonFileStore:
    """Whole-file JSON store. Writes go to a temp file first so a crash cannot leave half a ledger."""

    def __init__(self, path=LEDGER_PATH):
        self._path = path

    def load(self):
        if not os.path.exists(self._path):
            return {"version": SCHEMA_VERSION, "games": {}}
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            # Do NOT start fresh here: saving an empty ledger over a damaged one would destroy history.
            raise LedgerError(f"cannot read {self._path}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("games"), dict):
            raise LedgerError(f"{self._path} does not look like a ledger")
        return data

    def save(self, data):
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError as exc:
            raise LedgerError(f"cannot write {self._path}: {exc}") from exc


# ---------------------------------------------------------------- building records (pure)

def tip_key(game_id, tip):
    return (game_id, tip.get("type"), tip.get("period"), tip.get("side"), tip.get("line"))


def build_calls(prediction, tips, sent_keys):
    """Everything we predicted for one game, as a flat list of 'calls'."""
    game_id = prediction.get("game_id")
    calls = []

    full_pick = "home" if prediction["favorite"] == prediction["home"] else "away"
    calls.append({"id": "full:winner", "kind": "winner", "period": "full", "pick": full_pick,
                  "prob": prediction["favorite_prob"], "market_prob": prediction.get("favorite_implied_prob")})
    calls.append({"id": "full:points", "kind": "points", "period": "full",
                  "pred_home": prediction["predicted_home_score"], "pred_away": prediction["predicted_away_score"],
                  "pred_total": prediction["anchored_total"], "market_line": prediction.get("market_main_line")})

    for key, fc in (prediction.get("period_forecasts") or {}).items():
        pick = "home" if fc["home_win_prob"] >= 0.5 else "away"
        prob = fc["home_win_prob"] if pick == "home" else 1 - fc["home_win_prob"]
        calls.append({"id": f"{key}:winner", "kind": "winner", "period": key, "pick": pick, "prob": round(prob, 3)})
        calls.append({"id": f"{key}:points", "kind": "points", "period": key,
                      "pred_home": fc["home_score"], "pred_away": fc["away_score"], "pred_total": fc["total"]})

    for tip in tips:
        if tip.get("game_id") != game_id:
            continue
        calls.append({
            "id": f"tip:{tip['period']}:{tip['type']}:{tip['side']}:{tip.get('line')}",
            "kind": "tip", "tip_type": tip["type"], "period": tip["period"], "side": tip["side"],
            "line": tip.get("line"), "odds": tip["odds"], "prob": tip["our_estimated_prob"],
            "edge": tip["edge"], "sent": tip_key(game_id, tip) in sent_keys,
        })
    return calls


def build_record(prediction, tips, sent_keys, run_date):
    return {
        "game_id": prediction["game_id"], "game_date": prediction.get("game_date") or run_date,
        "run_date": run_date, "matchup": prediction["matchup"],
        "home": prediction["home"], "away": prediction["away"],
        "inputs": prediction.get("inputs") or {},
        "calls": build_calls(prediction, tips, sent_keys),
        "status": "pending", "result": None,
    }


# ---------------------------------------------------------------- grading (pure)

def actual_period(game, key):
    """(home_points, away_points) for the period, or None if quarter scores are missing."""
    if key == "full":
        if game.get("home_score") is None or game.get("away_score") is None:
            return None
        return game["home_score"], game["away_score"]
    home_q, away_q = game.get("home_q"), game.get("away_q")
    if not home_q or not away_q:
        return None
    idx = PERIODS[key]["quarters"]
    return sum(home_q[i] for i in idx), sum(away_q[i] for i in idx)


def went_to_overtime(game):
    home_q, away_q = game.get("home_q"), game.get("away_q")
    if not home_q or not away_q or game.get("home_score") is None:
        return False
    return sum(home_q) != game["home_score"] or sum(away_q) != game["away_score"]


def _outcome_from_margin(value):
    """value > 0 means our side won/covered; 0 is a push."""
    if value > 0:
        return "hit"
    return "push" if value == 0 else "miss"


def _grade_call(call, actual):
    home, away = actual
    margin, total = home - away, home + away
    kind = call["kind"]

    if kind == "winner" or (kind == "tip" and call["tip_type"] == "moneyline"):
        side = call["pick"] if kind == "winner" else call["side"]
        outcome = _outcome_from_margin(margin if side == "home" else -margin)
    elif kind == "tip" and call["tip_type"] == "handicap":
        outcome = _outcome_from_margin((margin if call["side"] == "home" else -margin) + call["line"])
    elif kind == "tip" and call["tip_type"] == "totals":
        diff = total - call["line"]
        outcome = _outcome_from_margin(diff if call["side"] == "over" else -diff)
    elif kind == "points":
        return {"outcome": "scored", "actual_home": home, "actual_away": away, "actual_total": total,
                "total_error": total - call["pred_total"],
                "margin_error": margin - (call["pred_home"] - call["pred_away"])}
    else:
        raise ValueError(f"unknown call kind {kind}")
    return {"outcome": outcome, "actual_home": home, "actual_away": away}


def _side_points(actual, side):
    return actual[0] if side == "home" else actual[1]


def explain_miss(record, call, actual, overtime):
    """Plain-English 'likely contributing factors' for a missed call. Hints, not proof."""
    reasons = []
    home, away = actual
    margin, total = home - away, home + away
    side = call.get("pick") or call.get("side")
    period = call["period"]
    inputs = record.get("inputs") or {}
    ptype = call.get("tip_type")

    if overtime and period == "full":
        reasons.append("Game went to overtime, which our forecast does not model.")
    if PERIODS[period]["fraction"] < 1:
        reasons.append("Short periods are very random; one hot or cold spell decides them.")

    if ptype != "totals" and side in ("home", "away"):
        mine, theirs = ("home", "away") if side == "home" else ("away", "home")
        if (inputs.get(mine) or {}).get("days_since_last_game") == 1 and \
                (inputs.get(theirs) or {}).get("days_since_last_game") not in (1, None):
            reasons.append("Our pick was on a back-to-back and the opponent was rested.")
        our_margin = margin if side == "home" else -margin
        if 0 > our_margin >= -CLOSE_GAME_POINTS:
            reasons.append("Lost by a few points: close to a coin flip, mostly luck.")
        elif our_margin <= -BLOWOUT_POINTS:
            reasons.append("Lost by a lot: the form gap we relied on did not show up.")

    pred = _predicted_points(record, period)
    if pred is not None:
        for team_side, label in (("home", record["home"]), ("away", record["away"])):
            got, expected = _side_points(actual, team_side), pred[team_side]
            if expected and got <= expected * (1 - BIG_SCORING_GAP):
                reasons.append(f"{label} scored far fewer than expected ({got} vs about {expected}): "
                               "their offence or the opponent's defence was better than our numbers said.")
            elif expected and got >= expected * (1 + BIG_SCORING_GAP):
                reasons.append(f"{label} scored far more than expected ({got} vs about {expected}): "
                               "their offence beat our read, or the opponent's defence was weaker.")
    return reasons


def _predicted_points(record, period):
    for call in record["calls"]:
        if call["kind"] == "points" and call["period"] == period:
            return {"home": call["pred_home"], "away": call["pred_away"]}
    return None


def grade_record(record, game):
    """Returns the record with every call graded (new dict). Calls we cannot grade are marked."""
    overtime = went_to_overtime(game)
    graded_calls = []
    for call in record["calls"]:
        call = dict(call)
        actual = actual_period(game, call["period"])
        if actual is None:
            call["result"] = {"outcome": "ungradable", "why": "no quarter scores for this game"}
        else:
            call["result"] = _grade_call(call, actual)
            if call["result"]["outcome"] == "miss":
                call["result"]["reasons"] = explain_miss(record, call, actual, overtime)
        graded_calls.append(call)
    out = dict(record)
    out.update({"calls": graded_calls, "status": "graded",
                "result": {"home_score": game["home_score"], "away_score": game["away_score"],
                           "overtime": overtime}})
    return out


# ---------------------------------------------------------------- the daily step

def _days_between(later, earlier):
    return (datetime.date.fromisoformat(later) - datetime.date.fromisoformat(earlier)).days


def grade_pending(data, finished_games, run_date):
    """Grades every pending record whose game has finished. Returns the list of newly graded records."""
    by_id = {g["id"]: g for g in finished_games}
    newly = []
    for game_id, record in list(data["games"].items()):
        if record["status"] != "pending":
            continue
        game = by_id.get(record["game_id"])
        if game is not None and game.get("home_score") is not None:
            try:
                data["games"][game_id] = grade_record(record, game)
            except _BAD_DATA as exc:
                log.error("nba_ledger_grade_failed", extra={"ctx": {
                    "game_id": game_id, "error_type": type(exc).__name__, "error": str(exc)}})
                continue
            newly.append(data["games"][game_id])
            log.info("nba_ledger_graded", extra={"ctx": {
                "game_id": game_id, "matchup": record["matchup"],
                "calls": len(record["calls"]), "overtime": data["games"][game_id]["result"]["overtime"]}})
        elif _days_between(run_date, record["game_date"]) > UNRESOLVED_AFTER_DAYS:
            record["status"] = "unresolved"
            log.warning("nba_ledger_unresolved", extra={"ctx": {"game_id": game_id, "matchup": record["matchup"]}})
    return newly


def add_predictions(data, predictions, tips, selected_tips, run_date):
    """Saves today's forecasts. A graded record is never touched; a pending one may be refreshed pre-game."""
    sent_keys = {tip_key(t.get("game_id"), t) for t in selected_tips}
    saved = 0
    for prediction in predictions:
        try:
            game_id = str(prediction["game_id"])
            existing = data["games"].get(game_id)
            if existing and existing["status"] != "pending":
                continue
            data["games"][game_id] = build_record(prediction, tips, sent_keys, run_date)
            saved += 1
        except _BAD_DATA as exc:
            log.error("nba_ledger_record_failed", extra={"ctx": {
                "matchup": prediction.get("matchup"), "error_type": type(exc).__name__, "error": str(exc)}})
    return saved


def record_and_grade(predictions, tips, selected_tips, run_date, store=None, get_finished=None):
    """
    The one call main.py makes. Grades yesterday's forecasts, then saves today's.
    Returns {"newly_graded": [...], "summary": {...}, "saved": n} or None if the ledger is unusable.
    Never raises for ledger or data-feed trouble: it logs and lets the rest of the bot carry on.
    """
    from nba_stats_fetcher import NbaDataError, get_finished_games
    store = store or JsonFileStore()
    get_finished = get_finished or get_finished_games

    try:
        data = store.load()
    except LedgerError as exc:
        log.error("nba_ledger_unreadable", extra={"ctx": {"error": str(exc)}})
        return None

    newly = []
    if any(r["status"] == "pending" for r in data["games"].values()):
        try:
            newly = grade_pending(data, get_finished(), run_date)
        except NbaDataError as exc:
            log.warning("nba_ledger_grading_skipped", extra={"ctx": {"error": str(exc)}})

    saved = add_predictions(data, predictions, tips, selected_tips, run_date)
    try:
        store.save(data)
    except LedgerError as exc:
        log.error("nba_ledger_save_failed", extra={"ctx": {"error": str(exc)}})
        return None
    log.info("nba_ledger_saved", extra={"ctx": {
        "saved_forecasts": saved, "newly_graded_games": len(newly), "games_in_ledger": len(data["games"])}})
    return {"newly_graded": newly, "summary": summarize(data), "saved": saved}


# ---------------------------------------------------------------- track record (pure)

def _graded_calls(data):
    for record in data["games"].values():
        if record["status"] != "graded":
            continue
        for call in record["calls"]:
            if call["result"]["outcome"] != "ungradable":
                yield record, call


def _brier(rows):
    return sum((p - (1 if hit else 0)) ** 2 for p, hit in rows) / len(rows) if rows else None


def summarize(data):
    """Hit rates, calibration buckets and score-forecast errors across everything graded so far."""
    win_rows, by_group, points, sent = [], {}, {}, {"n": 0, "hit": 0, "miss": 0, "push": 0, "profit": 0.0}
    for _record, call in _graded_calls(data):
        outcome = call["result"]["outcome"]
        if call["kind"] == "points":
            bucket = points.setdefault(call["period"], {"n": 0, "abs_total": 0.0, "abs_margin": 0.0, "bias": 0.0})
            bucket["n"] += 1
            bucket["abs_total"] += abs(call["result"]["total_error"])
            bucket["abs_margin"] += abs(call["result"]["margin_error"])
            bucket["bias"] += call["result"]["total_error"]
            continue
        if outcome == "push":
            continue
        hit = outcome == "hit"
        group = by_group.setdefault(call["period"] if call["kind"] == "winner" else "tips",
                                    {"n": 0, "hit": 0, "probs": []})
        group["n"] += 1
        group["hit"] += hit
        group["probs"].append(call["prob"])
        win_rows.append((call["prob"], hit))
        if call["kind"] == "tip" and call.get("sent"):
            sent["n"] += 1
            sent["hit" if hit else "miss"] += 1
            sent["profit"] += (call["odds"] - 1) if hit else -1
    sent["pushes"] = sum(1 for _r, c in _graded_calls(data)
                         if c["kind"] == "tip" and c.get("sent") and c["result"]["outcome"] == "push")

    buckets = []
    for low, high in BUCKETS:
        rows = [(p, h) for p, h in win_rows if low <= p < high]
        if rows:
            buckets.append({"range": (low, min(high, 1.0)), "n": len(rows),
                            "avg_prob": sum(p for p, _ in rows) / len(rows),
                            "hit_rate": sum(1 for _, h in rows if h) / len(rows)})
    return {
        "games_graded": sum(1 for r in data["games"].values() if r["status"] == "graded"),
        "games_pending": sum(1 for r in data["games"].values() if r["status"] == "pending"),
        "groups": {k: {"n": v["n"], "hit_rate": v["hit"] / v["n"], "avg_prob": sum(v["probs"]) / v["n"]}
                   for k, v in by_group.items() if v["n"]},
        "buckets": buckets, "brier": _brier(win_rows),
        "points": {k: {"n": v["n"], "mae_total": v["abs_total"] / v["n"],
                       "mae_margin": v["abs_margin"] / v["n"], "total_bias": v["bias"] / v["n"]}
                   for k, v in points.items()},
        "sent_tips": sent,
    }


# ---------------------------------------------------------------- the message (pure)

def _pct(x):
    return f"{x * 100:.0f}%"


def _call_name(call):
    label = PERIODS[call["period"]]["label"]
    if call["kind"] == "winner":
        return f"{label} winner ({call['pick']}, {_pct(call['prob'])} sure)"
    if call["tip_type"] == "moneyline":
        return f"TIP {label} winner ({call['side']}, odds {call['odds']})"
    if call["tip_type"] == "handicap":
        return f"TIP {label} handicap ({call['side']} {call['line']:+g}, odds {call['odds']})"
    return f"TIP {label} {call['side']} {call['line']:g} (odds {call['odds']})"


def format_report(result):
    """Plain-text Telegram message: yesterday's results with reasons, then the running track record."""
    newly, s = result["newly_graded"], result["summary"]
    lines = ["NBA track record", "Predictions are checked against the real results the next day.\n"]

    for record in newly:
        res = record["result"]
        ot = " (overtime)" if res["overtime"] else ""
        lines.append(f"{record['away']} {res['away_score']} @ {record['home']} {res['home_score']}{ot}")
        for call in record["calls"]:
            outcome = call["result"]["outcome"]
            if call["kind"] == "points":
                if call["period"] == "full":
                    pred_t = call["pred_total"]
                    lines.append(f"  Score: predicted total {pred_t:.0f}, actual {call['result']['actual_total']} "
                                 f"({call['result']['total_error']:+.0f})")
                continue
            if call["period"] != "full" and call["kind"] != "tip":
                continue  # quarters/halves are summarised below, not listed game by game
            if outcome in ("hit", "miss", "push"):
                lines.append(f"  {outcome.upper()}: {_call_name(call)}")
                for reason in call["result"].get("reasons", [])[:3]:
                    lines.append(f"     - {reason}")
        lines.append("")

    lines.append(f"Running totals: {s['games_graded']} games graded, {s['games_pending']} waiting for results.")
    for key in ("full", "h1", "h2", "q1", "q2", "q3", "q4"):
        g = s["groups"].get(key)
        if g:
            lines.append(f"  {PERIODS[key]['label']} winner: {g['hit_rate'] * 100:.0f}% right "
                         f"over {g['n']} calls (we expected {_pct(g['avg_prob'])})")
    if s["buckets"]:
        lines.append("Calibration (when we say X% sure, how often are we right?):")
        for b in s["buckets"]:
            lines.append(f"  {_pct(b['range'][0])}-{_pct(b['range'][1])}: right {_pct(b['hit_rate'])} "
                         f"of {b['n']} (we said {_pct(b['avg_prob'])})")
    if s["brier"] is not None:
        lines.append(f"  Brier score {s['brier']:.3f} (0 is perfect, 0.25 is a coin flip; lower is better)")
    for key in ("full", "h1", "h2", "q1", "q2", "q3", "q4"):
        p = s["points"].get(key)
        if p:
            lines.append(f"  {PERIODS[key]['label']} score forecast: off by {p['mae_total']:.1f} total points on average "
                         f"(bias {p['total_bias']:+.1f}), margin off by {p['mae_margin']:.1f}")
    t = s["sent_tips"]
    if t["n"]:
        lines.append(f"Tips sent: {t['hit']} won, {t['miss']} lost, {t.get('pushes', 0)} push, "
                     f"profit {t['profit']:+.1f} units at 1 unit each")
    if s["games_graded"] < 30:
        lines.append("\nToo few games so far to draw conclusions; treat these numbers as early signs only.")

    text = "\n".join(lines)
    return text if len(text) <= MAX_REPORT_CHARS else text[:MAX_REPORT_CHARS - 20] + "\n...(cut short)"


def send_report(result, send=None):
    """Sends the track-record message, but only on days when something new was graded."""
    if not result or not result["newly_graded"]:
        return False
    if send is None:
        from telegram_sender import send_alert
        send = send_alert
    send(format_report(result))
    log.info("nba_ledger_report_sent", extra={"ctx": {"games": len(result["newly_graded"])}})
    return True
