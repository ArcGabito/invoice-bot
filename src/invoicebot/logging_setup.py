"""Logs a stderr (texto o JSON) y, con INVOICEBOT_LOG_DIR, a un archivo JSON diario.

En contenedores (Azure Container Apps, Docker) usa INVOICEBOT_LOG_FORMAT=json:
cada línea es un objeto que Log Analytics indexa sin configuración extra.
Los logs no incluyen el texto de las facturas.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from invoicebot.config import LogConfig

_STANDARD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(config: LogConfig, *, verbose: bool = False) -> logging.Logger:
    level_name = (
        "debug"
        if verbose
        else {"warn": "warning", "silent": "critical"}.get(config.level, config.level)
    )
    level = getattr(logging, level_name.upper(), logging.WARNING)
    logger = logging.getLogger("invoicebot")
    logger.setLevel(min(level, logging.INFO) if config.dir else level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(
        JsonFormatter()
        if config.format == "json"
        else logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(console)

    if config.dir:
        Path(config.dir).mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            Path(config.dir) / "invoicebot.log",
            when="midnight",
            backupCount=config.retention_days,
            encoding="utf-8",
            utc=True,
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)
    return logger
