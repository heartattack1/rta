# RTA Monorepo Skeleton

RTA — это dev-ориентированный монорепозиторий с набором микросервисов для обработки входящих задач из Telegram. Основной сценарий: получить текст/голос, прогнать через пайплайн обработки (ASR → refine → tool → summarize → TTS при необходимости) и вернуть результат пользователю через callback в Telegram-бот.  
Оркестрация сейчас реализована внутри `tracker`: при создании задачи она ставится во внутреннюю очередь, а фоновой worker последовательно вызывает остальные сервисы по HTTP.

## Архитектура

### Что это за система

Система состоит из 7 сервисов в `services/` и JSON-контрактов в `packages/contracts`.  
`telegram-bot` принимает webhook-обновления, создает `project/task` в `tracker`, а затем получает callback о завершении и отправляет ответ пользователю. `tracker` хранит состояние в SQLite, ведет историю статусов и выполняет синхронную оркестрацию пайплайна через `asr`, `refine`, `tooler`, `summarizer`, `tts`.

### Контекст и компоненты

```mermaid
flowchart LR
    TG[Telegram API]
    BOT[telegram-bot\nservices/telegram-bot]
    TR[tracker\nservices/tracker]
    ASR[asr\nservices/asr]
    RF[refine\nservices/refine]
    TL[tooler\nservices/tooler]
    SM[summarizer\nservices/summarizer]
    TTS[tts\nservices/tts]
    DB[(SQLite\n/tmp/tracker.db)]
    FS[(Local FS\nstorage/*)]
    GIT[(Git repo\n/workspace/rta)]

    TG -->|Webhook update| BOT
    BOT -->|POST /tasks, /projects| TR
    TR --> DB

    TR -->|POST /asr/transcribe| ASR
    TR -->|POST /refine| RF
    TR -->|POST /tooler/run| TL
    TR -->|POST /summarize| SM
    TR -->|POST /tts| TTS

    BOT -->|save voice file| FS
    TTS -->|write .wav/.ogg| FS
    TL -->|git-autocommit| GIT

    TR -->|POST /callbacks/task-result| BOT
    BOT -->|sendMessage/sendVoice| TG
```

### Как работает пайплайн

#### Сценарий text
1. `telegram-bot` получает `/webhook` с текстом, создает/кэширует проект через `POST /projects`, затем создает задачу `POST /tasks` с `input_type=text`.
2. `tracker` создает задачу со статусом `RECEIVED`, кладет `task_id` во внутреннюю очередь (`queue.Queue`) и worker начинает обработку.
3. `tracker` переводит задачу в `ROUTED` → `REFINING`, вызывает `refine /refine`.
4. Затем `tracker` делает `TOOL_QUEUED` → `TOOL_RUNNING`, создает запись `tool_runs` и вызывает `tooler /tooler/run`.
5. После выполнения инструмента `tracker` переходит в `SUMMARIZING`, вызывает `summarizer /summarize`.
6. Для text-задачи после summary сразу `DELIVERED`, затем `tracker` отправляет callback в `telegram-bot /callbacks/task-result`, бот отправляет `sendMessage` в Telegram.

#### Сценарий voice (end-to-end)
```mermaid
sequenceDiagram
    participant TG as Telegram
    participant BOT as telegram-bot
    participant TR as tracker(worker)
    participant ASR as asr
    participant RF as refine
    participant TL as tooler
    participant SM as summarizer
    participant TTS as tts

    TG->>BOT: POST /webhook (voice)
    BOT->>TG: getFile + download file
    BOT->>TR: POST /projects (once, cached)
    BOT->>TR: POST /tasks {input_type: voice, raw_audio_uri, source_chat_id}
    BOT->>TG: sendMessage("в очереди")

    TR->>TR: enqueue task + worker loop
    TR->>ASR: POST /asr/transcribe
    ASR-->>TR: transcript_text
    TR->>RF: POST /refine
    RF-->>TR: refined_text
    TR->>TL: POST /tooler/run
    TL-->>TR: tool result
    TR->>SM: POST /summarize mode=audio
    SM-->>TR: summary_text
    TR->>TTS: POST /tts
    TTS-->>TR: audio_uri
    TR->>BOT: POST /callbacks/task-result
    BOT->>TG: sendVoice (or sendMessage fallback)
```

### Поток данных и статусы

#### State machine: Task
```mermaid
stateDiagram-v2
    [*] --> RECEIVED
    RECEIVED --> ROUTED
    RECEIVED --> FAILED

    ROUTED --> TRANSCRIBING
    ROUTED --> REFINING
    ROUTED --> FAILED

    TRANSCRIBING --> REFINING
    TRANSCRIBING --> FAILED

    REFINING --> TOOL_QUEUED
    REFINING --> FAILED

    TOOL_QUEUED --> TOOL_RUNNING
    TOOL_QUEUED --> FAILED

    TOOL_RUNNING --> SUMMARIZING
    TOOL_RUNNING --> FAILED

    SUMMARIZING --> TTS_GENERATING
    SUMMARIZING --> DELIVERED
    SUMMARIZING --> FAILED

    TTS_GENERATING --> DELIVERED
    TTS_GENERATING --> FAILED

    DELIVERED --> [*]
    FAILED --> [*]
```

#### State machine: ToolRun
```mermaid
stateDiagram-v2
    [*] --> QUEUED
    QUEUED --> RUNNING
    RUNNING --> SUCCEEDED
    RUNNING --> FAILED
    SUCCEEDED --> [*]
    FAILED --> [*]
```

### Runtime / deployment (docker-compose)

```mermaid
flowchart TB
    subgraph dc[docker-compose]
      BOT[telegram-bot:8000\n(host 8001)]
      TR[tracker:8000\n(host 8002)]
      TL[tooler:8000\n(host 8003)]
      ASR[asr:8000\n(host 8004)]
      RF[refine:8000\n(host 8005)]
      SM[summarizer:8000\n(host 8006)]
      TTS[tts:8000\n(host 8007)]
    end

    BOT --> TR
    TR --> ASR
    TR --> RF
    TR --> TL
    TR --> SM
    TR --> TTS
```

## Компоненты

| Сервис | Назначение | Порт (host→container) | Ключевые endpoint'ы |
|---|---|---|---|
| `telegram-bot` | Принимает Telegram webhook, создает задачи в tracker, отправляет результаты обратно в Telegram | `8001→8000` | `POST /webhook`, `POST /callbacks/task-result`, `GET /health` |
| `tracker` | Оркестратор пайплайна + SQLite-хранилище проектов/задач/tool-runs + status history | `8002→8000` | `POST /projects`, `GET /projects`, `POST /tasks`, `GET/PATCH /tasks/<id>`, `POST /tool-runs`, `GET /tool-runs/<id>`, `GET /health` |
| `tooler` | Запуск инструментов (`dummy`, `codex`, `git-autocommit`), sync и async API | `8003→8000` | `POST /tooler/run`, `POST /tool-runs`, `GET /tool-runs/<id>`, `GET /health` |
| `asr` | Транскрибация аудио (`audio_uri`) в текст | `8004→8000` | `POST /asr/transcribe`, `GET /health` |
| `refine` | Нормализация/очистка текста и инференс project slug (mock/gemini) | `8005→8000` | `POST /refine`, `GET /health` |
| `summarizer` | Суммаризация результата tool-run (mock/LLM fallback) | `8006→8000` | `POST /summarize`, `GET /health` |
| `tts` | Генерация голосового ответа по тексту (`mock`/`silero`) | `8007→8000` | `POST /tts`, `POST /tts/synthesize`, `GET /health` |

## Хранилища и артефакты

- `tracker` использует SQLite (`DATABASE_URL`, в compose: `sqlite:////tmp/tracker.db`) с таблицами: `projects`, `tasks`, `task_status_history`, `tool_runs`.
- `telegram-bot` сохраняет скачанные voice-файлы в `STORAGE_DIR` (в compose: `/app/storage/telegram`, смонтировано из `./storage`).
- `tts` пишет `.wav/.ogg` в `storage/tts` (или `TTS_OUTPUT_DIR`).
- `tooler` (async mode) пишет логи и артефакты в `TOOLER_ARTIFACTS_DIR`.

## Git workflow (реализованный в tooler)

Для `tool_name=git-autocommit` сервис `tooler`:
- проверяет наличие `.git` в `input.workdir`,
- делает `git checkout -B autobot/YYYY-MM-DD`,
- выполняет `git add -A` и `git commit -m <subject>` (если есть staged changes),
- опционально делает `git push` при `GIT_PUSH=true`.

`tracker` может использовать этот режим синхронно через `SYNC_TOOL_NAME=git-autocommit` и прокидывает `workdir/subject` в `POST /tooler/run`.

## Запуск локально

```bash
docker compose up --build -d
```

Проверка health:

- `http://localhost:8001/health` → telegram-bot
- `http://localhost:8002/health` → tracker
- `http://localhost:8003/health` → tooler
- `http://localhost:8004/health` → asr
- `http://localhost:8005/health` → refine
- `http://localhost:8006/health` → summarizer
- `http://localhost:8007/health` → tts

Ожидаемый ответ:

```json
{
  "status": "ok",
  "service": "tracker"
}
```

## Ограничения / что пока не реализовано

- В `tracker` оркестрация сейчас синхронная и однопоточная по внутренней очереди процесса (in-memory queue), без внешнего брокера сообщений.
- Отдельный внешний worker/process manager (Celery/RQ/Kafka/NATS) в репозитории пока не реализован.
- Полноценная наблюдаемость (метрики/tracing) и формализованный OpenAPI на уровне всех сервисов пока не реализованы; в `packages/contracts` лежат схемы сущностей.

## Полезные директории

- `services/telegram-bot/` — прием webhook и отправка ответа в Telegram.
- `services/tracker/` — доменная модель задач и оркестрация пайплайна.
- `services/tracker/sql/schema.sql` — схема SQLite.
- `services/tooler/` — запуск инструментов, включая `git-autocommit`.
- `services/asr/`, `services/refine/`, `services/summarizer/`, `services/tts/` — этапы обработки.
- `packages/contracts/` — контракты и схемы.
