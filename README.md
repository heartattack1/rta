# RTA Monorepo Skeleton

Dev-only monorepo bootstrap for stage-based pipeline services:

- `telegram-bot`
- `tracker`
- `tooler`
- `asr`
- `refine`
- `summarizer`
- `tts`

## Quick start (docker compose)

```bash
docker compose up --build -d
```

## Health endpoints

After startup:

- `http://localhost:8001/health` → telegram-bot
- `http://localhost:8002/health` → tracker
- `http://localhost:8003/health` → tooler
- `http://localhost:8004/health` → asr
- `http://localhost:8005/health` → refine
- `http://localhost:8006/health` → summarizer
- `http://localhost:8007/health` → tts

Expected response shape:

```json
{
  "status": "ok",
  "service": "tracker"
}
```

## E2E wiring: one voice -> task -> ASR/refine -> tool -> git commit -> summary -> (tts) -> bot callback

Current compose wiring includes:

- tracker worker orchestration for full pipeline,
- synchronous tool execution via `tooler /tooler/run`,
- task result callback `tracker -> telegram-bot /callbacks/task-result`,
- voice mode path with TTS generation,
- optional git auto-commit mode.

### Demo scenario A: fast dummy tool

1. Start stack (default mode is `SYNC_TOOL_NAME=dummy`):

```bash
docker compose up --build -d
```

2. Create project:

```bash
PROJECT_ID=$(curl -sS -X POST http://localhost:8002/projects -H 'content-type: application/json' -d '{"name":"e2e-dummy"}' | python -c 'import sys,json; print(json.load(sys.stdin)["id"])')
```

3. Create voice task:

```bash
TASK_ID=$(curl -sS -X POST http://localhost:8002/tasks -H 'content-type: application/json' -d "{\"project_id\":\"$PROJECT_ID\",\"input_type\":\"voice\",\"raw_audio_uri\":\"/tmp/fake.ogg\",\"source_chat_id\":123456}" | python -c 'import sys,json; print(json.load(sys.stdin)["id"])')
```

4. Poll task until delivered:

```bash
curl -sS http://localhost:8002/tasks/$TASK_ID
```

Expected:

- `status=DELIVERED`,
- `transcript`, `refined_text`, `final_summary` filled,
- `final_audio_uri` filled for voice.

### Demo scenario B: git tool creates commit

1. Restart tracker with git mode:

```bash
SYNC_TOOL_NAME=git-autocommit docker compose up --build -d tracker tooler
```

2. Ensure repo has at least one local unstaged change before creating a task (for visible commit effect).

3. Create text or voice task as above.

4. Verify commit was created by pipeline:

```bash
git log --oneline -n 3
```

By default git tool uses:

- workdir: `/workspace/rta` (mounted repo in tooler container),
- branch: `autobot/YYYY-MM-DD`,
- subject prefix from `SYNC_GIT_SUBJECT_PREFIX`.

## Repository layout

```text
services/
  telegram-bot/
  tracker/
  tooler/
  asr/
  refine/
  summarizer/
  tts/
packages/
  contracts/
    openapi.yaml
    schemas/
      task.schema.json
      toolrun.schema.json
      project.schema.json
```

## Tooler: git-autocommit

`tooler` supports a `git-autocommit` tool run that:

- validates `.git` in `input.workdir`,
- checks out/creates `autobot/YYYY-MM-DD`,
- runs `git add -A` and commits with `input.subject`,
- optionally pushes when `GIT_PUSH=true` (default in dev: disabled).

Returned run payload includes `branch` and `commit_hash`, and the same values are appended to `artifacts`.

## TTS service

`tts` accepts:

- `POST /tts` with body `{ "text": "...", "task_id": "optional" }`
- legacy alias `POST /tts/synthesize`

Response:

```json
{ "audio_uri": "storage/tts/<task_id>.ogg" }
```

Modes:

- `TTS_PROVIDER=mock` (default): writes placeholder `.wav` and `.ogg` files.
- `TTS_PROVIDER=silero`: runs SileroTTS on CPU, writes `.wav`, then converts to OGG/Opus via `ffmpeg`.
