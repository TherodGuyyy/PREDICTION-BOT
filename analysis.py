"""
Turns two teams' recent form into a win-probability estimate, then checks
whether the bookmaker's odds imply a LOWER probability than our estimate
(i.e. the odds are generous relative to the team's actual form) — that gap
is what makes something a "tip" rather than just picking the favorite.

This is a starting model, not a finished one. It's intentionally simple so
you can see exactly why it makes each call, and so we can improve it piece
by piece (e.g. adding head-to-head record, rest days, injuries) once it's
live and you can watch how it performs.
"""

import math
from config import MIN_ODDS, MIN_EDGE, TOTAL_POINTS_STD_DEV

HOME_ADVANTAGE = 0.06  # flat bump in win probability for the home team


def estimate_win_probability(home_form, away_form):
    """
    home_form / away_form: dicts from stats_fetcher.team_form_summary()
    Returns (prob_home_wins, prob_away_wins) — two floats that sum to 1.0.
    """
    # Difference in recent win% and point differential, weighted.
    # These weights are a reasonable starting point, not tuned on real
    # results yet — worth revisiting once you have a few weeks of tips
    # logged against actual outcomes.
    win_pct_diff = home_form["win_pct"] - away_form["win_pct"]
    point_diff_diff = home_form["avg_point_diff"] - away_form["avg_point_diff"]

    score = (win_pct_diff * 1.2) + (point_diff_diff * 0.03) + HOME_ADVANTAGE

    # logistic squash into a 0-1 probability
    prob_home = 1 / (1 + math.exp(-score * 3))
    prob_away = 1 - prob_home
    return prob_home, prob_away


def implied_probability(decimal_odds):
    """Converts decimal odds (e.g. 1.85) to the probability the market implies."""
    return 1 / decimal_odds


def find_value_tip(game, home_form, away_form, home_odds, away_odds):
    """
    game: the raw game dict from balldontlie (for team names / matchup label)
    home_odds / away_odds: decimal odds for each side, from your odds feed
    Returns a tip dict if either side clears MIN_ODDS + MIN_EDGE, else None.
    If BOTH sides clear it (rare), returns the one with the bigger edge.
    """
    prob_home, prob_away = estimate_win_probability(home_form, away_form)

    candidates = []

    if home_odds and home_odds >= MIN_ODDS:
        edge = prob_home - implied_probability(home_odds)
        if edge >= MIN_EDGE:
            candidates.append({
                "type": "moneyline",
                "team": game["home_team"]["full_name"],
                "opponent": game["visitor_team"]["full_name"],
                "side": "home",
                "odds": home_odds,
                "our_estimated_prob": round(prob_home, 3),
                "market_implied_prob": round(implied_probability(home_odds), 3),
                "edge": round(edge, 3),
            })

    if away_odds and away_odds >= MIN_ODDS:
        edge = prob_away - implied_probability(away_odds)
        if edge >= MIN_EDGE:
            candidates.append({
                "type": "moneyline",
                "team": game["visitor_team"]["full_name"],
                "opponent": game["home_team"]["full_name"],
                "side": "away",
                "odds": away_odds,
                "our_estimated_prob": round(prob_away, 3),
                "market_implied_prob": round(implied_probability(away_odds), 3),
                "edge": round(edge, 3),
            })

    if not candidates:
        return None

    # if both sides somehow clear the bar, take the bigger edge
    return max(candidates, key=lambda c: c["edge"])


# ---------------------------------------------------------------------------
# Totals (over/under)
# ---------------------------------------------------------------------------
# Moneyline predicts WHO wins. Totals predicts a NUMBER (combined score) and
# checks whether that's likely to land above or below whatever line a
# bookmaker is offering. That's a genuinely different kind of prediction —
# instead of a single win probability, we estimate a probability CURVE
# around our predicted total, using a normal distribution. This is a
# standard, well-established approach for totals modeling, but the specific
# spread we're assuming (TOTAL_POINTS_STD_DEV) is a reasonable starting
# estimate, not something derived from your actual league data yet — worth
# revisiting once you've logged real totals outcomes vs. predictions,
# same as the moneyline model.


def predicted_total(home_form, away_form):
    """
    Simple total-points estimate: average of (home's scoring tendency +
    away's defensive tendency) and (away's scoring tendency + home's
    defensive tendency). This accounts for both teams' offense AND
    defense, not just one side's average.
    """
    home_side = (home_form["avg_points_scored"] + away_form["avg_points_allowed"]) / 2
    away_side = (away_form["avg_points_scored"] + home_form["avg_points_allowed"]) / 2
    return home_side + away_side


def _normal_cdf(x, mean, std_dev):
    """Standard normal CDF — P(X <= x) for X ~ Normal(mean, std_dev)."""
    return 0.5 * (1 + math.erf((x - mean) / (std_dev * math.sqrt(2))))


def prob_over_under(predicted, line):
    """
    Returns (prob_over, prob_under) for a given predicted total and a
    specific betting line, using a normal distribution around our
    prediction. E.g. if we predict 168 points and the line is 165.5,
    "over" should be somewhat more likely than "under".
    """
    prob_under = _normal_cdf(line, predicted, TOTAL_POINTS_STD_DEV)
    prob_over = 1 - prob_under
    return prob_over, prob_under


def find_totals_value_tip(game, predicted, totals_odds_list):
    """
    game: raw game dict from balldontlie (for the matchup label)
    predicted: our predicted combined total (from predicted_total())
    totals_odds_list: list of {"line", "over_odds", "under_odds"} from
                       odds_fetcher.get_totals_odds()
    Returns the single best-edge tip across all available lines/sides
    that clears MIN_ODDS + MIN_EDGE, or None.
    """
    candidates = []
    matchup = f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}"

    for entry in totals_odds_list:
        line = entry["line"]
        if line is None:
            continue
        prob_over, prob_under = prob_over_under(predicted, line)

        over_odds = entry.get("over_odds")
        if over_odds and over_odds >= MIN_ODDS:
            edge = prob_over - implied_probability(over_odds)
            if edge >= MIN_EDGE:
                candidates.append({
                    "type": "totals",
                    "matchup": matchup,
                    "side": "over",
                    "line": line,
                    "odds": over_odds,
                    "our_estimated_prob": round(prob_over, 3),
                    "market_implied_prob": round(implied_probability(over_odds), 3),
                    "edge": round(edge, 3),
                })

        under_odds = entry.get("under_odds")
        if under_odds and under_odds >= MIN_ODDS:
            edge = prob_under - implied_probability(under_odds)
            if edge >= MIN_EDGE:
                candidates.append({
                    "type": "totals",
                    "matchup": matchup,
                    "side": "under",
                    "line": line,
                    "odds": under_odds,
                    "our_estimated_prob": round(prob_under, 3),
                    "market_implied_prob": round(implied_probability(under_odds), 3),
                    "edge": round(edge, 3),
                })

    if not candidates:
        return None

    return max(candidates, key=lambda c: c["edge"])
