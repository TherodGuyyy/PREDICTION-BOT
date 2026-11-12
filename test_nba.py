"""
Tests for the NBA pipeline (stats fetcher, odds name matching, model, runner).
No network: every outside call is replaced by a fake.
Run with:  pytest -q
"""

import datetime

import pytest
import requests

import nba_model as nm
import nba_odds_fetcher as nof
import nba_runner as nr
import nba_stats_fetcher as ns
import rundown_client as rc

TODAY = datetime.date(2026, 11, 11)
NOW = datetime.datetime(2026, 11, 11, 14, 0, tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------- helpers

class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("bad json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), headers))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(queue, clock_times=None):
    sleeps = []
    ticks = iter(clock_times) if clock_times else None
    clock = (lambda: next(ticks)) if ticks else (lambda: 0.0)
    client = ns.BalldontlieClient("key", base_url="https://x/v1", session=FakeSession(queue),
                                  sleep=sleeps.append, clock=clock)
    return client, sleeps


def raw_game(gid, date, home, away, hs=None, vs=None, state="final", dt=None):
    return {
        "id": gid, "date": date, "datetime": dt, "status": "Final" if state == "final" else "7:00 pm ET",
        "status_state": state, "home_team_score": hs, "visitor_team_score": vs,
        "home_team": {"id": home, "full_name": f"Team {home}"},
        "visitor_team": {"id": away, "full_name": f"Team {away}"},
    }


def form(diff=0.0, scored=114.0, allowed=114.0, rest=2, win_pct=0.5):
    return {"games_sampled": 10, "win_pct": win_pct, "avg_point_diff": diff,
            "avg_points_scored": scored, "avg_points_allowed": allowed,
            "days_since_last_game": rest}


GAME = {"id": 1, "date": "2026-11-11", "state": "scheduled",
        "home_team": {"id": 1, "full_name": "Home Hawks"},
        "visitor_team": {"id": 2, "full_name": "Away Bulls"}}


# ---------------------------------------------------------------- client

def test_missing_key_is_rejected():
    with pytest.raises(ns.NbaAccessError):
        ns.BalldontlieClient("")
    with pytest.raises(ns.NbaAccessError):
        ns.BalldontlieClient("PASTE_YOUR_KEY_HERE")


def test_key_sent_without_bearer():
    client, _ = make_client([FakeResponse(200, {"data": []})])
    client.get("/games")
    assert client._session.calls[0][2] == {"Authorization": "key"}


def test_requests_are_paced_13_seconds_apart():
    # clock reads, in order: after request 1 (100), while pacing request 2 (105), after request 2 (105)
    client, sleeps = make_client(
        [FakeResponse(200, {}), FakeResponse(200, {})], clock_times=[100.0, 105.0, 105.0])
    client.get("/a")
    client.get("/b")
    assert sleeps == [pytest.approx(8.0)]  # 13s minimum - 5s already elapsed


def test_429_backs_off_15_then_30_seconds():
    client, sleeps = make_client(
        [FakeResponse(429), FakeResponse(429), FakeResponse(200, {"ok": 1})])
    assert client.get("/a") == {"ok": 1}
    assert [s for s in sleeps if s in (15, 30)] == [15, 30]


def test_network_error_retries_then_raises_specific_error():
    errs = [requests.Timeout("t")] * (ns.MAX_RETRIES + 1)
    client, _ = make_client(errs)
    with pytest.raises(ns.NbaDataError):
        client.get("/a")


@pytest.mark.parametrize("status", [401, 403])
def test_access_errors_not_retried(status):
    client, _ = make_client([FakeResponse(status)])
    with pytest.raises(ns.NbaAccessError):
        client.get("/a")
    assert len(client._session.calls) == 1


def test_non_json_raises():
    client, _ = make_client([FakeResponse(200, None)])
    with pytest.raises(ns.NbaDataError):
        client.get("/a")


def test_pagination_follows_cursor():
    client, _ = make_client([
        FakeResponse(200, {"data": [1, 2], "meta": {"next_cursor": 99}}),
        FakeResponse(200, {"data": [3], "meta": {}}),
    ])
    assert client.get_all_pages("/games", {"per_page": 100}) == [1, 2, 3]
    assert client._session.calls[1][1]["cursor"] == 99


# ---------------------------------------------------------------- games + form

def test_normalize_final_and_scheduled():
    final = ns.normalize_game(raw_game(1, "2026-11-10", 1, 2, 110, 100))
    assert final["status"] == "post" and final["home_score"] == 110 and final["away_score"] == 100
    sched = ns.normalize_game(raw_game(2, "2026-11-11", 1, 2, state="scheduled"))
    assert sched["status"] == "scheduled"
    assert ns.normalize_game({"home_team": {}, "visitor_team": {"id": 2}}) is None


def test_is_upcoming_window():
    soon = ns.normalize_game(raw_game(1, "2026-11-11", 1, 2, state="scheduled", dt="2026-11-12T00:00:00Z"))
    tomorrows_slate = ns.normalize_game(raw_game(2, "2026-11-12", 1, 2, state="scheduled", dt="2026-11-13T00:00:00Z"))
    started = ns.normalize_game(raw_game(3, "2026-11-11", 1, 2, state="in_progress", dt="2026-11-11T13:00:00Z"))
    assert ns.is_upcoming(soon, NOW) is True
    assert ns.is_upcoming(tomorrows_slate, NOW) is False
    assert ns.is_upcoming(started, NOW) is False


def history(team=1, n=10, own=120, opp=110, last="2026-11-10"):
    out = []
    last_date = datetime.date.fromisoformat(last)
    for i in range(n):
        d = (last_date - datetime.timedelta(days=2 * i)).isoformat()
        out.append(ns.normalize_game(raw_game(100 + i, d, team, 50 + i, own, opp)))
    return out


def test_compute_form_numbers_and_back_to_back():
    result = ns.compute_team_form(history(), 1, TODAY)
    assert result["games_sampled"] == 10
    assert result["win_pct"] == 1.0
    assert result["avg_point_diff"] == 10.0
    assert result["avg_points_scored"] == 120.0
    assert result["days_since_last_game"] == 1  # played yesterday = back-to-back


def test_form_none_when_too_few_games():
    assert ns.compute_team_form(history(n=4), 1, TODAY) is None


def test_form_ignores_games_without_scores():
    games = history(n=6)
    games[0]["home_score"] = None
    assert ns.compute_team_form(games, 1, TODAY)["games_sampled"] == 5


def test_stats_filters_today_and_loads_history_once():
    todays = [raw_game(1, "2026-11-11", 1, 2, state="scheduled", dt="2026-11-12T00:00:00Z"),
              raw_game(2, "2026-11-12", 3, 4, state="scheduled", dt="2026-11-13T01:00:00Z")]
    past = [raw_game(10 + i, f"2026-11-{1 + i:02d}", 1, 9, 110, 100) for i in range(8)]
    client, _ = make_client([FakeResponse(200, {"data": todays}), FakeResponse(200, {"data": past})])
    stats = ns.NbaStats(client, today=TODAY, now=NOW)
    games = stats.todays_games()
    assert [g["id"] for g in games] == [1]
    stats.team_form(1)
    stats.team_form(9)
    assert len(client._session.calls) == 2  # games + ONE history download


def test_history_window_never_starts_before_opening_night(monkeypatch):
    monkeypatch.delenv("NBA_SEASON_START", raising=False)
    client, _ = make_client([FakeResponse(200, {"data": []})])
    ns.NbaStats(client, today=TODAY, now=NOW).team_form(1)
    assert client._session.calls[0][1]["start_date"] == "2026-10-20"  # not 35 days back


# ---------------------------------------------------------------- odds helpers

def test_names_match_handles_la_vs_los_angeles_and_rejects_neighbours():
    assert nof.names_match("LA Clippers", "Los Angeles Clippers")
    assert nof.names_match("Portland Trail Blazers", "Trail Blazers")
    assert not nof.names_match("Los Angeles Lakers", "Los Angeles Clippers")
    assert not nof.names_match("New York Knicks", "Brooklyn Nets")
    assert not nof.names_match("", "Boston Celtics")


def test_sport_lookup_needs_exact_nba_not_wnba(monkeypatch):
    monkeypatch.setattr(nof, "_nba_sport_id", None)
    monkeypatch.setattr(rc, "_get_all_sports", lambda: [{"id": 8, "name": "WNBA"}, {"id": 4, "name": "NBA"}])
    assert nof._get_nba_sport_id() == 4


def test_sport_lookup_falls_back_when_listing_fails(monkeypatch):
    monkeypatch.setattr(nof, "_nba_sport_id", None)

    def boom():
        raise requests.ConnectionError("down")

    monkeypatch.setattr(rc, "_get_all_sports", boom)
    assert nof._get_nba_sport_id() == nof.NBA_FALLBACK_SPORT_ID


def test_find_event_matches_by_nickname():
    events = [{"event_id": "e", "teams": [{"name": "Chicago Bulls"}, {"name": "Atlanta Hawks"}]}]
    assert nof.find_event(events, "Atlanta Hawks", "Chicago Bulls") is events[0]
    assert nof.find_event(events, "Boston Celtics", "Chicago Bulls") is None


# ---------------------------------------------------------------- model

def test_equal_teams_get_only_home_court():
    assert nm.predict_margin(form(), form()) == nm.HOME_COURT_POINTS
    assert nm.margin_to_home_win_prob(nm.HOME_COURT_POINTS) == pytest.approx(0.583, abs=0.005)


def test_form_gap_is_shrunk_not_taken_at_face_value():
    assert nm.predict_margin(form(diff=10), form(diff=0)) == pytest.approx(2.5 + 0.6 * 10)


def test_back_to_back_rules():
    assert nm.predict_margin(form(rest=2), form(rest=1)) == nm.HOME_COURT_POINTS + nm.B2B_POINTS
    assert nm.predict_margin(form(rest=1), form(rest=2)) == nm.HOME_COURT_POINTS - nm.B2B_POINTS
    assert nm.predict_margin(form(rest=1), form(rest=1)) == nm.HOME_COURT_POINTS
    assert nm.predict_margin(form(rest=None), form(rest=1)) == nm.HOME_COURT_POINTS


def test_devig_removes_bookmaker_margin():
    assert nm.devig_home_prob(1.909, 1.909) == pytest.approx(0.5)
    assert nm.devig_home_prob(None, 2.0) is None


def test_blend_pulls_extreme_model_toward_market():
    strong, weak = form(diff=10), form(diff=-10)
    final, model_p, market_p, _ = nm.blended_home_prob(strong, weak, 1.50, 2.60)
    assert model_p > final > market_p


def test_moneyline_tip_only_when_view_differs_from_market():
    strong, weak = form(diff=10), form(diff=-10)
    tip = nm.find_moneyline_tip(GAME, strong, weak, 1.50, 2.60)
    assert tip["team"] == "Home Hawks" and tip["league"] == "NBA" and tip["edge"] >= 0.03
    # market already agrees with us -> no tip
    assert nm.find_moneyline_tip(GAME, strong, weak, 1.10, 8.0) is None
    # equal teams, fair market -> no tip
    assert nm.find_moneyline_tip(GAME, form(), form(), 1.85, 1.95) is None


def test_model_total_and_fatigue():
    assert nm.model_total(form(scored=115, allowed=110), form(scored=112, allowed=118)) == pytest.approx(
        (115 + 118) / 2 + (112 + 110) / 2)
    assert nm.model_total(form(rest=1), form()) == pytest.approx(228 - nm.FATIGUE_TOTAL_PENALTY)


def totals(line, over=1.91, under=1.91):
    return {"line": line, "over_odds": over, "under_odds": under, "market_id": 3}


def test_main_line_is_the_most_balanced_and_plausible():
    odds = [totals(215.5, 1.45, 2.80), totals(225.5), totals(235.5, 2.80, 1.45), totals(55.5)]
    assert nm.pick_main_line(odds) == 225.5
    assert nm.pick_main_line([totals(55.5)]) is None


def test_anchoring_moves_30_percent_toward_our_number():
    assert nm.anchored_total(240.0, 220.0) == pytest.approx(226.0)
    assert nm.anchored_total(240.0, None) == 240.0


def test_totals_tip_needs_a_big_disagreement_with_market():
    hot = form(scored=120, allowed=120)
    tip = nm.find_totals_tip(GAME, hot, hot, [totals(220.0)])
    assert tip["side"] == "over" and tip["league"] == "NBA" and tip["model_total"] == 240.0
    normal = form(scored=114, allowed=114)
    assert nm.find_totals_tip(GAME, normal, normal, [totals(226.0)]) is None
    assert nm.find_totals_tip(GAME, hot, hot, []) is None


def test_prediction_without_market_uses_model_only():
    p = nm.build_prediction(GAME, form(diff=5), form(diff=-5))
    assert p["used_market"] is False and p["market_main_line"] is None
    assert p["favorite"] == "Home Hawks" and 0.5 < p["favorite_prob"] < 1


def test_digest_splits_long_lists_and_stays_plain_text():
    preds = [nm.build_prediction(GAME, form(diff=5), form(diff=-5), {"home_odds": 1.6, "away_odds": 2.4},
                                 [totals(226.5)]) for _ in range(40)]
    messages = nm.format_digest(preds, "2026-11-11")
    assert len(messages) > 1
    assert all(len(m) <= nm.MAX_MESSAGE_CHARS + 400 for m in messages)
    assert "Home Hawks" in messages[0] and "*" not in messages[0]


# ---------------------------------------------------------------- runner

def deps(games=None, forms=None, odds=None, totals_odds=None, games_error=None):
    forms = forms if forms is not None else {1: form(diff=10), 2: form(diff=-10)}

    def get_games():
        if games_error:
            raise games_error
        return games if games is not None else [GAME]

    return {
        "get_games": get_games,
        "get_form": lambda team_id, as_of: forms.get(team_id),
        "get_match_odds": lambda h, a, d: odds,
        "get_totals_odds": lambda h, a, d: totals_odds,
        "debug_status": lambda h, a, d: "debug",
    }


def test_runner_full_flow_returns_tip_and_prediction():
    tips, preds = nr.run_nba("2026-11-11", deps(
        odds={"home_odds": 1.50, "away_odds": 2.60}, totals_odds=[totals(226.5)]))
    assert len(preds) == 1 and any(t["type"] == "moneyline" for t in tips)


def test_runner_no_games_and_games_unavailable_are_graceful():
    assert nr.run_nba("2026-11-11", deps(games=[])) == ([], [])
    assert nr.run_nba("2026-11-11", deps(games_error=ns.NbaAccessError("bad key"))) == ([], [])


def test_runner_skips_game_without_enough_form():
    assert nr.run_nba("2026-11-11", deps(forms={1: form()})) == ([], [])


def test_runner_still_predicts_when_odds_feed_fails():
    d = deps()

    def odds_down(h, a, day):
        raise rc.RundownAuthError("no key")

    d["get_match_odds"] = odds_down
    d["get_totals_odds"] = odds_down
    tips, preds = nr.run_nba("2026-11-11", d)
    assert tips == [] and len(preds) == 1 and preds[0]["used_market"] is False


def test_runner_bad_data_in_one_game_does_not_stop_the_next():
    second = dict(GAME, id=2, home_team={"id": 3, "full_name": "Home Jazz"},
                  visitor_team={"id": 4, "full_name": "Away Suns"})
    broken = {1: {"win_pct": 0.5}, 2: form(), 3: form(diff=5), 4: form(diff=-5)}  # team 1 lacks fields
    _tips, preds = nr.run_nba("2026-11-11", deps(games=[GAME, second], forms=broken))
    assert [p["home"] for p in preds] == ["Home Jazz"]


def test_select_tips_ranks_and_caps():
    tips = [{"our_estimated_prob": p, "edge": 0.05} for p in (0.61, 0.9, 0.7, 0.65, 0.8, 0.75, 0.62)]
    picked = nr.select_tips(tips)
    assert len(picked) == nm.NBA_MAX_TIPS_PER_DAY
    assert [t["our_estimated_prob"] for t in picked] == [0.9, 0.8, 0.75, 0.7, 0.65]


def test_send_digest_sends_each_message_and_nothing_when_empty():
    sent, naps = [], []
    assert nr.send_digest([], "2026-11-11", send=sent.append, sleep=naps.append) == 0 and sent == []
    preds = [nm.build_prediction(GAME, form(diff=5), form(diff=-5)) for _ in range(60)]
    count = nr.send_digest(preds, "2026-11-11", send=sent.append, sleep=naps.append)
    assert count == len(sent) > 1 and len(naps) == count - 1


# ---------------------------------------------------------------- integration

def test_end_to_end_real_stats_class_with_fake_balldontlie_and_fake_odds():
    """Real NbaStats + real model + real runner; only the network is faked."""
    tonight = raw_game(1, "2026-11-11", 1, 2, state="scheduled", dt="2026-11-12T00:00:00Z")
    past = []
    for i in range(8):
        day = f"2026-11-{9 - i:02d}"
        past.append(raw_game(10 + i, day, 1, 20 + i, 125, 105))      # team 1 wins big
        past.append(raw_game(30 + i, day, 40 + i, 2, 125, 105))      # team 2 loses big (away)
    client, _ = make_client([FakeResponse(200, {"data": [tonight]}), FakeResponse(200, {"data": past})])
    stats = ns.NbaStats(client, today=TODAY, now=NOW)
    real = {
        "get_games": stats.todays_games,
        "get_form": stats.team_form,
        "get_match_odds": lambda h, a, d: {"home_odds": 1.50, "away_odds": 2.60},
        "get_totals_odds": lambda h, a, d: [totals(226.5)],
        "debug_status": lambda h, a, d: "",
    }
    tips, preds = nr.run_nba("2026-11-11", real)
    assert len(preds) == 1
    assert preds[0]["favorite"] == "Team 1" and preds[0]["predicted_margin"] > 10
    assert tips and tips[0]["team"] == "Team 1"
