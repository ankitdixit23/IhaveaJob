"""Structured (JSON-lines) logging to stderr."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

_STD_ATTRS = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None, json_logs: bool | None = None) -> None:
    level = (level or os.getenv("IHAVEAJOB_LOG_LEVEL") or "INFO").upper()
    if json_logs is None:
        json_logs = os.getenv("IHAVEAJOB_LOG_FORMAT", "json") == "json"
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_logs else logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("urllib3", "alembic"):
        logging.getLogger(noisy).setLevel("WARNING")
