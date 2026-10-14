class RagEngineError(Exception):
    """status_code carries the HTTP status when the raising site passes it;
    None does not imply local/client-side."""

    def __init__(self, *args: object, status_code: int | None = None) -> None:
        super().__init__(*args)
        self.status_code = status_code


class MaxTurnsError(RagEngineError):
    """The search agent used every step it was allowed without reaching an answer."""


def _rag_engine_cause(exc: BaseException | None) -> RagEngineError | None:
    """The RagEngineError behind a framework's wrapper exception, if any."""
    while exc is not None:
        if isinstance(exc, RagEngineError):
            return exc
        exc = exc.__cause__
    return None
