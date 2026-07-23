"""CloudWatch Logs writer for call transcripts and lifecycle events."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)


@dataclass
class TranscriptEvent:
    """One turn of conversation transcript."""

    call_id: str
    participant: str
    subscriber: str
    role: str  # USER | ASSISTANT
    text: str
    generation_stage: str | None = None
    event_type: str = "transcript"
    ts: str | None = None

    def to_json(self) -> str:
        payload = asdict(self)
        payload["ts"] = self.ts or datetime.now(timezone.utc).isoformat()
        return json.dumps(payload, ensure_ascii=False)


class TranscriptLogger:
    """Append structured transcript events to CloudWatch Logs.

    Local logging is immediate; CloudWatch writes run on a background thread so
    the Nova Sonic audio path is never blocked on PutLogEvents.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        log_group: str,
        stream_prefix: str,
        region: str,
    ) -> None:
        self.enabled = enabled
        self.log_group = log_group
        self.stream_prefix = stream_prefix
        self.region = region
        self._client = None
        self._sequence_tokens: dict[str, str | None] = {}
        self._ensured_streams: set[str] = set()
        self._queue: queue.Queue[TranscriptEvent | None] = queue.Queue()
        self._worker: threading.Thread | None = None

        if self.enabled:
            self._client = boto3.client("logs", region_name=region)
            self._ensure_log_group()
            self._worker = threading.Thread(
                target=self._run_worker,
                name="cloudwatch-transcripts",
                daemon=True,
            )
            self._worker.start()

    def _ensure_log_group(self) -> None:
        assert self._client is not None
        try:
            self._client.create_log_group(logGroupName=self.log_group)
            logger.info("Created CloudWatch log group %s", self.log_group)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code != "ResourceAlreadyExistsException":
                logger.warning("Could not create log group %s: %s", self.log_group, exc)

    def _stream_name(self, call_id: str) -> str:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        safe_call = call_id.replace("/", "_")[:120]
        return f"{self.stream_prefix}/{day}/{safe_call}"

    def _ensure_stream(self, stream_name: str) -> None:
        if not self.enabled or stream_name in self._ensured_streams:
            return
        assert self._client is not None
        try:
            self._client.create_log_stream(
                logGroupName=self.log_group,
                logStreamName=stream_name,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code != "ResourceAlreadyExistsException":
                logger.warning("Could not create log stream %s: %s", stream_name, exc)
                return
        self._ensured_streams.add(stream_name)
        self._sequence_tokens.setdefault(stream_name, None)

    def _run_worker(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            try:
                self._put_event(event)
            except Exception:  # noqa: BLE001
                logger.exception("Background CloudWatch write failed")

    def _put_event(self, event: TranscriptEvent) -> None:
        if self._client is None:
            return

        stream_name = self._stream_name(event.call_id)
        self._ensure_stream(stream_name)
        message = event.to_json()

        kwargs: dict[str, Any] = {
            "logGroupName": self.log_group,
            "logStreamName": stream_name,
            "logEvents": [
                {
                    "timestamp": int(time.time() * 1000),
                    "message": message,
                }
            ],
        }
        token = self._sequence_tokens.get(stream_name)
        if token:
            kwargs["sequenceToken"] = token

        try:
            response = self._client.put_log_events(**kwargs)
            self._sequence_tokens[stream_name] = response.get("nextSequenceToken")
        except (ClientError, BotoCoreError) as exc:
            logger.warning("CloudWatch put_log_events failed: %s", exc)
            try:
                kwargs.pop("sequenceToken", None)
                response = self._client.put_log_events(**kwargs)
                self._sequence_tokens[stream_name] = response.get("nextSequenceToken")
            except (ClientError, BotoCoreError) as retry_exc:
                logger.error("CloudWatch transcript write failed: %s", retry_exc)

    def emit(self, event: TranscriptEvent) -> None:
        """Log locally; enqueue CloudWatch write off the audio path."""
        logger.info(
            "transcript call_id=%s role=%s stage=%s text=%s",
            event.call_id,
            event.role,
            event.generation_stage,
            event.text,
        )
        if self.enabled and self._client is not None:
            self._queue.put(event)

    def call_started(
        self, *, call_id: str, participant: str, subscriber: str
    ) -> None:
        self.emit(
            TranscriptEvent(
                call_id=call_id,
                participant=participant,
                subscriber=subscriber,
                role="SYSTEM",
                text="call_started",
                event_type="lifecycle",
            )
        )

    def call_ended(
        self, *, call_id: str, participant: str, subscriber: str, reason: str = "hangup"
    ) -> None:
        self.emit(
            TranscriptEvent(
                call_id=call_id,
                participant=participant,
                subscriber=subscriber,
                role="SYSTEM",
                text=reason,
                event_type="lifecycle",
            )
        )
