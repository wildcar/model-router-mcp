"""Application settings, loaded from the environment / a local .env file.

Follows the pydantic-settings pattern used across the sibling repos (see
ort_bot/config.py). Every field carries an explicit UPPER_CASE alias so the
.env file reads like a conventional environment file.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    # --- Server ---------------------------------------------------------
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    log_file: str = Field(default="logs/router.log", alias="LOG_FILE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    db_path: str = Field(default=str(DATA_DIR / "router.db"), alias="DB_PATH")
    blob_dir: str = Field(default=str(DATA_DIR / "blobs"), alias="BLOB_DIR")
    # When set, requests to /mcp must carry `Authorization: Bearer <AUTH_TOKEN>`.
    # The web console/API is guarded separately (WEB_AUTH_* below); /health is open.
    auth_token: str | None = Field(default=None, alias="AUTH_TOKEN")
    # Extra Host headers /mcp accepts, comma-separated (e.g. the public domain
    # nginx forwards as `Host`). The MCP SDK guards against DNS rebinding and
    # otherwise answers 421 to anything but 127.0.0.1/localhost/[::1].
    mcp_allowed_hosts: str = Field(default="", alias="MCP_ALLOWED_HOSTS")

    # --- Web console login ------------------------------------------------
    # An htpasswd file (apr1 / bcrypt / {SHA} lines) whose users may sign in to
    # the console on /login. Unset means the console is not gated by the app at
    # all — local dev, or an nginx in front that still does Basic auth.
    web_auth_htpasswd: str | None = Field(default=None, alias="WEB_AUTH_HTPASSWD")
    # Signs the session cookie. Unset: a random key is generated once and kept
    # next to the database, so sessions survive a restart.
    web_auth_secret: str | None = Field(default=None, alias="WEB_AUTH_SECRET")
    web_auth_session_days: int = Field(default=30, alias="WEB_AUTH_SESSION_DAYS")
    # A request that reaches the app straight on loopback, without the
    # X-Real-IP that nginx adds, is a neighbouring service on this host (they
    # call 127.0.0.1:8088/api directly); it was never behind Basic auth either.
    web_auth_trust_loopback: bool = Field(default=True, alias="WEB_AUTH_TRUST_LOOPBACK")

    # --- HTTP defaults --------------------------------------------------
    http_timeout_seconds: float = Field(default=60.0, alias="HTTP_TIMEOUT_SECONDS")
    http_retries: int = Field(default=2, alias="HTTP_RETRIES")

    # --- Self identity ----------------------------------------------------
    # Who this router says it is to providers that take an app identity
    # (OpenRouter's HTTP-Referer / X-Title) when the caller supplied none:
    # requests with no app_url/app_name are made as the router itself.
    self_app_url: str = Field(default="https://mr.wildcar.org", alias="SELF_APP_URL")
    self_app_name: str = Field(default="model-router-mcp", alias="SELF_APP_NAME")

    # --- OpenRouter (chat + image catalog) ------------------------------
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    model_cache_ttl_seconds: int = Field(default=1800, alias="MODEL_CACHE_TTL_SECONDS")

    # --- Catalog sync ---------------------------------------------------
    # How often the background task re-reads every provider's model list.
    # 0 disables the timer; the TTL-guarded lazy sync still runs on reads.
    catalog_sync_interval_seconds: int = Field(
        default=21600, alias="CATALOG_SYNC_INTERVAL_SECONDS"
    )

    # --- DeepSeek (OpenAI-compatible chat) ------------------------------
    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/v1", alias="DEEPSEEK_BASE_URL"
    )

    # --- Anthropic / Claude API -----------------------------------------
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com", alias="ANTHROPIC_BASE_URL"
    )
    claude_api_model: str = Field(
        default="claude-sonnet-4-6", alias="CLAUDE_API_MODEL"
    )

    # --- Deepgram (speech-to-text) --------------------------------------
    deepgram_api_key: str | None = Field(default=None, alias="DEEPGRAM_API_KEY")
    deepgram_base_url: str = Field(
        default="https://api.deepgram.com/v1", alias="DEEPGRAM_BASE_URL"
    )
    deepgram_model: str = Field(default="nova-2", alias="DEEPGRAM_MODEL")

    # --- ElevenLabs (text-to-speech) ------------------------------------
    elevenlabs_api_key: str | None = Field(default=None, alias="ELEVENLABS_API_KEY")
    elevenlabs_base_url: str = Field(
        default="https://api.elevenlabs.io", alias="ELEVENLABS_BASE_URL"
    )
    elevenlabs_tts_model: str = Field(
        default="eleven_multilingual_v2", alias="ELEVENLABS_TTS_MODEL"
    )
    elevenlabs_tts_voice_name: str = Field(default="Nadia", alias="ELEVENLABS_TTS_VOICE_NAME")
    elevenlabs_tts_voice_id: str | None = Field(default=None, alias="ELEVENLABS_TTS_VOICE_ID")

    # --- Yandex SpeechKit (text-to-speech) --------------------------------
    yandex_api_key: str | None = Field(default=None, alias="YANDEX_API_KEY")
    yandex_tts_base_url: str = Field(
        default="https://tts.api.cloud.yandex.net", alias="YANDEX_TTS_BASE_URL"
    )
    yandex_tts_voice: str = Field(default="marina", alias="YANDEX_TTS_VOICE")
    # Needed only with IAM-token auth; an Api-Key already carries the folder.
    yandex_folder_id: str | None = Field(default=None, alias="YANDEX_FOLDER_ID")

    # --- Replicate (predictions; mostly billed per second of compute) ----
    replicate_api_token: str | None = Field(default=None, alias="REPLICATE_API_TOKEN")
    replicate_base_url: str = Field(
        default="https://api.replicate.com/v1", alias="REPLICATE_BASE_URL"
    )
    # Which models the registry carries, comma separated `owner/name` or
    # `owner/name:task`. Replicate publishes thousands; a registry is a curated
    # list, not a mirror. Empty means the adapter reports no catalog at all —
    # never an empty one, which the sync would read as "deprecate everything".
    replicate_models: str = Field(default="", alias="REPLICATE_MODELS")
    # Hold the create call open this long for a synchronous answer; the API caps
    # it at 60. A fast image then never enters the poll loop at all.
    replicate_wait_seconds: int = Field(default=60, alias="REPLICATE_WAIT_SECONDS")
    # Give up and cancel after this. Also sent as `Cancel-After`, so a prediction
    # this process abandons stops billing instead of running to completion.
    replicate_poll_deadline_seconds: int = Field(
        default=900, alias="REPLICATE_POLL_DEADLINE_SECONDS"
    )
    replicate_poll_min_interval_seconds: float = Field(
        default=1.0, alias="REPLICATE_POLL_MIN_INTERVAL_SECONDS"
    )
    replicate_poll_max_interval_seconds: float = Field(
        default=10.0, alias="REPLICATE_POLL_MAX_INTERVAL_SECONDS"
    )
    # How long a model's version hash and input schema are cached in process.
    replicate_model_ttl_seconds: int = Field(default=3600, alias="REPLICATE_MODEL_TTL_SECONDS")
    # Results are fetched into the blob store because Replicate's own links
    # expire in about an hour. Bigger ones stay links and say so.
    replicate_max_output_mb: float = Field(default=32.0, alias="REPLICATE_MAX_OUTPUT_MB")
    replicate_max_output_files: int = Field(default=8, alias="REPLICATE_MAX_OUTPUT_FILES")
    # Put the model's own progress output on the run timeline.
    replicate_stream_logs: bool = Field(default=True, alias="REPLICATE_STREAM_LOGS")

    # --- Handing our own files to a provider ------------------------------
    # A provider that takes file inputs by URL needs to reach ours. Empty means
    # no such host exists and only small files (inline data URIs) can be sent.
    # Set it to a domain that resolves to this service, e.g. https://blob.example.org
    public_blob_base_url: str | None = Field(default=None, alias="PUBLIC_BLOB_BASE_URL")
    # Links are signed and expire: blobs hold user requests and answers, and
    # "every blob is public" is a different and much worse thing than "this file,
    # for the next few minutes".
    public_blob_url_ttl_seconds: int = Field(
        default=900, alias="PUBLIC_BLOB_URL_TTL_SECONDS"
    )
    # The HMAC key. Without it no public link is ever issued, which is the safe
    # default: an unsigned public path would serve any ref to anyone who guesses.
    public_blob_signing_key: str | None = Field(default=None, alias="PUBLIC_BLOB_SIGNING_KEY")

    # --- Local models (computed in this process) -------------------------
    # Where `deploy/fetch-models.sh` puts the weights; one subdirectory per model
    # (`uvdoc/UVDoc_grid.onnx` + `.data`). Missing files answer
    # `model_not_loaded`, an installation error rather than a request one.
    local_models_dir: str = Field(default=str(DATA_DIR / "models"), alias="LOCAL_MODELS_DIR")
    # How many local runs may compute at once. Two because inference shares the
    # CPU with the router itself: a third concurrent run would slow every `chat`
    # answer in the same process, so it waits for a slot instead.
    local_max_concurrency: int = Field(default=2, alias="LOCAL_MAX_CONCURRENCY")
    # A local run past this answers `timeout` (retriable); the thread finishes
    # on its own and keeps its slot until it does.
    local_run_timeout_seconds: float = Field(default=60.0, alias="LOCAL_RUN_TIMEOUT_SECONDS")

    # --- Codex (OAuth image generation) ---------------------------------
    codex_auth_path: str | None = Field(default=None, alias="CODEX_AUTH_PATH")
    codex_base_url: str = Field(
        default="https://chatgpt.com/backend-api/codex", alias="CODEX_BASE_URL"
    )
    # The text model that *calls* the image tool; the picture itself comes from
    # `codex_image_tool_model`. It leads only while its codex-oauth registry row
    # is active; otherwise the adapter leads with a live text row of the provider
    # and the log carries `fallback_reason` (ADR-0010). Two slugs died under a
    # running service: gpt-5.4 (2026-09-05, HTTP 400 «model is not supported when
    # using Codex with a ChatGPT account», catalog dropped it the same day) and
    # gpt-5.5 (2026-09-08, HTTP 404 model_not_found while the catalog kept
    # listing it). Still point it at a live slug: the fallback costs one refused
    # call per retired leader.
    codex_image_main_model: str = Field(default="gpt-5.6-sol", alias="CODEX_IMAGE_MAIN_MODEL")
    # The model named in the `image_generation` tool — and the id of the registry
    # row an image request is logged under. The backend does not read it (nor
    # the tool's size and quality): every slug, real or made up, draws with its
    # own `gpt-image-2-codex` at `auto`/`auto` (checked 2026-09-10). What it did
    # apply is logged in `usage_raw` as `backend_image_model`, `actual_size`,
    # `actual_quality`. Changing this value renames the row, nothing more.
    codex_image_tool_model: str = Field(default="gpt-image-2", alias="CODEX_IMAGE_TOOL_MODEL")
    # The model list endpoint filters by client version (a model can declare a
    # `minimal_client_version`), so the version is a request parameter, not
    # decoration. Keep it at or above the codex CLI installed beside us.
    codex_client_version: str = Field(default="0.146.0", alias="CODEX_CLIENT_VERSION")
    codex_image_size: str = Field(default="1024x1536", alias="CODEX_IMAGE_SIZE")
    codex_image_quality: str = Field(default="medium", alias="CODEX_IMAGE_QUALITY")

    # --- CLI engines ----------------------------------------------------
    codex_command: str = Field(default="codex exec --model gpt-5.6-sol", alias="CODEX_COMMAND")
    claude_command: str = Field(default="claude", alias="CLAUDE_COMMAND")
    cli_run_timeout_sec: int = Field(default=1800, alias="CLI_RUN_TIMEOUT_SEC")
    # The claude CLI reports `total_cost_usd` whether it ran against an API key
    # or a subscription. Under a subscription that sum was never charged, so it
    # is logged as notional and kept out of real-money totals.
    claude_cli_subscription: bool = Field(default=True, alias="CLAUDE_CLI_SUBSCRIPTION")
    # Comma-separated list of directory prefixes a CLI request may use as cwd.
    cli_allowed_cwd_roots: str = Field(default="", alias="CLI_ALLOWED_CWD_ROOTS")
    # Default working directory for CLI runs; empty = the router process cwd.
    cli_default_cwd: str | None = Field(default=None, alias="CLI_DEFAULT_CWD")

    # --- Runs in flight --------------------------------------------------
    # How many runs the console may have going at once. Three because the whole
    # service shares one SQLite connection behind one write lock, and a
    # multi-model run of six would queue on that lock at exactly the moment the
    # operator is watching six timelines tick (concept §21, open question 5).
    max_concurrent_runs: int = Field(default=3, alias="MAX_CONCURRENT_RUNS")
    # How long a finished run stays in the in-process registry, for the tab that
    # was closed and reopened. The log is the durable record; this is a cache.
    run_registry_size: int = Field(default=200, alias="RUN_REGISTRY_SIZE")

    # --- Retention --------------------------------------------------------
    # 0 means "keep forever", which is what the service did before there was a
    # policy at all — so an installation that never sets these behaves as it did.
    log_retention_days: int = Field(default=0, alias="LOG_RETENTION_DAYS")
    # Response files are the bulk on disk; they can go earlier than the rows
    # that point at them, which then show the payload as no longer stored.
    blob_retention_days: int = Field(default=0, alias="BLOB_RETENTION_DAYS")
    # Runs the purge on a timer. 0 leaves it to the button on «Обзор».
    retention_sweep_interval_seconds: int = Field(
        default=86400, alias="RETENTION_SWEEP_INTERVAL_SECONDS"
    )

    # --- Notifications (a failed run reaches the operator) ---------------
    # Telegram first: a bot token from @BotFather and the chat(s) to write to,
    # comma separated (a personal chat id, or a group's negative id). Both empty
    # means the channel is off and nothing is ever sent.
    notify_telegram_bot_token: str | None = Field(default=None, alias="NOTIFY_TELEGRAM_BOT_TOKEN")
    notify_telegram_chat_ids: str = Field(default="", alias="NOTIFY_TELEGRAM_CHAT_IDS")
    notify_telegram_api_base: str = Field(
        default="https://api.telegram.org", alias="NOTIFY_TELEGRAM_API_BASE"
    )
    # E-mail, the second channel: plain SMTP. On only when both a recipient and
    # a host are set; STARTTLS on the usual submission port by default.
    notify_email_to: str = Field(default="", alias="NOTIFY_EMAIL_TO")
    notify_email_from: str | None = Field(default=None, alias="NOTIFY_EMAIL_FROM")
    notify_smtp_host: str | None = Field(default=None, alias="NOTIFY_SMTP_HOST")
    notify_smtp_port: int = Field(default=587, alias="NOTIFY_SMTP_PORT")
    notify_smtp_user: str | None = Field(default=None, alias="NOTIFY_SMTP_USER")
    notify_smtp_password: str | None = Field(default=None, alias="NOTIFY_SMTP_PASSWORD")
    notify_smtp_starttls: bool = Field(default=True, alias="NOTIFY_SMTP_STARTTLS")
    # Error codes (domain/errors.py `code`) that never page anyone: failures
    # that are a property of the request, not of the service. A refused
    # picture repeats for every retry of that picture and teaches the operator
    # nothing; an empty account (`insufficient_credits`) is exactly what the
    # channel exists for and is deliberately not in this list.
    notify_mute_codes: str = Field(
        default="content_filter,response_truncated,empty_response,bad_request,image_too_large",
        alias="NOTIFY_MUTE_CODES",
    )
    # Applications (`request_logs.app`) or callers (`external_user_id`) whose
    # failures stay in the log only, comma separated — a test bench, a client
    # known to send garbage. Empty means everyone's failures count.
    notify_mute_apps: str = Field(default="", alias="NOTIFY_MUTE_APPS")
    notify_mute_users: str = Field(default="", alias="NOTIFY_MUTE_USERS")
    # The same failure (code + provider + model) is sent once per window; the
    # repeats are counted and the count travels with the next message. 73 image
    # refusals in a day must not be 73 messages.
    notify_cooldown_seconds: int = Field(default=600, alias="NOTIFY_COOLDOWN_SECONDS")

    # --- Routing defaults (per modality) --------------------------------
    default_chat_model: str | None = Field(default=None, alias="DEFAULT_CHAT_MODEL")
    default_image_model: str | None = Field(default=None, alias="DEFAULT_IMAGE_MODEL")
    default_stt_model: str | None = Field(default=None, alias="DEFAULT_STT_MODEL")
    default_tts_model: str | None = Field(default=None, alias="DEFAULT_TTS_MODEL")

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH), env_file_encoding="utf-8", case_sensitive=False
    )

    def allowed_cwd_roots(self) -> list[str]:
        return [p.strip() for p in self.cli_allowed_cwd_roots.split(",") if p.strip()]

    @staticmethod
    def _csv(value: str | None) -> list[str]:
        return [p.strip() for p in (value or "").split(",") if p.strip()]

    def mcp_allowed_host_list(self) -> list[str]:
        return self._csv(self.mcp_allowed_hosts)

    def notify_chat_ids(self) -> list[str]:
        return self._csv(self.notify_telegram_chat_ids)

    def notify_recipients(self) -> list[str]:
        return self._csv(self.notify_email_to)

    def notify_muted_codes(self) -> set[str]:
        return set(self._csv(self.notify_mute_codes))

    def notify_muted_apps(self) -> set[str]:
        return set(self._csv(self.notify_mute_apps))

    def notify_muted_users(self) -> set[str]:
        return set(self._csv(self.notify_mute_users))


def load_settings() -> Settings:
    return Settings()
