"""
Config for the WNBA Tips Bot.
Fill in the values below (or set them as environment variables with the same names
on Render/GitHub Actions/wherever you deploy this).
"""

import os

# --- balldontlie API (free tier) ---
# Get a free key at https://app.balldontlie.io (sign up, verify email, copy key
# from Account Settings). Free tier = 5 requests/minute, core endpoints (teams,
# players, games). That's enough for what this bot needs.
BALLDONTLIE_API_KEY = os.getenv("BALLDONTLIE_API_KEY", "PASTE_YOUR_KEY_HERE")
BALLDONTLIE_BASE_URL = "https://api.balldontlie.io/v1"
BALLDONTLIE_WNBA_BASE_URL = "https://api.balldontlie.io/wnba/v1"  # WNBA-specific endpoints

# --- OddsPapi (free tier — WNBA odds) ---
# Sign up free at https://oddspapi.io/signup (no card needed), copy your
# API key from your account page, paste it here (or set as env var).
ODDSPAPI_API_KEY = os.getenv("ODDSPAPI_API_KEY", "PASTE_YOUR_KEY_HERE")

# --- Telegram ---
# Use the SAME bot/channel as your other prediction-style bots if you want,
# or a fresh one — your call. This is a separate script from the memecoin bot
# either way, so no account-mixing risk here.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE")

# --- Tip rules ---
MIN_ODDS = 1.40          # never post a tip below this
MAX_TIPS_PER_DAY = 5     # hard cap, across all sports eventually (WNBA only for now)
MIN_EDGE = 0.03          # only tip if our estimated fair probability beats the
                          # market-implied probability by at least this much (3%)
                          # — this is what keeps the bot from just tipping favorites
MIN_GAMES_FOR_ANALYSIS = 5  # a team needs at least this many completed games in
                          # the current season before the bot will tip either side
                          # of their matchup — protects against early-season
                          # small-sample noise (this matters most when NBA/NCAAB
                          # get added later and start from game 1; WNBA is already
                          # well past this by August)
TOTAL_POINTS_STD_DEV = 13.0  # approximate game-to-game variability in WNBA
                          # combined scores, used to estimate over/under
                          # probabilities around our predicted total. This is a
                          # reasonable starting estimate, not derived from your
                          # actual league data yet — worth revisiting once
                          # there's a few weeks of real totals outcomes logged.

# --- Sport (for when we add more leagues later, this keeps things labeled) ---
SPORT_LABEL = "WNBA"
