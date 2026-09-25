"""A pool profile whose OAuth refresh died keeps its credentials file; it must
stay out of rotation until `claude /login` rewrites that file (2026-09-25)."""
import os
import time
from datetime import datetime, timezone

from framework.cli import claude_pool as cp


def _profile(tmp_path, err_offset_s=None):
    home = tmp_path / "profile-x"
    (home / ".claude").mkdir(parents=True)
    creds = home / ".claude" / ".credentials.json"
    creds.write_text("{}")
    p = {"id": "profile-x", "home": str(home)}
    if err_offset_s is not None:
        ts = creds.stat().st_mtime + err_offset_s
        p["auth_error_at"] = datetime.fromtimestamp(ts, timezone.utc).isoformat()
    return p, creds


def test_no_error_is_usable(tmp_path):
    p, _ = _profile(tmp_path)
    assert cp._is_usable(p)


def test_error_after_login_is_dead(tmp_path):
    p, _ = _profile(tmp_path, err_offset_s=+60)
    assert not cp._is_usable(p)


def test_relogin_after_error_revives(tmp_path):
    p, creds = _profile(tmp_path, err_offset_s=+60)
    later = time.time() + 120
    os.utime(creds, (later, later))
    assert cp._is_usable(p)


def test_missing_credentials_is_dead(tmp_path):
    p, creds = _profile(tmp_path)
    creds.unlink()
    assert not cp._is_usable(p)


def test_picker_skips_dead_profile(tmp_path):
    dead, _ = _profile(tmp_path / "a", err_offset_s=+60)
    live, _ = _profile(tmp_path / "b")
    dead["id"], live["id"] = "profile-dead", "profile-live"
    picked = cp._pick_profile({"profile-dead": dead, "profile-live": live})
    assert picked and picked["id"] == "profile-live"
