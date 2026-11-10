"""
Native structured JSON logging for the whole bot (stdlib only, no deps).

Every record is one JSON object per line on stdout, so GitHub Actions logs
stay greppable and the same lines can later be shipped to a log store or a
website dashboard without re-parsing free text.

Usage:
    from structured_logging import get_logger
    log = get_logger("props_probe")
    log.info("probe_started", extra={"ctx": {"sport_id": 4}})

Severity guide used across this project:
    DEBUG   noisy internals (raw payload shapes, per-request timing)
    INFO    normal milestones (run started, N events found, tip sent)
    WARNING recoverable trouble (retry, fallback used, partial data)
    ERROR   something failed and a human should look (auth rejected, send failed)

Pro tip: log an *event name* ("probe_started") as the message and put the
variable data in `ctx`. Event names stay constant, so you can count and
alert on them; free-text messages that embed values cannot be aggregated.
"""

import datetime
import json
import logging
import os
import sys

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Formats a LogRecord as a single-line JSON object."""

    def format(self, record):
        payload = {
            "ts": datetime.datetime.fromtimestamp(
                record.created, tz=datetime.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if ctx:
            payload["ctx"] = ctx
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # default=str keeps logging from ever crashing the bot on an
        # unserialisable value (dates, sets, exceptions inside ctx)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _configure_root():
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger("predbot")
    root.setLevel(level)
    root.handlers = [handler]
    root.propagate = False
    _CONFIGURED = True


def get_logger(name):
    """Returns a child logger of the shared 'predbot' JSON logger."""
    _configure_root()
    return logging.getLogger(f"predbot.{name}")
