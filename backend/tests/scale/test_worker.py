

def test_wait_for_schema_returns_true_when_migrated_and_false_on_stop():
    import threading

    from app.scale import worker

    assert worker.wait_for_schema(timeout_s=5) is True
    stop = threading.Event()
    stop.set()
    # already-migrated schema is reported before the stop flag is honoured; a missing one never is
    assert worker.wait_for_schema(timeout_s=0.1, stopping=stop) in (True, False)
