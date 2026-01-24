# Real-Time Meeting Assistant MVP

## Обзор

Система real-time подсказок во время митингов. Overlay-приложение, которое слушает аудио, транскрибирует, анализирует контекст и генерирует подсказки по запросу или автоматически.

## Архитектура
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Audio Capture  │────▶│  Transcription   │────▶│ transcript.txt  │
│  (mic + system) │     │  (Whisper local) │     │   (raw text)    │
└─────────────────┘     └──────────────────┘     └────────┬────────┘
                                                          │
                        ┌─────────────────────────────────┼─────────────────────────────────┐
                        │                                 ▼                                 │
                        │  ┌──────────────────┐    ┌──────────────────┐                    │
                        │  │  Context Agent   │───▶│   context.txt    │                    │
                        │  │  (periodic)      │    │ (current state)  │                    │
                        │  │                  │    │                  │                    │
                        │  │  ┌────────────┐  │    └────────┬─────────┘                    │
                        │  │  │ Fast (4o)  │  │             │                              │
                        │  │  ├────────────┤  │             │                              │
                        │  │  │ Deep (o1)  │  │             │                              │
                        │  │  └────────────┘  │             │                              │
                        │  └──────────────────┘             │                              │
                        │                                   ▼                              │
                        │                        ┌──────────────────┐    ┌──────────────┐  │
                        │                        │   Hint Agent     │───▶│   Overlay    │  │
                        │                        │  (on trigger)    │    │   Window     │  │
                        │                        │                  │    │              │  │
                        │                        │  ┌────────────┐  │    └──────────────┘  │
                        │                        │  │ Fast (4o)  │  │                      │
                        │                        │  ├────────────┤  │                      │
                        │                        │  │ Deep (o1)  │  │                      │
                        │                        │  └────────────┘  │                      │
                        │                        └──────────────────┘                      │
                        │                                                                  │
                        │                         Claude API / claude.ai                   │
                        └──────────────────────────────────────────────────────────────────┘

## Компоненты

### 1. Audio Capture Module

Назначение: Захват аудио из двух источников раздельно.

Требования:

- Захват микрофонного аудио (твой голос)
- Захват системного аудио (голоса других участников)
- Раздельные потоки для возможности различать speakers
- Буферизация для передачи в транскрибер

Технологии:

- macOS: BlackHole или Soundflower для system audio + стандартный mic input
- Python: sounddevice или pyaudio для захвата
- Альтернатива: sox для CLI-захвата

Файлы:

- audio_capture.py — основной модуль захвата
- Конфиг для выбора устройств

### 2. Transcription Module

Назначение: Преобразование аудио в текст в реальном времени.

Требования:

- Локальная транскрипция (низкая латентность)
- Поддержка нескольких языков (EN, RU)
- Speaker diarization (желательно, но не MVP)
- Streaming mode — постоянная запись в файл

Технологии:

- whisper.cpp или faster-whisper (локально, быстро)
- Модель: small или medium для баланса скорость/качество

Output:
transcript.txt
---
[00:00:15] So the main issue with the current implementation...
[00:00:23] Right, I see.


What about the authentication flow?
[00:00:31] We should probably refactor that part first.
...

Файлы:

- transcriber.py — основной модуль
- Пишет в ~/.meeting-assistant/transcript.txt

### 3. Context Agent

Назначение: Периодический анализ транскрипта, поддержание актуального контекста разговора.

Режимы работы:

|Режим|Модель                    |Триггер                      |Latency   |Цель                                   |
|-----|--------------------------|-----------------------------|----------|---------------------------------------|
|Fast |gpt-4o-mini / Claude Haiku|Каждые 30 сек                |~1-2 сек  |Быстрое обновление статуса             |
|Deep |o1-preview / Claude Sonnet|Каждые 2-3 мин или по событию|~10-30 сек|Глубокий анализ, выявление неочевидного|

Логика:

1. Читает transcript.txt (последние N строк или с последней обработки)
1. Читает предыдущий context.txt
1. Генерирует обновлённый контекст
1. Пишет в context.txt

Output формат (context.txt):
last_updated: "2025-01-24T15:30:00"
meeting_topic: "Backend refactoring discussion"
participants_detected:
  - "You (Mikhail)"
  - "Unknown speaker 1"
  - "Unknown speaker 2"

current_discussion:
  topic: "Authentication flow refactoring"
  started_at: "00:05:23"
  key_points:
    - "Current implementation has security concerns"
    - "Proposal to use OAuth2 PKCE"

discussed_topics:
  - topic: "Project timeline"
    summary: "Agreed on 2-week sprint for MVP"
    decisions:
      - "Start date: next Monday"
  - topic: "Database migration"
    summary: "Need to migrate from MySQL to PostgreSQL"
    open_questions:
      - "Who handles data conversion scripts?"

pending_questions:
  - "What's the deadline for the auth refactor?"
  - "Do we need backward compatibility?"

action_items_mentioned:
  - "Mikhail to review the OAuth2 implementation"
  - "Someone to prepare migration plan"

potential_hints_ready:
  - trigger: "if asked about OAuth2"
    hint: "PKCE flow recommended for SPAs, RFC 7636"
  - trigger: "if asked about timeline"
    hint: "Sprint started Monday, 2 weeks = Jan 27 - Feb 7"

Файлы:

- context_agent.py
- Промпты: prompts/context_fast.txt, prompts/context_deep.txt

### 4. Hint Agent

Назначение: Генерация конкретных подсказок по запросу или автоматически.

Триггеры:

1. Manual — горячая клавиша (например, `Cmd+Shift+H`)
1. Auto — детектирован вопрос к тебе (по имени или контексту)
1. Deep ready — пришёл ответ от медленной модели

Режимы работы:

|Режим  |Модель             |Назначение                     |
|-------|-------------------|-------------------------------|
|Instant|gpt-4o-mini / Haiku|Быстрый ответ за 1-2 сек       |
|Deep   |o1-preview / Sonnet|Качественный ответ за 10-30 сек|

Логика:

1. Читает context.txt
1. Читает последние N строк transcript.txt
1. Определяет, что сейчас происходит (вопрос? обсуждение? решение?)
1. Генерирует подсказку

Output:

- Instant hint → сразу в overlay
- Deep hint → в overlay с пометкой “💡 Уточнение”

Формат подсказки:
┌─────────────────────────────────────────┐
│ 💬 Вопрос: "What about the timeline?"   │
├─────────────────────────────────────────┤
│ 📌 Instant:                             │
│ Sprint 2 weeks, started Jan 27          │
├─────────────────────────────────────────┤
│ 💡 Deep (pending...):                   │
│ ⏳ Generating...                        │
└─────────────────────────────────────────┘

После прихода deep:
┌─────────────────────────────────────────┐
│ 💬 Вопрос: "What about the timeline?"   │
├─────────────────────────────────────────┤
│ 📌 Instant:                             │
│ Sprint 2 weeks, started Jan 27          │
├─────────────────────────────────────────┤
│ 💡 Deep:                                │
│ Sprint: Jan 27 - Feb 7 (2 weeks)        │
│ Auth refactor: ~5 days based on similar │
│ tasks in Q3. Buffer recommended.        │
│ ⚠️ Differs from instant - consider      │
│ mentioning need for buffer time.        │
└─────────────────────────────────────────┘

Файлы:

- hint_agent.py
- Промпты: prompts/hint_instant.txt, prompts/hint_deep.txt

### 5. Overlay UI


Назначение: Always-on-top окно с подсказками.

Требования:

- Прозрачный фон, только контент
- Always on top
- Перетаскиваемое
- Минимизируемое в иконку
- Горячие клавиши

Функции:

- Показ текущего контекста (сворачиваемый)
- Показ подсказок
- Индикатор статуса (listening, processing, ready)
- История подсказок (scroll)

Технологии:

- Electron (кроссплатформенность, проще)
- Или Tauri (легче, быстрее, но сложнее)
- Для MVP: простой Python + tkinter/PyQt тоже подойдёт

Файлы:

- overlay/ — директория с UI
- overlay/main.js или overlay/main.py

### 6. Orchestrator

Назначение: Координация всех компонентов.

Функции:

- Запуск/остановка всех модулей
- Мониторинг состояния
- Обработка горячих клавиш
- IPC между компонентами

Файлы:

- main.py — точка входа
- config.yaml — конфигурация

## Структура проекта
meeting-assistant/
├── main.py                    # Точка входа, orchestrator
├── config.yaml                # Конфигурация
├── requirements.txt
│
├── audio/
│   ├── __init__.py
│   └── capture.py             # Audio capture module
│
├── transcription/
│   ├── __init__.py
│   └── transcriber.py         # Whisper transcription
│
├── agents/
│   ├── __init__.py
│   ├── base.py                # Базовый класс агента
│   ├── context_agent.py       # Context analysis
│   └── hint_agent.py          # Hint generation
│
├── api/
│   ├── __init__.py
│   ├── claude.py              # Claude API wrapper
│   └── openai.py              # OpenAI API wrapper (опционально)
│
├── overlay/
│   ├── __init__.py
│   └── window.py              # Overlay UI
│
├── prompts/
│   ├── context_fast.txt
│   ├── context_deep.txt
│   ├── hint_instant.txt
│   └── hint_deep.txt
│
├── data/                      # Runtime data (gitignored)
│   ├── transcript.txt
│   └── context.txt
│
└── tests/
    ├── test_transcription.py
    ├── test_agents.py
    └── fixtures/
        └── sample_audio.wav

## Конфигурация (config.yaml)
audio:
  mic_device: "default"
  system_device: "BlackHole 2ch"
  sample_rate: 16000
  chunk_duration_sec: 5

transcription:
  model: "small"  # tiny, base, small, medium, large
  language: "auto"  # auto, en, ru
  device: "cpu"  # cpu, cuda, mps

agents:
  context:
    fast:
      model: "claude-3-haiku-20240307"
      interval_sec: 30
      transcript_lines: 50
    deep:
      model: "claude-3-5-sonnet-20241022"
      interval_sec: 180
      transcript_lines: 200
  
  hint:
    instant:
      model: "claude-3-haiku-20240307"
    deep:
      model: "claude-3-5-sonnet-20241022"
      # Или для максимального качества:
      # model: "o1-preview"

api:
  # Для MVP используем claude.ai через браузер (бесплатно в рамках подписки)
  # Или API с ключом:
  claude_api_key: "${ANTHROPIC_API_KEY}"
  # openai_api_key: "${OPENAI_API_KEY}"

hotkeys:
  trigger_hint: "cmd+shift+h"
  toggle_overlay: "cmd+shift+o"
  toggle_listening: "cmd+shift+l"

overlay:
  width: 400
  height: 300
  opacity: 0.95
  position: "top-right"  # top-left, top-right, bottom-left, bottom-right

## План реализации

### Фаза 1: Базовая инфраструктура (День 1-2)

Задачи:

1. Инициализация проекта, структура директорий
1. config.yaml парсер
1. Базовый logging
1. Data directory setup

Результат: Скелет проекта, который запускается.

### Фаза 2: Audio Capture (День 2-3)

Задачи:

1. Захват микрофонного аудио
1. Захват системного аудио (исследовать BlackHole/Soundflower)
1. Буферизация и сохранение chunks
1. Тесты с реальным аудио

Результат: Аудио захватывается и сохраняется в файлы.

### Фаза 3: Transcription (День 3-4)

Задачи:

1. Интеграция faster-whisper
1. Streaming transcription pipeline
1. Запись в transcript.txt с timestamps
1. Тесты на sample audio

Результат: Аудио транскрибируется в текст в реальном времени.

### Фаза 4: Context Agent (День 4-6)

Задачи:

1. Базовый класс агента
1. Claude API wrapper
1. Context agent fast mode
1. Context agent deep mode
1. Промпты для анализа контекста
1. Periodic execution logic
1. Запись в context.txt

Результат: Контекст разговора обновляется автоматически.

### Фаза 5: Hint Agent (День 6-8)

Задачи:

1. Hint agent instant mode
1.


Hint agent deep mode
1. Dual-response logic (instant + deep)
1. Trigger detection (имя, вопросительная интонация)
1. Промпты для генерации подсказок

Результат: Подсказки генерируются по запросу.

### Фаза 6: Overlay UI (День 8-10)

Задачи:

1. Базовое окно (tkinter/PyQt для простоты)
1. Always-on-top
1. Отображение подсказок
1. Отображение контекста (сворачиваемый)
1. Статус индикаторы
1. Базовый styling

Результат: UI показывает подсказки.

### Фаза 7: Orchestrator & Integration (День 10-12)

Задачи:

1. Запуск всех компонентов
1. Горячие клавиши (pynput)
1. IPC между компонентами
1. Graceful shutdown
1. Error handling

Результат: Всё работает вместе.

### Фаза 8: Polish & Testing (День 12-14)

Задачи:

1. End-to-end тесты
1. Performance tuning
1. Edge cases handling
1. Documentation
1. README с инструкциями по установке

Результат: MVP готов к использованию.

## API Usage Strategy

### Вариант 1: Claude.ai Web (Бесплатно в подписке)

Использовать browser automation для отправки запросов через claude.ai:

- playwright или selenium для автоматизации
- Преимущество: бесплатно в рамках Pro подписки
- Недостаток: медленнее, менее надёжно, может сломаться

### Вариант 2: Anthropic API (Рекомендуется для MVP)

Прямые API вызовы:

- Haiku: ~$0.25 / 1M input tokens, ~$1.25 / 1M output tokens
- Sonnet: ~$3 / 1M input, ~$15 / 1M output

Оценка расходов за 1 час митинга:

- Транскрипт: ~5000 слов = ~7000 tokens
- Context agent (fast, 120 вызовов): ~120 * 2000 = 240K input tokens
- Context agent (deep, 20 вызовов): ~20 * 5000 = 100K input tokens
- Hint agent (10 вызовов): ~10 * 3000 = 30K input tokens

Итого за час: ~$0.50-2.00 в зависимости от интенсивности

### Вариант 3: Гибрид

- Fast models: локальные (Llama, Mistral через Ollama)
- Deep models: Claude API

## Промпты

### context_fast.txt
You are a meeting context analyzer. Your job is to quickly extract the current state of an ongoing meeting from a transcript.

<transcript>
{transcript_chunk}
</transcript>

<previous_context>
{previous_context}
</previous_context>

Update the meeting context. Focus on:
1. Current topic being discussed
2. Any new decisions or action items
3. Open questions
4. Who is speaking about what

Output in YAML format matching the context.txt schema.
Be concise. This runs every 30 seconds.

### context_deep.txt
You are a senior meeting analyst. Analyze this meeting transcript deeply.

<full_transcript>
{transcript}
</full_transcript>

<current_context>
{current_context}
</current_context>

Provide:
1. Nuanced understanding of discussion dynamics
2. Implicit concerns or tensions not explicitly stated
3. Technical accuracy check on discussed topics
4. Suggested clarifications or follow-ups
5. Potential misunderstandings between participants

Output comprehensive YAML context update.

### hint_instant.txt
You are a real-time meeting assistant for {user_name}, a Senior Backend Engineer.

<context>
{context}
</context>

<recent_transcript>
{recent_transcript}
</recent_transcript>

A hint has been requested. The user needs a quick, actionable response.

Generate a brief hint (2-3 sentences max) that helps the user respond effectively.
Focus on: facts, numbers, technical details, or suggested phrasing.

Be direct. No preamble.

### hint_deep.txt
You are a strategic meeting advisor for {user_name}, a Senior Backend Engineer specializing in identity/authentication systems.

<context>
{context}
</context>

<full_transcript>
{transcript}
</full_transcript>

<instant_hint_given>
{instant_hint}
</instant_hint_given>

Provide a deeper analysis:
1. Is the instant hint accurate? Any corrections needed?
2. What nuances might the user be missing?
3. Strategic suggestions for how to position their response
4. Any technical details that would strengthen their credibility
5. Potential follow-up questions they should ask

If your analysis differs significantly from the instant hint, clearly highlight this.

## Критерии успеха MVP

1. Работает end-to-end: Аудио → Транскрипт → Контекст → Подсказка → UI
1. Латентность instant hint: < 3 секунд от нажатия горячей клавиши
1.


Точность транскрипции: Понятно, о чём речь (не требуется 100% точность)
1. Полезность подсказок: Хотя бы 50% подсказок реально помогают
1. Стабильность: Работает без крашей 1+ час

## Известные ограничения MVP

1. Нет speaker diarization (не различаем кто говорит)
1. Нет интеграции с рабочими инструментами (Jira, Confluence)
1. Простой UI без анимаций
1. Только macOS (или требует адаптации для других ОС)
1. Нет сохранения истории между сессиями

## Будущие улучшения (Post-MVP)

1. Speaker diarization (pyannote.audio)
1. Интеграция с календарём (автозапуск на митингах)
1. Интеграция с Jira/Confluence для контекста
1. Обучение на прошлых митингах
1. Режим “подготовка к митингу” — саммари по теме
1. Мобильное приложение-компаньон
1. Запись и саммари после митинга
