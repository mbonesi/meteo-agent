import io
import logging

import requests

from meteoagent import telegram
from meteoagent.config import TelegramConfig
from meteoagent.http import make_session
from meteoagent.telegram import TokenRedactingFilter, redact

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


# --------------------------------------------------------------------- token redaction

TOKEN = "123456789:AAE-FakeSecret_tokenValue0123456789xyz"


class TestRedaction:
    def test_redact_masks_token_in_url_path(self):
        text = f"Max retries exceeded with url: /bot{TOKEN}/sendMessage (Caused by X)"
        assert redact(text) == "Max retries exceeded with url: /bot<redacted>/sendMessage (Caused by X)"

    def test_redact_leaves_other_text_alone(self):
        text = "GET /v1/forecast?latitude=44.49 failed: 502"
        assert redact(text) == text

    def test_send_does_not_log_token_from_exception(self, caplog):
        """Regression: connection-phase errors quote the URL, token included."""
        url_error = requests.ConnectionError(
            f"HTTPSConnectionPool(host='api.telegram.org', port=443): "
            f"Max retries exceeded with url: /bot{TOKEN}/sendMessage (Caused by NameResolutionError)"
        )
        cfg = TelegramConfig(token=TOKEN, chat_id="42")
        with caplog.at_level("WARNING"):
            assert not telegram.send(FakeSession(url_error, url_error), cfg, "hello")
        assert TOKEN not in caplog.text
        assert "/bot<redacted>/sendMessage" in caplog.text

    def test_filter_redacts_third_party_records_and_tracebacks(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(TokenRedactingFilter())
        handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
        lib = logging.getLogger("urllib3.connectionpool.test")
        lib.addHandler(handler)
        lib.propagate = False
        try:
            lib.warning("Retrying (%r) after connection broken by '%r': %s", "R", "E", f"/bot{TOKEN}/getMe")
            try:
                raise RuntimeError(f"boom at /bot{TOKEN}/getUpdates")
            except RuntimeError:
                lib.exception("unhandled")
        finally:
            lib.removeHandler(handler)
        out = stream.getvalue()
        assert TOKEN not in out
        assert out.count("/bot<redacted>/") == 2

    def test_end_to_end_with_real_session_and_closed_port(self, monkeypatch):
        """Real requests/urllib3 stack, connection refused on loopback: nothing may leak.

        Reproduces the audit finding: urllib3 logs each connect retry with the URL
        path, and MaxRetryError quotes it again in the exception message.
        """
        monkeypatch.setattr(telegram, "API_BASE", "http://127.0.0.1:1")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.WARNING)
        try:
            telegram.install_log_redaction()
            cfg = TelegramConfig(token=TOKEN, chat_id="42", timeout_s=2)
            assert not telegram.send(make_session(total_retries=1), cfg, "hello")
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        out = stream.getvalue()
        assert "/bot<redacted>/sendMessage" in out, "the failure must still be visible in the log"
        assert TOKEN not in out
