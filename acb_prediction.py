"""
Produces ACB predictions with NO odds and NO edge concept at all — a
deliberate, different kind of output from every other sport in this
project. WNBA/NCAAB/tennis all compare the model's estimate against a
real market price and only speak up when there's a mispricing (an
"edge"). ACB has no real odds source available at all (see
acb_stats_fetcher.py's docstring for why — Liga ACB's data is
exclusively licensed to Genius Sports, so it isn't in the free odds
ecosystem), so there is nothing to compare against here. Instead, this
reports the model's own read — a predicted winner with a confidence
percentage, a predicted total, and a predicted half split — gated ONLY
by how confident the model is, with the honest expectation that the
person checks whatever number comes out here against a real sportsbook
line themselves before deciding anything.

Deliberately kept SEPARATE from analysis.py rather than folded in,
so nothing here can accidentally get mixed up with an actual value
tip from a sport where a real market edge was found — every function
here returns a "prediction" dict, never a "tip" dict, and main.py's ACB
output is labeled accordingly (see main.py's run_acb()).

Reuses analysis.py's core model (estimate_win_probability,
predicted_total) rather than re-deriving win/total math from scratch —
same underlying logic that already works for WNBA/NCAAB, just without
the market-comparison step layered on top.
"""

from config import MIN_WIN_PROB
from analysis import estimate_win_probability, predicted_total


def predict_moneyline(game, home_form, away_form, h2h=None):
    """
    Returns a prediction dict if the model is confident enough
    (>= MIN_WIN_PROB — same accuracy bar used everywhere else in this
    project), or None if it isn't. No market/edge concept: confidence
    alone decides whether this is worth showing.
    """
    prob_home, prob_away = estimate_win_probability(home_form, away_form, h2h=h2h)

    if prob_home >= prob_away:
        team, opponent, prob = game["home_team"]["full_name"], game["visitor_team"]["full_name"], prob_home
    else:
        team, opponent, prob = game["visitor_team"]["full_name"], game["home_team"]["full_name"], prob_away

    if prob < MIN_WIN_PROB:
        return None

    return {
        "type": "moneyline_prediction",
        "matchup": f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}",
        "predicted_winner": team,
        "opponent": opponent,
        "confidence": round(prob, 3),
    }


def predict_total(game, home_form, away_form, std_dev=13.0):
    """
    Reports the model's predicted combined score as a number and a
    rough likely range (± 1 std dev), for the person to compare against
    whatever total line a sportsbook is actually offering. No over/
    under call is made here — there's no line to call over or under
    without real odds, so making one up would just be a coin flip
    dressed up as a prediction. std_dev default matches WNBA/NCAAB's
    TOTAL_POINTS_STD_DEV (config.py) — ACB-specific variance hasn't
    been measured yet, so this borrows the closest available estimate
    rather than inventing a number with no basis at all.
    """
    predicted = predicted_total(home_form, away_form)
    return {
        "type": "total_prediction",
        "matchup": f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}",
        "predicted_total": round(predicted, 1),
        "likely_range": (round(predicted - std_dev, 1), round(predicted + std_dev, 1)),
    }


def predict_half(game, home_form, away_form, std_dev=8.0):
    """
    Predicted combined score for EACH half, using each team's own REAL
    first-half/second-half scoring split from acb_stats_fetcher.py's
    quarter-by-quarter data — not the flat 50% assumption
    analysis.predicted_half_total() has to fall back to for WNBA/NCAAB
    (those providers have no period-by-period history to learn a real
    split from; ACB's does). Returns None if either team is missing the
    quarter-derived averages (e.g. a game record had incomplete score
    data) rather than silently falling back to a flat guess — if the
    real split isn't available, saying so is more honest than quietly
    downgrading to the same assumption this was built to avoid.
    std_dev is a rough estimate (roughly half the full-game std_dev,
    since a half is a smaller, higher-variance sample) — unmeasured for
    ACB specifically, same honest caveat as predict_total's.
    """
    required = (
        "avg_first_half_points_scored", "avg_first_half_points_allowed",
        "avg_second_half_points_scored", "avg_second_half_points_allowed",
    )
    if any(home_form.get(f) is None or away_form.get(f) is None for f in required):
        return None

    predicted_first_half = (
        (home_form["avg_first_half_points_scored"] + away_form["avg_first_half_points_allowed"]) / 2
        + (away_form["avg_first_half_points_scored"] + home_form["avg_first_half_points_allowed"]) / 2
    )
    predicted_second_half = (
        (home_form["avg_second_half_points_scored"] + away_form["avg_second_half_points_allowed"]) / 2
        + (away_form["avg_second_half_points_scored"] + home_form["avg_second_half_points_allowed"]) / 2
    )

    return {
        "type": "half_prediction",
        "matchup": f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}",
        "predicted_first_half_total": round(predicted_first_half, 1),
        "predicted_first_half_range": (round(predicted_first_half - std_dev, 1), round(predicted_first_half + std_dev, 1)),
        "predicted_second_half_total": round(predicted_second_half, 1),
        "predicted_second_half_range": (round(predicted_second_half - std_dev, 1), round(predicted_second_half + std_dev, 1)),
    }
