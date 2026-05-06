"""Framework-level code-editor backend abstraction.

The implementer agent (and any future agent that needs an LLM to
*directly edit files*) used to inline-shell `aider` with a hardcoded
fallback chain. That made it impossible to:

  * Try alternative agentic CLIs (plandex, goose, openhands) without
    forking implementer.
  * Configure preferred backends per site / per agent.
  * Reuse the fallback machinery for non-implementer agents.

This module lifts the abstraction. Each backend implements a thin
shell-out adapter:

    backend.edit(EditRequest) -> EditResult

EditRequest carries everything the caller has already prepared:
  - repo_path: where to run
  - prompt_path: file with the focused per-rec prompt (concrete edits,
    NOT an abstract runbook)
  - files: list of repo-relative paths to preload into the agent's
    context (`aider --file`, `plandex load`, `goose --with-file`)
  - pre_dirty_path: list of files dirty BEFORE editing (so the bulk-
    commit step can exclude them via set difference — same logic as
    the legacy implementer wrapper)

EditResult carries:
  - rc: 0 success, non-zero failure
  - backend_id: which backend ran ("aider-copilot-proxy", etc.)
  - log_excerpt: last ~50 lines of stdout/stderr (for diagnostics)
  - files_changed: best-effort list of files the backend reported
    editing (parsed from each tool's output format)

Selection + chaining is via `run_with_fallback(req, chain)`. The
chain is a list of backend ids resolved against the storage config
at `config/code-editor-config.json`. Per-deployment overrides:

    storage:  config/code-editor-config.json   (default chain + backend params)
    manifest: AgentManifest.code_editor_chain  (override for one agent)
    site.yaml: code_editor.chain               (override for one site/instance)

The default config (DEFAULT_CONFIG) ships sensible behaviour out of
the box: aider against the GitHub Copilot Pro proxy first, then aider
native github_copilot, then aider against Azure OpenAI, then plandex
against Azure (if installed), then goose against Azure (if installed).
A fresh install needs zero config to function — Copilot proxy is
sufficient.

Per the framework-first policy: site repos (aisleprompt, specpicks)
NEVER fork this code. They contribute either a manifest field or a
`site.yaml` knob — never code.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .storage import StorageBackend, get_storage


logger = logging.getLogger("framework.code_editor")

CONFIG_KEY = "config/code-editor-config.json"

# ---------------------------------------------------------------------------
# Default config — ships sensible defaults so a fresh install works without
# the operator writing any config file. Override via:
#   - this same storage key (per deployment)
#   - AgentManifest.code_editor_chain (per agent)
#   - site.yaml `code_editor.chain` (per site instance)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "schema_version": "1",
    # Ordered list of backend ids — first that's available + succeeds wins.
    # Chain rationale (validated 2026-05-04):
    #   1-3: aider — surgical whole-edit format, byte-stable diffs.
    #        Three model paths in priority of cost (Copilot Pro is free
    #        under existing budget; Azure pay-per-token).
    #   4:   opencode (sst/opencode) — modern provider-agnostic agent,
    #        OpenAI-compat + Azure native, headless `opencode run -q`.
    #   5:   codex (OpenAI Codex CLI) — `codex exec`, native Azure via
    #        ~/.codex/config.toml, sandbox flags for safe scripted use.
    #   6:   crush (charmbracelet/crush) — `crush run --quiet --yolo`,
    #        BYO model via ~/.config/crush/crush.json.
    #   7:   plandex-azure — pluggable but requires SaaS auth or
    #        self-hosted server; remains in chain so it activates when
    #        the operator wires it up.
    # Goose was removed 2026-05-04 after the bake-off showed it
    # rewriting whole files instead of doing surgical edits, even when
    # explicitly told not to.
    # Order rationale (validated end-to-end 2026-05-04):
    #   - Sonnet 4.6 (via Copilot proxy + native github_copilot) is
    #     surgical and instruction-following — produces correct, scoped
    #     edits and stops when uncertain.
    #   - opencode-azure runs gpt-4.1-mini under a more constrained
    #     editor harness — slower but disciplined.
    #   - aider-azure also runs gpt-4.1-mini but under the looser whole-
    #     edit format; on the aisleprompt repo it destructively rewrote
    #     Home.tsx during one validation run. Demoted to last resort.
    #   - codex/crush/plandex remain pluggable but each has external
    #     prerequisites (Responses-API access, sign-in, server).
    # plandex-azure is intentionally omitted from the default chain — it
    # requires an interactive `plandex sign-in` (Plandex Cloud SaaS) or a
    # self-hosted server before any subcommand will run, and there's no
    # headless way to detect that ahead of time. The backend definition
    # is still listed below so an operator who has signed in can opt it
    # back into the chain via config/code-editor-config.json.
    # 2026-05-06: jcode added at the head (between the implementer's claude-
    # pool path in run.sh and the existing aider chain) and at the tail
    # (Azure cloud + Ollama local) so we collect signal on whether jcode's
    # multi-file edit harness beats aider's whole-edit format. Promote
    # `jcode-copilot` to step 1 if its soft-fail rate stays below
    # aider-copilot-proxy's after a week of runs.
    "default_chain": [
        "jcode-copilot",
        "aider-copilot-proxy",
        "aider-github-copilot",
        "opencode-azure",
        "crush-azure",
        "aider-azure",
        "codex-azure",
        "jcode-azure",
        "jcode-ollama",
    ],
    # Backend-specific params. The "kind" tells run_with_fallback which
    # adapter class to instantiate; everything else is forwarded as
    # kwargs to that adapter.
    "backends": {
        "aider-copilot-proxy": {
            "kind": "aider",
            "model": "openai/claude-sonnet-4.6",
            # Sonnet supports diff format; whole format re-emits whole
            # files and easily blows the 16k Copilot output cap on
            # multi-file recs.
            "edit_format": "diff",
            "auth_env": "OPENAI_API_KEY",
            "auth_default_value": "dummy",
            "api_base_env": "OPENAI_API_BASE",
            "api_base_default": "http://localhost:4141",
            "preflight_url": "http://localhost:4141/v1/models",
        },
        "aider-github-copilot": {
            "kind": "aider",
            # litellm provider — needs ~/.config/litellm/github_copilot/api-key.json
            "model": "github_copilot/claude-sonnet-4",
            "edit_format": "diff",
            "preflight_path": "~/.config/litellm/github_copilot/api-key.json",
        },
        "aider-azure": {
            "kind": "aider",
            # azure deployment name — assumes the litellm `azure/<deploy>`
            # form. Env: AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT.
            "model": "azure/${AZURE_OPENAI_DEPLOYMENT:-chat}",
            "preflight_env": "AZURE_OPENAI_API_KEY",
            "extra_env": {
                "AZURE_API_KEY": "${AZURE_OPENAI_API_KEY}",
                "AZURE_API_BASE":
                    "${AZURE_OPENAI_ENDPOINT:-https://aisleprompt-ai.openai.azure.com/}",
                "AZURE_API_VERSION":
                    "${AZURE_OPENAI_API_VERSION:-2024-10-21}",
            },
        },
        "opencode-azure": {
            "kind": "opencode",
            # opencode 1.14+ — Azure provider via
            # ~/.config/opencode/opencode.json. Use apiVersion: "preview"
            # in the JSON config (the @ai-sdk/azure package rejects
            # "2024-10-21" as "API version not supported").
            "preflight_env": "AZURE_OPENAI_API_KEY",
            "model": "azure/chat",
        },
        "codex-azure": {
            "kind": "codex",
            # codex CLI 0.128+ — REQUIRES the Azure OpenAI Responses
            # API (`wire_api = "responses"` is the only supported wire
            # format as of v0.128). Our default `aisleprompt-ai`
            # resource has only chat-completions deployments, so this
            # backend will skip with rc=1 until an operator either:
            #   (a) provisions a Responses-API-enabled deployment, or
            #   (b) sets CODEX_PROVIDER_BASE_URL to a different account.
            # Configured via ~/.codex/config.toml.
            "preflight_env": "AZURE_OPENAI_API_KEY",
            "preflight_path": "~/.codex/config.toml",
            "model": "${AZURE_OPENAI_DEPLOYMENT:-chat}",
            "sandbox": "danger-full-access",
        },
        "crush-azure": {
            "kind": "crush",
            # crush 0.65+ — BYO model via ~/.config/crush/crush.json.
            # IMPORTANT: model id in the config must match the Azure
            # deployment name (e.g. "chat"), and base_url should
            # include /openai/deployments/<dep>. The model arg here
            # resolves to "azure/<deployment>".
            "preflight_env": "AZURE_OPENAI_API_KEY",
            "model": "azure/${AZURE_OPENAI_DEPLOYMENT:-chat}",
        },
        "plandex-azure": {
            "kind": "plandex",
            # plandex reads AZURE_OPENAI_API_KEY + AZURE_API_BASE natively.
            "preflight_env": "AZURE_OPENAI_API_KEY",
            "model_pack": "azure/gpt-4.1-mini",
        },
        "jcode-copilot": {
            "kind": "jcode",
            # Routes through the local copilot-api proxy (same one
            # aider-copilot-proxy uses on :4141). Profile must be
            # registered once via:
            #   jcode provider add copilot-proxy \
            #     --base-url http://localhost:4141/v1 \
            #     --model claude-opus-4.7 --no-api-key
            "provider_profile": "copilot-proxy",
            # Opus 4.7 is surgical and instruction-following — uses the
            # subscription-billed proxy, no per-token cost. Drop to
            # claude-sonnet-4.6 by overriding `model` in storage config
            # if the Opus weekly cap pinches.
            "model": "claude-opus-4.7",
        },
        "jcode-azure": {
            "kind": "jcode",
            # Profile registered via:
            #   printf '%s' "$AZURE_OPENAI_API_KEY" | jcode provider add \
            #     azure-openai-foundry \
            #     --base-url ${AZURE_OPENAI_ENDPOINT}/openai/v1 \
            #     --model chat --api-key-stdin
            "provider_profile": "azure-openai-foundry",
            "preflight_env": "AZURE_OPENAI_API_KEY",
            "model": "${AZURE_OPENAI_DEPLOYMENT:-chat}",
        },
        "jcode-ollama": {
            "kind": "jcode",
            # Native ollama provider — no profile needed, just `jcode
            # login --provider ollama` once.
            #
            # Model selection (validated 2026-05-06 on RTX 5090 / 32 GB VRAM):
            #   • qwen3-coder:30b — strong on benchmarks but in jcode's
            #     `run` harness it explores instead of converging; rc=0
            #     with zero file changes after 60s on a focused
            #     single-line edit. Doesn't reliably engage the Edit tool.
            #   • devstral-small-2:24b — Mistral's purpose-built agent-
            #     coding model. Engages the Edit tool, makes surgical
            #     1-line edits cleanly. ~15 GB VRAM. PRIMARY CHOICE.
            # Override per-deployment via storage `config/code-editor-config.json`
            # if a future model proves out better.
            "native_provider": "ollama",
            "model": "devstral-small-2:24b",
        },
    },
}


def load_config(storage: Optional[StorageBackend] = None) -> dict:
    cfg = None
    try:
        s = storage or get_storage()
        cfg = s.read_json(CONFIG_KEY)
    except Exception:
        # Storage backend may be misconfigured (e.g. missing
        # AZURE_STORAGE_CONNECTION_STRING). The code-editor primitive
        # works fine with DEFAULT_CONFIG — don't die.
        cfg = None
    if not isinstance(cfg, dict):
        return dict(DEFAULT_CONFIG)
    cfg.setdefault("default_chain", DEFAULT_CONFIG["default_chain"])
    cfg.setdefault("backends", {})
    # Merge backend defs so users can override SOME entries without
    # losing the rest.
    merged_backends = dict(DEFAULT_CONFIG["backends"])
    merged_backends.update(cfg.get("backends") or {})
    cfg["backends"] = merged_backends
    return cfg


def write_config(cfg: dict, storage: Optional[StorageBackend] = None) -> None:
    if not isinstance(cfg, dict):
        raise ValueError("code-editor config must be a dict")
    cfg.setdefault("schema_version", "1")
    s = storage or get_storage()
    s.write_json(CONFIG_KEY, cfg)


def ensure_default_config_exists(
    storage: Optional[StorageBackend] = None,
) -> bool:
    s = storage or get_storage()
    try:
        existing = s.read_json(CONFIG_KEY)
        if isinstance(existing, dict) and existing.get("backends"):
            return False
    except Exception:
        pass
    s.write_json(CONFIG_KEY, DEFAULT_CONFIG)
    return True


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EditRequest:
    repo_path: Path
    prompt_path: Path
    files: list[str] = field(default_factory=list)
    pre_dirty_path: Optional[Path] = None
    timeout_sec: int = 600


@dataclass
class EditResult:
    rc: int
    backend_id: str
    log_excerpt: str = ""
    files_changed: list[str] = field(default_factory=list)
    duration_sec: float = 0.0
    skipped: bool = False
    skip_reason: str = ""


# ---------------------------------------------------------------------------
# Env interpolation helper — resolves ${VAR} and ${VAR:-default} in
# config string values against the live process env.
# ---------------------------------------------------------------------------

_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand(val: str) -> str:
    if not isinstance(val, str):
        return val

    def sub(m):
        name = m.group(1)
        default = m.group(2) or ""
        return os.environ.get(name, default)

    return _VAR_RE.sub(sub, val)


def _expand_dict(d: dict) -> dict:
    return {k: _expand(v) if isinstance(v, str) else v
            for k, v in d.items()}


# ---------------------------------------------------------------------------
# Backend base + adapters
# ---------------------------------------------------------------------------

class CodeEditorBackend:
    """Subclass contract: implement is_available() + edit()."""

    kind: str = ""

    def __init__(self, backend_id: str, params: dict):
        self.backend_id = backend_id
        self.params = params

    def is_available(self) -> tuple[bool, str]:
        """Return (ok, reason). reason is logged when not ok."""
        return True, ""

    def edit(self, req: EditRequest) -> EditResult:  # pragma: no cover
        raise NotImplementedError


class AiderBackend(CodeEditorBackend):
    kind = "aider"

    def is_available(self) -> tuple[bool, str]:
        if not shutil.which("aider"):
            return False, "aider binary not on PATH"
        # preflight_env: skip if env var unset
        env_key = self.params.get("preflight_env")
        if env_key and not os.environ.get(env_key):
            return False, f"env {env_key} unset"
        # preflight_path: skip if file missing
        pp = self.params.get("preflight_path")
        if pp:
            p = Path(os.path.expanduser(pp))
            if not p.is_file():
                return False, f"path missing: {p}"
        # preflight_url: skip if endpoint unreachable (cheap probe)
        url = self.params.get("preflight_url")
        if url:
            try:
                import urllib.request
                req = urllib.request.Request(
                    url, headers={"Authorization": "Bearer dummy"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    if r.status >= 500:
                        return False, f"preflight {url} → {r.status}"
            except Exception as e:
                # Some proxies 401 unauth — that's fine, it means alive.
                if "401" not in str(e) and "403" not in str(e):
                    return False, f"preflight {url} unreachable: {e}"
        return True, ""

    def edit(self, req: EditRequest) -> EditResult:
        t0 = time.time()
        env = os.environ.copy()
        # Expand extra_env entries, set them.
        for k, v in (self.params.get("extra_env") or {}).items():
            env[k] = _expand(v)
        # api_base_env / auth_env handling
        if api_base_env := self.params.get("api_base_env"):
            env[api_base_env] = self.params.get("api_base_default", "")
        if auth_env := self.params.get("auth_env"):
            env.setdefault(auth_env,
                           self.params.get("auth_default_value", "dummy"))

        model = _expand(self.params.get("model", ""))
        edit_format = self.params.get("edit_format")
        # Ship a model-metadata file so aider knows real input/output
        # caps for our proxy aliases (otherwise it logs misleading
        # "of 0" token-limit warnings — see aider-model-metadata.json).
        metadata_file = Path(__file__).parent / "aider-model-metadata.json"
        cmd = [
            "aider",
            "--model", model,
            "--no-auto-commits", "--yes-always",
            "--no-show-model-warnings",
            "--no-pretty",
            "--no-stream",
            # Disable aider's full-repo scan (`--map-tokens 0`). The
            # per-rec prompt builder already lists target files
            # explicitly, and a 388-file repo blows past the 128k
            # token limit on Sonnet 4.6 via the Copilot proxy.
            "--map-tokens", "0",
            # Disable aider's auto-URL-fetch. The framework's per-rec
            # prompt builder already extracts entity primary keys from
            # URLs as `[slug_id=...]` markers; aider does NOT need to
            # scrape the page (a) it can't anyway without Playwright
            # in many sandboxes, and (b) auto-scraping can multiply
            # round-trips when several recs reference the same URL,
            # blowing the LLM context.
            "--no-detect-urls",
            "--model-metadata-file", str(metadata_file),
            # Cap chat-history budget so aider's auto-summarizer / file
            # auto-add behavior can't accumulate beyond a safe input
            # ceiling. The per-rec prompt is small (~5-20k); the bloat
            # comes from the model emitting SEARCH/REPLACE blocks for
            # many files that aider then loads to verify. 80k leaves
            # ample headroom for prompt + 8 files preloaded under the
            # 128k Copilot/Sonnet ceiling.
            "--max-chat-history-tokens", "80000",
            "--message-file", str(req.prompt_path),
        ]
        if edit_format:
            cmd[3:3] = ["--edit-format", edit_format]
        for f in req.files:
            cmd += ["--file", f]

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(req.repo_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=req.timeout_sec,
            )
            rc = proc.returncode
            log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return EditResult(
                rc=124, backend_id=self.backend_id,
                log_excerpt=f"timeout after {req.timeout_sec}s",
                duration_sec=time.time() - t0)

        files_changed = self._parse_files_changed(log)
        return EditResult(
            rc=rc,
            backend_id=self.backend_id,
            log_excerpt="\n".join(log.splitlines()[-80:]),
            files_changed=files_changed,
            duration_sec=time.time() - t0,
        )

    @staticmethod
    def _parse_files_changed(log: str) -> list[str]:
        # aider prints "Applied edit to <path>" or "Wrote <path>"
        out: list[str] = []
        for line in log.splitlines():
            m = re.search(r"^(?:Applied edit to|Wrote|Edited)\s+(\S+)", line)
            if m:
                out.append(m.group(1))
        # Dedup, preserve order.
        seen = set()
        deduped = []
        for f in out:
            if f not in seen:
                seen.add(f)
                deduped.append(f)
        return deduped


class PlandexBackend(CodeEditorBackend):
    """Plandex (https://github.com/plandex-ai/plandex). Plan-first
    coding agent, native Azure OpenAI support, headless via --apply
    and --skip-menu."""

    kind = "plandex"

    def is_available(self) -> tuple[bool, str]:
        bin_path = shutil.which("plandex") or shutil.which("pdx")
        if not bin_path:
            return False, "plandex/pdx binary not on PATH"
        env_key = self.params.get("preflight_env")
        if env_key and not os.environ.get(env_key):
            return False, f"env {env_key} unset"
        # Plandex v2 stores per-host auth tokens under
        # ~/.plandex-home-v2/<host>/. A fresh install creates only
        # `cache/`. Without auth, EVERY subcommand (including
        # `--version`) drops into an interactive TTY wizard and blocks
        # on stdin — that's the failure mode we hit in production with
        # rc=1 + EOF errors. Skip preemptively so the chain doesn't
        # waste a slot on a never-completable backend.
        plandex_home = Path("~/.plandex-home-v2").expanduser()
        if not plandex_home.is_dir():
            return False, "plandex not initialized (~/.plandex-home-v2 missing)"
        has_auth = any(
            entry.name not in ("cache",) and entry.is_dir()
            for entry in plandex_home.iterdir()
        )
        if not has_auth:
            return False, (
                "plandex un-authed (run `plandex sign-in` or point at a "
                "self-hosted server)"
            )
        return True, ""

    def edit(self, req: EditRequest) -> EditResult:
        t0 = time.time()
        bin_path = shutil.which("plandex") or shutil.which("pdx")
        env = os.environ.copy()
        # Plandex reads AZURE_OPENAI_API_KEY + AZURE_API_BASE natively.
        if "AZURE_OPENAI_ENDPOINT" in env and "AZURE_API_BASE" not in env:
            env["AZURE_API_BASE"] = env["AZURE_OPENAI_ENDPOINT"]

        prompt_text = ""
        try:
            prompt_text = Path(req.prompt_path).read_text()
        except OSError as e:
            return EditResult(
                rc=2, backend_id=self.backend_id,
                log_excerpt=f"cannot read prompt: {e}",
                duration_sec=time.time() - t0)

        # Plandex non-interactive flow:
        #   plandex new --no-tty -n <plan-name>
        #   plandex load <files>...
        #   plandex tell --apply --skip-menu --no-stream "<prompt>"
        # We assume `plandex` exists; if subcommand fails we fall back.
        plan_name = f"impl-{int(t0)}"
        log_lines: list[str] = []

        def _run(args: list[str], stdin: Optional[str] = None,
                 timeout: int = 60) -> tuple[int, str]:
            try:
                p = subprocess.run(
                    [bin_path, *args], cwd=str(req.repo_path), env=env,
                    input=stdin, capture_output=True, text=True,
                    timeout=timeout)
                return p.returncode, (p.stdout or "") + (p.stderr or "")
            except subprocess.TimeoutExpired:
                return 124, f"timeout: plandex {' '.join(args)}"

        # 1. Create plan. plandex 2.2 requires SaaS auth (`plandex
        # sign-in`) OR a self-hosted server (`plandex --host
        # http://localhost:8080`). If neither is set up, `plandex new`
        # drops into the auth REPL and exits non-zero. The framework
        # surfaces that as a backend skip on the next run.
        rc, out = _run(["new", "-n", plan_name, "--basic"], timeout=30)
        log_lines.append(f"$ plandex new -n {plan_name}\n{out}")
        if rc != 0:
            return EditResult(
                rc=rc, backend_id=self.backend_id,
                log_excerpt="\n".join(log_lines)[-4000:],
                duration_sec=time.time() - t0)

        # 2. Load files
        if req.files:
            rc, out = _run(["load", *req.files], timeout=60)
            log_lines.append(f"$ plandex load ...{len(req.files)}\n{out}")
            if rc != 0:
                logger.warning(f"plandex load rc={rc} (non-fatal)")

        # 3. Tell + apply
        rc, out = _run(
            ["tell", "--apply", "--skip-menu", "--no-stream", prompt_text],
            timeout=req.timeout_sec)
        log_lines.append(f"$ plandex tell --apply ...\n{out}")

        log = "\n".join(log_lines)
        files_changed = self._parse_files_changed(log)
        return EditResult(
            rc=rc, backend_id=self.backend_id,
            log_excerpt="\n".join(log.splitlines()[-100:]),
            files_changed=files_changed,
            duration_sec=time.time() - t0)

    @staticmethod
    def _parse_files_changed(log: str) -> list[str]:
        out: list[str] = []
        for line in log.splitlines():
            # "  ✓ updated <path>" or "applied: <path>"
            m = re.search(r"(?:updated|applied|wrote|created):?\s+(\S+\.\w+)",
                          line, re.IGNORECASE)
            if m:
                out.append(m.group(1))
        seen = set(); deduped = []
        for f in out:
            if f not in seen:
                seen.add(f); deduped.append(f)
        return deduped


class OpencodeBackend(CodeEditorBackend):
    """OpenCode (https://github.com/sst/opencode). Modern terminal
    coding agent with Azure OpenAI + 75+ providers via Models.dev.
    Headless via `opencode run -q -m <prov>/<model> "<prompt>"`."""

    kind = "opencode"

    def is_available(self) -> tuple[bool, str]:
        if not shutil.which("opencode"):
            return False, "opencode binary not on PATH"
        env_key = self.params.get("preflight_env")
        if env_key and not os.environ.get(env_key):
            return False, f"env {env_key} unset"
        cfg = Path("~/.config/opencode/opencode.json").expanduser()
        if not cfg.is_file():
            return False, "~/.config/opencode/opencode.json not configured"
        return True, ""

    def edit(self, req: EditRequest) -> EditResult:
        t0 = time.time()
        env = os.environ.copy()
        model = _expand(self.params.get("model", "azure/chat"))
        prompt_text = ""
        try:
            prompt_text = Path(req.prompt_path).read_text()
        except OSError as e:
            return EditResult(
                rc=2, backend_id=self.backend_id,
                log_excerpt=f"cannot read prompt: {e}",
                duration_sec=time.time() - t0)

        cmd = [
            "opencode", "run",
            "--model", model,
            "--dangerously-skip-permissions",
            "--dir", str(req.repo_path),
            prompt_text,
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=str(req.repo_path), env=env,
                capture_output=True, text=True, timeout=req.timeout_sec)
            rc = proc.returncode
            log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return EditResult(
                rc=124, backend_id=self.backend_id,
                log_excerpt=f"timeout after {req.timeout_sec}s",
                duration_sec=time.time() - t0)
        files_changed = self._parse_files_changed(log)
        return EditResult(
            rc=rc, backend_id=self.backend_id,
            log_excerpt="\n".join(log.splitlines()[-80:]),
            files_changed=files_changed,
            duration_sec=time.time() - t0)

    @staticmethod
    def _parse_files_changed(log: str) -> list[str]:
        out: list[str] = []
        # Strip ANSI escape sequences before matching — opencode
        # decorates tool-call lines with colour codes.
        ansi = re.compile(r"\x1b\[[0-9;]*m|\x1b\[[0-9;]*[a-zA-Z]")
        for raw in log.splitlines():
            line = ansi.sub("", raw)
            # opencode prints e.g. "← Edit src/foo.tsx" or
            # "+++ /path/to/src/foo.tsx" inside its tool dispatch.
            m = re.search(r"(?:^|\s)(?:Edit|Write|Patch|Update)\s+"
                          r"(\S+\.\w+)", line)
            if m and not m.group(1).startswith("http"):
                out.append(m.group(1))
                continue
            # Diff-style: lines starting with "+++" or "---" pointing
            # at a file in the repo
            m = re.search(r"^(?:\+\+\+|---)\s+(\S+\.\w+)", line)
            if m and not m.group(1).startswith("http"):
                out.append(m.group(1))
        seen = set(); deduped = []
        for f in out:
            if f not in seen:
                seen.add(f); deduped.append(f)
        return deduped


class CodexBackend(CodeEditorBackend):
    """OpenAI Codex CLI (https://github.com/openai/codex). Native Azure
    OpenAI via `~/.codex/config.toml`. Headless via `codex exec`."""

    kind = "codex"

    def is_available(self) -> tuple[bool, str]:
        if not shutil.which("codex"):
            return False, "codex binary not on PATH"
        env_key = self.params.get("preflight_env")
        if env_key and not os.environ.get(env_key):
            return False, f"env {env_key} unset"
        cfg = Path("~/.codex/config.toml").expanduser()
        if not cfg.is_file():
            return False, "~/.codex/config.toml not configured"
        return True, ""

    def edit(self, req: EditRequest) -> EditResult:
        t0 = time.time()
        env = os.environ.copy()
        sandbox = self.params.get("sandbox", "workspace-write")
        model = _expand(self.params.get("model", ""))
        prompt_text = ""
        try:
            prompt_text = Path(req.prompt_path).read_text()
        except OSError as e:
            return EditResult(
                rc=2, backend_id=self.backend_id,
                log_excerpt=f"cannot read prompt: {e}",
                duration_sec=time.time() - t0)

        cmd = [
            "codex", "exec",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C", str(req.repo_path),
        ]
        if model:
            cmd += ["-m", model]
        cmd.append(prompt_text)

        try:
            proc = subprocess.run(
                cmd, cwd=str(req.repo_path), env=env,
                capture_output=True, text=True, timeout=req.timeout_sec)
            rc = proc.returncode
            log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return EditResult(
                rc=124, backend_id=self.backend_id,
                log_excerpt=f"timeout after {req.timeout_sec}s",
                duration_sec=time.time() - t0)
        files_changed = self._parse_files_changed(log)
        return EditResult(
            rc=rc, backend_id=self.backend_id,
            log_excerpt="\n".join(log.splitlines()[-100:]),
            files_changed=files_changed,
            duration_sec=time.time() - t0)

    @staticmethod
    def _parse_files_changed(log: str) -> list[str]:
        out: list[str] = []
        for line in log.splitlines():
            # codex prints "apply_patch" tool calls with paths
            m = re.search(
                r"(?:apply_patch|edited|wrote|patched|update)\s+"
                r"(\S+\.\w+)",
                line, re.IGNORECASE)
            if m and not m.group(1).startswith("http"):
                out.append(m.group(1))
        seen = set(); deduped = []
        for f in out:
            if f not in seen:
                seen.add(f); deduped.append(f)
        return deduped


class CrushBackend(CodeEditorBackend):
    """Crush (https://github.com/charmbracelet/crush). Glamorous
    BYO-model coding agent. Native Azure OpenAI via
    `~/.config/crush/crush.json`. Headless via `crush run --quiet --yolo`."""

    kind = "crush"

    def is_available(self) -> tuple[bool, str]:
        if not shutil.which("crush"):
            return False, "crush binary not on PATH"
        env_key = self.params.get("preflight_env")
        if env_key and not os.environ.get(env_key):
            return False, f"env {env_key} unset"
        cfg = Path("~/.config/crush/crush.json").expanduser()
        if not cfg.is_file():
            return False, "~/.config/crush/crush.json not configured"
        return True, ""

    def edit(self, req: EditRequest) -> EditResult:
        t0 = time.time()
        env = os.environ.copy()
        model = _expand(self.params.get("model", ""))
        prompt_text = ""
        try:
            prompt_text = Path(req.prompt_path).read_text()
        except OSError as e:
            return EditResult(
                rc=2, backend_id=self.backend_id,
                log_excerpt=f"cannot read prompt: {e}",
                duration_sec=time.time() - t0)

        # crush 0.65: --yolo is a top-level flag that only affects
        # the interactive TUI. For `crush run` we instead rely on the
        # `permissions.allowed_tools` block in
        # ~/.config/crush/crush.json (the framework writer
        # provisions that with edit + write + bash + patch + glob).
        cmd = [
            "crush", "run",
            "--quiet",
            "--cwd", str(req.repo_path),
        ]
        if model:
            cmd += ["--model", model]
        cmd.append(prompt_text)

        try:
            proc = subprocess.run(
                cmd, cwd=str(req.repo_path), env=env,
                capture_output=True, text=True, timeout=req.timeout_sec)
            rc = proc.returncode
            log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return EditResult(
                rc=124, backend_id=self.backend_id,
                log_excerpt=f"timeout after {req.timeout_sec}s",
                duration_sec=time.time() - t0)
        files_changed = self._parse_files_changed(log)
        return EditResult(
            rc=rc, backend_id=self.backend_id,
            log_excerpt="\n".join(log.splitlines()[-100:]),
            files_changed=files_changed,
            duration_sec=time.time() - t0)

    @staticmethod
    def _parse_files_changed(log: str) -> list[str]:
        out: list[str] = []
        for line in log.splitlines():
            # crush emits structured edit summaries like
            # "Edited <path>" or "Wrote <path>"
            m = re.search(r"^\s*(?:Edited|Wrote|Patched|Updated)\s+"
                          r"(\S+\.\w+)", line)
            if m and not m.group(1).startswith("http"):
                out.append(m.group(1))
        seen = set(); deduped = []
        for f in out:
            if f not in seen:
                seen.add(f); deduped.append(f)
        return deduped


class JcodeBackend(CodeEditorBackend):
    """jcode (https://github.com/1jehuang/jcode). Rust-based multi-session
    coding agent with native support for Copilot, Azure OpenAI, Ollama, and
    OpenAI-compatible endpoints. Headless via `jcode run --quiet`.

    Three forms in the default chain:
      - `jcode-copilot` — uses our local copilot-api proxy (localhost:4141)
        through a `copilot-proxy` provider profile. Fast (claude-opus-4.7
        on subscription billing) and goes BEFORE aider in the chain.
      - `jcode-azure` — uses an `azure-openai-foundry` provider profile
        pointing at an Azure deployment. Cloud fallback after the aider
        chain if copilot+aider both soft-fail.
      - `jcode-ollama` — uses native `--provider ollama` against the local
        Ollama server (qwen3-coder:30b on the 5090 by default). Free
        last-resort fallback.

    Provider profiles live in `~/.jcode/config.toml` and are provisioned via
    `jcode provider add <name> ...`. The native `ollama` provider is just a
    `jcode login --provider ollama` away.
    """

    kind = "jcode"

    def is_available(self) -> tuple[bool, str]:
        if not shutil.which("jcode"):
            return False, "jcode binary not on PATH"
        env_key = self.params.get("preflight_env")
        if env_key and not os.environ.get(env_key):
            return False, f"env {env_key} unset"
        # Provider-profile preflight — the profile must already exist in
        # the user's config or we'll get a runtime error mid-edit. We
        # inspect ~/.jcode/config.toml; absence means the operator never
        # ran `jcode provider add <name>`.
        profile = self.params.get("provider_profile")
        if profile:
            cfg = Path("~/.jcode/config.toml").expanduser()
            if not cfg.is_file():
                return False, "~/.jcode/config.toml not found (no profiles configured)"
            try:
                if f"[providers.{profile}]" not in cfg.read_text(errors="ignore"):
                    return False, (
                        f"jcode provider profile {profile!r} not registered "
                        f"(run: jcode provider add {profile} ...)"
                    )
            except OSError as e:
                return False, f"cannot read jcode config: {e}"
        # Native-provider preflight — for `--provider ollama` we ping the
        # Ollama server. Skipping is cheap; ollama running but no models
        # pulled would only fail at edit-time, which the chain handles.
        native = self.params.get("native_provider")
        if native == "ollama":
            try:
                import urllib.request as _u
                _u.urlopen("http://localhost:11434/api/tags", timeout=2).read()
            except Exception:
                return False, "ollama server not reachable on :11434"
        return True, ""

    def edit(self, req: EditRequest) -> EditResult:
        t0 = time.time()
        env = os.environ.copy()
        prompt_text = ""
        try:
            prompt_text = Path(req.prompt_path).read_text()
        except OSError as e:
            return EditResult(
                rc=2, backend_id=self.backend_id,
                log_excerpt=f"cannot read prompt: {e}",
                duration_sec=time.time() - t0)

        cmd = ["jcode"]
        # Profile-based providers (copilot proxy, Azure) take precedence
        # over native providers — both shouldn't be set at the same time
        # but if they are, the profile wins.
        profile = self.params.get("provider_profile")
        native = self.params.get("native_provider")
        if profile:
            cmd += ["--provider-profile", profile]
        elif native:
            cmd += ["--provider", native]
        model = _expand(self.params.get("model", ""))
        if model:
            cmd += ["--model", model]
        cmd += ["--cwd", str(req.repo_path), "--no-update", "run", "--quiet", prompt_text]

        try:
            proc = subprocess.run(
                cmd, cwd=str(req.repo_path), env=env,
                capture_output=True, text=True, timeout=req.timeout_sec)
            rc = proc.returncode
            log = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            return EditResult(
                rc=124, backend_id=self.backend_id,
                log_excerpt=f"timeout after {req.timeout_sec}s",
                duration_sec=time.time() - t0)
        files_changed = self._parse_files_changed(log)
        return EditResult(
            rc=rc, backend_id=self.backend_id,
            log_excerpt="\n".join(log.splitlines()[-100:]),
            files_changed=files_changed,
            duration_sec=time.time() - t0)

    @staticmethod
    def _parse_files_changed(log: str) -> list[str]:
        out: list[str] = []
        ansi = re.compile(r"\x1b\[[0-9;]*m|\x1b\[[0-9;]*[a-zA-Z]")
        for raw in log.splitlines():
            line = ansi.sub("", raw)
            # jcode's tool calls emit edit/write/patch lines similar to
            # opencode. The framework's outer `git diff --quiet` check
            # in run.sh is the authoritative "did anything change" gate;
            # this regex is a best-effort hint for the dashboard's
            # files-changed column.
            m = re.search(
                r"(?:^|\s)(?:Edit|Write|Patch|Update|Edited|Wrote|Patched|Applied)"
                r"\s+(\S+\.\w+)",
                line,
            )
            if m and not m.group(1).startswith("http"):
                out.append(m.group(1))
                continue
            m = re.search(r"^(?:\+\+\+|---)\s+(\S+\.\w+)", line)
            if m and not m.group(1).startswith("http"):
                out.append(m.group(1))
        seen: set = set(); deduped: list[str] = []
        for f in out:
            if f not in seen:
                seen.add(f); deduped.append(f)
        return deduped


_BACKEND_REGISTRY: dict[str, type[CodeEditorBackend]] = {
    "aider": AiderBackend,
    "plandex": PlandexBackend,
    "opencode": OpencodeBackend,
    "codex": CodexBackend,
    "crush": CrushBackend,
    "jcode": JcodeBackend,
}


def register_backend_kind(
    kind: str, cls: type[CodeEditorBackend],
) -> None:
    """Plug-in hook for additional backends without modifying this
    module. Sites/agents that need a custom backend (e.g. an in-house
    LLM tool harness) can call this at import time."""
    _BACKEND_REGISTRY[kind] = cls


# ---------------------------------------------------------------------------
# Chain runner
# ---------------------------------------------------------------------------

def resolve_chain(
    *,
    manifest: Optional[dict] = None,
    site_yaml: Optional[dict] = None,
    config: Optional[dict] = None,
    storage: Optional[StorageBackend] = None,
) -> list[str]:
    """Resolve the ordered backend-id chain to try. Resolution order:

      1. manifest.code_editor_chain  (highest)
      2. site_yaml.code_editor.chain
      3. config.default_chain
      4. DEFAULT_CONFIG.default_chain (hardcoded fallback)
    """
    if isinstance(manifest, dict):
        c = manifest.get("code_editor_chain")
        if isinstance(c, list) and c:
            return [str(x) for x in c]
    if isinstance(site_yaml, dict):
        ce = site_yaml.get("code_editor") or {}
        if isinstance(ce, dict):
            c = ce.get("chain")
            if isinstance(c, list) and c:
                return [str(x) for x in c]
    cfg = config or load_config(storage=storage)
    chain = cfg.get("default_chain") or DEFAULT_CONFIG["default_chain"]
    return [str(x) for x in chain]


def build_backend(
    backend_id: str,
    *,
    config: Optional[dict] = None,
    storage: Optional[StorageBackend] = None,
) -> Optional[CodeEditorBackend]:
    cfg = config or load_config(storage=storage)
    params = (cfg.get("backends") or {}).get(backend_id)
    if not isinstance(params, dict):
        logger.warning(f"code-editor: unknown backend {backend_id!r} "
                       f"(not in config.backends)")
        return None
    kind = params.get("kind")
    cls = _BACKEND_REGISTRY.get(kind or "")
    if cls is None:
        logger.warning(f"code-editor: unknown backend kind {kind!r} "
                       f"for {backend_id!r}")
        return None
    return cls(backend_id, _expand_dict(params))


def run_with_fallback(
    req: EditRequest,
    chain: list[str],
    *,
    config: Optional[dict] = None,
    storage: Optional[StorageBackend] = None,
) -> tuple[EditResult, list[EditResult]]:
    """Walk the chain. Returns (winning_or_last_result, all_attempts).

    A backend "wins" when rc==0 AND files_changed is non-empty (or, if
    the request had 0 preload files and the prompt explicitly forbids
    edits, rc==0 alone). Backends that report unavailable are skipped
    silently with skipped=True in their EditResult.
    """
    cfg = config or load_config(storage=storage)
    attempts: list[EditResult] = []
    last: Optional[EditResult] = None
    for bid in chain:
        backend = build_backend(bid, config=cfg, storage=storage)
        if backend is None:
            attempts.append(EditResult(
                rc=2, backend_id=bid, skipped=True,
                skip_reason="backend not registered"))
            continue
        ok, why = backend.is_available()
        if not ok:
            attempts.append(EditResult(
                rc=0, backend_id=bid, skipped=True, skip_reason=why))
            logger.info(f"code-editor: skip {bid} ({why})")
            continue
        logger.info(f"code-editor: trying {bid}")
        result = backend.edit(req)
        attempts.append(result)
        last = result
        if result.rc == 0 and result.files_changed:
            logger.info(f"code-editor: {bid} succeeded "
                        f"(files_changed={len(result.files_changed)}, "
                        f"{result.duration_sec:.1f}s)")
            return result, attempts
        if result.rc == 0 and not result.files_changed:
            # Some backends (aider on a too-large repo, or when the
            # prompt resolves to no concrete edits) exit rc=0 without
            # editing anything. Don't accept that as a win — it makes
            # the wrapper commit a no-op (often just .gitignore tweaks)
            # under the rec label. Fall through to the next backend.
            # If every backend says rc=0/files=0, the wrapper still
            # treats the run as deferred (the bulk-commit step finds
            # 0 NEW files and skips).
            logger.warning(f"code-editor: {bid} rc=0 but files_changed=0 "
                           f"— treating as soft-fail, trying next in "
                           f"chain ({result.duration_sec:.1f}s)")
        else:
            logger.warning(f"code-editor: {bid} rc={result.rc} — "
                           f"trying next in chain")
    if last is None:
        last = EditResult(rc=2, backend_id="(none)",
                          log_excerpt="no backends available in chain")
    return last, attempts
