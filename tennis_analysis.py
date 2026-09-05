"""
Turns two tennis players' ranking, recent form, and surface-specific form
into a win-probability estimate, then applies the same value-tip logic as
the basketball bot: only tip when the odds imply a lower probability than
our estimate, by at least MIN_EDGE, at 1.40+ odds.

Same "starting model, not finished" philosophy as analysis.py — ranking
is the primary signal (a reasonable default: better-ranked players win
more often), with recent overall form and surface-specific form as
adjustments on top. The weights below are a reasonable starting point,
not tuned on your actual results yet.
"""

import math
from config import MIN_ODDS, MIN_EDGE, MAX_PLAUSIBLE_PROB

# how much each factor moves the estimate — surface weighted slightly
# higher since that was specifically asked for, rank is the anchor
W_RANK = 0.65
W_SURFACE = 1.5
W_FORM = 1.0
W_H2H = 0.4  # same weight as the WNBA model's H2H_WEIGHT, for consistency —
             # not independently tuned yet, worth revisiting once there's
             # enough logged tennis tip history to calibrate against


def _h2h_score_component(h2h):
    """
    h2h: dict from tennis_stats_fetcher.get_head_to_head(player_a_name,
    player_b_name, tour), oriented so player_a is player A here. Returns
    0.0 if there's no h2h data or too small a sample (get_head_to_head
    already enforces the minimum via TENNIS_H2H_MIN_MATCHUPS, but this
    stays defensive since callers may pass None).
    """
    if not h2h:
        return 0.0
    # center on 0.5 so a 50/50 head-to-head record contributes nothing
    return (h2h["player_a_win_pct"] - 0.5) * W_H2H


def estimate_win_probability(player_a_form, player_b_form, h2h=None):
    """
    player_a_form / player_b_form: dicts from
    tennis_stats_fetcher.player_form_summary()
    h2h: optional dict from tennis_stats_fetcher.get_head_to_head(
         player_a_name, player_b_name, tour) — pass None if unavailable
         or under the minimum sample (TENNIS_H2H_MIN_MATCHUPS).
    Returns (prob_a_wins, prob_b_wins).
    """
    # rank_score: positive favors player A. Using log of rank because
    # rank differences matter more at the top (1 vs 5 is a bigger gap
    # than 101 vs 105) than a raw rank subtraction would capture.
    rank_score = math.log(player_b_form["current_rank"]) - math.log(player_a_form["current_rank"])

    surface_diff = player_a_form["surface_win_pct"] - player_b_form["surface_win_pct"]
    form_diff = player_a_form["overall_win_pct"] - player_b_form["overall_win_pct"]

    score = (
        (rank_score * W_RANK)
        + (surface_diff * W_SURFACE)
        + (form_diff * W_FORM)
        + _h2h_score_component(h2h)
    )

    prob_a = 1 / (1 + math.exp(-score))
    prob_b = 1 - prob_a
    return prob_a, prob_b


def implied_probability(decimal_odds):
    return 1 / decimal_odds


def find_tennis_value_tip(player_a_name, player_b_name, player_a_form, player_b_form,
                            player_a_odds, player_b_odds, h2h=None):
    """
    Returns a tip dict (type='tennis') if either player clears MIN_ODDS +
    MIN_EDGE, or None. If both somehow clear it, returns the bigger edge.
    """
    prob_a, prob_b = estimate_win_probability(player_a_form, player_b_form, h2h)

    candidates = []

    if player_a_odds and player_a_odds >= MIN_ODDS:
        edge = prob_a - implied_probability(player_a_odds)
        if edge >= MIN_EDGE and prob_a <= MAX_PLAUSIBLE_PROB:
            candidates.append({
                "type": "tennis",
                "player": player_a_name,
                "opponent": player_b_name,
                "odds": player_a_odds,
                "our_estimated_prob": round(prob_a, 3),
                "market_implied_prob": round(implied_probability(player_a_odds), 3),
                "edge": round(edge, 3),
            })

    if player_b_odds and player_b_odds >= MIN_ODDS:
        edge = prob_b - implied_probability(player_b_odds)
        if edge >= MIN_EDGE and prob_b <= MAX_PLAUSIBLE_PROB:
            candidates.append({
                "type": "tennis",
                "player": player_b_name,
                "opponent": player_a_name,
                "odds": player_b_odds,
                "our_estimated_prob": round(prob_b, 3),
                "market_implied_prob": round(implied_probability(player_b_odds), 3),
                "edge": round(edge, 3),
            })

    if not candidates:
        return None

    return max(candidates, key=lambda c: c["edge"])
