"""The A/B harness is how the owner decides, with numbers, whether the scale layer beats the old code.
Its judgement must itself be right, so the scoring and the comparison are tested here against a fake app."""

import json

import httpx

from scripts import scale_ab as ab


def _sse(kind: str, payload: dict) -> bytes:
    # exactly what app/api/chat.py::_sse writes
    return f"event: {kind}\ndata: {json.dumps(payload)}\n\n".encode()


def _stream(answer: str, cites: list[dict], refused: bool = False) -> bytes:
    return (_sse("answer", {"type": "answer", "delta": answer[:10]})
            + _sse("answer", {"type": "answer", "delta": answer[10:]})
            + _sse("citations", {"type": "citations", "refused": refused, "citations": cites})
            + _sse("done", {"type": "done"}))


def test_the_chat_stream_is_folded_into_one_record():
    raw = _stream("The notice period is 90 days.", [{"document": "Falcon Supplier Agreement.pdf", "page": 4, "document_id": "d1"}])
    res = ab.fold_events(ab.parse_sse(raw.decode().splitlines(keepends=True)))
    assert res["answer"] == "The notice period is 90 days."
    assert res["cited"] == [{"document": "Falcon Supplier Agreement.pdf", "page": 4, "document_id": "d1"}]
    assert res["refused"] is False and res["error"] is None


def test_a_garbled_frame_is_skipped_not_fatal():
    lines = ["event: answer\n", "data: {not json\n", "\n", "event: answer\n", 'data: {"type":"answer","delta":"ok"}\n', "\n"]
    assert ab.fold_events(ab.parse_sse(lines))["answer"] == "ok"


def test_a_case_passes_only_when_every_requirement_holds():
    case = {"q": "q", "expect": ["falcon"], "answer_contains": ["90 days"], "forbid": ["salary"]}
    good = {"ok": True, "answer": "It is 90 days.", "cited": [{"document": "Falcon Supplier Agreement.pdf"}], "refused": False}
    assert ab.score_result(case, good)["passed"] is True
    wrong_doc = {**good, "cited": [{"document": "Handbook.pdf"}]}
    assert ab.score_result(case, wrong_doc)["passed"] is False
    wrong_text = {**good, "answer": "It is 30 days."}
    assert ab.score_result(case, wrong_text)["passed"] is False
    refused = {**good, "refused": True}
    assert ab.score_result(case, refused)["passed"] is False
    errored = {**good, "ok": False, "error": "HTTP 500"}
    assert ab.score_result(case, errored)["passed"] is False


def test_forbidden_text_in_the_answer_or_the_citations_is_counted_and_fails_the_case():
    case = {"q": "q", "expect": ["falcon"], "forbid": ["salary"]}
    leak_in_text = {"ok": True, "answer": "Her salary is 5.", "cited": [{"document": "Falcon.pdf"}], "refused": False}
    leak_in_cite = {"ok": True, "answer": "Fine.", "cited": [{"document": "Falcon.pdf"}, {"document": "Salary 2024.xlsx"}], "refused": False}
    for res in (leak_in_text, leak_in_cite):
        s = ab.score_result(case, res)
        assert s["forbidden"] == 1 and s["passed"] is False


def test_a_question_that_must_be_refused_passes_when_nothing_is_cited():
    case = {"q": "q", "expect_refusal": True, "forbid": ["salary"]}
    nothing = {"ok": True, "answer": "I couldn't find anything relevant.", "cited": [], "refused": False}
    assert ab.score_result(case, nothing)["passed"] is True
    cites = {**nothing, "cited": [{"document": "Salary.xlsx"}]}
    assert ab.score_result(case, cites)["passed"] is False


def _fake_app(answers: dict[str, bytes], seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            body = json.loads(request.content)
            return httpx.Response(200, json={"token": f"tok-{body['username']}"})
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        seen.append((request.headers["authorization"], body))
        if body["query"] not in answers:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, content=answers[body["query"]], headers={"content-type": "text/event-stream"})

    return httpx.Client(base_url="http://app", transport=httpx.MockTransport(handler))


def test_run_asks_each_question_as_its_user_with_no_document_narrowing(monkeypatch):
    monkeypatch.setenv("AB_PW_ALICE", "pw")
    seen: list = []
    answers = {"notice?": _stream("90 days.", [{"document": "Falcon.pdf", "page": 1}])}
    golden = [{"q": "notice?", "user": "alice", "expect": ["falcon"], "answer_contains": ["90"]},
              {"q": "broken?", "user": "alice", "expect": ["x"]}]
    with _fake_app(answers, seen) as client:
        out = ab.run(golden, client, {"alice": "AB_PW_ALICE"}, "alice", "scale_on")
    assert seen[0][0] == "Bearer tok-alice"
    assert seen[0][1]["document_ids"] is None, "the harness must ask the way the fixed UI does: search everything"
    assert out["cases"][0]["score"]["passed"] is True
    assert out["cases"][1]["ok"] is False and "HTTP 500" in out["cases"][1]["error"], "a failing chat is recorded, not raised"
    assert out["summary"]["questions"] == 2 and out["summary"]["pass_rate"] == 0.5 and out["summary"]["errors"] == 1


def test_compare_lists_regressions_and_improvements_by_question():
    def run(passed_by_q):
        cases = [{"q": q, "seconds": 1.0, "score": {"passed": p, "cited_recall": 1.0 if p else 0.0, "forbidden": 0, "answered": p}}
                 for q, p in passed_by_q.items()]
        return {"label": "x", "summary": ab.summarize(cases), "cases": cases}

    base = run({"a": True, "b": True, "c": False, "d": False})
    new = run({"a": True, "b": False, "c": True, "d": False, "e": True})
    got = ab.compare_runs(base, new)
    assert got["regressions"] == ["b"] and got["improvements"] == ["c"]
    assert got["both_fail"] == ["d"] and got["not_in_base"] == ["e"]


def test_the_gate_fails_on_a_regression_or_any_forbidden_document(tmp_path):
    def write(name, passed, forbidden=0):
        cases = [{"q": "a", "seconds": 1.0, "score": {"passed": passed, "cited_recall": 1.0, "forbidden": forbidden, "answered": True}}]
        (tmp_path / name).write_text(json.dumps({"label": name, "summary": ab.summarize(cases), "cases": cases}))
        return str(tmp_path / name)

    ok_base, bad_new = write("base.json", True), write("bad.json", False)
    leak_new = write("leak.json", True, forbidden=1)
    same_new = write("same.json", True)
    assert ab.main(["compare", ok_base, same_new]) == 0
    assert ab.main(["compare", ok_base, bad_new]) == 1
    assert ab.main(["compare", ok_base, bad_new, "--max-regressions", "1"]) == 0
    assert ab.main(["compare", ok_base, leak_new]) == 1
