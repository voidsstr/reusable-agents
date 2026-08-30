"""A disabled agent must stay disabled.

specpicks-scraper-watchdog was `"enabled": false` in its repo manifest AND
enabled=False in the registry, and was still firing its systemd timer every
five minutes on 2026-08-30, failing every time. Its own disabled_reason
recorded the symptom -- "it only came back because registration re-reads the
repo" -- without ever finding the cause, which was two unconditional paths:

  * register_agent() upserts the incoming manifest wholesale, and the
    autowire path builds one carrying AgentManifest.enabled's default of
    True -- so every registration re-enabled it in the registry.
  * the autowire and API register paths both called reload_and_enable()
    whenever a cron_expr existed, with no reference to `enabled` at all.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILED = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


def main():
    reg = (ROOT / "framework" / "core" / "registry.py").read_text()
    check('registry[manifest.id].get("enabled") is False' in reg,
          "register_agent preserves a stored enabled=False on upsert")
    check("manifest.enabled = False" in reg,
          "register_agent forces the manifest back to disabled, never to enabled")

    sched = (ROOT / "framework" / "core" / "scheduler.py").read_text()
    check("def systemctl_stop_and_disable" in sched,
          "scheduler has a stop_and_disable counterpart to enable_and_start")
    ast.parse(sched)

    base = (ROOT / "framework" / "core" / "agent_base.py").read_text()
    check("systemctl_stop_and_disable" in base,
          "autowire stops the timer for a disabled agent")
    # The enable call must be guarded, not bare.
    idx = base.find("scheduler.reload_and_enable(cls.agent_id)")
    check(idx != -1, "autowire still has an enable path")
    if idx != -1:
        window = base[max(0, idx - 400):idx]
        check('getattr(registered, "enabled", True)' in window,
              "autowire's enable call is gated on the registered enabled flag")

    api = (ROOT / "framework" / "api" / "app" / "routes" / "agents.py").read_text()
    idx = api.find("scheduler.reload_and_enable(req.id)")
    check(idx != -1, "API register still has an enable path")
    if idx != -1:
        window = api[max(0, idx - 400):idx]
        check('getattr(req, "enabled", True)' in window,
              "API register's enable call is gated on the enabled flag")
    ast.parse(api)

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
