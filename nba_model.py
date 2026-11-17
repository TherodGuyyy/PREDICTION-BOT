"""
NBA prediction model. Pure functions only: numbers in, numbers out. No
network, no files, so every rule here can be unit-tested in isolation.

THE IDEA, IN PLAIN WORDS
  1. Predict how many points the home team wins by (the "margin") from
     each team's last 10 games, nudged for home court and back-to-backs.
  2. Turn the margin into a WIN CHANCE with a normal curve.
  3. Do NOT trust that number alone. Ten games is a small sample, and the
     betting market already knows far more than we do. So we blend our
     number with the market's (winner/handicap) or anchor on the market's
     main line (totals). The bot only speaks up when our view is still
     clearly different after that blending. That keeps numbers realistic
     and keeps noise from becoming "tips".
  4. EDGE = our win chance minus the win chance the bookmaker's odds imply.
     Positive edge = the odds pay more than our chance says they should.

ONE ENGINE, EVERY PERIOD
  The same steps run for the full game, each half and each quarter, using
  that period's own scoring (balldontlie gives points per quarter). What
  changes per period:
    * home-court points scale with the share of the game (a quarter = 1/4)
    * randomness (std) scales with the square root of that share
    * form is shrunk MORE for shorter periods, because a single quarter's
      scoring is much noisier than a full game's
    * the edge needed is bigger for halves/quarters (MIN_EDGE_SUBMARKET)

NBA-SPECIFIC NUMBERS (the WNBA values in config.py would be wrong here):
  HOME_COURT_POINTS 2.5   typical NBA home-court edge in points
  MARGIN_STD        12.0  typical spread of NBA single-game margins
  FORM_SHRINK       0.6   last-10-game point differential is noisy; only
                          ~60% of it is real team quality (0.4 for halves,
                          0.25 for quarters - they are noisier still)
  B2B_POINTS        1.0   points lost on the 2nd night of a back-to-back
  TOTAL_STD         18.0  typical spread of NBA game totals
  PLAUSIBLE_TOTAL   190-270 for a full game (scaled for halves/quarters)
  MARKET_BLEND      0.5   winner/handicap: half model, half market
  TOTAL_MARKET_WEIGHT 0.7 totals: 70% market line, 30% our own number

These are reasoned starting values, not tuned on results. The prediction
ledger (a later build step) exists precisely so they can be checked against
real outcomes and changed with evidence.
"""

import math
from statistics import NormalDist

from analysis import _find_totals_candidates, _prob_is_plausible, implied_probability, passes_accuracy_bar
from config import MIN_EDGE, MIN_EDGE_SUBMARKET, MIN_ODDS

_NORMAL = NormalDist()

HOME_COURT_POINTS = 2.5
MARGIN_STD = 12.0
FORM_SHRINK = 0.6
B2B_POINTS = 1.0
TOTAL_STD = 18.0
MIN_PLAUSIBLE_TOTAL = 190
MAX_PLAUSIBLE_TOTAL = 270
MAX_PLAUSIBLE_HANDICAP = 25.0   # full-game handicap lines beyond this are not real NBA lines
MARKET_BLEND = 0.5
TOTAL_MARKET_WEIGHT = 0.7
FATIGUE_TOTAL_PENALTY = 1.0
NBA_MAX_TIPS_PER_DAY = 8
LEAGUE = "NBA"

# key -> label, share of the game, which quarters it covers, TheRundown period id, form shrink
PERIODS = {
    "full": {"label": "full game", "fraction": 1.0, "quarters": (0, 1, 2, 3), "period_id": 0, "shrink": FORM_SHRINK},
    "h1": {"label": "1st half", "fraction": 0.5, "quarters": (0, 1), "period_id": 1, "shrink": 0.4},
    "h2": {"label": "2nd half", "fraction": 0.5, "quarters": (2, 3), "period_id": 2, "shrink": 0.4},
    "q1": {"label": "1st quarter", "fraction": 0.25, "quarters": (0,), "period_id": 3, "shrink": 0.25},
    "q2": {"label": "2nd quarter", "fraction": 0.25, "quarters": (1,), "period_id": 4, "shrink": 0.25},
    "q3": {"label": "3rd quarter", "fraction": 0.25, "quarters": (2,), "period_id": 5, "shrink": 0.25},
    "q4": {"label": "4th quarter", "fraction": 0.25, "quarters": (3,), "period_id": 6, "shrink": 0.25},
}


def _normal_cdf(x, mean, std):
    return 0.5 * (1 + math.erf((x - mean) / (std * math.sqrt(2))))


def _clamp_prob(p):
    return min(max(p, 0.001), 0.999)


def _is_back_to_back(form):
    return form.get("days_since_last_game") == 1


def _rest_points(home_form, away_form):
    """
    +points for home if only the away team is on a back-to-back, and vice versa.
    If either team's rest is unknown we make no adjustment: an unknown must
    never be silently treated as "fully rested" (same rule as the WNBA code).
    """
    if home_form.get("days_since_last_game") is None or away_form.get("days_since_last_game") is None:
        return 0.0
    home_b2b, away_b2b = _is_back_to_back(home_form), _is_back_to_back(away_form)
    if away_b2b and not home_b2b:
        return B2B_POINTS
    if home_b2b and not away_b2b:
        return -B2B_POINTS
    return 0.0


# ---------------------------------------------------------------- per-period numbers

def period_points(form, key):
    """(average points scored, average points allowed) in that period, or None if unknown."""
    if key == "full":
        return form["avg_points_scored"], form["avg_points_allowed"]
    q_scored, q_allowed = form.get("q_scored"), form.get("q_allowed")
    if not q_scored or not q_allowed:
        return None
    idx = PERIODS[key]["quarters"]
    return sum(q_scored[i] for i in idx), sum(q_allowed[i] for i in idx)


def _period_diff(form, key):
    if key == "full":
        return form["avg_point_diff"]
    points = period_points(form, key)
    return None if points is None else points[0] - points[1]


def period_sigmas(key):
    """(margin std, total std) for a period: they shrink with the square root of its share of the game."""
    root = math.sqrt(PERIODS[key]["fraction"])
    return MARGIN_STD * root, TOTAL_STD * root


def period_margin(home_form, away_form, key="full"):
    """Predicted home margin in that period (positive = home ahead), or None if data is missing."""
    home_diff, away_diff = _period_diff(home_form, key), _period_diff(away_form, key)
    if home_diff is None or away_diff is None:
        return None
    cfg = PERIODS[key]
    return cfg["fraction"] * (HOME_COURT_POINTS + _rest_points(home_form, away_form)) + cfg["shrink"] * (
        home_diff - away_diff)


def predict_margin(home_form, away_form):
    """Full-game predicted home margin in points."""
    return period_margin(home_form, away_form, "full")


def margin_to_home_win_prob(margin, std=MARGIN_STD):
    return _normal_cdf(margin, 0.0, std)


def model_total(home_form, away_form, key="full"):
    """Our own predicted combined score for the period (before anchoring to the market), or None."""
    home, away = period_points(home_form, key), period_points(away_form, key)
    if home is None or away is None:
        return None
    home_pts = (home[0] + away[1]) / 2
    away_pts = (away[0] + home[1]) / 2
    penalty = FATIGUE_TOTAL_PENALTY * PERIODS[key]["fraction"]
    if _is_back_to_back(home_form):
        home_pts -= penalty
    if _is_back_to_back(away_form):
        away_pts -= penalty
    return home_pts + away_pts


# ---------------------------------------------------------------- market helpers

def devig_home_prob(home_odds, away_odds):
    """Removes the bookmaker's margin: market's fair home-win probability, or None."""
    if not home_odds or not away_odds:
        return None
    h, a = 1 / home_odds, 1 / away_odds
    return h / (h + a)


def market_margin_from_spread(spread_entries):
    """
    The margin the handicap market itself is pricing: take the handicap
    line whose two sides have the most even prices, then flip its sign
    (home -5.5 means the market expects home to win by about 5.5).
    entries: [{"side": "home"|"away", "line": float, "odds": decimal}]
    """
    home = {e["line"]: e["odds"] for e in spread_entries if e["side"] == "home" and e.get("odds")}
    away = {e["line"]: e["odds"] for e in spread_entries if e["side"] == "away" and e.get("odds")}
    pairs = [(line, home[line], away[-line]) for line in home if -line in away]
    if not pairs:
        return None
    best = min(pairs, key=lambda p: abs(1 / p[1] - 1 / p[2]))
    return -best[0]


def pick_main_line(totals_odds, line_range=(MIN_PLAUSIBLE_TOTAL, MAX_PLAUSIBLE_TOTAL)):
    """The 'main' total is the line whose over and under prices are closest to even."""
    usable = [t for t in totals_odds
              if t.get("line") is not None and t.get("over_odds") and t.get("under_odds")
              and line_range[0] <= t["line"] <= line_range[1]]
    if not usable:
        return None
    best = min(usable, key=lambda t: abs(1 / t["over_odds"] - 1 / t["under_odds"]))
    return best["line"]


def anchored_total(own_total, main_line):
    """Blend of the market's main line and our own number. No market -> our own."""
    if main_line is None:
        return own_total
    return TOTAL_MARKET_WEIGHT * main_line + (1 - TOTAL_MARKET_WEIGHT) * own_total


def _line_range(key):
    fraction = PERIODS[key]["fraction"]
    return MIN_PLAUSIBLE_TOTAL * fraction, MAX_PLAUSIBLE_TOTAL * fraction


def _min_edge(key):
    return MIN_EDGE if key == "full" else MIN_EDGE_SUBMARKET


def _passes(prob, odds, min_edge):
    return bool(odds) and odds >= MIN_ODDS and (prob - implied_probability(odds)) >= min_edge \
        and _prob_is_plausible(prob) and passes_accuracy_bar(prob, odds)


# ---------------------------------------------------------------- the view of one period

def period_view(home_form, away_form, key="full", market=None):
    """
    Everything we believe about one period, after blending with that
    period's market. Returns None if the forms lack data for the period.
    market: {"moneyline": {"home_odds", "away_odds"} | None, "spread": [...], "totals": [...]}
    """
    market = market or {}
    margin = period_margin(home_form, away_form, key)
    own_total = model_total(home_form, away_form, key)
    if margin is None or own_total is None:
        return None
    margin_std, total_std = period_sigmas(key)
    model_p = margin_to_home_win_prob(margin, margin_std)

    ml = market.get("moneyline") or {}
    market_p = devig_home_prob(ml.get("home_odds"), ml.get("away_odds"))
    if market_p is None:
        spread_margin = market_margin_from_spread(market.get("spread") or [])
        if spread_margin is not None:
            market_p = _normal_cdf(spread_margin, 0.0, margin_std)

    final_p = model_p if market_p is None else MARKET_BLEND * market_p + (1 - MARKET_BLEND) * model_p
    main_line = pick_main_line(market.get("totals") or [], _line_range(key))
    return {
        "key": key, "label": PERIODS[key]["label"],
        "model_p_home": model_p, "market_p_home": market_p, "final_p_home": final_p,
        "final_margin": margin_std * _NORMAL.inv_cdf(_clamp_prob(final_p)),
        "margin_std": margin_std, "total_std": total_std,
        "own_total": own_total, "main_line": main_line,
        "anchored_total": anchored_total(own_total, main_line),
    }


def blended_home_prob(home_form, away_form, home_odds=None, away_odds=None):
    """Full game only. Returns (final_prob_home, model_prob_home, market_prob_home_or_None, margin)."""
    market = {"moneyline": {"home_odds": home_odds, "away_odds": away_odds}}
    view = period_view(home_form, away_form, "full", market)
    return view["final_p_home"], view["model_p_home"], view["market_p_home"], predict_margin(home_form, away_form)


# ---------------------------------------------------------------- tips

def _label(game):
    return f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}"


def _best(candidates):
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c["our_estimated_prob"], c["edge"]))


def _winner_candidates(game, view, odds, key):
    p_home, cfg = view["final_p_home"], PERIODS[key]
    sides = (
        ("home", p_home, odds.get("home_odds"), game["home_team"]["full_name"], game["visitor_team"]["full_name"]),
        ("away", 1 - p_home, odds.get("away_odds"), game["visitor_team"]["full_name"], game["home_team"]["full_name"]),
    )
    out = []
    for side, prob, price, team, opponent in sides:
        if _passes(prob, price, _min_edge(key)):
            out.append({
                "type": "moneyline", "league": LEAGUE, "period": key, "period_label": cfg["label"],
                "team": team, "opponent": opponent, "side": side, "odds": price,
                "our_estimated_prob": round(prob, 3),
                "market_implied_prob": round(implied_probability(price), 3),
                "edge": round(prob - implied_probability(price), 3),
            })
    return out


def _handicap_candidates(game, view, spread_entries, key):
    cfg = PERIODS[key]
    margin, std = view["final_margin"], view["margin_std"]
    limit = MAX_PLAUSIBLE_HANDICAP * math.sqrt(cfg["fraction"])
    out = []
    for entry in spread_entries:
        line, price, side = entry.get("line"), entry.get("odds"), entry.get("side")
        if line is None or abs(line) > limit:
            continue
        if side == "home":
            prob, team = _normal_cdf(margin + line, 0.0, std), game["home_team"]["full_name"]
        elif side == "away":
            prob, team = _normal_cdf(-margin + line, 0.0, std), game["visitor_team"]["full_name"]
        else:
            continue
        if _passes(prob, price, _min_edge(key)):
            out.append({
                "type": "handicap", "league": LEAGUE, "period": key, "period_label": cfg["label"],
                "matchup": _label(game), "team": team, "side": side, "line": line, "odds": price,
                "market_id": 2, "our_estimated_prob": round(prob, 3),
                "market_implied_prob": round(implied_probability(price), 3),
                "edge": round(prob - implied_probability(price), 3),
            })
    return out


def _totals_candidates(game, view, totals_odds, key):
    cfg = PERIODS[key]
    if view["main_line"] is None:
        return []
    return _find_totals_candidates(
        _label(game), "totals", view["anchored_total"], totals_odds,
        std_dev=view["total_std"], min_edge=_min_edge(key), line_range=_line_range(key),
        extra_fields={"league": LEAGUE, "period": key, "period_label": cfg["label"],
                      "model_total": round(view["own_total"], 1),
                      "anchored_total": round(view["anchored_total"], 1), "main_line": view["main_line"]},
    )


def find_period_tips(game, home_form, away_form, key, market):
    """Best winner tip, best handicap tip and best total tip for ONE period (each may be absent)."""
    view = period_view(home_form, away_form, key, market)
    if view is None:
        return []
    tips = [
        _best(_winner_candidates(game, view, market.get("moneyline") or {}, key)),
        _best(_handicap_candidates(game, view, market.get("spread") or [], key)),
        _best(_totals_candidates(game, view, market.get("totals") or [], key)),
    ]
    return [t for t in tips if t]


def find_all_tips(game, home_form, away_form, markets):
    """Every qualifying tip across the full game, both halves and all four quarters."""
    tips = []
    for key in PERIODS:
        market = markets.get(key)
        if market:
            tips.extend(find_period_tips(game, home_form, away_form, key, market))
    return tips


# kept as small full-game helpers (used by tests and quick checks)

def find_moneyline_tip(game, home_form, away_form, home_odds, away_odds):
    market = {"moneyline": {"home_odds": home_odds, "away_odds": away_odds}}
    view = period_view(home_form, away_form, "full", market)
    return _best(_winner_candidates(game, view, market["moneyline"], "full"))


def find_totals_tip(game, home_form, away_form, totals_odds):
    view = period_view(home_form, away_form, "full", {"totals": totals_odds})
    return _best(_totals_candidates(game, view, totals_odds, "full"))


# ---------------------------------------------------------------- predictions + digest

def period_forecasts(home_form, away_form):
    """
    Our own half and quarter forecasts, no bookmaker needed: for each period
    we have quarter data for, the home team's win chance and the predicted score.
    """
    out = {}
    for key in ("h1", "h2", "q1", "q2", "q3", "q4"):
        view = period_view(home_form, away_form, key)
        if view is None:
            continue
        total, margin = view["own_total"], view["final_margin"]
        out[key] = {
            "label": view["label"], "home_win_prob": round(view["final_p_home"], 3),
            "home_score": round((total + margin) / 2), "away_score": round((total - margin) / 2),
            "total": round(total, 1),
        }
    return out


def _inputs(form):
    """The few numbers behind a forecast that we keep so a miss can be explained later."""
    return {k: form.get(k) for k in ("win_pct", "avg_point_diff", "avg_points_scored",
                                     "avg_points_allowed", "days_since_last_game")}


def build_prediction(game, home_form, away_form, market=None, periods_available=None):
    """One plain dict per game: the model's full-game view, used for the digest and the logs."""
    market = market or {}
    view = period_view(home_form, away_form, "full", market)
    home_name, away_name = game["home_team"]["full_name"], game["visitor_team"]["full_name"]
    p_home = view["final_p_home"]
    favorite_is_home = p_home >= 0.5
    ml = market.get("moneyline") or {}
    fav_odds = ml.get("home_odds") if favorite_is_home else ml.get("away_odds")
    fav_prob = p_home if favorite_is_home else 1 - p_home
    fav_implied = implied_probability(fav_odds) if fav_odds else None

    total = view["anchored_total"]
    margin = view["final_margin"]
    return {
        "game_id": game.get("id"), "game_date": game.get("date"),
        "matchup": _label(game), "home": home_name, "away": away_name,
        "inputs": {"home": _inputs(home_form), "away": _inputs(away_form)},
        "favorite": home_name if favorite_is_home else away_name,
        "favorite_prob": round(fav_prob, 3),
        "favorite_odds": fav_odds,
        "favorite_implied_prob": None if fav_implied is None else round(fav_implied, 3),
        "favorite_edge": None if fav_implied is None else round(fav_prob - fav_implied, 3),
        "model_only_home_prob": round(view["model_p_home"], 3),
        "market_home_prob": None if view["market_p_home"] is None else round(view["market_p_home"], 3),
        "predicted_margin": round(margin, 1),
        "predicted_home_score": round((total + margin) / 2),
        "predicted_away_score": round((total - margin) / 2),
        "model_total": round(view["own_total"], 1),
        "market_main_line": view["main_line"],
        "anchored_total": round(total, 1),
        "used_market": view["market_p_home"] is not None,
        "periods_available": sorted(periods_available or []),
        "period_forecasts": period_forecasts(home_form, away_form),
    }


MAX_MESSAGE_CHARS = 3500  # Telegram's hard limit is 4096; leave headroom
LEGEND = ("Win chance = how likely we think it is. Edge = our win chance minus the win chance the "
          "bookmaker's odds imply (above zero means the odds pay more than they should).\n")


def _pct(x):
    return f"{x * 100:.0f}%"


def _winner_line(p):
    line = f"  Winner: {p['favorite']}, {_pct(p['favorite_prob'])} win chance"
    if p["favorite_odds"] and p["favorite_edge"] is not None:
        verdict = "" if p["favorite_edge"] >= MIN_EDGE else ", no value"
        line += (f" (bookmaker odds {p['favorite_odds']} imply {_pct(p['favorite_implied_prob'])}, "
                 f"edge {p['favorite_edge'] * 100:+.0f}%{verdict})")
    return line + "\n"


def _period_lines(p):
    """Our own half/quarter forecasts (no bookmaker lines involved, so these are predictions, not value tips)."""
    f = p.get("period_forecasts") or {}
    if not f:
        return ""
    out = ""
    halves = [f[k] for k in ("h1", "h2") if k in f]
    if halves:
        out += "  Halves (home-away, home win chance): " + "; ".join(
            f"{h['label']} {h['home_score']}-{h['away_score']} ({_pct(h['home_win_prob'])})" for h in halves) + "\n"
    quarters = [f[k] for k in ("q1", "q2", "q3", "q4") if k in f]
    if quarters:
        out += "  Quarters (home-away): " + ", ".join(
            f"Q{i} {q['home_score']}-{q['away_score']}" for i, q in enumerate(quarters, 1)) + "\n"
    return out


def format_digest(predictions, date_str):
    """Returns a list of message strings (usually one) with plain text only."""
    header = (f"NBA model view for {date_str}\n"
              "Predictions only, not betting advice. Value tips (if any) come as separate messages.\n"
              + LEGEND)
    blocks = []
    for p in predictions:
        block = f"\n{p['matchup']}\n" + _winner_line(p)
        block += f"  Predicted score: {p['home']} {p['predicted_home_score']} - {p['away']} {p['predicted_away_score']}\n"
        if p["market_main_line"] is not None:
            block += f"  Total points: we predict {p['anchored_total']:.0f}, bookmaker line {p['market_main_line']}\n"
        else:
            block += f"  Total points: we predict {p['anchored_total']:.0f} (no bookmaker line found)\n"
        block += _period_lines(p)
        blocks.append(block)

    footer = ""
    seen = set().union(*(set(p.get("periods_available") or []) for p in predictions)) if predictions else set()
    if "full" in seen and seen == {"full"}:
        footer = ("\nNote: no half or quarter lines were available from the free odds feed today, "
                  "so only full-game tips were possible. The half and quarter lines above are our own forecasts.\n")

    messages, current = [], header
    for block in blocks:
        if len(current) + len(block) > MAX_MESSAGE_CHARS:
            messages.append(current)
            current = "NBA model view (continued)\n"
        current += block
    messages.append(current + footer)
    return messages
