"""What this installation can actually do, described to the console.

The rule the whole "Прогон" form rests on (§5 of `docs/UI_CONCEPT_v2.md`): a
field exists on screen only when the adapter behind the chosen
(task, provider) pair reads it. That matrix is derived here from
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
from router_mcp.domain.task import TASKS, Task, tasks_for
from router_mcp.web import labels

# The envelope shape the console is written against. The browser sends it back,
# so a page loaded before a restart cannot keep reading a newer API as if it
# were the old one.
# Version 2 added `tasks` and `params_by_task` (ADR-0006). `params`, keyed by
# billing group, is still served so a page loaded before the restart keeps
# working — which is the whole reason this field exists.
SCHEMA_VERSION = 2


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
    "system_prompt": _param(
        "system_prompt", "text", "Системный промпт",
        "Роль и правила, которые модель держит поверх запроса",
        default=None,
    ),
    "negative_prompt": _param(
        "negative_prompt", "text", "Чего не рисовать",
        "Список того, чего в картинке быть не должно",
        default=None,
    ),
    "aspect_ratio": _param(
        "aspect_ratio", "text", "Соотношение сторон",
        "Например 16:9. Что именно принимает модель, написано в её карточке",
        default=None,
    ),
    "width": _param(
        "width", "integer", "Ширина, пикселей", "Многие модели требуют кратности 8",
        min=64, max=4096, step=8, default=None,
    ),
    "height": _param(
        "height", "integer", "Высота, пикселей", "Многие модели требуют кратности 8",
        min=64, max=4096, step=8, default=None,
    ),
    "num_outputs": _param(
        "num_outputs", "integer", "Сколько вариантов",
        "Каждый вариант оплачивается отдельно",
        min=1, max=8, step=1, default=None,
    ),
    "num_inference_steps": _param(
        "num_inference_steps", "integer", "Шагов расчёта",
        "Больше шагов — дольше и дороже, качество растёт не бесконечно",
        min=1, max=200, step=1, default=None,
    ),
    "guidance_scale": _param(
        "guidance_scale", "number", "Строгость к описанию",
        "Выше — ближе к тексту и однообразнее",
        min=0.0, max=30.0, step=0.5, default=None,
    ),
    "strength": _param(
        "strength", "number", "Насколько менять исходник",
        "0 — почти не трогать, 1 — переписать заново",
        min=0.0, max=1.0, step=0.05, default=None,
    ),
    "mask": _param(
        "mask", "text", "Маска", "Ссылка на чёрно-белую картинку: белое меняется",
        default=None,
    ),
    "seed": _param(
        "seed", "integer", "Зерно",
        "Одно и то же зерно даёт тот же результат. Пусто — каждый раз новое",
        min=0, step=1, default=None,
    ),
    "output_format": _param(
        "output_format", "text", "Формат файла", "Например png, jpg, mp3", default=None,
    ),
    "output_quality": _param(
        "output_quality", "integer", "Качество сжатия",
        "Для jpg и webp: выше число — больше файл",
        min=1, max=100, step=1, default=None,
    ),
    "num_frames": _param(
        "num_frames", "integer", "Кадров", "Длина клипа в кадрах",
        min=1, max=1000, step=1, default=None,
    ),
    "fps": _param(
        "fps", "integer", "Кадров в секунду", "Обычно 8, 16 или 24",
        min=1, max=60, step=1, default=None,
    ),
    "duration": _param(
        "duration", "integer", "Длительность, секунд", "Сколько звука сгенерировать",
        min=1, max=600, step=1, default=None,
    ),
    "speed": _param(
        "speed", "number", "Скорость речи", "1.0 — как есть, меньше — медленнее",
        min=0.25, max=4.0, step=0.05, default=None,
    ),
    "translate": _param(
        "translate", "text", "Переводить на английский",
        "Расшифровка сразу переводится, вместо расшифровки на языке записи",
        default=None,
    ),
    "size": _param(
        "size", "select", "Размер картинки",
        "Ширина на высоту в пикселях. Пусто — размер из настроек сервера",
        options=["1024x1024", "1024x1536", "1536x1024"], default=None,
    ),
    "quality": _param(
        "quality", "select", "Качество картинки",
        "Выше качество — дольше и дороже",
        options=["low", "medium", "high"], default=None,
    ),
    "instructions": _param(
        "instructions", "text", "Системная инструкция",
        "Что модели держать в голове поверх запроса. Пусто — без инструкции",
        default=None,
    ),
    "reasoning_effort": _param(
        "reasoning_effort", "select", "Глубина рассуждения",
        "Сколько модели думать перед ответом. Дольше — дороже",
        options=["low", "medium", "high"], default=None,
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
    adapter: ProviderAdapter, task: Task, settings: Settings
) -> list[dict[str, Any]] | None:
    """The parameter list for one (task, provider) pair, or None if the
    adapter has not declared one (then the console shows no parameter block
    rather than inventing fields)."""
    if adapter.SUPPORTED_PARAMS is None:
        return None
    names = adapter.SUPPORTED_PARAMS.get(task)
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
    by_task: dict[str, dict[str, Any]] = {}
    for task in Task:
        per_provider: dict[str, Any] = {}
        for provider_id, adapter in adapters.items():
            if task not in adapter.supported_tasks:
                continue
            spec = _params_for(adapter, task, settings)
            if spec is None:
                continue
            per_provider[provider_id] = {
                "params": spec,
                "model_overrides": _model_overrides(provider_id),
            }
        if per_provider:
            by_task[task.value] = per_provider

    # The group-keyed view, for a page loaded before this restart. A group can
    # hold several tasks, so a provider's entry is whichever of its tasks in that
    # group it declared first — good enough for one release of compatibility, and
    # `schema_version` tells a current page to read `params_by_task` instead.
    params: dict[str, dict[str, Any]] = {}
    for group in Modality:
        per_provider = {}
        for task in tasks_for(group):
            for provider_id, spec in by_task.get(task.value, {}).items():
                per_provider.setdefault(provider_id, spec)
        if per_provider:
            params[group.value] = per_provider

    task_rows = {
        task.value: {
            **labels.TASKS.get(task.value, {"label": task.value, "help": "", "code": task.value}),
            "billing_group": sig.billing.value,
            "requires": sorted(m.value for m in sig.requires),
            "optional": sorted(m.value for m in sig.optional),
            "produces": sorted(m.value for m in sig.produces),
            "is_media_transform": sig.is_media_transform,
            # Two different answers, and conflating them is what the whole
            # taxonomy is against. `available`: some adapter here can drive this
            # task at all. `configured`: one of those adapters has its credentials.
            # The console disables the first and merely warns about the second, so
            # "мы так не умеем" never reads like "ключ не введён".
            "available": task.value in by_task,
            "configured": any(
                adapters[p].is_configured()
                for p in by_task.get(task.value, {})
                if p in adapters
            ),
        }
        for task, sig in TASKS.items()
    }

    provider_rows = [
        {
            "id": p.id,
            "display_name": p.display_name,
            "adapter": p.adapter,
            "enabled": p.id in enabled_ids,
            "configured": p.id in adapters and adapters[p.id].is_configured(),
            "env_var": adapters[p.id].env_var if p.id in adapters else None,
            "pricing_url": p.pricing_url,
            "tasks": sorted(
                t.value for t in adapters[p.id].supported_tasks
            ) if p.id in adapters else [],
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
        "tasks": task_rows,
        "params_by_task": by_task,
        # Deprecated in favour of `params_by_task`; see SCHEMA_VERSION.
        "params": params,
        "server": _server_settings(settings),
        "labels": labels.as_dict(),
    }
