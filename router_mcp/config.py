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
    # The web console/API is guarded separately (nginx Basic auth); /health is open.
    auth_token: str | None = Field(default=None, alias="AUTH_TOKEN")

    # --- HTTP defaults --------------------------------------------------
    http_timeout_seconds: float = Field(default=60.0, alias="HTTP_TIMEOUT_SECONDS")
    http_retries: int = Field(default=2, alias="HTTP_RETRIES")

    # --- OpenRouter (chat + image catalog) ------------------------------
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    model_cache_ttl_seconds: int = Field(default=1800, alias="MODEL_CACHE_TTL_SECONDS")

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
        default="eleven_flash_v2_5", alias="ELEVENLABS_TTS_MODEL"
    )
    elevenlabs_tts_voice_name: str = Field(default="Nadia", alias="ELEVENLABS_TTS_VOICE_NAME")
    elevenlabs_tts_voice_id: str | None = Field(default=None, alias="ELEVENLABS_TTS_VOICE_ID")

    # --- Codex (OAuth image generation) ---------------------------------
    codex_auth_path: str | None = Field(default=None, alias="CODEX_AUTH_PATH")
    codex_base_url: str = Field(
        default="https://chatgpt.com/backend-api/codex", alias="CODEX_BASE_URL"
    )
    codex_image_main_model: str = Field(default="gpt-5.4", alias="CODEX_IMAGE_MAIN_MODEL")
    codex_image_tool_model: str = Field(default="gpt-image-2", alias="CODEX_IMAGE_TOOL_MODEL")
    codex_image_size: str = Field(default="1024x1536", alias="CODEX_IMAGE_SIZE")
    codex_image_quality: str = Field(default="medium", alias="CODEX_IMAGE_QUALITY")

    # --- CLI engines ----------------------------------------------------
    codex_command: str = Field(default="codex exec --model gpt-5.5", alias="CODEX_COMMAND")
    claude_command: str = Field(default="claude", alias="CLAUDE_COMMAND")
    cli_run_timeout_sec: int = Field(default=1800, alias="CLI_RUN_TIMEOUT_SEC")
    # Comma-separated list of directory prefixes a CLI request may use as cwd.
    cli_allowed_cwd_roots: str = Field(default="", alias="CLI_ALLOWED_CWD_ROOTS")
    # Default working directory for CLI runs; empty = the router process cwd.
    cli_default_cwd: str | None = Field(default=None, alias="CLI_DEFAULT_CWD")

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


def load_settings() -> Settings:
    return Settings()
