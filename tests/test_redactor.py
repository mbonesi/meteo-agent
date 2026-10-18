import requests

from meteoagent import redactor
from meteoagent.config import LLMConfig

OPENAI = LLMConfig(
    provider="openai",
    endpoint="https://models.example/chat/completions",
    model="openai/gpt-4.1-mini",
    api_key_env="GITHUB_TOKEN",
    api_key="github_pat_x",
)
FACTS = {"titolo_regola": "Vento", "istanti": []}


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def fake_post(response, calls):
    def post(url, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json})
        if isinstance(response, Exception):
            raise response
        return response

    return post


class TestOpenAICompatible:
    def test_writes_prose(self, monkeypatch):
        calls = []
        ok = FakeResponse(200, {"choices": [{"message": {"content": "  Vento da E.  "}}]})
        monkeypatch.setattr(redactor.requests, "post", fake_post(ok, calls))
        assert redactor.Redactor(OPENAI).write(FACTS) == "Vento da E."
        (call,) = calls
        assert call["url"] == OPENAI.endpoint
        assert call["headers"]["Authorization"] == "Bearer github_pat_x"
        assert call["json"]["model"] == "openai/gpt-4.1-mini"
        assert call["json"]["messages"][0]["role"] == "system"

    def test_http_error_degrades_to_none(self, monkeypatch):
        bad = FakeResponse(401, text='{"error": "Bad credentials"}')
        monkeypatch.setattr(redactor.requests, "post", fake_post(bad, []))
        assert redactor.Redactor(OPENAI).write(FACTS) is None

    def test_network_error_degrades_to_none(self, monkeypatch):
        down = requests.ConnectionError("down")
        monkeypatch.setattr(redactor.requests, "post", fake_post(down, []))
        assert redactor.Redactor(OPENAI).write(FACTS) is None

    def test_malformed_response_degrades_to_none(self, monkeypatch):
        weird = FakeResponse(200, {"unexpected": True})
        monkeypatch.setattr(redactor.requests, "post", fake_post(weird, []))
        assert redactor.Redactor(OPENAI).write(FACTS) is None


def test_disabled_never_calls_out(monkeypatch):
    calls = []
    monkeypatch.setattr(redactor.requests, "post", fake_post(None, calls))
    r = redactor.Redactor(LLMConfig(enabled=False, provider="openai"))
    assert not r.available
    assert r.write(FACTS) is None
    assert calls == []
