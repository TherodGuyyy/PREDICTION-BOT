"""
NBA prediction model. Pure functions only: numbers in, numbers out. No
network, no files, so every rule here can be unit-tested in isolation.

THE IDEA, IN PLAIN WORDS
  1. Predict the point margin (home minus away) from each team's last 10
     games, nudged for home court and back-to-backs.
  2. Turn the margin into a win probability with a normal curve.
  3. Do NOT trust that number alone. Ten games is a small sample, and the
     betting market already knows far more than we do. So we blend our
     number with the market's (moneyline) or anchor on the market's main
     line (totals). The bot then only speaks up when our view is still
     clearly different after that blending. This is what keeps numbers
     realistic and keeps noise from becoming "tips".

NBA-SPECIFIC NUMBERS (the WNBA values in config.py would be wrong here):
  HOME_COURT_POINTS 2.5   typical NBA home-court edge in points
  MARGIN_STD        12.0  typical spread of NBA single-game margins
  FORM_SHRINK       0.6   last-10-game point differential is noisy; only
                          ~60% of it is real team quality, so we scale it down
  B2B_POINTS        1.0   points lost by a team playing the 2nd night of a
                          back-to-back (kept conservative)
  TOTAL_STD         18.0  typical spread of NBA game totals around a good estimate
  PLAUSIBLE_TOTAL   190-270  lines outside this are not full-game NBA totals
  MARKET_BLEND      0.5   moneyline: half model, half market
  TOTAL_MARKET_WEIGHT 0.7 totals: 70% market line, 30% our own number

These are reasoned starting values, not tuned on results. The prediction
ledger (next build step) exists precisely so they can be checked against
real outcomes and changed with evidence.
"""

import math

from analysis import _find_totals_candidates, _prob_is_plausible, implied_probability, passes_accuracy_bar
from config import MIN_EDGE, MIN_ODDS

HOME_COURT_POINTS = 2.5
MARGIN_STD = 12.0
FORM_SHRINK = 0.6
B2B_POINTS = 1.0
TOTAL_STD = 18.0
MIN_PLAUSIBLE_TOTAL = 190
MAX_PLAUSIBLE_TOTAL = 270
MARKET_BLEND = 0.5
TOTAL_MARKET_WEIGHT = 0.7
FATIGUE_TOTAL_PENALTY = 1.0
NBA_MAX_TIPS_PER_DAY = 5
LEAGUE = "NBA"


def _normal_cdf(x, mean, std):
    return 0.5 * (1 + math.erf((x - mean) / (std * math.sqrt(2))))


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


def predict_margin(home_form, away_form):
    """Predicted home margin in points (positive = home wins by that many)."""
    form_gap = home_form["avg_point_diff"] - away_form["avg_point_diff"]
    return HOME_COURT_POINTS + FORM_SHRINK * form_gap + _rest_points(home_form, away_form)


def margin_to_home_win_prob(margin):
    return _normal_cdf(margin, 0.0, MARGIN_STD)


def devig_home_prob(home_odds, away_odds):
    """Removes the bookmaker's margin: market's fair home-win probability, or None."""
    if not home_odds or not away_odds:
        return None
    h, a = 1 / home_odds, 1 / away_odds
    return h / (h + a)


def blended_home_prob(home_form, away_form, home_odds=None, away_odds=None):
    """
    Returns (final_prob_home, model_prob_home, market_prob_home_or_None, margin).
    With no market odds the final probability is the model's alone.
    """
    margin = predict_margin(home_form, away_form)
    model_p = margin_to_home_win_prob(margin)
    market_p = devig_home_prob(home_odds, away_odds)
    if market_p is None:
        return model_p, model_p, None, margin
    final_p = MARKET_BLEND * market_p + (1 - MARKET_BLEND) * model_p
    return final_p, model_p, market_p, margin


def find_moneyline_tip(game, home_form, away_form, home_odds, away_odds):
    """Same gates as the WNBA/NCAAB tips (min odds, edge, win-prob, max odds, sanity cap)."""
    p_home, _model_p, _market_p, _margin = blended_home_prob(home_form, away_form, home_odds, away_odds)
    sides = (
        ("home", p_home, home_odds, game["home_team"]["full_name"], game["visitor_team"]["full_name"]),
        ("away", 1 - p_home, away_odds, game["visitor_team"]["full_name"], game["home_team"]["full_name"]),
    )
    candidates = []
    for side, prob, odds, team, opponent in sides:
        if not odds or odds < MIN_ODDS:
            continue
        edge = prob - implied_probability(odds)
        if edge >= MIN_EDGE and _prob_is_plausible(prob) and passes_accuracy_bar(prob, odds):
            candidates.append({
                "type": "moneyline", "league": LEAGUE, "team": team, "opponent": opponent,
                "side": side, "odds": odds, "our_estimated_prob": round(prob, 3),
                "market_implied_prob": round(implied_probability(odds), 3), "edge": round(edge, 3),
            })
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c["our_estimated_prob"], c["edge"]))


def model_total(home_form, away_form):
    """Our own predicted combined score (before anchoring to the market)."""
    home = (home_form["avg_points_scored"] + away_form["avg_points_allowed"]) / 2
    away = (away_form["avg_points_scored"] + home_form["avg_points_allowed"]) / 2
    if _is_back_to_back(home_form):
        home -= FATIGUE_TOTAL_PENALTY
    if _is_back_to_back(away_form):
        away -= FATIGUE_TOTAL_PENALTY
    return home + away


def pick_main_line(totals_odds):
    """The 'main' total is the line whose over and under prices are closest to even."""
    usable = [t for t in totals_odds
              if t.get("line") is not None and t.get("over_odds") and t.get("under_odds")
              and MIN_PLAUSIBLE_TOTAL <= t["line"] <= MAX_PLAUSIBLE_TOTAL]
    if not usable:
        return None
    best = min(usable, key=lambda t: abs(1 / t["over_odds"] - 1 / t["under_odds"]))
    return best["line"]


def anchored_total(own_total, main_line):
    """Blend of the market's main line and our own number. No market -> our own."""
    if main_line is None:
        return own_total
    return TOTAL_MARKET_WEIGHT * main_line + (1 - TOTAL_MARKET_WEIGHT) * own_total


def find_totals_tip(game, home_form, away_form, totals_odds):
    own = model_total(home_form, away_form)
    main_line = pick_main_line(totals_odds)
    if main_line is None:
        return None
    predicted = anchored_total(own, main_line)
    matchup = f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}"
    candidates = _find_totals_candidates(
        matchup, "totals", predicted, totals_odds, std_dev=TOTAL_STD, min_edge=MIN_EDGE,
        line_range=(MIN_PLAUSIBLE_TOTAL, MAX_PLAUSIBLE_TOTAL),
        extra_fields={"league": LEAGUE, "model_total": round(own, 1),
                      "anchored_total": round(predicted, 1), "main_line": main_line},
    )
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c["our_estimated_prob"], c["edge"]))


def build_prediction(game, home_form, away_form, odds=None, totals_odds=None):
    """One plain dict per game: the model's view, used for the daily digest and the logs."""
    odds = odds or {}
    p_home, model_p, market_p, margin = blended_home_prob(
        home_form, away_form, odds.get("home_odds"), odds.get("away_odds"))
    own = model_total(home_form, away_form)
    main_line = pick_main_line(totals_odds or [])
    home_name, away_name = game["home_team"]["full_name"], game["visitor_team"]["full_name"]
    favorite_is_home = p_home >= 0.5
    return {
        "matchup": f"{away_name} @ {home_name}",
        "home": home_name, "away": away_name,
        "favorite": home_name if favorite_is_home else away_name,
        "favorite_prob": round(p_home if favorite_is_home else 1 - p_home, 3),
        "model_only_home_prob": round(model_p, 3),
        "market_home_prob": None if market_p is None else round(market_p, 3),
        "predicted_margin": round(margin, 1),
        "model_total": round(own, 1),
        "market_main_line": main_line,
        "anchored_total": round(anchored_total(own, main_line), 1),
        "used_market": market_p is not None,
    }


MAX_MESSAGE_CHARS = 3500  # Telegram's hard limit is 4096; leave headroom


def format_digest(predictions, date_str):
    """Returns a list of message strings (usually one) with plain text only."""
    header = (f"NBA model view for {date_str}\n"
              "Predictions only, not betting advice. Value tips (if any) come as separate messages.\n")
    blocks = []
    for p in predictions:
        lean = (p["favorite_prob"] * 100)
        line = (f"\n{p['matchup']}\n"
                f"  Lean: {p['favorite']} {lean:.0f}% (model margin {p['predicted_margin']:+.1f} for home)\n")
        if p["market_main_line"] is not None:
            line += f"  Total: model {p['model_total']} vs market {p['market_main_line']}\n"
        else:
            line += f"  Total: model {p['model_total']} (no market line found)\n"
        blocks.append(line)

    messages, current = [], header
    for block in blocks:
        if len(current) + len(block) > MAX_MESSAGE_CHARS:
            messages.append(current)
            current = "NBA model view (continued)\n"
        current += block
    messages.append(current)
    return messages
