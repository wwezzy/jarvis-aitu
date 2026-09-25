import json
import logging
import uuid
from contextvars import ContextVar

correlation_id = ContextVar("correlation_id", default="startup")


def new_correlation():
    return correlation_id.set(uuid.uuid4().hex[:16])


class PrivateFormatter(logging.Formatter):
    """Even third-party log messages can contain tokens/SQL/URLs: omit payloads."""
    def format(self, record):
        return json.dumps({"time": self.formatTime(record), "level": record.levelname,
            "logger": record.name, "event": f"{record.module}.{record.funcName}:{record.lineno}",
            "correlation_id": correlation_id.get(),
            "error_class": record.exc_info[0].__name__ if record.exc_info else None})


def configure_logging(level):
    handler = logging.StreamHandler()
    handler.setFormatter(PrivateFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
