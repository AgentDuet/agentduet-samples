"""OpenClaw Gateway HTTP client — the agent runtime for this sample.

Phone turns POST to /v1/chat/completions (same path as `openclaw agent`).
Calendar, web search, weather, Slack, etc. stay inside OpenClaw.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MD_RE = re.compile(r"[*_`#]+")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def for_speech(text: str) -> str:
    """Collapse markdown so Gemini can speak the OpenClaw reply."""
    t = _LINK_RE.sub(r"\1", text or "")
    t = _MD_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


class OpenClawGateway:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        model: str = "openclaw/default",
        timeout_s: float = 90.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout_s, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        r = await self._client.get("/v1/models")
        r.raise_for_status()
        return r.json()

    async def ask(self, *, session_user: str, user_text: str) -> str:
        """One phone turn against the OpenClaw agent. Session is keyed by `user`."""
        payload = {
            "model": self._model,
            "user": session_user,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "The user is on a live phone call. Answer as their personal "
                        "assistant in 1–4 short spoken sentences. No markdown, no "
                        "bullet lists, no URLs unless they asked for a link. Use "
                        "your tools (calendar, web, weather, messaging) when needed.\n\n"
                        f"Caller said: {user_text.strip()}"
                    ),
                }
            ],
        }
        logger.info("OpenClaw ask session=%s text=%r", session_user, user_text[:200])
        r = await self._client.post(
            "/v1/chat/completions",
            json=payload,
            headers={"x-openclaw-message-channel": "agentduet"},
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error("OpenClaw HTTP %s: %s", r.status_code, r.text[:800])
            raise

        data = r.json()
        content = (
            (data.get("choices") or [{}])[0].get("message") or {}
        ).get("content")
        if not isinstance(content, str) or not content.strip():
            logger.warning("OpenClaw empty content: %s", str(data)[:500])
            return ""
        spoken = for_speech(content)
        logger.info("OpenClaw reply (%d chars)", len(spoken))
        return spoken
