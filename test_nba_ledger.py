"""Tests for the NBA prediction ledger (nba_ledger.py). No network, no real files outside tmp_path."""

import json

import pytest

import nba_ledger as nl
import nba_model as nm
import nba_runner as nr
from test_nba import FULL_MARKET, GAME, form, market, qform, totals


def prediction(home_form=None, away_form=None, mkt=None):
    p = nm.build_prediction(GAME, home_form or qform(per_quarter=28.0, rest=2), away_form or qform(per_quarter=28.0),
                            mkt or FULL_MARKET, ["full"])
    return p


def finished(gid=1, hs=110, as_=100, hq=(28, 27, 28, 27), vq=(25, 25, 25, 25)):
    return {"id": gid, "date": "2026-11-11", "home_score": hs, "away_score": as_,
            "home_q": list(hq) if hq else None, "away_q": list(vq) if vq else None}


def tip(period="full", type_="moneyline", side="home", line=None, odds=1.8, prob=0.65, edge=0.08):
    return {"game_id": 1, "type": type_, "period": period, "side": side, "line": line,
            "odds": odds, "our_estimated_prob": prob, "edge": edge}


def record_for(pred=None, tips=(), sent=()):
    pred = pred or prediction()
    return nl.build_record(pred, list(tips), {nl.tip_key(1, t) for t in sent}, "2026-11-11")


def calls_by_id(rec):
    return {c["id"]: c for c in rec["calls"]}


# ---------------------------------------------------------------- store

def test_store_round_trip_and_missing_file(tmp_path):
    store = nl.JsonFileStore(str(tmp_path / "l.json"))
    assert store.load()["games"] == {}
    store.save({"version": 1, "games": {"1": {"x": 1}}})
    assert store.load()["games"] == {"1": {"x": 1}}
    assert not (tmp_path / "l.json.tmp").exists()


def test_corrupt_ledger_is_refused_not_overwritten(tmp_path):
    path = tmp_path / "l.json"
    path.write_text("{not json")
    with pytest.raises(nl.LedgerError):
        nl.JsonFileStore(str(path)).load()
    assert path.read_text() == "{not json"
    path.write_text(json.dumps({"version": 1}))
    with pytest.raises(nl.LedgerError):
        nl.JsonFileStore(str(path)).load()


def test_save_error_is_wrapped(tmp_path):
    with pytest.raises(nl.LedgerError):
        nl.JsonFileStore(str(tmp_path / "missing_dir" / "l.json")).save({"games": {}})


# ---------------------------------------------------------------- building

def test_prediction_carries_game_id_and_inputs():
    p = prediction()
    assert p["game_id"] == 1 and p["game_date"] == "2026-11-11"
    assert p["inputs"]["home"]["days_since_last_game"] == 2


def test_record_has_winner_and_points_for_every_period_plus_tips():
    t = tip()
    rec = record_for(tips=[t], sent=[t])
    ids = calls_by_id(rec)
    assert {"full:winner", "full:points", "h1:winner", "q4:points"} <= set(ids)
    tip_call = ids["tip:full:moneyline:home:None"]
    assert tip_call["sent"] is True and rec["status"] == "pending"


def test_unsent_tip_is_marked_not_sent_and_other_games_tips_ignored():
    other = dict(tip(), game_id=99)
    rec = record_for(tips=[tip(), other])
    assert [c["sent"] for c in rec["calls"] if c["kind"] == "tip"] == [False]


# ---------------------------------------------------------------- grading

def test_winner_hit_miss_and_push():
    rec = record_for()  # equal teams, home court -> picks home
    assert calls_by_id(rec)["full:winner"]["pick"] == "home"
    hit = nl.grade_record(rec, finished(hs=110, as_=100))
    assert calls_by_id(hit)["full:winner"]["result"]["outcome"] == "hit"
    miss = nl.grade_record(rec, finished(hs=90, as_=100))
    assert calls_by_id(miss)["full:winner"]["result"]["outcome"] == "miss"
    push = nl.grade_record(rec, finished(hs=100, as_=100, hq=(25,) * 4, vq=(25,) * 4))
    assert calls_by_id(push)["full:winner"]["result"]["outcome"] == "push"


def test_tip_types_graded_correctly():
    tips = [tip(type_="handicap", side="home", line=-5.5), tip(type_="totals", side="over", line=200.5),
            tip(type_="totals", side="under", line=210.0)]
    graded = calls_by_id(nl.grade_record(record_for(tips=tips), finished(hs=110, as_=100)))  # margin 10, total 210
    assert graded["tip:full:handicap:home:-5.5"]["result"]["outcome"] == "hit"
    assert graded["tip:full:totals:over:200.5"]["result"]["outcome"] == "hit"
    assert graded["tip:full:totals:under:210.0"]["result"]["outcome"] == "push"


def test_away_handicap_and_miss():
    graded = calls_by_id(nl.grade_record(
        record_for(tips=[tip(type_="handicap", side="away", line=3.5)]), finished(hs=110, as_=100)))
    assert graded["tip:full:handicap:away:3.5"]["result"]["outcome"] == "miss"


def test_half_and_quarter_use_quarter_scores():
    graded = calls_by_id(nl.grade_record(record_for(), finished(hq=(30, 30, 10, 10), vq=(20, 20, 20, 20))))
    h1, h2 = graded["h1:winner"]["result"], graded["h2:winner"]["result"]
    assert (h1["actual_home"], h1["actual_away"]) == (60, 40)
    assert (h2["actual_home"], h2["actual_away"]) == (20, 40)
    assert graded["q1:points"]["result"]["actual_total"] == 50


def test_missing_quarter_data_marks_period_calls_ungradable_but_full_game_still_graded():
    graded = calls_by_id(nl.grade_record(record_for(), finished(hq=None, vq=None)))
    assert graded["h1:winner"]["result"]["outcome"] == "ungradable"
    assert graded["full:winner"]["result"]["outcome"] == "hit"


def test_points_error_signs():
    rec = record_for()
    pred = calls_by_id(rec)["full:points"]
    graded = calls_by_id(nl.grade_record(rec, finished(hs=130, as_=120, hq=None, vq=None)))["full:points"]["result"]
    assert graded["total_error"] == 250 - pred["pred_total"]
    assert graded["margin_error"] == 10 - (pred["pred_home"] - pred["pred_away"])


# ---------------------------------------------------------------- reasons

def reasons_for(rec, game, call_id="full:winner"):
    return calls_by_id(nl.grade_record(rec, game))[call_id]["result"].get("reasons", [])


def test_back_to_back_reason():
    rec = record_for(prediction(home_form=qform(rest=1), away_form=qform(rest=2)))
    assert any("back-to-back" in r for r in reasons_for(rec, finished(hs=95, as_=100)))
    rested = record_for()
    assert not any("back-to-back" in r for r in reasons_for(rested, finished(hs=95, as_=100)))


def test_close_loss_and_blowout_reasons():
    rec = record_for()
    assert any("coin flip" in r for r in reasons_for(rec, finished(hs=98, as_=100)))
    assert any("form gap" in r for r in reasons_for(rec, finished(hs=85, as_=110)))


def test_scoring_gap_reasons_name_the_team():
    rec = record_for()  # forecast roughly 112-ish each side
    text = " ".join(reasons_for(rec, finished(hs=80, as_=125, hq=None, vq=None)))
    assert "Home Hawks scored far fewer" in text and "Away Bulls scored far more" in text


def test_overtime_reason_and_quarter_variance_reason():
    rec = record_for()
    ot = finished(hs=110, as_=120, hq=(25, 25, 25, 25), vq=(25, 25, 25, 25))
    assert any("overtime" in r.lower() for r in reasons_for(rec, ot))
    assert any("Short periods" in r for r in reasons_for(rec, finished(hq=(5, 5, 5, 5), vq=(30, 30, 30, 30)), "q1:winner"))


# ---------------------------------------------------------------- daily step

def fake_store(data=None):
    class Store:
        def __init__(self):
            self.data = data or {"version": 1, "games": {}}
            self.saved = 0

        def load(self):
            return json.loads(json.dumps(self.data))

        def save(self, d):
            self.data = d
            self.saved += 1
    return Store()


def test_record_then_grade_next_day_end_to_end():
    store = fake_store()
    t = tip()
    first = nl.record_and_grade([prediction()], [t], [t], "2026-11-11", store=store, get_finished=lambda: [])
    assert first["saved"] == 1 and first["newly_graded"] == []
    assert store.data["games"]["1"]["status"] == "pending"

    second = nl.record_and_grade([], [], [], "2026-11-12", store=store, get_finished=lambda: [finished()])
    assert len(second["newly_graded"]) == 1 and store.data["games"]["1"]["status"] == "graded"
    assert second["summary"]["games_graded"] == 1 and second["summary"]["sent_tips"]["n"] == 1


def test_graded_record_is_never_overwritten():
    store = fake_store()
    nl.record_and_grade([prediction()], [], [], "2026-11-11", store=store, get_finished=lambda: [])
    nl.record_and_grade([], [], [], "2026-11-12", store=store, get_finished=lambda: [finished()])
    nl.record_and_grade([prediction(away_form=qform(per_quarter=33))], [], [], "2026-11-12",
                        store=store, get_finished=lambda: [])
    assert store.data["games"]["1"]["status"] == "graded"


def test_pending_forecast_can_be_refreshed_before_the_game():
    store = fake_store()
    nl.record_and_grade([prediction()], [], [], "2026-11-11", store=store, get_finished=lambda: [])
    before = calls_by_id(store.data["games"]["1"])["full:points"]["pred_home"]
    nl.record_and_grade([prediction(home_form=qform(per_quarter=31))], [], [], "2026-11-11",
                        store=store, get_finished=lambda: [])
    assert calls_by_id(store.data["games"]["1"])["full:points"]["pred_home"] > before
    assert len(store.data["games"]) == 1


def test_old_unfound_game_becomes_unresolved():
    store = fake_store()
    nl.record_and_grade([prediction()], [], [], "2026-11-11", store=store, get_finished=lambda: [])
    nl.record_and_grade([], [], [], "2026-11-20", store=store, get_finished=lambda: [])
    assert store.data["games"]["1"]["status"] == "unresolved"


def test_feed_failure_keeps_forecasts_and_skips_grading():
    from nba_stats_fetcher import NbaDataError
    store = fake_store()
    nl.record_and_grade([prediction()], [], [], "2026-11-11", store=store, get_finished=lambda: [])

    def down():
        raise NbaDataError("down")

    result = nl.record_and_grade([], [], [], "2026-11-12", store=store, get_finished=down)
    assert result["newly_graded"] == [] and store.data["games"]["1"]["status"] == "pending"


def test_unreadable_ledger_returns_none_and_writes_nothing(tmp_path):
    path = tmp_path / "l.json"
    path.write_text("garbage")
    assert nl.record_and_grade([prediction()], [], [], "2026-11-11",
                               store=nl.JsonFileStore(str(path)), get_finished=lambda: []) is None
    assert path.read_text() == "garbage"


def test_one_bad_prediction_does_not_block_the_others():
    bad = {"game_id": 7, "matchup": "x"}  # missing fields
    store = fake_store()
    result = nl.record_and_grade([bad, prediction()], [], [], "2026-11-11", store=store, get_finished=lambda: [])
    assert result["saved"] == 1 and list(store.data["games"]) == ["1"]


# ---------------------------------------------------------------- track record + message

def graded_ledger(n_home_wins, n_games):
    data = {"version": 1, "games": {}}
    for i in range(n_games):
        rec = record_for()
        rec["game_id"] = i + 1
        win = i < n_home_wins
        data["games"][str(i + 1)] = nl.grade_record(rec, finished(i + 1, hs=110 if win else 90, as_=100))
    return data


def test_summary_calibration_and_errors():
    s = nl.summarize(graded_ledger(3, 4))
    assert s["games_graded"] == 4
    full = s["groups"]["full"]
    assert full["n"] == 4 and full["hit_rate"] == 0.75
    assert s["buckets"] and s["brier"] is not None
    assert s["points"]["full"]["n"] == 4 and s["points"]["h1"]["mae_total"] >= 0


def test_summary_of_empty_ledger_is_safe():
    s = nl.summarize({"version": 1, "games": {}})
    assert s["games_graded"] == 0 and s["brier"] is None and s["buckets"] == []


def test_sent_tip_profit_counts_hits_and_losses():
    data = {"version": 1, "games": {}}
    for gid, (hs, as_) in enumerate([(110, 100), (90, 100)], start=1):
        t = dict(tip(), game_id=gid)
        rec = nl.build_record(dict(prediction(), game_id=gid), [t], {nl.tip_key(gid, t)}, "2026-11-11")
        data["games"][str(gid)] = nl.grade_record(rec, finished(gid, hs=hs, as_=as_))
    sent = nl.summarize(data)["sent_tips"]
    assert (sent["hit"], sent["miss"]) == (1, 1) and sent["profit"] == pytest.approx(0.8 - 1)


def test_report_is_plain_text_lists_misses_with_reasons_and_warns_when_early():
    rec = record_for(prediction(home_form=qform(rest=1), away_form=qform(rest=2)))
    graded = nl.grade_record(rec, finished(hs=95, as_=100))
    data = {"version": 1, "games": {"1": graded}}
    text = nl.format_report({"newly_graded": [graded], "summary": nl.summarize(data), "saved": 0})
    assert "MISS" in text and "back-to-back" in text and "Calibration" in text
    assert "Too few games" in text and "*" not in text and len(text) <= nl.MAX_REPORT_CHARS


def test_report_sent_only_when_something_was_graded():
    sent = []
    assert nl.send_report(None, send=sent.append) is False
    assert nl.send_report({"newly_graded": [], "summary": {}}, send=sent.append) is False
    graded = nl.grade_record(record_for(), finished())
    result = {"newly_graded": [graded], "summary": nl.summarize({"version": 1, "games": {"1": graded}})}
    assert nl.send_report(result, send=sent.append) is True and len(sent) == 1


# ---------------------------------------------------------------- runner tags tips with the game id

def test_runner_tips_carry_game_id():
    import test_nba as base
    hot = qform(per_quarter=30.0)
    d = base.deps(forms={1: hot, 2: hot}, markets={"full": market(totals_=[totals(210.0)])})
    tips, preds = nr.run_nba("2026-11-11", d)
    assert tips and all(t["game_id"] == 1 for t in tips) and preds[0]["game_id"] == 1
