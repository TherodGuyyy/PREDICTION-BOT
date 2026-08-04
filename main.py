"""
WNBA Tips Bot — main entry point.

Run this once a day (via GitHub Actions — see .github/workflows/wnba-tips.yml)
to scan today's WNBA games and post any tips — moneyline OR totals
(over/under) — that clear the 1.40 min odds + edge bar, capped at
MAX_TIPS_PER_DAY combined across both tip types.
"""

import datetime
from config import MAX_TIPS_PER_DAY, SPORT_LABEL
from stats_fetcher import get_todays_games, team_form_summary
from analysis import find_value_tip, predicted_total, find_totals_value_tip
from odds_fetcher import get_match_odds, get_totals_odds, debug_fixture_status
from telegram_sender import send_tips


def run():
    today = datetime.date.today().isoformat()
    print(f"[{SPORT_LABEL}] Checking games for {today}...")

    games = get_todays_games()
    if not games:
        print("No games today.")
        return

    all_tips = []

    for game in games:
        home = game["home_team"]
        away = game["visitor_team"]
        print(f"Analyzing: {away['full_name']} @ {home['full_name']}")

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

    # rank by edge, keep only the best MAX_TIPS_PER_DAY across both types
    all_tips.sort(key=lambda t: t["edge"], reverse=True)
    final_tips = all_tips[:MAX_TIPS_PER_DAY]

    print(f"\nSending {len(final_tips)} tip(s) to Telegram...")
    send_tips(final_tips, today)


if __name__ == "__main__":
    run()
