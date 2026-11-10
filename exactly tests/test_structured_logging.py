import json
import logging

from structured_logging import JsonFormatter


def make_record(**extra):
    record = logging.LogRecord("predbot.t", logging.INFO, __file__, 1, "evt_name", None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formats_single_line_json_with_core_fields():
    line = JsonFormatter().format(make_record())
    assert "\n" not in line
    data = json.loads(line)
    assert data["event"] == "evt_name"
    assert data["level"] == "INFO"
    assert data["logger"] == "predbot.t"
    assert "ts" in data


def test_includes_ctx_when_present():
    data = json.loads(JsonFormatter().format(make_record(ctx={"a": 1})))
    assert data["ctx"] == {"a": 1}


def test_unserialisable_ctx_never_crashes():
    data = json.loads(JsonFormatter().format(make_record(ctx={"s": {1, 2}})))
    assert "s" in data["ctx"]
