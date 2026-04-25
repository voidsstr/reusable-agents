"""Site config loader: parses a YAML config file, validates against the
JSON schema in shared/schemas/site-config.schema.json, returns a typed-ish
dict the rest of the pipeline reads from.

Single point of "where do I get the GSC site URL / GA4 property / repo
path / msmtp account / runs root".

Usage:

    from shared.site_config import load_config, SiteConfig
    cfg = load_config("examples/sites/aisleprompt.yaml")
    print(cfg["site"]["id"], cfg["site"]["mode"])
    runs_dir = cfg.run_dir_for_now()  # ~/.reusable-agents/seo/runs/aisleprompt/<UTC-ts>/
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import yaml  # noqa
except ImportError as e:
    raise SystemExit(
        "PyYAML not installed. `pip install pyyaml jsonschema` (or "
        "use the requirements.txt in any sub-agent's dir)."
    ) from e

try:
    import jsonschema  # noqa
except ImportError as e:
    raise SystemExit(
        "jsonschema not installed. `pip install jsonschema`."
    ) from e


SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "site-config.schema.json"


class SiteConfig(dict):
    """Thin dict subclass with a few helpers."""

    @property
    def site_id(self) -> str:
        return self["site"]["id"]

    @property
    def domain(self) -> str:
        return self["site"]["domain"]

    @property
    def mode(self) -> str:
        return self["site"]["mode"]

    @property
    def is_implement_mode(self) -> bool:
        return self.mode == "implement"

    @property
    def runs_root(self) -> Path:
        return Path(os.path.expanduser(self.get("runs_root", "~/.reusable-agents/seo/runs")))

    @property
    def site_runs_dir(self) -> Path:
        return self.runs_root / self.site_id

    def run_dir_for_ts(self, run_ts: str) -> Path:
        return self.site_runs_dir / run_ts

    def latest_run_dir(self) -> Optional[Path]:
        latest = self.site_runs_dir / "latest"
        if latest.is_symlink() or latest.exists():
            try:
                return latest.resolve()
            except OSError:
                return None
        return None

    def make_run_dir(self) -> tuple[str, Path]:
        """Make a fresh run dir, return (run_ts, path). Updates `latest` symlink."""
        run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = self.run_dir_for_ts(run_ts)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "data").mkdir(exist_ok=True)
        # Update latest symlink (best-effort)
        latest = self.site_runs_dir / "latest"
        try:
            if latest.is_symlink() or latest.exists():
                latest.unlink()
            latest.symlink_to(run_ts)
        except OSError:
            pass
        return run_ts, run_dir

    def auth_oauth_path(self) -> Path:
        path = self.get("auth", {}).get("oauth_file", "~/.reusable-agents/seo/.oauth.json")
        return Path(os.path.expanduser(path))


def _expand_paths(node: Any) -> Any:
    """Recursively expand ~ and $ENV in string leaves."""
    if isinstance(node, str):
        return os.path.expandvars(os.path.expanduser(node))
    if isinstance(node, dict):
        return {k: _expand_paths(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_paths(v) for v in node]
    return node


def load_config(path: str | Path, validate: bool = True) -> SiteConfig:
    """Read a site config YAML file, validate against schema, return SiteConfig."""
    cfg_path = Path(path).resolve()
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Config not found: {cfg_path}")
    with cfg_path.open() as f:
        raw = yaml.safe_load(f)
    raw = _expand_paths(raw)
    if validate:
        with SCHEMA_PATH.open() as f:
            schema = json.load(f)
        try:
            jsonschema.validate(raw, schema)
        except jsonschema.ValidationError as e:
            raise SystemExit(
                f"Config validation failed for {cfg_path}:\n  {e.message}\n"
                f"  at: {' / '.join(str(p) for p in e.path)}"
            )
    cfg = SiteConfig(raw)
    cfg["_config_path"] = str(cfg_path)
    return cfg


def load_config_from_env(env_var: str = "SEO_AGENT_CONFIG") -> SiteConfig:
    """Load the config from $SEO_AGENT_CONFIG, or fail with a clear error."""
    path = os.getenv(env_var)
    if not path:
        raise SystemExit(
            f"${env_var} not set. Point it at a YAML config "
            f"(see examples/sites/*.yaml for templates)."
        )
    return load_config(path)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("Usage: python -m shared.site_config <config.yaml>")
    cfg = load_config(sys.argv[1])
    print(f"✓ Config valid")
    print(f"  site:    {cfg.site_id} ({cfg.domain})")
    print(f"  mode:    {cfg.mode}")
    print(f"  runs:    {cfg.runs_root}")
    print(f"  KPIs:    {[k['id'] for k in cfg.get('revenue_kpis', [])]}")
