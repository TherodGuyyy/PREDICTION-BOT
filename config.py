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
WNBA_MAX_TIPS_PER_DAY = 5   # separate cap for WNBA (moneyline + totals combined)
TENNIS_MAX_TIPS_PER_DAY = 3  # separate cap for tennis — 8 total between the two
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
MIN_PLAUSIBLE_TOTAL = 120  # hard safety bound: a real full-game WNBA combined
MAX_PLAUSIBLE_TOTAL = 220  # score is essentially always in this range. Any
                          # totals line outside it is almost certainly NOT a
                          # full-game market (a quarter, half, team, or player
                          # total slipped through) and is discarded before it
                          # can ever become a tip — this is what stops a bug
                          # like matching a quarter-total line from producing
                          # a nonsensical real tip again, even if the market
                          # name-based filtering misses a future edge case.
MAX_PLAUSIBLE_PROB = 0.97  # hard safety cap, applies to ALL models (WNBA
                          # moneyline, WNBA totals, tennis). A real edge from
                          # a simple model like this should essentially never
                          # look like 97%+ certainty. If it does, that's a
                          # signal something's mismatched (wrong market,
                          # stale data, a fixture that doesn't really match)
                          # — not a signal the model is unusually confident.
                          # A tip whose estimated probability exceeds this is
                          # blocked outright regardless of the reason.

# --- Expanded totals markets (team / half / quarter) ---
# balldontlie's free tier has no period-by-period scoring history, so these
# CANNOT be built as genuine dedicated models the way the full-game total
# is — there's no real data to learn a WNBA-specific first-half or
# first-quarter scoring split from. What's built instead: individual TEAM
# totals use the exact same per-team scoring/allowed data as everything else
# (full rigor, no guessing). Half/quarter totals use a flat proportion of
# the full-game predicted total — an honest simplification, not a
# team-specific or league-verified split. Treat half/quarter tips as lower
# confidence than the main markets; that's exactly why they get their own,
# stricter edge requirement below.
ENABLE_TEAM_TOTALS = True
ENABLE_HALF_TOTALS = True
ENABLE_QUARTER_TOTALS = True

HALF_TOTAL_PROPORTION = 0.50   # flat assumption: first half ≈ 50% of the full-game total
QUARTER_TOTAL_PROPORTION = 0.25  # flat assumption: any single quarter ≈ 25% of the full-game total

MIN_EDGE_SUBMARKET = 0.06  # stricter than MIN_EDGE (0.03) — applied to half
                          # and quarter totals specifically, since those
                          # predictions rest on a flat proportion assumption
                          # rather than real period-level data. A bigger
                          # required edge is the guardrail against that
                          # extra uncertainty producing a false-positive tip.

# --- Tennis data (Jeff Sackmann's free tennis_atp / tennis_wta archives) ---
# Free, well-structured, community-maintained — but NOT live. Updates can lag
# by days to weeks depending on how busy the tour is. Credit: Jeff Sackmann,
# https://github.com/JeffSackmann/tennis_atp and tennis_wta (Creative Commons
# Attribution-NonCommercial-ShareAlike — personal non-commercial use is fine,
# just crediting him, which this comment + the README do).
ATP_MATCHES_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv"
WTA_MATCHES_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{year}.csv"
TENNIS_MIN_MATCHES_FOR_ANALYSIS = 5  # a player needs at least this many recent
                          # matches on record before the bot will tip either
                          # side of their match — same small-sample protection
                          # as MIN_GAMES_FOR_ANALYSIS, adapted for tennis
TENNIS_SURFACE_MIN_MATCHES = 3  # minimum matches on THIS SPECIFIC SURFACE
                          # before surface-specific form is trusted over
                          # overall form — a player with 1 clay match this
                          # year shouldn't have their clay "form" taken
                          # seriously yet
TENNIS_MAX_MATCHES_PER_RUN = 25  # safety cap — a Grand Slam first round can
                          # have 60+ singles matches with odds posted in one
                          # day. Analyzing all of them isn't just slow, it
                          # risks burning through OddsPapi's free-tier
                          # request quota fast. If there are more live
                          # matches than this cap, the bot analyzes the
                          # first N found rather than trying everything.

# --- Sport (for when we add more leagues later, this keeps things labeled) ---
SPORT_LABEL = "WNBA"
