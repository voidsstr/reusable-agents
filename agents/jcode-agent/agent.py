"""Jcode Agent — AgentBase wrapper.

This module wraps the Jcode harness execution in the AgentBase lifecycle
so runs show up in the dashboard with proper progress.json + run-index + status.

Triggered by:
  - Manual trigger via the dashboard
  - Dispatch from other agents via the framework's dispatch mechanism
  - Direct CLI invocation

The Jcode Agent executes tasks through the Jcode harness API, providing
access to all Jcode tools, skills, and memory systems within the reusable-
agents framework.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from framework.core.agent_base import AgentBase, RunResult  # noqa: E402


class JcodeAgent(AgentBase):
    agent_id = "jcode-agent"
    name = "Jcode Agent"
    description = (
        "Wrapper for Jcode harness. Executes Jcode tasks and "
        "integrates with the reusable-agents framework."
    )
    category = "ops"
    send_run_summary_email = False

    def run(self) -> RunResult:
        # Get parameters from environment
        task = os.environ.get("JCODE_TASK", "")
        prompt = os.environ.get("JCODE_PROMPT", "")
        working_dir = os.environ.get("JCODE_WORKING_DIR", str(Path.cwd()))
        
        # Validate that we have sufficient input
        if not task and not prompt:
            return RunResult(
                status="failure",
                summary="No task or prompt provided",
                error_text="JCODE_TASK or JCODE_PROMPT environment variable is required",
            )
        
        self.decide("plan",
                    f"executing Jcode task: {task or prompt[:100]}...")
        self.status("executing Jcode harness", progress=0.10,
                    current_action="jcode-execution")
        
        # Check if Jcode is available and properly configured
        jcode_available = False
        try:
            # Test if jcode command is available
            result = subprocess.run(["which", "jcode"], capture_output=True, text=True, timeout=5)
            jcode_available = (result.returncode == 0)
        except Exception:
            jcode_available = False
        
        if jcode_available:
            # Try to check Jcode version/authentication
            try:
                jcode_auth = subprocess.run(
                    ["jcode", "version"],
                    capture_output=True,
                    text=True,
                    env={**os.environ},
                    timeout=10
                )
                if jcode_auth.returncode == 0:
                    self.status("authenticated Jcode harness", progress=0.30,
                                current_action="jcode-auth")
                else:
                    return RunResult(
                        status="failure",
                        summary=f"Jcode authentication failed",
                        error_text=f"Jcode failed with: {jcode_auth.stderr[:500]}",
                    )
            except subprocess.TimeoutExpired:
                return RunResult(
                    status="failure",
                    summary="Jcode authentication timed out",
                    error_text="Jcode took too long to authenticate",
                )
        
        # Build Jcode command
        cmd = ["jcode", "run", "--json"]
        if task:
            cmd.append(task)
        elif prompt:
            cmd.append(prompt)
        
        # Execute Jcode command with timeout
        try:
            # Change to working directory if specified
            original_cwd = os.getcwd()
            if working_dir and Path(working_dir).exists():
                os.chdir(working_dir)
            
            self.status("running Jcode command", progress=0.50,
                        current_action="jcode-execution")
            
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env={**os.environ},
                timeout=120  # 120 second timeout for actual execution
            )
            
            # Restore working directory
            os.chdir(original_cwd)
            
            self.status("processing Jcode results", progress=0.90,
                        current_action="result-processing")
            
            success = proc.returncode == 0
            
            # Parse result for metrics
            metrics = {
                "exit_code": proc.returncode,
                "stdout_length": len(proc.stdout),
                "stderr_length": len(proc.stderr),
                "has_output": bool(proc.stdout.strip())
            }
            
            # Build summary
            stdout_summary = proc.stdout[:200] if proc.stdout else "No output"
            stderr_summary = proc.stderr[:200] if proc.stderr else "No errors"
            
            if success:
                summary = f"✓ Jcode execution completed successfully: {stdout_summary}"
                self.decide("result", "Jcode execution completed successfully")
                return RunResult(
                    status="success",
                    summary=summary,
                    metrics=metrics,
                    output=proc.stdout
                )
            else:
                summary = f"✗ Jcode execution failed: {stderr_summary}"
                self.decide("failure", f"Jcode execution failed with exit code {proc.returncode}")
                return RunResult(
                    status="failure",
                    summary=summary,
                    error_text=proc.stderr,
                    metrics=metrics
                )
                    
        except subprocess.TimeoutExpired:
            self.decide("failure", "Jcode execution timed out")
            return RunResult(
                status="failure",
                summary="⏱️ Jcode execution timed out after 120 seconds",
                error_text="Jcode took too long to complete the task. This may indicate authentication issues or a long-running task.",
                metrics={"timeout": "120s"}
            )
                    
        except Exception as e:
            self.decide("failure", f"Jcode execution failed with exception: {e}")
            return RunResult(
                status="failure",
                summary=f"💥 Jcode execution failed: {str(e)}",
                error_text=str(e)
            )


def main() -> int:
    return 0 if JcodeAgent().run_once().status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
