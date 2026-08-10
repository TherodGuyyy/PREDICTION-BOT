"""Sends formatted tips to your Telegram chat/channel."""

import time
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

MIN_SECONDS_BETWEEN_MESSAGES = 1.5  # Telegram rate-limits rapid-fire messages
# to the same chat — sending 5 tips back-to-back with no delay risks the
# last one(s) getting silently rejected, which is exactly what happened
# live (5 qualifying tips, only 4 arrived). Pacing sends avoids tripping
# that limit in the first place.


def _escape_md(text):
    """
    Escapes characters Telegram's legacy Markdown treats as formatting
    (*, _, `, [) in dynamic text (team/player/tournament names) that comes
    from external data sources we don't control. This is what would have
    prevented the market_id bug's whole CLASS of failure — not just that
    one instance — since any of these names could theoretically contain
    one of these characters and silently break the message the same way.
    """
    text = str(text)
    for ch in ["\\", "*", "_", "`", "["]:
        text = text.replace(ch, f"\\{ch}")
    return text


def format_tip_message(tip, game_date):
    if tip["type"] == "totals":
        return (
            f"🏀 {_escape_md(tip['matchup'])}\n"
            f"📅 {game_date}\n"
            f"🎯 *{tip['side'].upper()} {tip['line']}*\n"
            f"💰 Odds: {tip['odds']}\n"
            f"📊 Our estimate: {tip['our_estimated_prob']*100:.1f}% chance "
            f"(market implies {tip['market_implied_prob']*100:.1f}%)\n"
            f"📈 Edge: {tip['edge']*100:.1f}%\n"
            f"🔍 Market ID: {tip.get('market_id', 'unknown')}"
        )

    if tip["type"] == "tennis":
        return (
            f"🎾 *{_escape_md(tip['player'])}* to beat {_escape_md(tip['opponent'])}\n"
            f"📅 {game_date}\n"
            f"💰 Odds: {tip['odds']}\n"
            f"📊 Our estimate: {tip['our_estimated_prob']*100:.1f}% win chance "
            f"(market implies {tip['market_implied_prob']*100:.1f}%)\n"
            f"📈 Edge: {tip['edge']*100:.1f}%"
        )

    # moneyline (WNBA)
    return (
        f"🏀 *{_escape_md(tip['team'])}* to beat {_escape_md(tip['opponent'])}\n"
        f"📅 {game_date}\n"
        f"💰 Odds: {tip['odds']}\n"
        f"📊 Our estimate: {tip['our_estimated_prob']*100:.1f}% win chance "
        f"(market implies {tip['market_implied_prob']*100:.1f}%)\n"
        f"📈 Edge: {tip['edge']*100:.1f}%"
    )


def _format_plain(tip, game_date):
    """
    Plain-text fallback with no Markdown at all — used if a formatted
    send still fails for any reason. This guarantees a tip is never
    silently dropped just because of a formatting quirk we didn't
    anticipate; worst case, it arrives without bold styling instead of
    not arriving at all.
    """
    if tip["type"] == "totals":
        return (
            f"{tip['matchup']}\n{game_date}\n"
            f"{tip['side'].upper()} {tip['line']}\n"
            f"Odds: {tip['odds']}\n"
            f"Our estimate: {tip['our_estimated_prob']*100:.1f}% "
            f"(market implies {tip['market_implied_prob']*100:.1f}%)\n"
            f"Edge: {tip['edge']*100:.1f}%\n"
            f"Market ID: {tip.get('market_id', 'unknown')}"
        )
    if tip["type"] == "tennis":
        return (
            f"{tip['player']} to beat {tip['opponent']}\n{game_date}\n"
            f"Odds: {tip['odds']}\n"
            f"Our estimate: {tip['our_estimated_prob']*100:.1f}% "
            f"(market implies {tip['market_implied_prob']*100:.1f}%)\n"
            f"Edge: {tip['edge']*100:.1f}%"
        )
    return (
        f"{tip['team']} to beat {tip['opponent']}\n{game_date}\n"
        f"Odds: {tip['odds']}\n"
        f"Our estimate: {tip['our_estimated_prob']*100:.1f}% "
        f"(market implies {tip['market_implied_prob']*100:.1f}%)\n"
        f"Edge: {tip['edge']*100:.1f}%"
    )


def _tip_label(tip):
    """Short identifier for a tip, used in error logging only."""
    if tip["type"] == "totals":
        return f"{tip['side']} {tip['line']} ({tip['matchup']})"
    if tip["type"] == "tennis":
        return tip["player"]
    return tip["team"]


def send_tips(tips, game_date):
    if not tips:
        return

    last_send_time = 0
    for tip in tips:
        # pace sends so we don't trip Telegram's rate limit
        elapsed = time.time() - last_send_time
        if elapsed < MIN_SECONDS_BETWEEN_MESSAGES:
            time.sleep(MIN_SECONDS_BETWEEN_MESSAGES - elapsed)

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
            last_send_time = time.time()

            if resp.status_code == 429:
                # Telegram tells us exactly how long to wait — respect it
                # and retry once rather than just dropping the message
                retry_after = resp.json().get("parameters", {}).get("retry_after", 3)
                print(f"  Telegram rate-limited us, waiting {retry_after}s and retrying: {_tip_label(tip)}")
                time.sleep(retry_after + 0.5)
                resp = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                    timeout=15,
                )
                last_send_time = time.time()

            if not resp.ok:
                # don't fail silently — and don't just give up either.
                # Try once more as plain text (no Markdown at all) so an
                # unanticipated formatting quirk can never silently drop
                # a real tip the way market_id's underscore just did.
                print(f"  Telegram send failed for {_tip_label(tip)}: {resp.status_code} {resp.text}")
                print(f"  Retrying as plain text (no formatting)...")
                plain_resp = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    data={"chat_id": TELEGRAM_CHAT_ID, "text": _format_plain(tip, game_date)},
                    timeout=15,
                )
                if not plain_resp.ok:
                    print(f"  Plain-text retry ALSO failed for {_tip_label(tip)}: {plain_resp.status_code} {plain_resp.text}")
                else:
                    print(f"  Plain-text retry succeeded for {_tip_label(tip)}.")
        except requests.RequestException as e:
            print(f"  Telegram send error for {_tip_label(tip)}: {e}")
