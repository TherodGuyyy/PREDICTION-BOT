"""
Tips Bot — main entry point.

Run this once a day (via GitHub Actions — see .github/workflows/wnba-tips.yml)
to scan today's WNBA games AND tennis matches, and post any tips —
WNBA moneyline, WNBA totals, or tennis match-winner — that clear the
1.40 min odds + edge bar. WNBA and tennis each have their OWN cap
(WNBA_MAX_TIPS_PER_DAY, TENNIS_MAX_TIPS_PER_DAY) rather than sharing one
combined pool — so a big tennis day can't crowd out WNBA tips or vice versa.
"""

import datetime
from config import WNBA_MAX_TIPS_PER_DAY, TENNIS_MAX_TIPS_PER_DAY, SPORT_LABEL, TENNIS_MAX_MATCHES_PER_RUN
from stats_fetcher import get_todays_games, team_form_summary
from analysis import find_value_tip, predicted_total, find_totals_value_tip
from odds_fetcher import get_match_odds, get_totals_odds, debug_fixture_status
from tennis_stats_fetcher import player_form_summary
from tennis_analysis import find_tennis_value_tip
from tennis_odds_fetcher import (
    get_match_odds as get_tennis_match_odds,
    debug_fixture_status as tennis_debug_fixture_status,
    _get_tennis_fixtures_for_date,
)
from telegram_sender import send_tips


def run_wnba(today, all_tips):
    print(f"[{SPORT_LABEL}] Checking games for {today}...")

    games = get_todays_games()
    if not games:
        print("No WNBA games today.")
        return

    for game in games:
        home = game["home_team"]
        away = game["visitor_team"]
        print(f"Analyzing: {away['full_name']} @ {home['full_name']}")

        try:
            home_form = team_form_summary(home["id"])
            away_form = team_form_summary(away["id"])

            if not home_form or not away_form:
                print("  Skipping — not enough recent-game data yet.")
                continue

            # --- moneyline ---
            odds = get_match_odds(home["full_name"], away["full_name"], today)
            if odds:
                tip = find_value_tip(
                    game, home_form, away_form,
                    odds.get("home_odds"), odds.get("away_odds"),
                )
                if tip:
                    print(f"  MONEYLINE TIP: {tip['team']} @ {tip['odds']} (edge {tip['edge']})")
                    all_tips.append(tip)
                else:
                    print("  Moneyline: no value found on either side.")
            else:
                reason = debug_fixture_status(home["full_name"], away["full_name"], today)
                print(f"  Moneyline: couldn't find/match odds — {reason}")

            # --- totals (over/under) ---
            totals_odds = get_totals_odds(home["full_name"], away["full_name"], today)
            if totals_odds:
                predicted = predicted_total(home_form, away_form)
                totals_tip = find_totals_value_tip(game, predicted, totals_odds)
                if totals_tip:
                    print(
                        f"  TOTALS TIP: {totals_tip['side']} {totals_tip['line']} "
                        f"@ {totals_tip['odds']} (edge {totals_tip['edge']}, "
                        f"our predicted total: {round(predicted, 1)})"
                    )
                    all_tips.append(totals_tip)
                else:
                    print(f"  Totals: no value found (our predicted total: {round(predicted, 1)}).")
            else:
                reason = debug_fixture_status(home["full_name"], away["full_name"], today)
                print(f"  Totals: no totals odds available — {reason}")

        except Exception as e:
            # one bad game should never take down the whole run and lose
            # every other tip that day — log it and move on
            print(f"  ERROR analyzing this game, skipping it: {e}")
            continue


def run_tennis(today, all_tips):
    print(f"\n[TENNIS] Checking matches for {today}...")

    try:
        fixtures = _get_tennis_fixtures_for_date(today)
    except Exception as e:
        print(f"  Couldn't fetch tennis fixtures: {e}")
        return

    if not fixtures:
        print("No tennis matches today.")
        return

    # tennis has WAY more matches per day than WNBA across all the
    # concurrent tournaments — only bother running the (slow, rate-
    # limited-ish) stats lookup for matches that actually have odds
    # posted, rather than every single match on the tour today
    live_fixtures = [f for f in fixtures if f.get("hasOdds")]
    print(f"{len(fixtures)} singles matches found today, {len(live_fixtures)} with odds posted.")

    if len(live_fixtures) > TENNIS_MAX_MATCHES_PER_RUN:
        print(f"  Capping analysis to the first {TENNIS_MAX_MATCHES_PER_RUN} "
              f"(big draw day — protecting the free-tier request budget).")
        live_fixtures = live_fixtures[:TENNIS_MAX_MATCHES_PER_RUN]

    for fixture in live_fixtures:
        player_a = fixture["participant1Name"]
        player_b = fixture["participant2Name"]

        try:
            # NOTE: it's not confirmed whether OddsPapi's fixture objects
            # reliably include a surface field for tennis (couldn't verify
            # this live while building). Checking a few plausible field
            # names defensively; if none are present, this is flagged loudly
            # rather than silently guessing "Hard" and quietly breaking the
            # surface-specific analysis you asked for.
            surface = fixture.get("surface") or fixture.get("courtSurface") or fixture.get("surfaceType")
            if not surface:
                print(f"  WARNING: no surface field found on this fixture (checked 'surface', "
                      f"'courtSurface', 'surfaceType') — defaulting to Hard, but this needs a real "
                      f"fix. Raw fixture keys: {list(fixture.keys())}")
                surface = "Hard"

            print(f"Analyzing: {player_a} vs {player_b} ({surface})")

            # we don't know which tour (ATP/WTA) a fixture belongs to from
            # OddsPapi directly, so try ATP first, then WTA, for stats lookup
            a_form = player_form_summary(player_a, "atp", surface) or player_form_summary(player_a, "wta", surface)
            b_form = player_form_summary(player_b, "atp", surface) or player_form_summary(player_b, "wta", surface)

            if not a_form or not b_form:
                print("  Skipping — not enough recent match data for one or both players.")
                continue

            stale_days = max(a_form["most_recent_match_days_ago"], b_form["most_recent_match_days_ago"])
            if stale_days > 21:
                print(f"  Skipping — data looks stale (most recent match on record is {stale_days} days old).")
                continue

            odds = get_tennis_match_odds(player_a, player_b, today)
            if not odds:
                reason = tennis_debug_fixture_status(player_a, player_b, today)
                print(f"  Couldn't find/match odds — {reason}")
                continue

            tip = find_tennis_value_tip(
                player_a, player_b, a_form, b_form,
                odds.get("player_a_odds"), odds.get("player_b_odds"),
            )
            if tip:
                print(f"  TENNIS TIP: {tip['player']} @ {tip['odds']} (edge {tip['edge']})")
                all_tips.append(tip)
            else:
                print("  No value found on either side.")

        except Exception as e:
            # one bad match should never take down the whole run — with
            # potentially dozens of matches in a day, this matters a lot
            # more here than it does for WNBA's handful of games
            print(f"  ERROR analyzing this match, skipping it: {e}")
            continue


def run():
    today = datetime.date.today().isoformat()
    wnba_tips = []
    tennis_tips = []

    run_wnba(today, wnba_tips)
    run_tennis(today, tennis_tips)

    # rank each sport's tips by edge and cap SEPARATELY — a busy tennis
    # day can't crowd out WNBA tips or vice versa, since they're two
    # independent pools now, not one shared cap
    wnba_tips.sort(key=lambda t: t["edge"], reverse=True)
    tennis_tips.sort(key=lambda t: t["edge"], reverse=True)

    final_tips = wnba_tips[:WNBA_MAX_TIPS_PER_DAY] + tennis_tips[:TENNIS_MAX_TIPS_PER_DAY]

    print(f"\nSending {len(final_tips)} tip(s) to Telegram "
          f"({min(len(wnba_tips), WNBA_MAX_TIPS_PER_DAY)} WNBA, "
          f"{min(len(tennis_tips), TENNIS_MAX_TIPS_PER_DAY)} tennis)...")
    send_tips(final_tips, today)


if __name__ == "__main__":
    run()
