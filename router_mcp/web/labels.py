"""The glossary: one word per concept, in one file.

Today the same thing is called three different names — one in the concept, one
in the registry template, one in the schema. §3 of `docs/UI_CONCEPT_v2.md` fixes
the vocabulary and puts it in a single place, because a caption repeated in three
templates drifts in three directions.

Two rules the shapes below encode:

* a term appearing on screen for the first time carries a visible explanation
  under the control — a tooltip is for refinement, not for the first
  explanation. That is what `help` is; it is rendered, not hovered.
* the English identifier stays visible next to the Russian caption wherever the
  operator will go looking for it in the API or the logs (`model_id`,
  `finish_reason`, environment variable names).

Serving this from the server rather than hard-coding it in JS keeps the domain
knowledge — units, reason texts, what a field means — on one side of the wire,
and leaves the door open for a second language.
"""

from __future__ import annotations

from typing import Any


def _t(label: str, help_text: str = "", code: str | None = None) -> dict[str, Any]:
    """A caption, its visible explanation, and the identifier it maps to."""
    return {"label": label, "help": help_text, "code": code}


# Field captions. The key is the field name as it appears in the API and the
# database, so a screen never invents its own name for a column.
FIELDS: dict[str, dict[str, Any]] = {
    "external_user_id": _t(
        "Метка пользователя",
        "Любая строка для группировки статистики. На доступ и маршрутизацию не влияет",
        "external_user_id",
    ),
    "service_provider_id": _t(
        "Поставщик", "Через кого идёт запрос и чей ключ тратится", "service_provider_id"
    ),
    "model_provider": _t("Вендор", "Кто сделал модель", "model_provider"),
    "model_id": _t("Модель", "Идентификатор модели у поставщика", "model_id"),
    "tier": _t(
        "Ценовой уровень",
        "Проставлен в реестре по сумме ставок: free, до $1 за 1M, выше",
        "tier",
    ),
    "task": _t(
        "Задача",
        "Что именно просят сделать: текст в текст, текст в картинку, речь в текст",
        "task",
    ),
    "modality": _t(
        "Группа тарификации", "По какой единице считаются деньги за такой запрос", "modality"
    ),
    "channel": _t("Канал", "Откуда пришёл вызов: MCP-клиент, API, консоль", "channel"),
    "latency_ms": _t("Время ответа", "Включая повторы и ожидание", "latency_ms"),
    "attempts": _t(
        "Повторов", "Сколько раз запрос повторялся из-за временных отказов", "attempts"
    ),
    "finish_reason": _t(
        "Причина остановки", "Почему модель прекратила генерацию", "finish_reason"
    ),
    "provider_request_id": _t(
        "id запроса у провайдера",
        "Эту строку называют в обращении в поддержку поставщика",
        "provider_request_id",
    ),
    "cost_source": _t("Откуда сумма", "Насколько числу можно верить", "cost_source"),
    "blob": _t("Файл ответа", "Скачать; идентификатор виден во вкладке «Сырьё»"),
}

# Six states a price can be in. Several billing groups have no rate filled in on
# a fresh install, so "нет методики" has to be a visible state and not a quiet zero.
COST_SOURCES: dict[str, dict[str, Any]] = {
    "computed": _t("посчитана", "Ставка подтверждена: каталог, прайс-страница или админ"),
    "estimated": _t("прикидка", "Ставка не подтверждена, число указывает порядок"),
    "provider": _t("от провайдера", "Сумму назвал сам поставщик"),
    "provider_notional": _t(
        "условная", "Работа шла по подписке, деньги за этот запрос не списывались"
    ),
    "amortized": _t("по подписке", "Стоимость размазана по месячному пулу"),
    "unpriced": _t("нет методики", "Потребление есть, а ставки для него в реестре нет"),
    "cost_unknown": _t(
        "неизвестна",
        "Ответ не дошёл: запрос, скорее всего, отработан у поставщика и оплачен",
    ),
}

# Where a price in the registry came from.
PRICE_SOURCE_KINDS: dict[str, dict[str, Any]] = {
    "api": _t("из каталога", "Цену отдал сам поставщик по API"),
    "page": _t("с прайс-страницы", "Человек взял цену со страницы поставщика"),
    "manual": _t("введена вручную", "Цену ввёл администратор, синхронизация её не тронет"),
    "estimate": _t("прикидка", "Цену никто не подтверждал"),
    "unknown": _t("цены нет", "Каталог отдал модель без цены"),
}

# Что именно просят сделать. Это ключ маршрутизации: модель объявляет набор
# задач, и запрос попадает только к той, которая его умеет.
TASKS: dict[str, dict[str, Any]] = {
    "text-to-text": _t("Текст в текст", "Обычный запрос к языковой модели", "text-to-text"),
    "image-to-text": _t(
        "Картинка в текст",
        "Модель смотрит на изображение и отвечает текстом: описание, распознавание надписей",
        "image-to-text",
    ),
    "text-to-image": _t("Текст в картинку", "Рисует изображение по описанию", "text-to-image"),
    "image-to-image": _t(
        "Картинка в картинку",
        "Правит готовое изображение по описанию. Умеют это далеко не все модели, "
        "которые умеют рисовать",
        "image-to-image",
    ),
    "speech-to-text": _t("Речь в текст", "Расшифровка записи", "speech-to-text"),
    "text-to-speech": _t("Текст в речь", "Озвучивание текста голосом", "text-to-speech"),
    "text-to-audio": _t(
        "Текст в музыку и звук",
        "Сочиняет музыку или звуковой эффект по описанию. Голос читает «Текст в речь»",
        "text-to-audio",
    ),
    "text-to-video": _t("Текст в видео", "Снимает короткий клип по описанию", "text-to-video"),
    "image-to-video": _t(
        "Картинка в видео", "Оживляет готовое изображение", "image-to-video"
    ),
    "cli": _t("CLI", "Запуск локального движка codex или claude", "cli"),
}

# Группа тарификации, она же имя ручки. Задач больше, чем групп: «текст в
# картинку» и «картинка в картинку» считаются одинаково, за картинку.
MODALITIES: dict[str, dict[str, Any]] = {
    "chat": _t("Чат", "Считается по токенам"),
    "image": _t("Картинка", "Считается за готовое изображение"),
    "stt": _t("Распознавание", "Считается по секундам входной записи"),
    "tts": _t("Синтез", "Считается по символам озвученного текста"),
    "cli": _t("CLI", "Считается по токенам локального движка"),
    "audio": _t("Звук", "Музыка и звуковые эффекты, считаются по секундам работы модели"),
    "video": _t("Видео", "Считается по секундам работы модели"),
}

CHANNELS: dict[str, dict[str, Any]] = {
    "ui": _t("консоль", "Вызов из этой веб-консоли"),
    "api": _t("API", "Прямой вызов JSON API"),
    "mcp": _t("MCP", "Вызов инструмента по MCP"),
}

STATUSES: dict[str, dict[str, Any]] = {
    "queued": _t("в очереди", "Прогон принят, ещё не начат"),
    "running": _t("выполняется", "Прогон идёт прямо сейчас"),
    "ok": _t("ok", "Запрос отработал"),
    "error": _t("ошибка", "Запрос не отработал"),
    "cancelled": _t("прерван", "Прогон остановил оператор"),
    "interrupted": _t("оборван", "Сервис перезапустился, пока прогон шёл"),
}

SOURCES: dict[str, dict[str, Any]] = {
    "catalog": _t("из каталога", "Строку принесла синхронизация с поставщиком"),
    "seed": _t("из заготовки", "Строка создана при первом запуске сервиса"),
    "manual": _t("добавлена вручную", "Строку завёл администратор"),
}

# Fallback as one select in plain words. The policy/scope pair behind each line
# is an implementation detail the operator never has to assemble by hand: three
# controls in a row do not read without the sources open.
FALLBACK_CHOICES: list[dict[str, Any]] = [
    {"value": "off", "policy": "off", "scope": "provider", "label": "показать ошибку"},
    {
        "value": "same:provider",
        "policy": "same",
        "scope": "provider",
        "label": "подставить похожую по цене у того же поставщика",
    },
    {
        "value": "cheaper:provider",
        "policy": "cheaper",
        "scope": "provider",
        "label": "подставить дешевле у того же поставщика",
    },
    {
        "value": "pricier:provider",
        "policy": "pricier",
        "scope": "provider",
        "label": "подставить дороже у того же поставщика",
    },
    {
        "value": "same:vendor",
        "policy": "same",
        "scope": "vendor",
        "label": "подставить похожую у того же вендора",
    },
    {
        "value": "same:any",
        "policy": "same",
        "scope": "any",
        "label": "подставить похожую у любого поставщика",
    },
]

# Stop reasons, spelled out. `length` is the one that reads as "the model said
# nothing" when it is shown raw.
FINISH_REASONS: dict[str, dict[str, Any]] = {
    "stop": _t("дошла до конца", "Модель закончила ответ сама"),
    "length": _t(
        "обрыв по лимиту токенов", "Модель упёрлась в max_tokens, ответ неполный"
    ),
    "max_tokens": _t(
        "обрыв по лимиту токенов", "Модель упёрлась в max_tokens, ответ неполный"
    ),
    "content_filter": _t("остановлена фильтром", "Поставщик прервал генерацию"),
    "tool_calls": _t("запросила инструмент", "Модель просит вызвать инструмент"),
    "end_turn": _t("дошла до конца", "Модель закончила ответ сама"),
}


# What a file is *for*, when a model takes more than one of a kind. The
# vocabulary is closed in `domain/messages.ATTACHMENT_ROLES`; this is the word an
# operator sees next to each chosen file, in the order worth offering them.
ATTACHMENT_ROLES: list[dict[str, Any]] = [
    {"value": "image", **_t("Основное изображение", "Та картинка, которую правим")},
    {"value": "mask", **_t("Маска", "Чёрно-белая картинка: белое меняется")},
    {"value": "face", **_t("Лицо", "Чьё лицо переносим")},
    {"value": "garment", **_t("Одежда", "Что примеряем")},
    {"value": "reference", **_t("Референс", "На что ориентироваться по содержанию")},
    {"value": "style", **_t("Стиль", "На что ориентироваться по манере")},
]


def as_dict() -> dict[str, Any]:
    """Everything the console needs to name things, in one payload."""
    return {
        "fields": FIELDS,
        "attachment_roles": ATTACHMENT_ROLES,
        "cost_sources": COST_SOURCES,
        "price_source_kinds": PRICE_SOURCE_KINDS,
        "tasks": TASKS,
        "modalities": MODALITIES,
        "channels": CHANNELS,
        "statuses": STATUSES,
        "sources": SOURCES,
        "finish_reasons": FINISH_REASONS,
        "fallback_choices": FALLBACK_CHOICES,
    }
