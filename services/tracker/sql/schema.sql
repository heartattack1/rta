CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    metadata TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    input_type TEXT NOT NULL CHECK(input_type IN ('text', 'voice')),
    raw_text TEXT,
    raw_audio_uri TEXT,
    transcript TEXT,
    refined_text TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'RECEIVED',
        'ROUTED',
        'TRANSCRIBING',
        'REFINING',
        'TOOL_QUEUED',
        'TOOL_RUNNING',
        'SUMMARIZING',
        'TTS_GENERATING',
        'DELIVERED',
        'FAILED'
    )),
    final_summary TEXT,
    final_audio_uri TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_status_history (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tool_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    input TEXT,
    output TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tool_runs_task_id ON tool_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_task_status_history_task_id ON task_status_history(task_id);
