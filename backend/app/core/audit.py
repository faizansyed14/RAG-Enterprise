"""Security event log: who did what, from where. One line per event on the
`security` logger, tagged with the request id. Never pass passwords, tokens or
chat text in here."""

import contextvars
import logging

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_log = logging.getLogger("security")


def audit(event: str, **fields: object) -> None:
    detail = " ".join(f"{key}={value}" for key, value in fields.items())
    _log.info("event=%s request_id=%s %s", event, request_id_var.get(), detail)
