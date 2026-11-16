"""
Tests for the NBA pipeline (stats fetcher, odds reading, model, runner).
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
from config import MIN_EDGE_SUBMARKET

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


def raw_game(gid, date, home, away, hs=None, vs=None, state="final", dt=None, hq=None, vq=None):
    raw = {
        "id": gid, "date": date, "datetime": dt, "status": "Final" if state == "final" else "7:00 pm ET",
        "status_state": state, "home_team_score": hs, "visitor_team_score": vs,
        "home_team": {"id": home, "full_name": f"Team {home}"},
        "visitor_team": {"id": away, "full_name": f"Team {away}"},
    }
    for i, value in enumerate(hq or [], start=1):
        raw[f"home_q{i}"] = value
    for i, value in enumerate(vq or [], start=1):
        raw[f"visitor_q{i}"] = value
    return raw


def form(diff=0.0, scored=114.0, allowed=114.0, rest=2, win_pct=0.5, q_scored=None, q_allowed=None):
    return {"games_sampled": 10, "win_pct": win_pct, "avg_point_diff": diff,
            "avg_points_scored": scored, "avg_points_allowed": allowed,
            "days_since_last_game": rest, "q_scored": q_scored, "q_allowed": q_allowed}


def qform(per_quarter=28.0, diff=0.0, rest=2):
    """A team that scores AND allows `per_quarter` in every quarter (so 2 quarters = 56, game = 112)."""
    return form(diff=diff, scored=per_quarter * 4, allowed=per_quarter * 4, rest=rest,
                q_scored=[per_quarter] * 4, q_allowed=[per_quarter] * 4)


GAME = {"id": 1, "date": "2026-11-11", "state": "scheduled",
        "home_team": {"id": 1, "full_name": "Home Hawks"},
        "visitor_team": {"id": 2, "full_name": "Away Bulls"}}


def totals(line, over=1.91, under=1.91):
    return {"line": line, "over_odds": over, "under_odds": under, "market_id": 3}


def market(ml=None, spread=None, totals_=None):
    return {"moneyline": ml, "spread": spread or [], "totals": totals_ or []}


def spread_pair(home_line, home_odds=1.91, away_odds=1.91):
    return [{"side": "home", "line": home_line, "odds": home_odds},
            {"side": "away", "line": -home_line, "odds": away_odds}]


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


def test_normalize_reads_quarter_scores_or_none():
    with_q = ns.normalize_game(raw_game(1, "2026-11-10", 1, 2, 110, 100, hq=[30, 28, 25, 27], vq=[20, 25, 30, 25]))
    assert with_q["home_q"] == [30, 28, 25, 27] and with_q["away_q"] == [20, 25, 30, 25]
    partial = ns.normalize_game(raw_game(1, "2026-11-10", 1, 2, 110, 100, hq=[30, 28], vq=[20, 25, 30, 25]))
    assert partial["home_q"] is None
    assert ns.normalize_game(raw_game(1, "2026-11-10", 1, 2, 110, 100))["home_q"] is None


def test_is_upcoming_window():
    soon = ns.normalize_game(raw_game(1, "2026-11-11", 1, 2, state="scheduled", dt="2026-11-12T00:00:00Z"))
    tomorrows_slate = ns.normalize_game(raw_game(2, "2026-11-12", 1, 2, state="scheduled", dt="2026-11-13T00:00:00Z"))
    started = ns.normalize_game(raw_game(3, "2026-11-11", 1, 2, state="in_progress", dt="2026-11-11T13:00:00Z"))
    assert ns.is_upcoming(soon, NOW) is True
    assert ns.is_upcoming(tomorrows_slate, NOW) is False
    assert ns.is_upcoming(started, NOW) is False


def history(team=1, n=10, own=120, opp=110, last="2026-11-10", with_quarters=False):
    out = []
    last_date = datetime.date.fromisoformat(last)
    for i in range(n):
        d = (last_date - datetime.timedelta(days=2 * i)).isoformat()
        hq = [30, 30, 30, 30] if with_quarters else None
        vq = [27, 27, 27, 29] if with_quarters else None
        out.append(ns.normalize_game(raw_game(100 + i, d, team, 50 + i, own, opp, hq=hq, vq=vq)))
    return out


def test_compute_form_numbers_and_back_to_back():
    result = ns.compute_team_form(history(), 1, TODAY)
    assert result["games_sampled"] == 10
    assert result["win_pct"] == 1.0
    assert result["avg_point_diff"] == 10.0
    assert result["avg_points_scored"] == 120.0
    assert result["days_since_last_game"] == 1  # played yesterday = back-to-back
    assert result["q_scored"] is None  # no quarter data supplied


def test_compute_form_quarter_averages():
    result = ns.compute_team_form(history(with_quarters=True), 1, TODAY)
    assert result["q_scored"] == [30, 30, 30, 30]
    assert result["q_allowed"] == [27, 27, 27, 29]


def test_quarter_averages_none_when_too_little_quarter_data():
    games = history(with_quarters=True)
    for g in games[:7]:
        g["home_q"] = None
    result = ns.compute_team_form(games, 1, TODAY)
    assert result["q_scored"] is None and result["games_sampled"] == 10


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


# ---------------------------------------------------------------- odds reading

def price_line(value, american):
    return {"value": value, "prices": {"19": {"price": american}}}


def market_block(market_id, period_id, participants):
    block = {"market_id": market_id, "participants": [{"name": n, "lines": lines} for n, lines in participants]}
    if period_id is not None:
        block["period_id"] = period_id
    return block


EVENT = {"event_id": "e", "teams": [{"name": "Chicago Bulls"}, {"name": "Atlanta Hawks"}], "markets": [
    market_block(1, 0, [("Atlanta Hawks", [price_line(None, -150)]), ("Chicago Bulls", [price_line(None, 130)])]),
    market_block(2, 0, [("Atlanta Hawks", [price_line(-5.5, -110)]), ("Chicago Bulls", [price_line(5.5, -110)])]),
    market_block(3, 0, [("Over", [price_line(226.5, -110)]), ("Under", [price_line(226.5, -110)])]),
    market_block(3, 1, [("Over", [price_line(112.5, -115)]), ("Under", [price_line(112.5, -105)])]),
]}


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
    events = [EVENT]
    assert nof.find_event(events, "Atlanta Hawks", "Chicago Bulls") is EVENT
    assert nof.find_event(events, "Boston Celtics", "Chicago Bulls") is None


def test_untagged_market_counts_as_full_game_only_never_a_half():
    event = {"markets": [market_block(3, None, [("Over", [price_line(226.5, -110)])])]}
    assert list(nof.iter_period_prices(event, 3, 0))
    assert not list(nof.iter_period_prices(event, 3, 1))
    assert not list(nof.iter_period_prices(event, 3, 3))


def test_read_winner_market_best_prices_and_decimal_conversion():
    ml = nof.read_winner_market(EVENT, 0, "Atlanta Hawks", "Chicago Bulls")
    assert ml["home_odds"] == pytest.approx(1.6667, abs=1e-3)
    assert ml["away_odds"] == pytest.approx(2.3)


def test_three_way_winner_market_is_skipped():
    event = {"markets": [market_block(1, 3, [
        ("Atlanta Hawks", [price_line(None, 120)]), ("Chicago Bulls", [price_line(None, 140)]),
        ("Tie", [price_line(None, 1500)])])]}
    assert nof.read_winner_market(event, 3, "Atlanta Hawks", "Chicago Bulls") is None


def test_read_handicap_market_sides_and_lines():
    entries = nof.read_handicap_market(EVENT, 0, "Atlanta Hawks", "Chicago Bulls")
    assert {(e["side"], e["line"]) for e in entries} == {("home", -5.5), ("away", 5.5)}


def test_read_totals_market_per_period():
    full = nof.read_totals_market(EVENT, 0)
    half = nof.read_totals_market(EVENT, 1)
    assert full[0]["line"] == 226.5 and half[0]["line"] == 112.5


def test_read_all_periods_only_lists_periods_with_data():
    markets = nof.read_all_periods(EVENT, "Atlanta Hawks", "Chicago Bulls")
    assert set(markets) == {"full", "h1"}
    assert markets["h1"]["moneyline"] is None and markets["h1"]["totals"]


def test_get_game_markets_and_unknown_game(monkeypatch):
    monkeypatch.setattr(nof, "_events_for_date", lambda d: [EVENT])
    assert "full" in nof.get_game_markets("Atlanta Hawks", "Chicago Bulls", "2026-11-11")
    assert nof.get_game_markets("Boston Celtics", "Miami Heat", "2026-11-11") == {}


# ---------------------------------------------------------------- model: periods and margin

def test_period_points_and_shares():
    f = form(q_scored=[28, 29, 27, 30], q_allowed=[26, 27, 25, 28])
    assert nm.period_points(f, "full") == (114.0, 114.0)
    assert nm.period_points(f, "h1") == (57, 53)
    assert nm.period_points(f, "q3") == (27, 25)
    assert nm.period_points(form(), "h1") is None


def test_equal_teams_get_home_court_scaled_by_period():
    assert nm.predict_margin(form(), form()) == nm.HOME_COURT_POINTS
    assert nm.period_margin(qform(), qform(), "h1") == pytest.approx(1.25)
    assert nm.period_margin(qform(), qform(), "q1") == pytest.approx(0.625)
    assert nm.margin_to_home_win_prob(nm.HOME_COURT_POINTS) == pytest.approx(0.583, abs=0.005)


def test_shorter_periods_trust_form_less_and_have_smaller_spread():
    home = form(diff=10, q_scored=[30, 30, 30, 30], q_allowed=[27.5] * 4)   # +2.5 per quarter
    away = qform()
    assert nm.predict_margin(home, away) == pytest.approx(2.5 + 0.6 * 10)
    assert nm.period_margin(home, away, "q1") == pytest.approx(0.625 + 0.25 * 2.5)
    assert nm.period_margin(home, away, "h1") == pytest.approx(1.25 + 0.4 * 5)
    assert nm.period_sigmas("q1") == (pytest.approx(6.0), pytest.approx(9.0))
    assert nm.period_sigmas("full") == (12.0, 18.0)


def test_period_view_needs_quarter_data_for_halves_and_quarters():
    assert nm.period_view(form(), form(), "h1") is None
    assert nm.period_view(qform(), qform(), "q2") is not None


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


def test_market_margin_comes_from_the_most_even_handicap_line():
    entries = spread_pair(-5.5) + spread_pair(-6.5, home_odds=2.1, away_odds=1.75)
    assert nm.market_margin_from_spread(entries) == 5.5
    assert nm.market_margin_from_spread([]) is None
    assert nm.market_margin_from_spread([{"side": "home", "line": -3, "odds": 1.9}]) is None


def test_blend_pulls_extreme_model_toward_market():
    strong, weak = form(diff=10), form(diff=-10)
    final, model_p, market_p, _ = nm.blended_home_prob(strong, weak, 1.50, 2.60)
    assert model_p > final > market_p


# ---------------------------------------------------------------- model: tips

def test_moneyline_tip_only_when_view_differs_from_market():
    strong, weak = form(diff=10), form(diff=-10)
    tip = nm.find_moneyline_tip(GAME, strong, weak, 1.50, 2.60)
    assert tip["team"] == "Home Hawks" and tip["league"] == "NBA" and tip["edge"] >= 0.03
    assert tip["period_label"] == "full game"
    assert nm.find_moneyline_tip(GAME, strong, weak, 1.10, 8.0) is None
    assert nm.find_moneyline_tip(GAME, form(), form(), 1.85, 1.95) is None


def test_model_total_and_fatigue():
    assert nm.model_total(form(scored=115, allowed=110), form(scored=112, allowed=118)) == pytest.approx(
        (115 + 118) / 2 + (112 + 110) / 2)
    assert nm.model_total(form(rest=1), form()) == pytest.approx(228 - nm.FATIGUE_TOTAL_PENALTY)
    assert nm.model_total(qform(rest=1), qform(), "q1") == pytest.approx(56 - nm.FATIGUE_TOTAL_PENALTY * 0.25)


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


def test_handicap_tip_when_we_expect_a_bigger_win_than_the_line():
    strong, weak = form(diff=10), form(diff=-10)
    tips = nm.find_period_tips(GAME, strong, weak, "full", market(spread=spread_pair(-6.5)))
    handicaps = [t for t in tips if t["type"] == "handicap"]
    assert len(handicaps) == 1
    tip = handicaps[0]
    assert tip["side"] == "home" and tip["line"] == -6.5 and tip["team"] == "Home Hawks"
    assert tip["our_estimated_prob"] >= 0.6 and tip["edge"] >= 0.03


def test_no_handicap_tip_when_model_agrees_with_market_or_line_is_absurd():
    assert nm.find_period_tips(GAME, form(), form(), "full", market(spread=spread_pair(-2.5))) == []
    strong, weak = form(diff=10), form(diff=-10)
    assert nm.find_period_tips(GAME, strong, weak, "full", market(spread=spread_pair(-40.0))) == []


def test_half_total_tip_and_its_stricter_edge_rule():
    hot = qform(per_quarter=30.0)           # first-half own total = 120
    tips = nm.find_period_tips(GAME, hot, hot, "h1", market(totals_=[totals(105.0)]))
    assert [t["side"] for t in tips] == ["over"]
    assert tips[0]["period_label"] == "1st half" and tips[0]["edge"] >= MIN_EDGE_SUBMARKET
    # a gap that would be enough for the full game is NOT enough for a half
    mild = qform(per_quarter=28.0)          # own half total 112
    assert nm.find_period_tips(GAME, mild, mild, "h1", market(totals_=[totals(111.0)])) == []


def test_quarter_winner_tip_uses_quarter_numbers():
    home = form(diff=10, q_scored=[33] * 4, q_allowed=[23] * 4)
    away = form(diff=-10, q_scored=[23] * 4, q_allowed=[33] * 4)
    tips = nm.find_period_tips(GAME, home, away, "q1", market(ml={"home_odds": 1.55, "away_odds": 2.5}))
    assert tips and tips[0]["type"] == "moneyline" and tips[0]["period"] == "q1"
    assert tips[0]["edge"] >= MIN_EDGE_SUBMARKET


def test_find_all_tips_covers_every_period_that_has_a_market():
    hot = qform(per_quarter=30.0)
    markets = {"full": market(totals_=[totals(210.0)]), "h1": market(totals_=[totals(105.0)]),
               "q4": {}}  # empty market is ignored
    periods = {t["period"] for t in nm.find_all_tips(GAME, hot, hot, markets)}
    assert periods == {"full", "h1"}


# ---------------------------------------------------------------- predictions + digest

FULL_MARKET = market(ml={"home_odds": 1.60, "away_odds": 2.40}, totals_=[totals(226.5)])


def test_prediction_without_market_uses_model_only():
    p = nm.build_prediction(GAME, form(diff=10), form(diff=-10))
    assert p["used_market"] is False and p["market_main_line"] is None
    assert p["favorite"] == "Home Hawks" and 0.5 < p["favorite_prob"] < 1
    assert p["favorite_odds"] is None and p["favorite_edge"] is None
    assert p["predicted_home_score"] > p["predicted_away_score"]
    assert p["predicted_home_score"] + p["predicted_away_score"] == pytest.approx(p["anchored_total"], abs=1)


def test_prediction_reports_odds_implied_chance_and_edge():
    p = nm.build_prediction(GAME, form(diff=10), form(diff=-10), FULL_MARKET, ["full"])
    assert p["favorite_odds"] == 1.60
    assert p["favorite_implied_prob"] == pytest.approx(0.625)
    assert p["favorite_edge"] == pytest.approx(p["favorite_prob"] - 0.625, abs=1e-3)


def test_digest_uses_win_chance_and_edge_not_margin():
    p = nm.build_prediction(GAME, form(diff=10), form(diff=-10), FULL_MARKET, ["full", "h1"])
    text = nm.format_digest([p], "2026-11-11")[0]
    assert "win chance" in text and "edge" in text.lower() and "Predicted score" in text
    assert "margin" not in text.lower()
    assert "Home Hawks" in text and "bookmaker odds 1.6" in text
    assert "*" not in text


def test_digest_flags_no_value_and_missing_line():
    even = nm.build_prediction(GAME, form(), form(), market(ml={"home_odds": 1.40, "away_odds": 3.0}), ["full", "h1"])
    assert "no value" in nm.format_digest([even], "d")[0]
    bare = nm.build_prediction(GAME, form(), form())
    assert "no bookmaker line found" in nm.format_digest([bare], "d")[0]


def test_digest_notes_when_only_full_game_lines_exist():
    only_full = nm.build_prediction(GAME, form(), form(), FULL_MARKET, ["full"])
    with_half = nm.build_prediction(GAME, form(), form(), FULL_MARKET, ["full", "h1"])
    assert "no half or quarter lines" in nm.format_digest([only_full], "d")[-1]
    assert "no half or quarter lines" not in nm.format_digest([with_half], "d")[-1]
    no_odds = nm.build_prediction(GAME, form(), form())
    assert "no half or quarter lines" not in nm.format_digest([no_odds], "d")[-1]


def test_digest_splits_long_lists_and_stays_plain_text():
    preds = [nm.build_prediction(GAME, form(diff=5), form(diff=-5), FULL_MARKET, ["full"]) for _ in range(40)]
    messages = nm.format_digest(preds, "2026-11-11")
    assert len(messages) > 1
    assert all(len(m) <= nm.MAX_MESSAGE_CHARS + 600 for m in messages)
    assert "Home Hawks" in messages[0] and "*" not in messages[0]


# ---------------------------------------------------------------- runner

def deps(games=None, forms=None, markets=None, games_error=None):
    forms = forms if forms is not None else {1: form(diff=10), 2: form(diff=-10)}

    def get_games():
        if games_error:
            raise games_error
        return games if games is not None else [GAME]

    return {
        "get_games": get_games,
        "get_form": lambda team_id, as_of: forms.get(team_id),
        "get_markets": lambda h, a, d: markets if markets is not None else {},
        "debug_status": lambda h, a, d: "debug",
    }


def test_runner_full_flow_returns_tip_and_prediction():
    tips, preds = nr.run_nba("2026-11-11", deps(markets={"full": FULL_MARKET}))
    assert len(preds) == 1 and any(t["type"] == "moneyline" for t in tips)
    assert preds[0]["periods_available"] == ["full"]


def test_runner_no_games_and_games_unavailable_are_graceful():
    assert nr.run_nba("2026-11-11", deps(games=[])) == ([], [])
    assert nr.run_nba("2026-11-11", deps(games_error=ns.NbaAccessError("bad key"))) == ([], [])


def test_runner_skips_game_without_enough_form():
    assert nr.run_nba("2026-11-11", deps(forms={1: form()})) == ([], [])


def test_runner_still_predicts_when_odds_feed_fails():
    d = deps()

    def odds_down(h, a, day):
        raise rc.RundownAuthError("no key")

    d["get_markets"] = odds_down
    tips, preds = nr.run_nba("2026-11-11", d)
    assert tips == [] and len(preds) == 1 and preds[0]["used_market"] is False


def test_runner_bad_data_in_one_game_does_not_stop_the_next():
    second = dict(GAME, id=2, home_team={"id": 3, "full_name": "Home Jazz"},
                  visitor_team={"id": 4, "full_name": "Away Suns"})
    broken = {1: {"win_pct": 0.5}, 2: form(), 3: form(diff=5), 4: form(diff=-5)}  # team 1 lacks fields
    _tips, preds = nr.run_nba("2026-11-11", deps(games=[GAME, second], forms=broken))
    assert [p["home"] for p in preds] == ["Home Jazz"]


def test_runner_finds_period_tips_when_quarter_data_and_lines_exist():
    hot_home, hot_away = qform(per_quarter=30.0), qform(per_quarter=30.0)
    markets = {"full": market(totals_=[totals(240.0)]), "h1": market(totals_=[totals(105.0)])}
    tips, preds = nr.run_nba("2026-11-11", deps(forms={1: hot_home, 2: hot_away}, markets=markets))
    assert {t["period"] for t in tips} == {"h1"}
    assert preds[0]["periods_available"] == ["full", "h1"]


def test_select_tips_ranks_and_caps():
    tips = [{"our_estimated_prob": p, "edge": 0.05} for p in (0.61, 0.9, 0.7, 0.65, 0.8, 0.75, 0.62, 0.66, 0.68)]
    picked = nr.select_tips(tips)
    assert len(picked) == nm.NBA_MAX_TIPS_PER_DAY == 8
    assert [t["our_estimated_prob"] for t in picked][:3] == [0.9, 0.8, 0.75]


def test_send_digest_sends_each_message_and_nothing_when_empty():
    sent, naps = [], []
    assert nr.send_digest([], "2026-11-11", send=sent.append, sleep=naps.append) == 0 and sent == []
    preds = [nm.build_prediction(GAME, form(diff=5), form(diff=-5), FULL_MARKET, ["full"]) for _ in range(60)]
    count = nr.send_digest(preds, "2026-11-11", send=sent.append, sleep=naps.append)
    assert count == len(sent) > 1 and len(naps) == count - 1


# ---------------------------------------------------------------- integration

def test_end_to_end_real_stats_class_with_fake_balldontlie_and_odds_event():
    """Real NbaStats + real odds reader + real model + real runner; only the network is faked."""
    tonight = raw_game(1, "2026-11-11", 1, 2, state="scheduled", dt="2026-11-12T00:00:00Z")
    tonight["home_team"]["full_name"] = "Atlanta Hawks"
    tonight["visitor_team"]["full_name"] = "Chicago Bulls"
    past = []
    for i in range(8):
        day = f"2026-11-{9 - i:02d}"
        past.append(raw_game(10 + i, day, 1, 20 + i, 125, 105, hq=[31, 31, 31, 32], vq=[26, 26, 26, 27]))
        past.append(raw_game(30 + i, day, 40 + i, 2, 125, 105, hq=[31, 31, 31, 32], vq=[26, 26, 26, 27]))
    client, _ = make_client([FakeResponse(200, {"data": [tonight]}), FakeResponse(200, {"data": past})])
    stats = ns.NbaStats(client, today=TODAY, now=NOW)
    real = {
        "get_games": stats.todays_games,
        "get_form": stats.team_form,
        "get_markets": lambda h, a, d: nof.read_all_periods(EVENT, h, a),
        "debug_status": lambda h, a, d: "",
    }
    tips, preds = nr.run_nba("2026-11-11", real)
    assert len(preds) == 1
    assert preds[0]["favorite"] == "Atlanta Hawks" and preds[0]["predicted_home_score"] > preds[0]["predicted_away_score"]
    assert preds[0]["periods_available"] == ["full", "h1"]
    assert any(t["type"] in ("moneyline", "handicap") for t in tips)


# ---------------------------------------------------------------- dedicated half/quarter market ids

def test_discovery_adds_unknown_quarter_ids_and_ignores_live_and_team_markets(monkeypatch):
    monkeypatch.setattr(nof, "_market_table", None)
    monkeypatch.setattr(rc, "_get", lambda path, params=None, retries=3: [
        {"id": 1012, "name": "moneyline_first_quarter", "period_id": 3},
        {"id": 1013, "name": "totals_first_quarter", "period_id": 3},
        {"id": 2000, "name": "live_totals_first_quarter", "period_id": 3},
        {"id": 2001, "name": "team_totals_first_half", "period_id": 1},
    ])
    table = nof.discover_period_markets()
    assert table[1012] == ("q1", "winner") and table[1013] == ("q1", "total")
    assert 2000 not in table and 2001 not in table and table[6] == ("h1", "total")


def test_discovery_failure_falls_back_to_known_ids(monkeypatch):
    monkeypatch.setattr(nof, "_market_table", None)

    def boom(path, params=None, retries=3):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(rc, "_get", boom)
    assert nof.discover_period_markets() == nof.KNOWN_MARKETS


def test_half_lines_are_read_from_their_own_market_ids(monkeypatch):
    monkeypatch.setattr(nof, "_market_table", dict(nof.KNOWN_MARKETS))
    event = {"markets": [
        market_block(3, 0, [("Over", [price_line(226.5, -110)]), ("Under", [price_line(226.5, -110)])]),
        market_block(6, 1, [("Over", [price_line(113.5, -110)]), ("Under", [price_line(113.5, -110)])]),
        market_block(5, 1, [("Atlanta Hawks", [price_line(-3.5, -110)]), ("Chicago Bulls", [price_line(3.5, -110)])]),
        market_block(1008, 2, [("Over", [price_line(112.5, -110)]), ("Under", [price_line(112.5, -110)])]),
    ]}
    markets = nof.read_all_periods(event, "Atlanta Hawks", "Chicago Bulls")
    assert set(markets) == {"full", "h1", "h2"}
    assert markets["h1"]["totals"][0]["line"] == 113.5 and markets["h1"]["spread"]
    assert markets["h2"]["totals"][0]["line"] == 112.5
