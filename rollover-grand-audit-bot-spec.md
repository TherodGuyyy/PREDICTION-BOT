# Rollover + Grand Audit Bot — Build Spec

Built on top of the existing tips bot infrastructure (WNBA + tennis models, GitHub Actions, OddsPapi + balldontlie/Sackmann data). This spec adds two new *output modes* on top of the same underlying prediction engine — it does not replace the tips bot, it extends what it does with the picks it already finds.

---

## 1. The two modes, side by side

| | **ROLLOVER mode** | **GRAND AUDIT mode** |
|---|---|---|
| Runs | Twice daily (morning + evening) | Only on days that pass the fixture-density trigger |
| Legs | 1–3 max | As many as clear the confidence bar (no fixed cap) |
| Odds floor per leg | 1.40 | 1.40 |
| Target | N/A — just compounds day to day | Combined odds reach roughly your 1000x target |
| Stake behavior | Small, every valid day | Bigger, only on trigger days |
| Telegram label | `🔁 ROLLOVER` | `🎯 GRAND AUDIT` |

Both modes pull from the **same underlying model output** (edge-scored picks from the tips bot's WNBA/tennis/whatever sports are live) — they're just two different filters/packagers sitting on top of it, not two separate prediction engines.

---

## 2. ROLLOVER mode logic

1. Run twice a day (same GitHub Actions schedule pattern as the tips bot — one morning cron, one evening cron).
2. Pull all picks that clear the edge threshold for that run.
3. Apply a **stricter** confidence bar than the normal tips bot uses — since one bad leg kills the whole rollover chain, rollover picks should be a subset of your *best* tips, not just anything that clears the normal bar. (Concretely: raise the minimum edge/probability threshold specifically for rollover-tagged picks — e.g. if the tips bot's normal bar is "edge > X," rollover could require "edge > X + buffer.")
4. Cap output at 3 legs max, even if more qualify — pick the top 3 by confidence score, not just the first 3 found.
5. **Correlation check**: if two candidate legs are from the same tournament/league on the same day (e.g. two matches from the same tennis tournament), prefer diversifying across tournaments/sports where possible, since a rain delay, walkover, or tournament-wide disruption could tank both at once.
6. If fewer than 1 qualifying pick exists that run, **send nothing** rather than lowering the bar to force a pick. This is the most important rule — the instinct to always have *something* to post is exactly what erodes a rollover's real hit rate.
7. Tag output clearly: `🔁 ROLLOVER — [morning/evening]` header, list the 1–3 picks with odds and a one-line reason each.

---

## 3. GRAND AUDIT mode logic

**Step 1 — Trigger detection (runs once daily, e.g. with the morning check):**
Count how many *qualifying* fixtures (picks that clear your normal edge bar, across all live sports) exist for that day. If the count crosses a threshold you set (start with something like 10+ qualifying fixtures — tune this once you see real data), grand audit mode fires for that day. If not, it stays silent — no grand audit message at all on quiet days.

**Step 2 — Leg selection:**
Pull every qualifying pick for the day (not capped at 3), sorted by confidence score.

**Step 3 — Build toward the target:**
Multiply combined odds leg by leg (starting with your highest-confidence picks first) until the running total crosses your target multiplier (~1000x). Stop adding legs once the target is crossed — don't keep piling on lower-confidence picks past the point you need.
- Reality check on the math: at a flat 1.40 per leg, it takes about **21 legs** to reach 1000x. If your real picks come in with mixed odds (some 1.40, some 1.6–1.8+ when the edge is stronger), you'll get there with fewer legs — the bot should just keep adding by confidence order until the target is crossed, not force an exact leg count.

**Step 4 — Correlation check (stricter than rollover):**
With this many legs riding on one slip, avoid pulling multiple legs from the same event/tournament/league-day more than necessary — spread across as many independent sports/competitions as the day's fixture list allows.

**Step 5 — Output:**
Tag clearly: `🎯 GRAND AUDIT — [date]`, list every leg with odds, show the running combined multiplier, and flag total leg count. Since this is a rare, high-stakes message, make it visually distinct from a normal rollover post (e.g. a longer message, maybe pinned in the channel).

---

## 4. Shared safety layer (applies to both modes)

- **Late recheck**: re-verify each selected pick shortly before match time (lineup changes, late scratches, odds having moved significantly) rather than trusting the morning/initial pull blindly. Cheap to add since you already have the odds-fetching code — just re-run the fetch for already-selected picks closer to game time and flag/drop anything that's changed materially.
- **Plausibility bounds**: reuse the tips bot's existing MAX_PLAUSIBLE_PROB safety net (already caught a real bug where a model was overconfident) — apply it to both rollover and grand audit picks too, since grand audit especially can't afford an overconfident phantom pick.

---

## 5. Sport pool — current reality check (as of early Sept 2026)

- **Tennis** — your strongest, most reliable leg source right now. Runs year-round, individual sport, ranking-driven predictability, already has a working model + track record.
- **WNBA** — currently on its FIBA World Cup break (Aug 31–Sept 16, 2026), resumes for regular season through Sept 24, then playoffs. Short remaining runway this season — don't lean on it heavily for the next few weeks.
- **Spanish Liga ACB (basketball)** — hasn't started its 2026-27 season yet; kicks off with the Supercopa Sept 19-20, league play begins the weekend of Sept 26-27. Not usable as a leg source until then.
- **Grand audit implication**: right now, your fixture-density trigger will mostly fire on tennis volume alone (which does have genuinely busy days across tours) — the basketball-heavy version of grand audit really turns on once ACB starts in late September. Worth building rollover mode first (works fine on tennis alone), and treating grand audit as ready-to-activate once ACB's fixture volume kicks in.

---

## 6. Suggested build order

1. Add a `rollover` output filter to the existing tips bot pipeline (reuses all current model/odds code — just a new selection + formatting layer). Test on tennis-only data since that's what's live right now.
2. Add the fixture-density trigger check + grand audit selection/formatting logic. Can be tested/dry-run now on tennis volume even before ACB starts, so it's ready to go live the moment basketball volume shows up.
3. Add the late-recheck layer to both.
4. Once ACB starts (Sept 26-27), monitor real grand-audit trigger days and tune the fixture-count threshold based on what actually shows up.

Want me to start writing the actual rollover-mode code first, since that's usable immediately on the tennis data you already have live?
