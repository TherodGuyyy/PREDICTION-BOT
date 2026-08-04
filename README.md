# WNBA Tips Bot — Setup Guide

What this does: every day it checks today's WNBA games, estimates each
team's win chance AND predicts the combined final score, compares both
to real bookmaker odds (moneyline + totals/over-under), and sends you
up to 5 Telegram tips — only when the odds are 1.40 or higher AND our
estimate says the market price is more generous than it should be
(that's what makes it a "tip" rather than just picking favorites).

Runs automatically once a day via **GitHub Actions** — the same
pattern as your arb bots. Not Render — Render is for things that need
to stay running continuously (like the memecoin bot); this one wakes
up once a day, checks, and goes back to sleep.

---

## What's in this folder
```
wnba_tips_bot/
├── config.py                          <- your API keys go here (or as GitHub secrets)
├── stats_fetcher.py                     (pulls WNBA games + team form from balldontlie)
├── analysis.py                          (estimates win probability, finds value tips)
├── odds_fetcher.py                      (pulls WNBA odds from OddsPapi)
├── telegram_sender.py                   (sends tips to Telegram)
├── main.py                              (ties it all together — runs daily)
├── requirements.txt
└── .github/workflows/wnba-tips.yml      (the daily automation)
```

All files are finished and wired together. The one thing to do before
trusting it fully is Step 3 below — a quick sanity check on the odds
source, since I built it from documentation without being able to
call the live API myself.

---

## Step 1 — Get your two free API keys
1. **balldontlie** (stats) — go to https://app.balldontlie.io, sign up
   (free), verify your email, copy your API key from Account Settings.
2. **OddsPapi** (odds) — go to https://oddspapi.io/signup, sign up
   (free, no card needed), copy your API key from your account page.

## Step 2 — Set up your Telegram bot
Create a bot via @BotFather the same way you did for your other
projects (or reuse an existing channel). You'll need:
- The bot token (from BotFather)
- Your chat ID (the channel/group/user the bot should post to)

## Step 3 — Sanity-check the odds source (do this before deploying)
1. On your computer, open a terminal in this folder and run:
   ```
   pip install -r requirements.txt
   ```
2. Open `config.py` and paste in your two API keys (balldontlie and
   OddsPapi) directly, just for this local test.
3. Run:
   ```
   python odds_fetcher.py
   ```
4. It'll print: the sport ID, the WNBA tournament ID, the moneyline
   market it picked, all the totals (over/under) lines it found,
   today's WNBA fixtures, and — if any game has live odds — a real
   moneyline result AND a real totals result. Double-check the
   moneyline market and totals lines look sensible (real point totals
   for basketball, not something like 2.5).
5. If anything looks off — wrong tournament, wrong market picked, no
   fixtures found on a day you know has games — copy the printed
   output and send it to me here and I'll fix it in one pass.
6. Once it looks right, **remove your real keys from `config.py`
   again** before uploading to GitHub (Step 6 uses secrets instead, so
   your keys never sit in the code itself).

## Step 4 — Test the full bot locally (optional but recommended)
```
python main.py
```
You'll see console output for every game it checked and why it did or
didn't send a tip. If there are no WNBA games that day, it'll just say
so and stop — that's normal.

## Step 5 — Upload to GitHub
1. Go to https://github.com and log in.
2. Click **New repository** → name it something like `wnba-tips-bot` →
   set it to **Private** → Create repository.
3. On the repo page, use **Add file → Upload files**.
4. Drag in every file from this folder, including the `.github` folder
   with the workflow file inside it. If the upload UI doesn't pick up
   hidden folders when you drag the parent folder, upload
   `.github/workflows/wnba-tips.yml` directly — GitHub will recreate
   the folder structure automatically.
5. Commit the files.

## Step 6 — Add your secrets
1. In your repo: **Settings → Secrets and variables → Actions**.
2. Click **New repository secret** and add each of these four:
   - `BALLDONTLIE_API_KEY`
   - `ODDSPAPI_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

## Step 7 — Run it
1. Go to the **Actions** tab in your repo.
2. Click "WNBA Tips Bot" in the left sidebar.
3. Click **Run workflow** to trigger it manually and watch the logs.
4. Once that runs clean, it'll also run automatically every day at
   14:00 UTC (15:00 Lagos time) — change the schedule in
   `.github/workflows/wnba-tips.yml` if you want a different time.

---

## Notes
- Two independent models run per game: **moneyline** (win probability
  from last 10 games' win% + point differential + home edge) and
  **totals** (predicted combined score from both teams' scoring AND
  allowing tendencies, compared against every over/under line a
  bookmaker offers). Both are intentionally simple starting models —
  no opponent-adjusted defense, pace/style, injuries, or rest days
  factored in yet. Once it's run for a couple of weeks, it's worth
  logging real outcomes vs. predictions and tuning both from there —
  happy to help with that whenever you're ready.
- The totals model assumes a fixed amount of game-to-game scoring
  variability (`TOTAL_POINTS_STD_DEV` in config.py) rather than one
  calculated from actual WNBA data — a reasonable starting estimate,
  not a precise one.
- A day with zero games meeting the 1.40 odds + edge bar (on either
  moneyline or totals) sends nothing — that's expected, not a bug.
- Next up when you're ready: NBA (October), NCAAB/NCAAW (November),
  both reusing this same setup, then NBA player-prop stats (needs
  balldontlie's paid tier), then tennis.
