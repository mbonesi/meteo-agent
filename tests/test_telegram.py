import requests

from meteoagent import telegram
from meteoagent.config import TelegramConfig

CFG = TelegramConfig(token="123:abc", chat_id="42")


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"ok": True}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    """Returns the planned outcomes in order: an exception or a response."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def post(self, url, json, timeout):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestSend:
    def test_ok(self):
        session = FakeSession(FakeResponse())
        assert telegram.send(session, CFG, "hello")
        assert session.calls == 1

    def test_retries_once_on_stale_keepalive_connection(self):
        """Regression: ~40% of notifications lost in production.

        After hours of inactivity the pooled connection has already been
        closed on Telegram's side and the first POST dies with RemoteDisconnected.
        """
        stale = requests.ConnectionError("('Connection aborted.', RemoteDisconnected(...))")
        session = FakeSession(stale, FakeResponse())
        assert telegram.send(session, CFG, "hello")
        assert session.calls == 2

    def test_gives_up_after_repeated_connection_errors(self):
        session = FakeSession(requests.ConnectionError("down"), requests.ConnectionError("down"))
        assert not telegram.send(session, CFG, "hello")
        assert session.calls == 2

    def test_does_not_retry_read_timeout(self):
        # The message may already have arrived: retrying would create a duplicate.
        session = FakeSession(requests.ReadTimeout("slow"), FakeResponse())
        assert not telegram.send(session, CFG, "hello")
        assert session.calls == 1

    def test_http_error_is_not_retried(self):
        session = FakeSession(FakeResponse(400, {"description": "Bad Request: chat not found"}))
        assert not telegram.send(session, CFG, "hello")
        assert session.calls == 1
