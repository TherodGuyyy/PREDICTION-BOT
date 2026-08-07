# Tips Bot — Setup Guide (WNBA + Tennis)

What this does: every day it checks today's WNBA games AND tennis
matches across the tour, analyzes each one, and sends Telegram tips —
WNBA moneyline, WNBA totals (over/under), and tennis match-winner —
only when the odds are 1.40+ AND our estimate says the market price is
more generous than it should be. WNBA and tennis each have their OWN
daily cap (5 for WNBA, 3 for tennis — 8 total on a busy day) rather
than sharing one pool, so a big tennis day can't crowd out WNBA tips.

Runs automatically once a day via **GitHub Actions** — not Render,
since this wakes up once a day, checks, and goes back to sleep rather
than needing to stay running continuously.

---

## What's in this folder
```
wnba_tips_bot/
├── config.py                  <- your API keys go here (or as GitHub secrets)
├── stats_fetcher.py             (WNBA games + team form, from balldontlie)
├── analysis.py                  (WNBA moneyline + totals models)
├── odds_fetcher.py              (WNBA odds, from OddsPapi)
├── tennis_stats_fetcher.py      (tennis player form + rankings, from Sackmann's archives)
├── tennis_analysis.py           (tennis win-probability model)
├── tennis_odds_fetcher.py       (tennis odds, from OddsPapi)
├── telegram_sender.py           (sends tips to Telegram — all sports)
├── main.py                      (ties it all together — runs daily)
├── requirements.txt
└── .github/workflows/wnba-tips.yml   (the daily automation)
```

---

## Step 1 — Get your two free API keys
1. **balldontlie** (WNBA stats) — https://app.balldontlie.io, sign up
   free, verify email, copy your key from Account Settings.
2. **OddsPapi** (odds for BOTH sports) — https://oddspapi.io/signup,
   sign up free (no card needed), copy your key from your account page.

Tennis stats need no key — they come from free public GitHub data (see
"About the tennis data" below).

## Step 2 — Set up your Telegram bot
Same as before — a bot via @BotFather, get the token + your chat ID.

## Step 3 — Sanity-check BOTH odds sources (do this before deploying)
Paste your real keys into `config.py` temporarily, run:
```
pip install -r requirements.txt
python odds_fetcher.py
python tennis_odds_fetcher.py
```
For tennis specifically, check the printed **raw fixture keys** —
there's a real open question about whether OddsPapi labels court
surface consistently for tennis, and the script will warn loudly in
`main.py`'s output if it can't find one (rather than silently guessing
"Hard" and quietly breaking the surface-specific analysis). If you see
that warning, send me the raw fixture output and I'll fix the field
name in one pass.

Also test the tennis stats source directly:
```
python tennis_stats_fetcher.py
```
This checks a well-known player (Carlos Alcaraz) and — importantly —
prints how many **days old** their most recent match on record is. See
"About the tennis data" below for why that number matters.

Once everything looks right, **remove your real keys from `config.py`**
before uploading to GitHub (secrets handle that instead).

## Step 4 — Test the full bot locally (optional but recommended)
```
python main.py
```
Console output shows every WNBA game AND every tennis match with odds
posted today, and why each did or didn't produce a tip.

## Step 5 — Upload to GitHub
Same as before: new **private** repo → Add file → Upload files → drag
in everything including the `.github` folder → Commit. If the `.github`
folder doesn't show up when dragging (Windows sometimes hides
dot-folders), use Add file → Create new file → type the path
`.github/workflows/wnba-tips.yml` directly.

## Step 6 — Add your secrets
Same four as before — tennis doesn't need any new ones:
- `BALLDONTLIE_API_KEY`
- `ODDSPAPI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Step 7 — Run it
Actions tab → "WNBA Tips Bot" → **Run workflow** to test, then it runs
automatically every day at 14:00 UTC (15:00 Lagos time).

---

## About the tennis data (please read this one)
Tennis stats come from Jeff Sackmann's free, community-maintained
`tennis_atp`/`tennis_wta` archives on GitHub — credit to him, used here
under Creative Commons Attribution-NonCommercial-ShareAlike (fine for
your personal use). Unlike balldontlie, **this is not a live feed** —
it can lag behind the actual tour by anywhere from a day to a couple
of weeks depending on how busy things are. `tennis_main.py`'s output
tells you exactly how stale the data was for each match it analyzed,
and the bot will refuse to tip a match if the data looks too stale
(over 21 days old) rather than guess blind. Worth keeping an eye on
this more than the WNBA side, since it's the one part of this whole
project running on data that isn't close to real-time.

## How the tennis model works
Combines three things into a win-probability estimate:
1. **Current ranking** (the main signal — better-ranked players win more often)
2. **Recent overall form** (win% over their last several matches)
3. **Surface-specific form** (win% on the SAME surface as today's match,
   specifically because you asked for this — a player's grass results
   don't tell you much about how they'll do on clay)

If a player doesn't have enough matches on record (5 minimum) or
enough matches on today's specific surface (3 minimum, else it falls
back to overall form), the bot skips that match rather than guessing.

Same as the WNBA models: this is a starting point, not a finished
product, and the weights (`W_RANK`, `W_SURFACE`, `W_FORM` in
`tennis_analysis.py`) are reasonable defaults to revisit once there's
real logged results to tune against.

## Notes
- **Why tennis still has an edge threshold, not just the 1.40 odds
  floor**: the odds floor stops tiny payouts, the edge threshold stops
  the bot from just tipping whoever's ranked higher every time. They do
  different jobs, so both stay — same as WNBA.
- Tennis singles only — doubles matches are automatically filtered out.
- All tip types (WNBA moneyline, WNBA totals, tennis) each have their
  OWN daily cap (5 for WNBA, 3 for tennis) — a busy day in one sport
  can't reduce how many tips the other sport gets to send.
- A day with zero qualifying tips across everything sends nothing —
  expected, not a bug.
- Next up when you're ready: NBA (October), NCAAB/NCAAW (November),
  then NBA player-prop stats (needs balldontlie's paid tier).
