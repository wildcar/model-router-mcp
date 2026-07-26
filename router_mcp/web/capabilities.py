"""What this installation can actually do, described to the console.

The rule the whole "Прогон" form rests on (§5 of `docs/UI_CONCEPT_v2.md`): a
field exists on screen only when the adapter behind the chosen
(modality, provider) pair reads it. That matrix is derived here from
`ProviderAdapter.SUPPORTED_PARAMS` — the same tuple the adapter builds its
payload from — because a hand-written copy in `web/` diverges from the code on
the first new adapter.

Also served here: what the server fixes and the caller cannot change (timeouts,
retries, allowed cwd roots), by environment-variable *name* and never by value,
and the glossary from `web/labels.py`.
"""

from __future__ import annotations

from typing import Any

from router_mcp import __version__
from router_mcp.adapters.base import ProviderAdapter
from router_mcp.config import Settings
from router_mcp.domain.modality import Modality
from router_mcp.web import labels

# The envelope shape the console is written against. The browser sends it back,
# so a page loaded before a restart cannot keep reading a newer API as if it
# were the old one.
SCHEMA_VERSION = 1


def _param(
    name: str,
    kind: str,
    label: str,
    help_text: str,
    **extra: Any,
) -> dict[str, Any]:
    return {"name": name, "type": kind, "label": label, "help": help_text, **extra}


# One description per parameter name, shared by every adapter that reads it: the
# meaning of `temperature` does not change between providers, only whether it is
# read at all.
PARAM_SPECS: dict[str, dict[str, Any]] = {
    "temperature": _param(
        "temperature", "number", "Температура",
        "Разброс ответов: 0 — почти повторяемо, выше 1 — свободнее",
        min=0.0, max=2.0, step=0.1, default=None,
    ),
    "top_p": _param(
        "top_p", "number", "top_p",
        "Доля вероятной массы, из которой выбираются слова. Меняют вместо температуры, не вместе с ней",
        min=0.0, max=1.0, step=0.05, default=None,
    ),
    "max_tokens": _param(
        "max_tokens", "integer", "Предел ответа, токенов",
        "Жёсткая граница. Упёршись в неё, модель обрывает ответ на полуслове",
        min=1, max=200000, step=1, default=None,
    ),
    "reasoning": _param(
        "reasoning", "text", "Рассуждение",
        "Имеет смысл только у рассуждающих моделей; остальные его игнорируют",
        default=None,
    ),
    "voice": _param(
        "voice", "text", "Голос",
        "Имя или идентификатор голоса у поставщика. Пусто — голос по умолчанию",
        default=None,
    ),
    "language": _param(
        "language", "text", "Язык", "Код языка синтеза, например ru", default="ru",
    ),
    "cwd": _param(
        "cwd", "select", "Рабочий каталог",
        "Каталог, в котором выполнится запуск. Список ограничен сервером",
        options=[], default=None,
    ),
    "timeout_sec": _param(
        "timeout_sec", "integer", "Таймаут запуска, с",
        "Через сколько секунд процесс будет убит",
        min=1, max=3600, step=1, default=None,
    ),
    "model": _param(
        "model", "text", "Модель CLI",
        "Передаётся движку как есть; пусто — модель по умолчанию у движка",
        default=None,
    ),
}


def _params_for(
    adapter: ProviderAdapter, modality: Modality, settings: Settings
) -> list[dict[str, Any]] | None:
    """The parameter list for one (modality, provider) pair, or None if the
    adapter has not declared one (then the console shows no parameter block
    rather than inventing fields)."""
    if adapter.SUPPORTED_PARAMS is None:
        return None
    names = adapter.SUPPORTED_PARAMS.get(modality)
    if names is None:
        return None
    out: list[dict[str, Any]] = []
    for name in names:
        spec = dict(PARAM_SPECS.get(name) or _param(name, "text", name, ""))
        if name == "cwd":
            # Free-form cwd would be an arbitrary-directory execution field on a
            # page behind one password; the roots are whitelisted server-side, so
            # offer exactly those.
            spec["options"] = settings.allowed_cwd_roots()
            spec["default"] = settings.cli_default_cwd
        if name == "timeout_sec":
            spec["default"] = settings.cli_run_timeout_sec
        out.append(spec)
    return out


# Per-model departures from the pair-level matrix: one pair is not enough for a
# key, because Anthropic makes max_tokens mandatory (and defaults it) where the
# OpenAI-shaped providers leave it optional.
def _model_overrides(provider_id: str) -> dict[str, Any]:
    if provider_id == "anthropic-api":
        return {
            "max_tokens": {
                "required": True,
                "default": 4096,
                "help": "У Anthropic это обязательное поле; без него запрос не уйдёт",
            }
        }
    return {}


def _server_settings(settings: Settings) -> list[dict[str, Any]]:
    """What the server fixes, shown read-only next to the form.

    Names of environment variables and a "задано" flag only. Values of secrets
    and filesystem paths are never shown: the console is on the internet behind
    one password, and a path is a map of the host.
    """
    roots = settings.allowed_cwd_roots()
    return [
        {
            "key": "http_timeout_seconds",
            "label": "Таймаут запроса к поставщику",
            "value": f"{settings.http_timeout_seconds:g} с",
            "env_var": "HTTP_TIMEOUT_SECONDS",
        },
        {
            "key": "http_retries",
            "label": "Повторов при временном отказе",
            "value": str(settings.http_retries),
            "env_var": "HTTP_RETRIES",
        },
        {
            "key": "cli_run_timeout_sec",
            "label": "Таймаут запуска CLI",
            "value": f"{settings.cli_run_timeout_sec} с",
            "env_var": "CLI_RUN_TIMEOUT_SEC",
        },
        {
            "key": "cli_allowed_cwd_roots",
            "label": "Разрешённых корней для CLI",
            "value": str(len(roots)) if roots else "не задано",
            "env_var": "CLI_ALLOWED_CWD_ROOTS",
        },
        {
            "key": "catalog_sync_interval_seconds",
            "label": "Период сверки каталогов",
            "value": (
                f"{settings.catalog_sync_interval_seconds // 3600} ч"
                if settings.catalog_sync_interval_seconds
                else "выключена"
            ),
            "env_var": "CATALOG_SYNC_INTERVAL_SECONDS",
        },
    ]


def build(
    settings: Settings,
    adapters: dict[str, ProviderAdapter],
    providers: list[Any],
    enabled_ids: set[str],
) -> dict[str, Any]:
    """The full capabilities payload."""
    params: dict[str, dict[str, Any]] = {}
    for modality in Modality:
        per_provider: dict[str, Any] = {}
        for provider_id, adapter in adapters.items():
            if modality not in adapter.supported_modalities:
                continue
            spec = _params_for(adapter, modality, settings)
            if spec is None:
                continue
            per_provider[provider_id] = {
                "params": spec,
                "model_overrides": _model_overrides(provider_id),
            }
        if per_provider:
            params[modality.value] = per_provider

    provider_rows = [
        {
            "id": p.id,
            "display_name": p.display_name,
            "adapter": p.adapter,
            "enabled": p.id in enabled_ids,
            "configured": p.id in adapters and adapters[p.id].is_configured(),
            "env_var": adapters[p.id].env_var if p.id in adapters else None,
            "pricing_url": p.pricing_url,
            "modalities": sorted(
                m.value for m in adapters[p.id].supported_modalities
            ) if p.id in adapters else [],
        }
        for p in providers
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "app_version": __version__,
        "providers": provider_rows,
        "params": params,
        "server": _server_settings(settings),
        "labels": labels.as_dict(),
    }
