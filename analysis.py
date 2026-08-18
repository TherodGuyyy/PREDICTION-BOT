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
from config import MIN_ODDS, MIN_EDGE, TOTAL_POINTS_STD_DEV, MIN_PLAUSIBLE_TOTAL, MAX_PLAUSIBLE_TOTAL, MAX_PLAUSIBLE_PROB

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


def _fatigue_total_adjustment(home_form, away_form):
    """
    Small downward nudge on the predicted total for each team playing on
    zero days rest (back-to-back) — tired teams shoot and defend
    marginally worse. Returns 0.0 for a team whose rest is unknown,
    same "unknown isn't assumed rested" rule as the win-probability side.
    """
    penalty = 0.0
    for form in (home_form, away_form):
        if form.get("days_rest") == 0:
            penalty += FATIGUE_TOTAL_PENALTY
    return penalty


def _predicted_total_basic(home_form, away_form):
    """
    Original totals estimate, used as a fallback when pace data isn't
    available for one or both teams (e.g. the /stats endpoint isn't on
    your balldontlie tier — see get_team_pace()'s docstring). Averages
    scoring tendency against the opponent's defensive tendency.
    """
    home_side = (home_form["avg_points_scored"] + away_form["avg_points_allowed"]) / 2
    away_side = (away_form["avg_points_scored"] + home_form["avg_points_allowed"]) / 2
    return home_side + away_side


def _predicted_total_pace_based(home_form, away_form):
    """
    Pace-adjusted totals estimate. Two teams that both average 80 points
    could get there via very different possession counts — the basic
    formula above can't tell those apart, so a team's pace shift (new
    rotation, an injury to a ball-handler) won't show up in the total
    until raw scoring averages drift, which lags real changes.

    This instead separates SCORING RATE (points per 100 possessions)
    from PACE (possessions per game), estimates the pace this specific
    matchup will likely play at (average of both teams' pace), and
    multiplies rate by that shared pace — the standard way pace-adjusted
    projections are done. Requires pace to be present for both teams;
    predicted_total() falls back to _predicted_total_basic otherwise.
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

    return expected_home_score + expected_away_score


def predicted_total(home_form, away_form):
    """
    Predicted combined score for the game. Uses the pace-adjusted
    estimate when both teams have usable pace data (see get_team_pace),
    otherwise falls back to the basic scoring-average estimate — same
    "unknown isn't silently assumed" rule used elsewhere in this file.
    Either way, applies the fatigue adjustment for teams on 0 days rest.
    """
    home_pace = home_form.get("pace")
    away_pace = away_form.get("pace")

    if home_pace and away_pace:
        base_total = _predicted_total_pace_based(home_form, away_form)
    else:
        base_total = _predicted_total_basic(home_form, away_form)

    return base_total - _fatigue_total_adjustment(home_form, away_form)


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
        if not (MIN_PLAUSIBLE_TOTAL <= line <= MAX_PLAUSIBLE_TOTAL):
            # safety net: a full-game WNBA total is essentially always in
            # this range. Anything outside it is almost certainly a
            # quarter/half/team/player sub-market that slipped through
            # the market-name filtering upstream — skip it outright
            # rather than ever risk tipping on it.
            continue
        prob_over, prob_under = prob_over_under(predicted, line)

        over_odds = entry.get("over_odds")
        if over_odds and over_odds >= MIN_ODDS:
            edge = prob_over - implied_probability(over_odds)
            if edge >= MIN_EDGE and _prob_is_plausible(prob_over):
                candidates.append({
                    "type": "totals",
                    "matchup": matchup,
                    "side": "over",
                    "line": line,
                    "odds": over_odds,
                    "market_id": entry.get("market_id"),
                    "our_estimated_prob": round(prob_over, 3),
                    "market_implied_prob": round(implied_probability(over_odds), 3),
                    "edge": round(edge, 3),
                })

        under_odds = entry.get("under_odds")
        if under_odds and under_odds >= MIN_ODDS:
            edge = prob_under - implied_probability(under_odds)
            if edge >= MIN_EDGE and _prob_is_plausible(prob_under):
                candidates.append({
                    "type": "totals",
                    "matchup": matchup,
                    "side": "under",
                    "line": line,
                    "odds": under_odds,
                    "market_id": entry.get("market_id"),
                    "our_estimated_prob": round(prob_under, 3),
                    "market_implied_prob": round(implied_probability(under_odds), 3),
                    "edge": round(edge, 3),
                })

    if not candidates:
        return None

    return max(candidates, key=lambda c: c["edge"])
