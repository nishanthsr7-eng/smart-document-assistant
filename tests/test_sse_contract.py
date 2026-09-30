"""The wire contract of POST /query, which is an SSE stream.

The frontend parses these frames by hand, so the contract is the frame syntax and the event
vocabulary, not just the final payload. Nothing here asserts answer quality: it asserts that a
client written against the documented events keeps working.
"""

import json

import pytest
from fastapi.testclient import TestClient

from src.api import deps, router
from src.api.schemas import QueryResponse
from src.auth.principal import Principal
from src.core.errors import GenerationError
from src.core.tracing import Trace
from src.generation.answerer import Answer

EDITOR = Principal(
    user_id="00000000-0000-0000-0000-000000000001",
    tenant_id="00000000-0000-0000-0000-0000000000aa",
    email="editor@acme.test",
    role="editor",
)

# Everything a client is allowed to see. A new event name is a breaking change for any client
# that switches on this set, so adding one here is the deliberate act it should be.
EVENTS = {"stage", "token", "done", "error"}


@pytest.fixture
def client(monkeypatch):
    for name in ("embedder", "vector_store", "llm_client", "keyword_index", "reranker"):
        monkeypatch.setattr(deps, name, lambda: None)
    router.app.dependency_overrides[deps.current_principal] = lambda: EDITOR
    yield TestClient(router.app)
    router.app.dependency_overrides.clear()


def _answering(monkeypatch, text="Leave is 25 days [1]."):
    def fake(question, doc_ids, *args, on_token=None, on_stage=None, **kwargs):
        trace = Trace()
        if on_stage is not None:
            on_stage("Retrieving")
        if on_token is not None:
            for word in text.split(" "):
                on_token(word + " ")
        return Answer("answered", [], [], trace, [], answer_text=text)

    monkeypatch.setattr(router, "answer_question", fake)


@pytest.fixture
def slots_released():
    """The stream owns a worker slot for its life; every test here must give it back."""
    yield
    taken = []
    while router._QUERY_SLOTS.acquire(blocking=False):
        taken.append(True)
    for _ in taken:
        router._QUERY_SLOTS.release()
    assert len(taken) == router.SETTINGS.api.query_concurrency


def _frames(body: str) -> list[tuple[str, dict]]:
    """Parse the stream the way a client must: blank-line-delimited event/data pairs."""
    frames = []
    for block in body.split("\n\n"):
        block = block.strip("\n")
        if not block or block.startswith(":"):
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        frames.append((lines["event"], json.loads(lines["data"])))
    return frames


def _post(client):
    return client.post("/query", json={"question": "What is the leave policy?", "doc_ids": []})


def test_the_response_is_an_event_stream(client, monkeypatch, slots_released):
    _answering(monkeypatch)
    response = _post(client)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


def test_every_frame_is_a_named_event_with_json_data(client, monkeypatch, slots_released):
    _answering(monkeypatch)
    frames = _frames(_post(client).text)
    assert frames, "the stream produced no frames"
    for name, data in frames:
        assert name in EVENTS, f"undocumented event '{name}'"
        assert isinstance(data, dict)


def test_tokens_arrive_before_done_and_reassemble_into_the_answer(
    client, monkeypatch, slots_released
):
    _answering(monkeypatch, "Leave is 25 days [1].")
    frames = _frames(_post(client).text)
    names = [name for name, _ in frames]
    assert names[-1] == "done"
    assert "token" in names and names.index("token") < names.index("done")
    streamed = "".join(data["text"] for name, data in frames if name == "token")
    assert streamed.strip() == "Leave is 25 days [1]."


def test_the_done_payload_validates_against_the_documented_schema(
    client, monkeypatch, slots_released
):
    _answering(monkeypatch)
    done = [data for name, data in _frames(_post(client).text) if name == "done"]
    assert len(done) == 1
    parsed = QueryResponse.model_validate(done[0])
    assert parsed.status == "answered"
    assert parsed.query_id


def test_a_provider_failure_is_an_error_frame_not_a_broken_stream(
    client, monkeypatch, slots_released
):
    def boom(*args, **kwargs):
        raise GenerationError("provider exploded")

    monkeypatch.setattr(router, "answer_question", boom)
    response = _post(client)
    # The status line is already sent by the time generation runs, so a failure has to arrive
    # in-band. A client that treats a 200 as success and ignores `error` would show an empty
    # answer, which is why this is part of the contract.
    assert response.status_code == 200
    frames = _frames(response.text)
    assert frames[-1][0] == "error"
    assert "provider exploded" in frames[-1][1]["detail"]


def test_an_unhandled_failure_does_not_leak_its_message(client, monkeypatch, slots_released):
    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(router, "answer_question", boom)
    frames = _frames(_post(client).text)
    assert frames[-1] == ("error", {"detail": "Internal server error."})


def test_a_rejected_question_is_a_status_code_not_a_stream(client, monkeypatch, slots_released):
    _answering(monkeypatch)
    response = client.post("/query", json={"question": "   ", "doc_ids": []})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


# --- the non-streaming variant of the same answer ---


def test_stream_false_returns_one_json_body(client, monkeypatch, slots_released):
    """Same answer, same pipeline, one response: what a programmatic client wants."""
    _answering(monkeypatch)
    response = client.post(
        "/query", json={"question": "What is the leave policy?", "doc_ids": [], "stream": False}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert QueryResponse.model_validate(response.json()).answer_text == "Leave is 25 days [1]."


def test_stream_false_reports_a_provider_failure_as_a_problem(client, monkeypatch, slots_released):
    def fails(*args, **kwargs):
        raise GenerationError("Provider is down.")

    monkeypatch.setattr(router, "answer_question", fails)
    response = client.post(
        "/query", json={"question": "What is the leave policy?", "doc_ids": [], "stream": False}
    )
    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["detail"] == "Provider is down."


def test_stream_false_is_not_counted_as_an_abandoned_query(client, monkeypatch, slots_released):
    """Returning on the first terminal event left the generator suspended, and closing it from
    there looks -- from inside the generator -- exactly like the client having gone away."""
    _answering(monkeypatch)
    abandoned = []
    monkeypatch.setattr(router, "_abandon", lambda cancel, reason: abandoned.append(reason))

    response = client.post(
        "/query", json={"question": "What is the leave policy?", "doc_ids": [], "stream": False}
    )

    assert response.status_code == 200
    assert abandoned == []
