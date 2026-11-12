"""The environment a pool dispatch runs claude under.

Regression for 2026-11-12: HOME moves to the profile directory, and Python
derives its user site from HOME, so python3 inside every pool session lost the
operator's user-installed packages. An article-author session then skipped the
eBay handoff ("azure-storage-blob missing")."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from framework.cli import claude_pool as cp


def test_home_moves_to_the_profile_but_the_python_user_base_stays(monkeypatch):
    monkeypatch.setattr(cp, "_real_home", lambda: "/home/operator")
    env = cp._dispatch_env("profile-4", "/pool/profile-4",
                           base_env={"HOME": "/home/operator", "PATH": "/usr/bin"})
    assert env["HOME"] == "/pool/profile-4"
    assert env["PYTHONUSERBASE"] == str(Path("/home/operator") / ".local")
    assert env["CLAUDE_PROXY_FOR_PROFILE"] == "profile-4"


def test_an_explicit_python_user_base_is_kept(monkeypatch):
    monkeypatch.setattr(cp, "_real_home", lambda: "/home/operator")
    env = cp._dispatch_env("profile-1", "/pool/profile-1",
                           base_env={"PATH": "/usr/bin", "PYTHONUSERBASE": "/opt/pybase"})
    assert env["PYTHONUSERBASE"] == "/opt/pybase"


def test_the_shim_dir_is_dropped_from_path(monkeypatch):
    monkeypatch.setattr(cp, "_real_home", lambda: "/home/operator")
    env = cp._dispatch_env("profile-2", "/pool/profile-2",
                           base_env={"PATH": f"{cp.SHIM_DIR}:/usr/bin:/bin"})
    assert str(cp.SHIM_DIR) not in env["PATH"].split(":")
    assert env["PATH"].split(":")[:2] == ["/usr/bin", "/bin"]


def test_real_home_ignores_a_profile_home(monkeypatch):
    monkeypatch.setenv("HOME", "/pool/profile-3")
    assert cp._real_home() != "/pool/profile-3"


def test_user_site_packages_resolve_under_the_dispatch_env(tmp_path, monkeypatch):
    """End to end: a python3 child under the dispatch env sees the real user site."""
    monkeypatch.setattr(cp, "_real_home", lambda: str(tmp_path / "operator"))
    env = cp._dispatch_env("profile-4", str(tmp_path / "profile-4"),
                           base_env={"PATH": os.environ.get("PATH", "/usr/bin")})
    out = subprocess.run([sys.executable, "-c", "import site; print(site.getuserbase())"],
                         env=env, capture_output=True, text=True, check=True).stdout.strip()
    assert out == str(tmp_path / "operator" / ".local")
