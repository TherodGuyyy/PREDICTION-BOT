"""Sends formatted tips to your Telegram chat/channel."""

import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def format_tip_message(tip, game_date):
    if tip["type"] == "totals":
        return (
            f"🏀 {tip['matchup']}\n"
            f"📅 {game_date}\n"
            f"🎯 *{tip['side'].upper()} {tip['line']}*\n"
            f"💰 Odds: {tip['odds']}\n"
            f"📊 Our estimate: {tip['our_estimated_prob']*100:.1f}% chance "
            f"(market implies {tip['market_implied_prob']*100:.1f}%)\n"
            f"📈 Edge: {tip['edge']*100:.1f}%"
        )

    # moneyline
    return (
        f"🏀 *{tip['team']}* to beat {tip['opponent']}\n"
        f"📅 {game_date}\n"
        f"💰 Odds: {tip['odds']}\n"
        f"📊 Our estimate: {tip['our_estimated_prob']*100:.1f}% win chance "
        f"(market implies {tip['market_implied_prob']*100:.1f}%)\n"
        f"📈 Edge: {tip['edge']*100:.1f}%"
    )


def _tip_label(tip):
    """Short identifier for a tip, used in error logging only."""
    if tip["type"] == "totals":
        return f"{tip['side']} {tip['line']} ({tip['matchup']})"
    return tip["team"]


def send_tips(tips, game_date):
    if not tips:
        return
    for tip in tips:
        message = format_tip_message(tip, game_date)
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": "Markdown",
                },
                timeout=15,
            )
            if not resp.ok:
                # don't fail silently — Markdown parse errors (e.g. a team
                # name containing a stray * or _) are a common cause here
                print(f"  Telegram send failed for {_tip_label(tip)}: {resp.status_code} {resp.text}")
        except requests.RequestException as e:
            print(f"  Telegram send error for {_tip_label(tip)}: {e}")
