# Голоса Yandex SpeechKit

Выжимка из официальной таблицы голосов для параметров `voice` и `role`
адаптера `yandex` (TTS API v3). Источник:
<https://aistudio.yandex.ru/docs/en/speechkit/tts/voices.html> (клиппинг
оператора от 2026-08-11; сайт отдаёт капчу автоматике — при расхождении
верить живой странице).

В v3 языкового параметра нет: язык определяется голосом. Роль — манера
чтения; первая в списке — роль по умолчанию.

## Русский (ru-RU)

| Голос | Пол | Роли |
| --- | --- | --- |
| `marina` (дефолт роутера) | Ж | neutral, whisper, friendly |
| `alena` | Ж | neutral, good |
| `jane` | Ж | neutral, good, evil |
| `omazh` | Ж | neutral, evil |
| `dasha` | Ж | neutral, good, friendly |
| `julia` | Ж | neutral, strict |
| `lera` | Ж | neutral, friendly |
| `masha` | Ж | good, strict, friendly |
| `saule_ru` | Ж | neutral, strict, whisper |
| `zamira_ru` | Ж | neutral, strict, friendly |
| `zhanar_ru` | Ж | neutral, strict, friendly |
| `yulduz_ru` | Ж | neutral, strict, friendly, whisper |
| `filipp` | М | — |
| `ermil` | М | neutral, good |
| `zahar` | М | neutral, good |
| `alexander` | М | neutral, good |
| `kirill` | М | neutral, strict, good |
| `anton` | М | neutral, good |
| `madi_ru` | М | — |

## Казахский (kk-KK / kk-KZ)

| Голос | Пол | Роли |
| --- | --- | --- |
| `amira` | Ж | — |
| `saule` | Ж | neutral, strict |
| `zhanar` | Ж | neutral, friendly |
| `madi` | М | — |

## Прочие языки

| Язык | Голоса |
| --- | --- |
| Английский (en-US) | `john` (М) |
| Немецкий (de-DE) | `lea` (Ж) |
| Иврит (he-IL) | `naomi` (Ж; роли modern, classic) |
| Узбекский (uz-UZ) | `nigora` (Ж), `zamira` (Ж; роли), `yulduz` (Ж; роли) |

Примечания:

- `oksana` снята с поддержки — вместо неё `alena` с ролью `good`.
- Неподдерживаемая пара голос+роль — это HTTP 400 от API с внятным текстом
  («role evil is not supported for voice marina»), адаптер пробрасывает его
  как ProviderError.
