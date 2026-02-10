# RTA Monorepo Skeleton

Dev-only monorepo bootstrap for stage-based pipeline services:

- `telegram-bot`
- `tracker`
- `tooler`
- `asr`
- `refine`
- `summarizer`
- `tts`

This subtask intentionally includes **only**:

- folder layout,
- service containers,
- `/health` endpoints,
- example env configs,
- shared DTO contracts (`Task`, `ToolRun`, `Project`).

No business logic is implemented.

## Quick start

```bash
docker compose up --build
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

