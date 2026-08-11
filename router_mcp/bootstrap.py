"""Assemble the application core: DB, adapters, registry, router.

This is the single place that knows how to wire concrete adapters from settings.
Both the FastAPI app (production) and tests build their core here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from router_mcp.adapters.base import ProviderAdapter
from router_mcp.adapters.claude_api import PRICING_PAGE as anthropic_pricing_page
from router_mcp.adapters.claude_api import ClaudeApiAdapter
from router_mcp.adapters.claude_cli import ClaudeCliAdapter
from router_mcp.adapters.codex_cli import CodexCliAdapter
from router_mcp.adapters.codex_oauth import CodexOAuthAdapter
from router_mcp.adapters.deepgram import DeepgramAdapter
from router_mcp.adapters.deepseek import PRICING_PAGE as deepseek_pricing_page
from router_mcp.adapters.deepseek import DeepSeekAdapter
from router_mcp.adapters.elevenlabs import ElevenLabsAdapter
from router_mcp.adapters.yandex import YandexSpeechKitAdapter
from router_mcp.adapters.openrouter import OpenRouterAdapter
from router_mcp.adapters.replicate import PRICING_PAGE as replicate_pricing_page
from router_mcp.adapters.replicate import ReplicateAdapter
from router_mcp.blobs.store import BlobStore
from router_mcp.config import Settings
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database
from router_mcp.web import public_blobs
from router_mcp.web.runs import RunRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProviderSpec:
    id: str
    display_name: str
    adapter_key: str
    base_url: str | None
    adapter: ProviderAdapter
    # Where this provider publishes its prices. Shown in the registry console so
    # a number can always be traced back to a source.
    pricing_url: str | None = None
    # Seed models for providers without a live catalog (CLI engines, single-model
    # services). Inserted only when the row does not exist yet: a restart must
    # never resurrect a model the provider has retired.
    seed_models: list[RegistryModel] = field(default_factory=list)


@dataclass(slots=True)
class AppCore:
    settings: Settings
    db: Database
    blobs: BlobStore
    registry: RegistryService
    router: ModelRouter
    adapters: dict[str, ProviderAdapter]
    # Runs this process has in flight. Assigned right after construction,
    # because the registry needs the assembled core to dispatch through.
    runs: "RunRegistry | None" = None
    # When this process built its core. The console shows uptime, and a service
    # that restarted an hour ago explains a lot of otherwise odd numbers.
    started_at: float = field(default_factory=time.time)

    async def aclose(self) -> None:
        if self.runs is not None:
            await self.runs.aclose()
        for adapter in self.adapters.values():
            try:
                await adapter.close()
            except Exception:  # pragma: no cover - best effort cleanup
                logger.warning("Error closing adapter %s", adapter.name, exc_info=True)
        try:
            await self.registry.close()
        finally:
            await self.db.close()


def _seed_model(
    sp_id: str,
    provider: str,
    model_id: str,
    name: str,
    task: Task,
    price_source: str | None = None,
    extra_tasks: tuple[Task, ...] = (),
    **prices,
) -> RegistryModel:
    """A fixed-catalog registry row for a provider without a live catalog."""
    return RegistryModel(
        service_provider_id=sp_id,
        model_provider=provider,
        model_id=model_id,
        display_name=name,
        modalities=(),
        tasks=(task, *extra_tasks),
        tier=prices.pop("tier", "expensive"),
        source="seed",
        price_source=price_source,
        price_source_kind="estimate",
        **prices,
    )


def _chat_seed(
    sp_id: str, provider: str, model_id: str, name: str,
    prompt_price: float, completion_price: float, price_source: str | None = None,
) -> RegistryModel:
    """A fixed chat registry row with a tier computed from approximate prices.

    These numbers are transcribed by hand and go stale, so they stay marked as
    estimates: costs computed from them are logged with `cost_source='estimated'`
    until someone confirms the price in the registry admin (/registry).
    """
    return RegistryModel(
        service_provider_id=sp_id,
        model_provider=provider,
        model_id=model_id,
        display_name=name,
        modalities=(),
        tasks=(Task.TEXT_TO_TEXT,),
        tier=RegistryModel.compute_tier(prompt_price, completion_price),
        prompt_price=prompt_price,
        completion_price=completion_price,
        source="seed",
        price_source=price_source,
        price_source_kind="estimate",
    )


def _build_adapters(settings: Settings) -> list[ProviderSpec]:
    """Construct every adapter we ship. Phase 1 = OpenRouter; Phase 2 adds
    Codex-image / Deepgram / ElevenLabs / Yandex SpeechKit; later phases append
    DeepSeek/Claude/CLI."""
    return [
        ProviderSpec(
            id="openrouter",
            display_name="OpenRouter",
            adapter_key="openrouter",
            base_url=settings.openrouter_base_url,
            pricing_url="https://openrouter.ai/models",
            adapter=OpenRouterAdapter(
                api_key=settings.openrouter_api_key,
                base_url=settings.openrouter_base_url,
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
        ),
        ProviderSpec(
            id="codex-oauth",
            display_name="Codex (OAuth)",
            adapter_key="codex_oauth",
            base_url=settings.codex_base_url,
            pricing_url="https://platform.openai.com/docs/pricing",
            adapter=CodexOAuthAdapter(
                auth_path=settings.codex_auth_path,
                base_url=settings.codex_base_url,
                main_model=settings.codex_image_main_model,
                image_model=settings.codex_image_tool_model,
                size=settings.codex_image_size,
                quality=settings.codex_image_quality,
                client_version=settings.codex_client_version,
            ),
            # Two models, because there are two: the image tool and the model
            # that answers text and reads pictures. One seeded row was why the
            # provider looked like it could only draw.
            seed_models=[
                _seed_model(
                    "codex-oauth", "openai", settings.codex_image_tool_model,
                    "Codex image generation", Task.TEXT_TO_IMAGE, tier="expensive",
                ),
                _seed_model(
                    "codex-oauth", "openai", settings.codex_image_main_model,
                    "Codex (текст и vision)", Task.TEXT_TO_TEXT, tier="expensive",
                    extra_tasks=(Task.IMAGE_TO_TEXT,),
                ),
            ],
        ),
        ProviderSpec(
            id="deepgram",
            display_name="Deepgram (STT)",
            adapter_key="deepgram",
            base_url=settings.deepgram_base_url,
            pricing_url="https://deepgram.com/pricing",
            adapter=DeepgramAdapter(
                api_key=settings.deepgram_api_key,
                base_url=settings.deepgram_base_url,
                model=settings.deepgram_model,
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
            seed_models=[
                _seed_model(
                    "deepgram", "deepgram", settings.deepgram_model,
                    "Deepgram speech-to-text", Task.SPEECH_TO_TEXT, tier="cheap",
                )
            ],
        ),
        ProviderSpec(
            id="elevenlabs",
            display_name="ElevenLabs (TTS)",
            adapter_key="elevenlabs",
            base_url=settings.elevenlabs_base_url,
            pricing_url="https://elevenlabs.io/pricing",
            adapter=ElevenLabsAdapter(
                api_key=settings.elevenlabs_api_key,
                base_url=settings.elevenlabs_base_url,
                model_id=settings.elevenlabs_tts_model,
                voice_name=settings.elevenlabs_tts_voice_name,
                voice_id=settings.elevenlabs_tts_voice_id,
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
            seed_models=[
                _seed_model(
                    "elevenlabs", "elevenlabs", settings.elevenlabs_tts_model,
                    "ElevenLabs text-to-speech", Task.TEXT_TO_SPEECH, tier="expensive",
                )
            ],
        ),
        ProviderSpec(
            id="yandex",
            display_name="Yandex SpeechKit (TTS)",
            adapter_key="yandex",
            base_url=settings.yandex_tts_base_url,
            pricing_url="https://yandex.cloud/en/docs/speechkit/pricing",
            adapter=YandexSpeechKitAdapter(
                api_key=settings.yandex_api_key,
                base_url=settings.yandex_tts_base_url,
                voice=settings.yandex_tts_voice,
                folder_id=settings.yandex_folder_id,
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
            seed_models=[
                # model_id must sort after "eleven_multilingual_v2": with two
                # unpriced TTS rows the sort falls through to model_id, and
                # ElevenLabs stays the unhinted default (see _candidate_sort_key).
                _seed_model(
                    "yandex", "yandex", "speechkit-tts-v1",
                    "Yandex SpeechKit TTS", Task.TEXT_TO_SPEECH, tier="expensive",
                    price_source="https://yandex.cloud/en/docs/speechkit/pricing",
                )
            ],
        ),
        ProviderSpec(
            id="replicate",
            display_name="Replicate",
            adapter_key="replicate",
            base_url=settings.replicate_base_url,
            pricing_url=replicate_pricing_page,
            adapter=ReplicateAdapter(
                api_token=settings.replicate_api_token,
                base_url=settings.replicate_base_url,
                models=settings.replicate_models,
                wait_seconds=settings.replicate_wait_seconds,
                poll_deadline_seconds=settings.replicate_poll_deadline_seconds,
                poll_min_interval=settings.replicate_poll_min_interval_seconds,
                poll_max_interval=settings.replicate_poll_max_interval_seconds,
                model_ttl_seconds=settings.replicate_model_ttl_seconds,
                max_output_mb=settings.replicate_max_output_mb,
                max_output_files=settings.replicate_max_output_files,
                stream_logs=settings.replicate_stream_logs,
                # A file this service holds, addressed so Replicate can fetch it.
                # None unless a public host and a signing key are both configured.
                public_blob_url=lambda ref: public_blobs.make_url(settings, ref),
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
            # No seeds: the catalog is REPLICATE_MODELS, and a hardcoded slug
            # outlives its author — which is exactly what happened with DeepSeek.
        ),
        ProviderSpec(
            id="deepseek",
            display_name="DeepSeek",
            adapter_key="deepseek",
            base_url=settings.deepseek_base_url,
            pricing_url=deepseek_pricing_page,
            adapter=DeepSeekAdapter(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
            # No seeds: DeepSeek renames its models (v4-pro/v4-flash replaced
            # deepseek-chat/deepseek-reasoner) and hardcoded ids kept coming back
            # from restarts long after the provider had retired them. The catalog
            # sync supplies the live list; prices are filled in /registry.
        ),
        ProviderSpec(
            id="anthropic-api",
            display_name="Claude (Anthropic API)",
            adapter_key="claude_api",
            base_url=settings.anthropic_base_url,
            pricing_url=anthropic_pricing_page,
            adapter=ClaudeApiAdapter(
                api_key=settings.anthropic_api_key,
                base_url=settings.anthropic_base_url,
                timeout_seconds=settings.http_timeout_seconds,
                retries=settings.http_retries,
            ),
            seed_models=[
                # Approximate USD/token (Anthropic list pricing) — adjust in /registry.
                # The model list itself is refreshed by the catalog sync; these
                # rows exist so prices survive before anyone edits them.
                _chat_seed("anthropic-api", "anthropic", "claude-haiku-4-5-20251001", "Claude Haiku 4.5", 8e-7, 4e-6, anthropic_pricing_page),
                _chat_seed("anthropic-api", "anthropic", "claude-sonnet-4-6", "Claude Sonnet 4.6", 3e-6, 1.5e-5, anthropic_pricing_page),
                _chat_seed("anthropic-api", "anthropic", "claude-opus-4-8", "Claude Opus 4.8", 1.5e-5, 7.5e-5, anthropic_pricing_page),
            ],
        ),
        ProviderSpec(
            id="codex-cli",
            display_name="Codex CLI",
            adapter_key="codex_cli",
            base_url=None,
            adapter=CodexCliAdapter(
                command=settings.codex_command,
                timeout_sec=settings.cli_run_timeout_sec,
                allowed_cwd_roots=settings.allowed_cwd_roots(),
                default_cwd=settings.cli_default_cwd,
            ),
            seed_models=[
                _seed_model("codex-cli", "openai", "codex", "Codex CLI", Task.CLI, tier="expensive")
            ],
        ),
        ProviderSpec(
            id="claude-cli",
            display_name="Claude CLI",
            adapter_key="claude_cli",
            base_url=None,
            adapter=ClaudeCliAdapter(
                command=settings.claude_command,
                timeout_sec=settings.cli_run_timeout_sec,
                subscription=settings.claude_cli_subscription,
                allowed_cwd_roots=settings.allowed_cwd_roots(),
                default_cwd=settings.cli_default_cwd,
            ),
            seed_models=[
                _seed_model("claude-cli", "anthropic", "claude", "Claude CLI", Task.CLI, tier="expensive")
            ],
        ),
    ]


async def build_core(settings: Settings) -> AppCore:
    db = Database(settings.db_path)
    await db.init()
    # Runs the previous process was executing when it stopped cannot be resumed;
    # left as they are they read «выполняется» forever and the console has no way
    # to tell them from a call that is genuinely in flight right now.
    interrupted = await db.interrupt_live_logs()
    if interrupted:
        logger.warning(
            "Marked %d run(s) interrupted: the service restarted while they ran",
            interrupted,
        )

    blobs = BlobStore(settings.blob_dir)

    specs = _build_adapters(settings)
    adapters: dict[str, ProviderAdapter] = {spec.id: spec.adapter for spec in specs}

    # Seed service_providers. `enabled` is an admin toggle: it takes the adapter's
    # configured state on first insert only — later restarts leave it alone
    # (upsert_service_provider skips it on conflict). Dispatch re-checks
    # is_configured() live; /api/registry/providers exposes both flags.
    # Then seed each provider's fixed-catalog models (idempotent upsert).
    for spec in specs:
        await db.upsert_service_provider(
            provider_id=spec.id,
            display_name=spec.display_name,
            adapter=spec.adapter_key,
            base_url=spec.base_url,
            enabled=spec.adapter.is_configured(),
            pricing_url=spec.pricing_url,
        )
        for seed in spec.seed_models:
            await db.seed_registry_model(seed)

    registry = RegistryService(
        db=db,
        adapters=adapters,
        ttl_seconds=settings.model_cache_ttl_seconds,
    )
    # Best-effort initial catalog sync (skips providers without keys/catalogs).
    try:
        await registry.sync_all(force=True)
    except Exception:  # pragma: no cover - never block startup on catalog
        logger.warning("Initial catalog sync failed", exc_info=True)

    defaults = {
        Modality.CHAT: settings.default_chat_model,
        Modality.IMAGE: settings.default_image_model,
        Modality.STT: settings.default_stt_model,
        Modality.TTS: settings.default_tts_model,
    }
    router = ModelRouter(
        db=db, registry=registry, adapters=adapters, blobs=blobs, defaults=defaults
    )

    core = AppCore(
        settings=settings,
        db=db,
        blobs=blobs,
        registry=registry,
        router=router,
        adapters=adapters,
    )
    core.runs = RunRegistry(
        core,
        max_concurrent=settings.max_concurrent_runs,
        keep=settings.run_registry_size,
    )
    return core
