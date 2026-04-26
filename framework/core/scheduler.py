"""systemd --user timer + service writer.

Auto-wired during agent registration when an agent has a non-empty
`cron_expr`. Produces two files under ~/.config/systemd/user/:

  agent-<id>.service   ExecStart=<entry_command>
  agent-<id>.timer     OnCalendar=<systemd-cal-from-cron>

…then runs `systemctl --user daemon-reload && enable --now agent-<id>.timer`.

Linger should be enabled once for the user (`loginctl enable-linger`)
so user-scope timers fire even when the user isn't logged in. The
framework's install.sh handles that one-time setup.

Cron → systemd OnCalendar conversion is best-effort. systemd's
calendar grammar is a superset of cron, so most simple cron expressions
translate cleanly. Complex expressions (lists with steps, etc.) fall
back to a multi-line OnCalendar list. If you hit an edge case, write
the timer by hand.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional


logger = logging.getLogger("framework.scheduler")


def systemd_user_dir() -> Path:
    base = Path(os.path.expanduser("~/.config/systemd/user"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def unit_paths(agent_id: str) -> tuple[Path, Path]:
    base = systemd_user_dir()
    return base / f"agent-{agent_id}.service", base / f"agent-{agent_id}.timer"


# ---------------------------------------------------------------------------
# cron → systemd OnCalendar
# ---------------------------------------------------------------------------

_DOW_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def cron_to_oncalendar(cron_expr: str) -> str:
    """Convert a 5-field cron expression to a systemd OnCalendar string.

    Field order: minute hour day-of-month month day-of-week
    OnCalendar format: 'DOW *-*-* HH:MM:SS' or '*:0/5' style for steps.

    Limitations:
      - Doesn't handle `?`, `L`, `W`, `#` (Quartz extensions).
      - Multi-step ranges fall back to expanded forms.
      - For unsupported inputs, raises ValueError so caller can fall
        back to hand-written timer.
    """
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"expected 5-field cron, got {len(parts)}: {cron_expr!r}")
    minute, hour, dom, month, dow = parts

    def _expand(field: str, lo: int, hi: int) -> str:
        # systemd accepts: '*', '*/N' (step), 'A-B' (range), 'A,B,C' (list), 'N' (single)
        if field == "*":
            return "*"
        if "/" in field and field.startswith("*/"):
            step = field[2:]
            return f"*/{step}"
        return field  # ranges and lists pass through

    minute_o = _expand(minute, 0, 59)
    hour_o = _expand(hour, 0, 23)
    dom_o = _expand(dom, 1, 31) if dom != "*" else "*"
    month_o = _expand(month, 1, 12) if month != "*" else "*"

    # day-of-week: cron uses 0|7 = Sun ... 6 = Sat. systemd: Mon..Sun names or numbers.
    # systemd OnCalendar wants weekday names if listed explicitly.
    if dow == "*":
        dow_o = ""
    else:
        # Normalize 0/7 → Sun
        days: list[str] = []
        for piece in dow.split(","):
            piece = piece.strip()
            if piece.isdigit():
                d = int(piece) % 7
                days.append(_DOW_NAMES[d])
            elif "-" in piece:
                a, b = piece.split("-", 1)
                if a.isdigit() and b.isdigit():
                    a_i, b_i = int(a) % 7, int(b) % 7
                    if a_i <= b_i:
                        days.append(",".join(_DOW_NAMES[i] for i in range(a_i, b_i + 1)))
                    else:
                        days.append(",".join(_DOW_NAMES[i] for i in [*range(a_i, 7), *range(0, b_i + 1)]))
                else:
                    raise ValueError(f"unsupported dow range: {piece!r}")
            else:
                # Already a name
                days.append(piece[:3].title())
        dow_o = ",".join(days)

    # Build calendar string
    date_part = f"*-{month_o}-{dom_o}"
    time_part = f"{hour_o}:{minute_o}:00"
    if dow_o:
        return f"{dow_o} {date_part} {time_part}"
    return f"{date_part} {time_part}"


# ---------------------------------------------------------------------------
# Service + timer writers
# ---------------------------------------------------------------------------

SERVICE_TEMPLATE = """[Unit]
Description=Agent {agent_id} (auto-wired by reusable-agents framework)
After=network.target

[Service]
Type=oneshot
WorkingDirectory={working_directory}
ExecStart={exec_start}
Environment=AGENT_ID={agent_id}
Environment=AGENT_TRIGGERED_BY=cron
{extra_env}
StandardOutput=append:{log_path}
StandardError=append:{log_path}

[Install]
WantedBy=default.target
"""

TIMER_TEMPLATE = """[Unit]
Description=Schedule for agent {agent_id}

[Timer]
OnCalendar={oncalendar}
Persistent=true
Unit=agent-{agent_id}.service

[Install]
WantedBy=timers.target
"""


def write_systemd_units(
    *,
    agent_id: str,
    cron_expr: str,
    entry_command: str,
    working_directory: str = "",
    extra_env: Optional[dict[str, str]] = None,
    log_dir: str = "/tmp/reusable-agents-logs",
    timezone: str = "UTC",
) -> tuple[Path, Path]:
    """Write the .service + .timer files. Returns the two paths.

    Does NOT call systemctl — caller must invoke systemctl_reload_and_enable()
    after batching multiple writes (avoids reload churn for bulk imports).
    """
    if not entry_command.strip():
        raise ValueError("entry_command is required")
    if not working_directory:
        working_directory = os.path.expanduser("~")

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = f"{log_dir}/agent-{agent_id}.log"

    extra_env_block = "\n".join(
        f"Environment={k}={v}" for k, v in (extra_env or {}).items()
    )

    service_content = SERVICE_TEMPLATE.format(
        agent_id=agent_id,
        working_directory=working_directory,
        exec_start=entry_command,
        extra_env=extra_env_block,
        log_path=log_path,
    )
    timer_content = TIMER_TEMPLATE.format(
        agent_id=agent_id,
        oncalendar=cron_to_oncalendar(cron_expr),
    )

    service_path, timer_path = unit_paths(agent_id)
    service_path.write_text(service_content)
    timer_path.write_text(timer_content)
    logger.info(f"wrote systemd units for {agent_id}")
    return service_path, timer_path


def remove_systemd_units(agent_id: str) -> None:
    """Disable + remove timer/service for an agent."""
    service_path, timer_path = unit_paths(agent_id)
    try:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", f"agent-{agent_id}.timer"],
            check=False, capture_output=True, timeout=10,
        )
    except Exception as e:
        logger.warning(f"systemctl disable failed for {agent_id}: {e}")
    for p in (service_path, timer_path):
        if p.exists():
            p.unlink()
    systemctl_reload()


def systemctl_reload() -> bool:
    if not shutil.which("systemctl"):
        logger.warning("systemctl not found; skipping reload")
        return False
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       check=True, capture_output=True, timeout=10)
        return True
    except Exception as e:
        logger.warning(f"systemctl daemon-reload failed: {e}")
        return False


def systemctl_enable_and_start(agent_id: str) -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", f"agent-{agent_id}.timer"],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except Exception as e:
        logger.warning(f"systemctl enable failed for {agent_id}: {e}")
        return False


def reload_and_enable(agent_id: str) -> bool:
    return systemctl_reload() and systemctl_enable_and_start(agent_id)


def status(agent_id: str) -> dict:
    """Return systemctl status info for the agent's timer + service."""
    out: dict = {"timer": {}, "service": {}}
    if not shutil.which("systemctl"):
        return out
    for unit, key in (("timer", f"agent-{agent_id}.timer"), ("service", f"agent-{agent_id}.service")):
        try:
            r = subprocess.run(
                ["systemctl", "--user", "show", key,
                 "--property=ActiveState,SubState,LoadState,NextElapseUSecRealtime,Result"],
                capture_output=True, text=True, timeout=5,
            )
            for line in (r.stdout or "").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[unit][k] = v
        except Exception as e:
            out[unit]["error"] = str(e)
    return out
