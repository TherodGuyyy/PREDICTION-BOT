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
from config import (
    MIN_ODDS, MIN_EDGE, MIN_EDGE_SUBMARKET, TOTAL_POINTS_STD_DEV,
    MIN_PLAUSIBLE_TOTAL, MAX_PLAUSIBLE_TOTAL, MAX_PLAUSIBLE_PROB,
    HALF_TOTAL_PROPORTION, QUARTER_TOTAL_PROPORTION,
)

HOME_ADVANTAGE = 0.06  # flat bump in win probability for the home team

# Rest-day adjustment. WNBA has a much tighter schedule than NBA (no
# rest-friendly scheduling), so back-to-backs are a real signal. This
# compares the two teams' days_rest and nudges the score toward whichever
# team is fresher, capped so a huge rest gap (e.g. one team just came off
# an All-Star break) doesn't dominate the whole estimate.
REST_WEIGHT = 0.02   # probability-score nudge per day of rest advantage
REST_DIFF_CAP = 3    # cap the rest-day difference considered, in days

# Head-to-head adjustment. Only applied when there's at least 2 matchups
# on record (see get_head_to_head_record) — a single game is noise, not
# signal. Weighted lightly relative to recent form and rank, since two
# teams' overall form already captures most of what h2h would add, and
# h2h samples are small by nature.
H2H_WEIGHT = 0.4

# Fatigue adjustment on predicted totals. A team on zero days rest tends
# to shoot/defend slightly worse than a normally-rested team — small,
# not dramatic. Applied per team, so two exhausted teams facing each
# other get a bigger combined knock than one fresh team vs one tired one.
FATIGUE_TOTAL_PENALTY = 2.0  # points shaved off the total per team on 0 days rest


def _prob_is_plausible(prob):
    """
    Safety net: an estimated probability this extreme almost always means
    something is mismatched (wrong market, stale odds, a fixture that
    doesn't really correspond to the odds data) rather than genuine model
    confidence. See MAX_PLAUSIBLE_PROB in config.py for the full reasoning.
    """
    return prob <= MAX_PLAUSIBLE_PROB


def _rest_score_component(home_form, away_form):
    """
    Returns a small score nudge favoring whichever team has more rest.
    Returns 0.0 if either team's rest is unknown (get_days_rest can
    return None) rather than guessing — an unknown shouldn't silently
    become "assume fully rested".
    """
    home_rest = home_form.get("days_rest")
    away_rest = away_form.get("days_rest")
    if home_rest is None or away_rest is None:
        return 0.0

    diff = home_rest - away_rest
    diff = max(-REST_DIFF_CAP, min(REST_DIFF_CAP, diff))
    return diff * REST_WEIGHT


def _h2h_score_component(h2h):
    """
    h2h: dict from stats_fetcher.get_head_to_head_record(home_id, away_id),
    oriented so team_a == home team. Returns 0.0 if there's no h2h data
    or too small a sample (get_head_to_head_record already enforces the
    minimum, but this stays defensive since callers may pass None).
    """
    if not h2h:
        return 0.0
    # center on 0.5 so a 50/50 h2h record contributes nothing
    return (h2h["team_a_win_pct"] - 0.5) * H2H_WEIGHT


def estimate_win_probability(home_form, away_form, h2h=None):
    """
    home_form / away_form: dicts from stats_fetcher.team_form_summary()
    h2h: optional dict from stats_fetcher.get_head_to_head_record(home_id,
         away_id) — pass None if unavailable or under the minimum sample.
    Returns (prob_home_wins, prob_away_wins) — two floats that sum to 1.0.
    """
    # Difference in recent win% and point differential, weighted.
    # These weights are a reasonable starting point, not tuned on real
    # results yet — worth revisiting once you have a few weeks of tips
    # logged against actual outcomes.
    win_pct_diff = home_form["win_pct"] - away_form["win_pct"]
    point_diff_diff = home_form["avg_point_diff"] - away_form["avg_point_diff"]

    score = (
        (win_pct_diff * 1.2)
        + (point_diff_diff * 0.03)
        + HOME_ADVANTAGE
        + _rest_score_component(home_form, away_form)
        + _h2h_score_component(h2h)
    )

    # logistic squash into a 0-1 probability
    prob_home = 1 / (1 + math.exp(-score * 3))
    prob_away = 1 - prob_home
    return prob_home, prob_away


def implied_probability(decimal_odds):
    """Converts decimal odds (e.g. 1.85) to the probability the market implies."""
    return 1 / decimal_odds


def find_value_tip(game, home_form, away_form, home_odds, away_odds, h2h=None):
    """
    game: the raw game dict from balldontlie (for team names / matchup label)
    home_odds / away_odds: decimal odds for each side, from your odds feed
    h2h: optional dict from stats_fetcher.get_head_to_head_record(home_id, away_id)
    Returns a tip dict if either side clears MIN_ODDS + MIN_EDGE, else None.
    If BOTH sides clear it (rare), returns the one with the bigger edge.
    """
    prob_home, prob_away = estimate_win_probability(home_form, away_form, h2h=h2h)

    candidates = []

    if home_odds and home_odds >= MIN_ODDS:
        edge = prob_home - implied_probability(home_odds)
        if edge >= MIN_EDGE and _prob_is_plausible(prob_home):
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
        if edge >= MIN_EDGE and _prob_is_plausible(prob_away):
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


def _fatigue_score_penalty(form):
    """
    Points shaved off THIS team's own predicted score if they're playing
    on zero days rest. Returns 0.0 if rest is unknown — same
    "unknown isn't assumed rested" rule used elsewhere.
    """
    return FATIGUE_TOTAL_PENALTY if form.get("days_rest") == 0 else 0.0


def _predicted_scores_basic(home_form, away_form):
    """
    Original totals estimate, used as a fallback when pace data isn't
    available for one or both teams (e.g. the /team_stats endpoint isn't
    on your balldontlie tier — see get_team_pace()'s docstring). Averages
    each team's scoring tendency against the opponent's defensive
    tendency. Returns (home_score, away_score).
    """
    home_score = (home_form["avg_points_scored"] + away_form["avg_points_allowed"]) / 2
    away_score = (away_form["avg_points_scored"] + home_form["avg_points_allowed"]) / 2
    return home_score, away_score


def _predicted_scores_pace_based(home_form, away_form):
    """
    Pace-adjusted per-team score estimate. Two teams that both average 80
    points could get there via very different possession counts — the
    basic formula above can't tell those apart, so a team's pace shift
    (new rotation, an injury to a ball-handler) won't show up until raw
    scoring averages drift, which lags real changes.

    This separates SCORING RATE (points per 100 possessions) from PACE
    (possessions per game), estimates the pace this specific matchup will
    likely play at (average of both teams' pace), and multiplies rate by
    that shared pace — the standard way pace-adjusted projections are
    done. Requires pace for both teams; predicted_scores() falls back to
    _predicted_scores_basic otherwise. Returns (home_score, away_score).
    """
    home_pace = home_form["pace"]
    away_pace = away_form["pace"]
    game_pace = (home_pace + away_pace) / 2

    home_off_rating = (home_form["avg_points_scored"] / home_pace) * 100
    home_def_rating = (home_form["avg_points_allowed"] / home_pace) * 100
    away_off_rating = (away_form["avg_points_scored"] / away_pace) * 100
    away_def_rating = (away_form["avg_points_allowed"] / away_pace) * 100

    expected_home_score = ((home_off_rating + away_def_rating) / 2) * (game_pace / 100)
    expected_away_score = ((away_off_rating + home_def_rating) / 2) * (game_pace / 100)

    return expected_home_score, expected_away_score


def predicted_scores(home_form, away_form):
    """
    Predicted (home_score, away_score) for the full game — the shared
    building block behind the game total, individual team totals, and
    (via a flat proportion) half/quarter totals. Uses the pace-adjusted
    estimate when both teams have usable pace data, otherwise the basic
    scoring-average estimate. Applies each team's own fatigue penalty to
    ITS OWN score (a back-to-back team's own total drops — this doesn't
    get arbitrarily split across both teams the way a single combined
    penalty would).
    """
    home_pace = home_form.get("pace")
    away_pace = away_form.get("pace")

    if home_pace and away_pace:
        home_score, away_score = _predicted_scores_pace_based(home_form, away_form)
    else:
        home_score, away_score = _predicted_scores_basic(home_form, away_form)

    home_score -= _fatigue_score_penalty(home_form)
    away_score -= _fatigue_score_penalty(away_form)
    return home_score, away_score


def predicted_total(home_form, away_form):
    """Predicted COMBINED score for the game — sum of predicted_scores()."""
    home_score, away_score = predicted_scores(home_form, away_form)
    return home_score + away_score


def predicted_half_total(home_form, away_form, proportion=HALF_TOTAL_PROPORTION):
    """
    Predicted combined score for the first half — a flat proportion
    (default 50%) of the full-game predicted total. This is a
    simplification, not a half-specific model: balldontlie's free tier
    has no period-by-period scoring history to learn a real split from.
    Treat this as meaningfully less reliable than the full-game total.
    """
    return predicted_total(home_form, away_form) * proportion


def predicted_quarter_total(home_form, away_form, proportion=QUARTER_TOTAL_PROPORTION):
    """
    Predicted combined score for a single quarter — a flat proportion
    (default 25%) of the full-game predicted total. Same caveat as
    predicted_half_total, more so: a single quarter is a smaller, higher-
    variance sample, so this is the least reliable of the totals
    estimates here. Its stricter MIN_EDGE_SUBMARKET threshold reflects
    that.
    """
    return predicted_total(home_form, away_form) * proportion


def _normal_cdf(x, mean, std_dev):
    """Standard normal CDF — P(X <= x) for X ~ Normal(mean, std_dev)."""
    return 0.5 * (1 + math.erf((x - mean) / (std_dev * math.sqrt(2))))


def prob_over_under(predicted, line, std_dev=TOTAL_POINTS_STD_DEV):
    """
    Returns (prob_over, prob_under) for a given predicted total and a
    specific betting line, using a normal distribution around our
    prediction. E.g. if we predict 168 points and the line is 165.5,
    "over" should be somewhat more likely than "under".

    std_dev defaults to the full-game TOTAL_POINTS_STD_DEV, but callers
    predicting a smaller window (a half, a quarter, one team's score)
    should pass a smaller std_dev — see HALF_STD_DEV / QUARTER_STD_DEV /
    TEAM_STD_DEV below for how those are derived.
    """
    prob_under = _normal_cdf(line, predicted, std_dev)
    prob_over = 1 - prob_under
    return prob_over, prob_under


# Std devs for sub-markets, derived from TOTAL_POINTS_STD_DEV rather than
# guessed independently, since we don't have real half/quarter/team-level
# variance data (same limitation as the proportion-based predictions
# above). Basketball scoring is reasonably modeled as a compound process
# where variance scales roughly with time/possessions played — so a
# window covering HALF_TOTAL_PROPORTION of the game gets sqrt(proportion)
# of the full-game variance, not a flat proportional cut. This is a
# standard approximation (used e.g. in basketball live-total modeling),
# not something tuned on your actual results yet.
HALF_STD_DEV = TOTAL_POINTS_STD_DEV * math.sqrt(HALF_TOTAL_PROPORTION)
QUARTER_STD_DEV = TOTAL_POINTS_STD_DEV * math.sqrt(QUARTER_TOTAL_PROPORTION)
# One team's score variance, assuming both teams contribute roughly equal
# variance to the combined total: Var(total) = Var(home) + Var(away), so
# each team's std ≈ total_std / sqrt(2).
TEAM_STD_DEV = TOTAL_POINTS_STD_DEV / math.sqrt(2)


def _find_totals_candidates(matchup_label, tip_type, predicted, totals_odds_list, std_dev, min_edge,
                             line_range=None, extra_fields=None):
    """
    Shared core for every totals-style market (full-game, team, half,
    quarter): given a predicted number and a list of {"line", "over_odds",
    "under_odds"} entries, returns every candidate tip that clears
    min_edge, tagged with tip_type. line_range, if given, is an
    (min, max) sanity bound — same safety-net philosophy as
    MIN_PLAUSIBLE_TOTAL/MAX_PLAUSIBLE_TOTAL: if a line falls way outside
    what's plausible for this market type, it's almost certainly a
    mismatched market that slipped through name-based filtering upstream,
    and gets skipped rather than ever risking a nonsensical tip.
    """
    candidates = []
    extra_fields = extra_fields or {}

    for entry in totals_odds_list:
        line = entry.get("line")
        if line is None:
            continue
        if line_range and not (line_range[0] <= line <= line_range[1]):
            continue

        prob_over, prob_under = prob_over_under(predicted, line, std_dev=std_dev)

        over_odds = entry.get("over_odds")
        if over_odds and over_odds >= MIN_ODDS:
            edge = prob_over - implied_probability(over_odds)
            if edge >= min_edge and _prob_is_plausible(prob_over):
                candidates.append({
                    "type": tip_type,
                    "matchup": matchup_label,
                    "side": "over",
                    "line": line,
                    "odds": over_odds,
                    "market_id": entry.get("market_id"),
                    "our_estimated_prob": round(prob_over, 3),
                    "market_implied_prob": round(implied_probability(over_odds), 3),
                    "edge": round(edge, 3),
                    **extra_fields,
                })

        under_odds = entry.get("under_odds")
        if under_odds and under_odds >= MIN_ODDS:
            edge = prob_under - implied_probability(under_odds)
            if edge >= min_edge and _prob_is_plausible(prob_under):
                candidates.append({
                    "type": tip_type,
                    "matchup": matchup_label,
                    "side": "under",
                    "line": line,
                    "odds": under_odds,
                    "market_id": entry.get("market_id"),
                    "our_estimated_prob": round(prob_under, 3),
                    "market_implied_prob": round(implied_probability(under_odds), 3),
                    "edge": round(edge, 3),
                    **extra_fields,
                })

    return candidates


def find_totals_value_tip(game, predicted, totals_odds_list):
    """
    game: raw game dict from balldontlie (for the matchup label)
    predicted: our predicted combined total (from predicted_total())
    totals_odds_list: list of {"line", "over_odds", "under_odds"} from
                       odds_fetcher.get_totals_odds()
    Returns the single best-edge tip across all available lines/sides
    that clears MIN_ODDS + MIN_EDGE, or None.
    """
    matchup = f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}"
    candidates = _find_totals_candidates(
        matchup, "totals", predicted, totals_odds_list,
        std_dev=TOTAL_POINTS_STD_DEV, min_edge=MIN_EDGE,
        line_range=(MIN_PLAUSIBLE_TOTAL, MAX_PLAUSIBLE_TOTAL),
    )
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["edge"])


# ---------------------------------------------------------------------------
# Expanded totals sub-markets: individual team totals, first-half totals,
# quarter totals. See config.py's ENABLE_TEAM_TOTALS / ENABLE_HALF_TOTALS /
# ENABLE_QUARTER_TOTALS comments for what's genuinely modeled here vs. what's
# a flat-proportion simplification, and MIN_EDGE_SUBMARKET for why half/
# quarter tips require a bigger edge than the main markets.
# ---------------------------------------------------------------------------

def find_team_totals_value_tip(game, home_form, away_form, team_totals_odds):
    """
    team_totals_odds: dict from odds_fetcher.get_team_totals_odds():
        {"home": [{"line", "over_odds", "under_odds"}, ...],
         "away": [{"line", "over_odds", "under_odds"}, ...]}
    Uses predicted_scores() — the SAME per-team numbers behind the main
    game total, no proportion guessing involved — so this gets the
    normal MIN_EDGE, not the stricter sub-market threshold.
    Returns the single best-edge tip across both teams' lines, or None.
    """
    home_score, away_score = predicted_scores(home_form, away_form)
    home_name = game["home_team"]["full_name"]
    away_name = game["visitor_team"]["full_name"]

    candidates = []
    candidates += _find_totals_candidates(
        home_name, "team_totals", home_score, team_totals_odds.get("home", []),
        std_dev=TEAM_STD_DEV, min_edge=MIN_EDGE,
        extra_fields={"team": home_name, "opponent": away_name},
    )
    candidates += _find_totals_candidates(
        away_name, "team_totals", away_score, team_totals_odds.get("away", []),
        std_dev=TEAM_STD_DEV, min_edge=MIN_EDGE,
        extra_fields={"team": away_name, "opponent": home_name},
    )

    if not candidates:
        return None
    return max(candidates, key=lambda c: c["edge"])


def find_half_totals_value_tip(game, home_form, away_form, half_totals_odds):
    """
    half_totals_odds: list of {"line", "over_odds", "under_odds"} for the
    FIRST HALF, from odds_fetcher.get_first_half_totals_odds().
    predicted is a flat proportion of the full-game total (see
    predicted_half_total's docstring) — uses MIN_EDGE_SUBMARKET, the
    stricter threshold, to reflect that extra uncertainty.
    """
    matchup = f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']}"
    predicted = predicted_half_total(home_form, away_form)
    candidates = _find_totals_candidates(
        matchup, "half_totals", predicted, half_totals_odds,
        std_dev=HALF_STD_DEV, min_edge=MIN_EDGE_SUBMARKET,
    )
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["edge"])


def find_quarter_totals_value_tip(game, home_form, away_form, quarter_totals_odds, quarter_label="1st Quarter"):
    """
    quarter_totals_odds: list of {"line", "over_odds", "under_odds"} for a
    single quarter, from odds_fetcher.get_quarter_totals_odds().
    Same flat-proportion caveat as half totals, more so (smaller, higher-
    variance sample) — uses MIN_EDGE_SUBMARKET.
    """
    matchup = f"{game['visitor_team']['full_name']} @ {game['home_team']['full_name']} ({quarter_label})"
    predicted = predicted_quarter_total(home_form, away_form)
    candidates = _find_totals_candidates(
        matchup, "quarter_totals", predicted, quarter_totals_odds,
        std_dev=QUARTER_STD_DEV, min_edge=MIN_EDGE_SUBMARKET,
    )
    if not candidates:
        return None
    return max(candidates, key=lambda c: c["edge"])
