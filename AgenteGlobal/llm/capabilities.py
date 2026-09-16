from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field


class CapabilityStatus(StrEnum):
    NOT_TESTED = "not_tested"
    SUPPORTED = "supported"
    DEGRADED = "degraded"
    FALLBACK = "fallback"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class SnapshotFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


CapabilityFreshness = SnapshotFreshness
CAPABILITY_SNAPSHOT_TTL_DAYS = 30
LOGGER = logging.getLogger(__name__)


class CapabilityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CapabilityStatus = CapabilityStatus.NOT_TESTED
    detail: str = "Não testado."
    latency_ms: int | None = Field(default=None, ge=0)


class ModelTokenLimits(BaseModel):
    """Optional model limits used by the token-aware context budget.

    All fields are optional so snapshots generated before ContextBudget was
    introduced remain valid.  A source and observation are kept alongside
    limits because a conservative policy must not be mistaken for a live
    provider probe.
    """

    model_config = ConfigDict(extra="forbid")

    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    output_reserve_tokens: int | None = Field(default=None, ge=0)
    source: str | None = Field(default=None, max_length=2_000)
    observation: str | None = Field(default=None, max_length=4_000)


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    provider: str = "huawei_modelarts_maas"
    model: str
    base_url: str
    tested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    endpoint_identity: str | None = None
    deployment_identity: str | None = None
    runtime_version: str | None = None
    configuration_identity: str | None = None
    freshness: SnapshotFreshness = SnapshotFreshness.UNKNOWN
    sdk_versions: dict[str, str] = Field(default_factory=dict)
    simple_chat: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    streaming: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    tools: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    parallel_tools: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    reasoning_none: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    reasoning_max: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    json_object: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    json_schema: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    structured_fallback: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    agents_sdk: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    agent_as_tool: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    agents_sdk_structured_output: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    no_openai_network: CapabilityEvidence = Field(default_factory=CapabilityEvidence)
    observed_hosts: list[str] = Field(default_factory=list)
    # Token limits were added after the original live snapshot.  Defaults are
    # deliberately nullable to preserve compatibility with that snapshot.
    context_window_tokens: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    output_reserve_tokens: int | None = Field(default=None, ge=0)
    limits_source: str | None = Field(default=None, max_length=2_000)
    limits_observation: str | None = Field(default=None, max_length=4_000)
    token_limits: ModelTokenLimits | None = None
    # ``limits`` is accepted as a compatibility spelling for external
    # snapshots; ContextBudget resolves ``token_limits`` first, then ``limits``.
    limits: ModelTokenLimits | None = None

    def supports(self, capability: str) -> bool:
        evidence = getattr(self, capability, None)
        if not isinstance(evidence, CapabilityEvidence):
            raise ValueError(f"Capacidade desconhecida: {capability}")
        return evidence.status is CapabilityStatus.SUPPORTED

    def freshness_status(
        self,
        *,
        now: datetime | None = None,
        model: str | None = None,
        provider: str | None = None,
        base_url: str | None = None,
        configuration_identity: str | None = None,
        ttl_days: int = CAPABILITY_SNAPSHOT_TTL_DAYS,
    ) -> SnapshotFreshness:
        """Classify a snapshot without disabling historical capability use."""

        if ttl_days < 1 or not self.tested_at:
            return SnapshotFreshness.UNKNOWN
        if model is not None and self.model != model:
            return SnapshotFreshness.UNKNOWN
        if provider is not None and self.provider != provider:
            return SnapshotFreshness.UNKNOWN
        if base_url is not None and _endpoint_identity(self.base_url) != _endpoint_identity(base_url):
            return SnapshotFreshness.UNKNOWN
        if (
            configuration_identity is not None
            and self.configuration_identity != configuration_identity
        ):
            return SnapshotFreshness.UNKNOWN
        observed = self.tested_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        age_seconds = (reference - observed).total_seconds()
        return (
            SnapshotFreshness.STALE
            if age_seconds > ttl_days * 86_400
            else SnapshotFreshness.FRESH
        )

    def refresh_freshness(
        self,
        *,
        now: datetime | None = None,
        model: str | None = None,
        provider: str | None = None,
        base_url: str | None = None,
        configuration_identity: str | None = None,
        ttl_days: int = CAPABILITY_SNAPSHOT_TTL_DAYS,
    ) -> SnapshotFreshness:
        self.freshness = self.freshness_status(
            now=now,
            model=model,
            provider=provider,
            base_url=base_url,
            configuration_identity=configuration_identity,
            ttl_days=ttl_days,
        )
        return self.freshness

    def structured_output_strategy(self) -> tuple[str, ...]:
        """Ordem segura comprovada na Fase 1 para o deployment observado."""
        strategies: list[str] = []
        if self.supports("structured_fallback"):
            strategies.append("function_call")
        if self.supports("json_object"):
            strategies.append("json_object")
        strategies.append("validated_json")
        return tuple(strategies)

    def set_evidence(
        self,
        capability: str,
        status: CapabilityStatus,
        detail: str,
        latency_ms: int | None = None,
    ) -> None:
        if capability not in type(self).model_fields or not isinstance(
            getattr(self, capability, None), CapabilityEvidence
        ):
            raise ValueError(f"Capacidade desconhecida: {capability}")
        setattr(
            self,
            capability,
            CapabilityEvidence(status=status, detail=detail, latency_ms=latency_ms),
        )

    @property
    def context_window(self) -> int | None:
        """Compatibility alias used by older model registries."""

        return self.context_window_tokens

    @property
    def max_context_tokens(self) -> int | None:
        return self.context_window_tokens

    @property
    def input_token_limit(self) -> int | None:
        return self.max_input_tokens

    @property
    def output_token_limit(self) -> int | None:
        return self.max_output_tokens

    def resolved_token_limits(self) -> dict[str, int | str | None]:
        """Return direct/nested limit values without provider/network access."""

        nested = self.token_limits or self.limits
        return {
            "context_window_tokens": self.context_window_tokens
            if self.context_window_tokens is not None
            else (nested.context_window_tokens if nested else None),
            "max_input_tokens": self.max_input_tokens
            if self.max_input_tokens is not None
            else (nested.max_input_tokens if nested else None),
            "max_output_tokens": self.max_output_tokens
            if self.max_output_tokens is not None
            else (nested.max_output_tokens if nested else None),
            "output_reserve_tokens": self.output_reserve_tokens
            if self.output_reserve_tokens is not None
            else (nested.output_reserve_tokens if nested else None),
            "source": self.limits_source or (nested.source if nested else None),
            "observation": self.limits_observation or (nested.observation if nested else None),
        }


def load_capabilities_snapshot(
    path: Path,
    *,
    model: str,
    base_url: str,
    provider: str | None = None,
    configuration_identity: str | None = None,
    now: datetime | None = None,
    ttl_days: int = CAPABILITY_SNAPSHOT_TTL_DAYS,
) -> ModelCapabilities:
    """Carrega evidência somente quando modelo e endpoint correspondem ao probe."""
    expected_provider = provider or "huawei_modelarts_maas"

    def unknown() -> ModelCapabilities:
        return ModelCapabilities(
            model=model,
            base_url=base_url,
            provider=expected_provider,
            endpoint_identity=_endpoint_identity_text(base_url),
            configuration_identity=configuration_identity,
            freshness=SnapshotFreshness.UNKNOWN,
        )

    if not path.is_file():
        return unknown()
    capabilities = ModelCapabilities.model_validate_json(path.read_text(encoding="utf-8-sig"))
    observed_endpoint = _endpoint_identity(capabilities.base_url)
    configured_endpoint = _endpoint_identity(base_url)
    if (
        capabilities.model != model
        or observed_endpoint != configured_endpoint
        or (provider is not None and capabilities.provider != provider)
        or (
            configuration_identity is not None
            and capabilities.configuration_identity != configuration_identity
        )
    ):
        return unknown()
    if capabilities.endpoint_identity is None:
        capabilities.endpoint_identity = _endpoint_identity_text(capabilities.base_url)
    capabilities.refresh_freshness(
        now=now,
        model=model,
        provider=provider,
        base_url=base_url,
        configuration_identity=configuration_identity,
        ttl_days=ttl_days,
    )
    if capabilities.freshness is SnapshotFreshness.STALE:
        LOGGER.warning(
            "Capability snapshot stale; model=%s provider=%s tested_at=%s",
            capabilities.model,
            capabilities.provider,
            capabilities.tested_at.isoformat(),
        )
    return capabilities


def _endpoint_identity(value: str) -> tuple[str, str, int | None, str, str, str]:
    parsed = urlparse(value.rstrip("/"))
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower(),
        parsed.port,
        parsed.path,
        parsed.query,
        parsed.fragment,
    )


def _endpoint_identity_text(value: str) -> str:
    scheme, host, port, path, query, fragment = _endpoint_identity(value)
    authority = f"{host}:{port}" if port is not None else host
    suffix = path or "/"
    if query:
        suffix += f"?{query}"
    if fragment:
        suffix += f"#{fragment}"
    return f"{scheme}://{authority}{suffix}"
