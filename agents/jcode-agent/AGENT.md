# Jcode Agent (`jcode-agent`)

> An AgentBase wrapper meant to run one [jcode](https://github.com/1jehuang/jcode)
> CLI task (a prompt) as a framework run, so the run shows up in the dashboard.
> It is flagged as a **blueprint**, is **not registered**, and has **no schedule**.
> It drives no site metric today.

Do not confuse this dir with the implementer's jcode backends. The code-editor
chain (`jcode-copilot`, `jcode-azure`, `jcode-ollama`) is implemented in
`framework/core/code_editor.py` (`JcodeBackend`) and does **not** use this agent.

## At a glance

| | |
|---|---|
| Agent id | `jcode-agent` |
| Home | `reusable-agents/agents/jcode-agent/` (`agent.py`, `manifest.json`, `AGENT.md`) |
| Kind | AgentBase Python. `manifest.metadata.is_blueprint: true` |
| Schedule | None. `cron_expr: ""`, `task_type: manual`, `runnable_modes: ["manual", "chained"]`. No systemd unit exists |
| Entry command | `PYTHONPATH=/home/voidsstr/development/reusable-agents python3 /home/voidsstr/development/reusable-agents/agents/jcode-agent/agent.py` |
| Category | `ops` |
| Status | **Not registered (blueprint)**. `install/register-all-from-dir.sh` skips any manifest with `metadata.is_blueprint`. `GET /api/agents/jcode-agent` returns 404 (checked 2026-09-23). It has no run history and no log file |
| Code history | One commit, `ab2fc5e` ("sweep: framework + SEO pipeline updates…"), 2026-05-13 |

## What it does (as written in `run()`)

1. It reads `JCODE_TASK` or `JCODE_PROMPT` from the environment. If both are
   empty, it returns `failure` ("No task or prompt provided").
2. If `which jcode` succeeds, it runs `jcode version`. A non-zero exit or a
   timeout after 10 s returns `failure`. If `jcode` is not on PATH, this check
   is skipped.
3. It changes into `JCODE_WORKING_DIR` (default: the current directory) and
   runs `jcode run --json <task-or-prompt>` with a **120 s** timeout.
4. It maps the result to a `RunResult` with metrics `exit_code`,
   `stdout_length`, `stderr_length` and `has_output`. On timeout, the metrics
   are `{"timeout": "120s"}`.

`send_run_summary_email = False`. The agent sends no email, queues no recs,
makes no handoffs, and has no goals.

## Known defects (verified against code on 2026-09-23)

| Defect | Effect |
|---|---|
| The success path builds `RunResult(..., output=proc.stdout)`, but `RunResult` (`framework/core/agent_base.py`) has **no `output` field** | The `TypeError` is caught by the broad `except Exception`, so **every successful jcode execution is reported as `failure`** ("Jcode execution failed: … unexpected keyword argument 'output'") |
| `jcode` is not on the fleet PATH on whitebeast (the `10-fleet-path.conf` drop-in dirs, `~/.local/bin`, `/usr/local/bin`), and `~/.jcode/` does not exist | A run fails with `FileNotFoundError` when it reaches step 3 |
| `jcode run --json` | The framework's own jcode backend calls `jcode … run --quiet`. Whether `--json` is a valid jcode flag is **unverified** |
| No goals and no `target_metric` | Violates the "declare goals before wiring" rule in `CLAUDE.md`. Fine while it stays a blueprint; required before it is registered |

Fix the `output=` bug and declare goals before anyone registers or chains this agent.

## Inputs

| Env var | Default | Meaning |
|---|---|---|
| `JCODE_TASK` | — | Task string passed to `jcode run`. Takes precedence over `JCODE_PROMPT` |
| `JCODE_PROMPT` | — | Prompt string, used when `JCODE_TASK` is empty |
| `JCODE_WORKING_DIR` | current directory | Directory to run jcode in. Ignored if it does not exist |
| `AGENT_ID` | class attribute `jcode-agent` | Standard AgentBase override |

## Short-circuit & idempotency

It has no `signals()` override. That is appropriate, because the agent is
manual or chained and each input is a one-off prompt.

## Running & inspecting

Only for local experiments, since the agent is not registered. This is subject
to the defects above:

```bash
JCODE_PROMPT="summarize README.md" JCODE_WORKING_DIR=/home/voidsstr/development/reusable-agents \
PYTHONPATH=/home/voidsstr/development/reusable-agents \
python3 /home/voidsstr/development/reusable-agents/agents/jcode-agent/agent.py
```

To make it a real agent: fix the bug, add goals, remove
`metadata.is_blueprint`, then register with
`bash /home/voidsstr/development/reusable-agents/install/register-agent.sh /home/voidsstr/development/reusable-agents/agents/jcode-agent`.
`register-agent.sh` defaults to `http://localhost:8090` and needs
`FRAMEWORK_API_TOKEN`.

## Related

- `framework/core/code_editor.py`: `JcodeBackend`, the jcode path the implementer actually uses.
- `agents/implementer/run.sh`: its chains include `jcode-copilot` and `jcode-ollama`.
- `CLAUDE.md`: lists `jcode-agent` under short-circuit "n/a (inbox/reactive …)".
