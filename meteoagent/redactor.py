"""Notification text redaction via an LLM.

Deliberately narrow role: the LLM receives the data that has already
triggered the rule and turns it into two readable lines. It doesn't decide
whether to notify, doesn't interpret thresholds, has no network access. If
the API is slow, errors out, or has no credit, write() returns None and the
caller sends the bare message instead.

Two providers, chosen with [llm].provider:
  anthropic  Anthropic API, via the SDK (default).
  openai     any endpoint compatible with OpenAI chat/completions, via a
             JSON POST: GitHub Models with a PAT, OpenAI, a local proxy...

SYSTEM_PROMPT is left untouched (Italian): it's the actual instruction that
shapes the Telegram notification's language and tone, not developer-facing
documentation — see message.py's module docstring for the same rationale.
"""

from __future__ import annotations

import json
import logging

import requests

from .config import LLMConfig

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Sei il redattore di un agente meteo personale. Ricevi in JSON i dati che hanno fatto scattare una regola di allerta e scrivi il testo della notifica Telegram.

Regole ferree:
- Usa ESCLUSIVAMENTE i dati presenti nel JSON. Non aggiungere previsioni, cause meteorologiche, consigli o valori non forniti.
- Se un dato non c'e', non nominarlo. Non inventare mai un numero.
- Italiano, tono asciutto e tecnico, 2 o 3 frasi, massimo 350 caratteri.
- Riassumi: quando inizia, quanto dura, i valori di picco. Chi legge vuole sapere cosa succede e quando.
- Niente saluti, niente emoji, niente formattazione Markdown, niente titolo: il titolo lo mette gia' l'agente.
- Distingui sempre previsione da osservazione, il campo tipo_dato lo dice.

Rispondi solo con il testo della notifica."""


class Redactor:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = None
        self._available = False
        if not cfg.enabled:
            return
        if cfg.provider == "openai":
            self._available = True  # requests is enough, no SDK
            return
        try:
            import anthropic
        except ImportError:  # pragma: no cover
            log.error("'anthropic' package not installed: redaction disabled")
            return
        self._client = anthropic.Anthropic(api_key=cfg.api_key, timeout=cfg.timeout_s)
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    def write(self, facts: dict) -> str | None:
        """Returns the prose, or None if redaction is unavailable or fails."""
        if not self._available:
            return None
        payload = json.dumps(facts, ensure_ascii=False, indent=1)
        try:
            if self.cfg.provider == "openai":
                text = self._write_openai(payload)
            else:
                text = self._write_anthropic(payload)
        except Exception as exc:
            # Any API-side error is non-fatal by design.
            log.warning(
                "redaction via %s failed (%s): sending the message without prose",
                self.cfg.provider,
                exc,
            )
            return None

        text = (text or "").strip()
        if not text:
            log.warning("%s returned an empty text", self.cfg.provider)
            return None
        log.debug("prose generated (%d chars, %s)", len(text), self.cfg.provider)
        return text

    def _write_anthropic(self, payload: str) -> str:
        response = self._client.messages.create(
            model=self.cfg.model_name,
            max_tokens=self.cfg.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": payload}],
        )
        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )

    def _write_openai(self, payload: str) -> str:
        # requests.post and not the shared session: a fresh connection for each
        # notification, no keep-alive gone stale after hours of inactivity.
        response = requests.post(
            self.cfg.endpoint,
            headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            json={
                "model": self.cfg.model_name,
                "max_tokens": self.cfg.max_tokens,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": payload},
                ],
            },
            timeout=self.cfg.timeout_s,
        )
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        return response.json()["choices"][0]["message"]["content"]
