"""Unit tests for props_probe.py. No network: all HTTP is faked."""

import datetime
import json

import pytest
import requests

import props_probe as pp


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    """Replays queued responses/exceptions in order and records calls."""

    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(queue, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    session = FakeSession(queue)
    return pp.RundownHttp("key123", session=session, sleep=sleeps.append), session, sleeps


def prop_market(market_id=29, with_prices=True):
    prices = {"19": {"price": -110}} if with_prices else {}
    return {
        "market_id": market_id,
        "participants": [
            {"name": "Over", "lines": [{"value": 24.5, "prices": prices}]},
            {"name": "Under", "lines": [{"value": 24.5, "prices": prices}]},
        ],
    }


# ---------- RundownHttp ----------

def test_missing_key_raises_config_error():
    with pytest.raises(pp.ProbeConfigError):
        pp.RundownHttp("")


def test_sends_key_header():
    client, session, _ = make_client([FakeResponse(200, {"ok": 1})])
    client.get_json("/sports")
    assert session.calls[0][2] == {"X-TheRundown-Key": "key123"}


def test_retries_503_with_exponential_backoff_then_succeeds():
    client, session, sleeps = make_client(
        [FakeResponse(503), FakeResponse(503), FakeResponse(200, {"ok": 1})]
    )
    assert client.get_json("/x") == {"ok": 1}
    assert sleeps == [1, 2]
    assert len(session.calls) == 3


def test_retries_network_error_then_succeeds():
    client, _, sleeps = make_client(
        [requests.ConnectionError("boom"), FakeResponse(200, {"ok": 1})]
    )
    assert client.get_json("/x") == {"ok": 1}
    assert sleeps == [1]


def test_network_error_raised_after_retries_exhausted():
    errs = [requests.Timeout("t") for _ in range(pp.MAX_RETRIES + 1)]
    client, _, sleeps = make_client(errs)
    with pytest.raises(requests.Timeout):
        client.get_json("/x")
    assert sleeps == [1, 2, 4]


@pytest.mark.parametrize("status", [401, 402, 403])
def test_access_errors_are_not_retried(status):
    client, session, sleeps = make_client([FakeResponse(status, text="nope")])
    with pytest.raises(pp.ProbeAccessError) as info:
        client.get_json("/x")
    assert info.value.status == status
    assert sleeps == []
    assert len(session.calls) == 1


def test_non_json_body_raises_response_error():
    client, _, _ = make_client([FakeResponse(200, None)])
    with pytest.raises(pp.ProbeResponseError):
        client.get_json("/x")


# ---------- find_nba_sport_id ----------

def test_sport_lookup_requires_exact_nba_not_wnba():
    sports = [{"id": 8, "name": "WNBA"}, {"id": 4, "name": "NBA"}]
    client, _, _ = make_client([FakeResponse(200, sports)])
    assert pp.find_nba_sport_id(client) == 4


def test_sport_lookup_falls_back_when_missing():
    client, _, _ = make_client([FakeResponse(200, [{"id": 8, "name": "WNBA"}])])
    assert pp.find_nba_sport_id(client) == pp.NBA_FALLBACK_SPORT_ID


def test_sport_lookup_falls_back_on_http_error():
    client, _, _ = make_client([FakeResponse(404)])
    assert pp.find_nba_sport_id(client) == pp.NBA_FALLBACK_SPORT_ID


# ---------- summarise / verdict ----------

def test_summary_counts_lines_books_and_sample():
    events = [
        {"event_id": "a", "markets": [prop_market(29), prop_market(35)]},
        {"event_id": "b", "markets": [{"market_id": 1, "participants": []}]},
    ]
    summary = pp.summarise_events(events)
    assert summary["events_total"] == 2
    assert summary["events_with_props"] == 1
    assert summary["per_market"]["points"]["lines"] == 2
    assert summary["per_market"]["points"]["books"] == 1
    assert "rebounds" in summary["per_market"]
    assert json.loads(summary["raw_sample_first_prop_market"])["market_id"] == 29


def test_prop_market_without_prices_does_not_count():
    summary = pp.summarise_events([{"event_id": "a", "markets": [prop_market(29, with_prices=False)]}])
    assert summary["events_with_props"] == 0
    assert pp.decide_verdict(summary) == pp.VERDICT_NO_PROPS


def test_verdicts():
    assert pp.decide_verdict({"events_total": 0, "events_with_props": 0}) == pp.VERDICT_NO_GAMES
    assert pp.decide_verdict({"events_total": 3, "events_with_props": 0}) == pp.VERDICT_NO_PROPS
    assert pp.decide_verdict({"events_total": 3, "events_with_props": 2}) == pp.VERDICT_PROPS


# ---------- run_probe / main ----------

def test_run_probe_dedupes_events_across_dates():
    ev = {"event_id": "same", "markets": [prop_market(29)]}
    client, session, _ = make_client([
        FakeResponse(200, [{"id": 4, "name": "NBA"}]),
        FakeResponse(200, {"events": [ev]}),
        FakeResponse(200, {"events": [ev]}),
    ])
    report = pp.run_probe(client, ["2026-11-10", "2026-11-11"])
    assert report["events_total"] == 1
    assert report["verdict"] == pp.VERDICT_PROPS
    assert "/sports/4/events/2026-11-10" in session.calls[1][0]


def test_main_returns_2_without_key():
    assert pp.main(env={}, today=datetime.date(2026, 11, 10)) == 2


def test_main_returns_1_on_access_denied(monkeypatch):
    def deny(client, dates):
        raise pp.ProbeAccessError(403, "/sports/4/events/x", "plan")

    monkeypatch.setattr(pp, "run_probe", deny)
    assert pp.main(env={"THERUNDOWN_API_KEY": "k"}, today=datetime.date(2026, 11, 10)) == 1


def test_main_returns_0_on_success(monkeypatch):
    monkeypatch.setattr(pp, "run_probe", lambda c, d: {
        "verdict": pp.VERDICT_PROPS, "events_total": 1, "events_with_props": 1,
        "per_market": {}, "dates": d, "sport_id": 4, "raw_sample_first_prop_market": None})
    assert pp.main(env={"THERUNDOWN_API_KEY": "k"}, today=datetime.date(2026, 11, 10)) == 0
