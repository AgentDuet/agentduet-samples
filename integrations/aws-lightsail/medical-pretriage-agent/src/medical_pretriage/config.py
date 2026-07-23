"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Application settings."""

    # AgentDuet
    agentduet_api_key: str
    agentduet_connector_uuid: str

    # Amazon Nova 2 Sonic / Bedrock
    aws_region: str
    nova_model_id: str
    nova_voice_id: str
    sample_rate: int
    endpointing_sensitivity: str

    # CloudWatch transcript observability
    cloudwatch_enabled: bool
    cloudwatch_log_group: str
    cloudwatch_log_stream_prefix: str

    # Health HTTP server (Lightsail / Docker)
    health_host: str
    health_port: int

    log_level: str

    @classmethod
    def from_env(cls) -> Settings:
        sample_rate = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
        if sample_rate not in (8000, 16000, 24000):
            raise RuntimeError("AUDIO_SAMPLE_RATE must be 8000, 16000, or 24000")

        endpointing = os.getenv("NOVA_ENDPOINTING_SENSITIVITY", "HIGH").strip().upper()
        if endpointing not in {"HIGH", "MEDIUM", "LOW"}:
            raise RuntimeError(
                "NOVA_ENDPOINTING_SENSITIVITY must be HIGH, MEDIUM, or LOW"
            )

        return cls(
            agentduet_api_key=_require("AGENTDUET_API_KEY"),
            agentduet_connector_uuid=_require("AGENTDUET_CONNECTOR_UUID"),
            aws_region=os.getenv("AWS_REGION", "us-east-1"),
            nova_model_id=os.getenv("NOVA_MODEL_ID", "amazon.nova-2-sonic-v1:0"),
            nova_voice_id=os.getenv("NOVA_VOICE_ID", "tiffany"),
            sample_rate=sample_rate,
            endpointing_sensitivity=endpointing,
            cloudwatch_enabled=_bool("CLOUDWATCH_ENABLED", True),
            cloudwatch_log_group=os.getenv(
                "CLOUDWATCH_LOG_GROUP", "/medical-pretriage/transcripts"
            ),
            cloudwatch_log_stream_prefix=os.getenv(
                "CLOUDWATCH_LOG_STREAM_PREFIX", "call"
            ),
            health_host=os.getenv("HEALTH_HOST", "0.0.0.0"),
            health_port=int(os.getenv("HEALTH_PORT", "8080")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
