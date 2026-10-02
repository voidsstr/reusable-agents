"""framework.core.locks: the deploy-target lock (no network, no Azure)."""
import os
import uuid

import pytest

from framework.core.locks import site_deploy_lock


def _target():
    return f"test-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def test_site_deploy_lock_serializes_one_target():
    # 2026-10-02: two dispatches deployed the same Container App minutes
    # apart and failed each other's smoke checks. A second deploy of the same
    # target must wait; a different target must not.
    target = _target()
    first, second = site_deploy_lock(target), site_deploy_lock(target)
    other = site_deploy_lock(_target())
    assert first.try_acquire()
    try:
        assert not second.try_acquire()
        assert other.try_acquire()
    finally:
        first.release()
        other.release()
    assert second.try_acquire()
    second.release()


def test_site_deploy_lock_wait_times_out():
    target = _target()
    holder, waiter = site_deploy_lock(target), site_deploy_lock(target, timeout_s=1)
    assert holder.try_acquire()
    try:
        with pytest.raises(TimeoutError):
            waiter.acquire()
    finally:
        waiter.release()
        holder.release()


def test_site_deploy_lock_names_are_path_safe():
    lock = site_deploy_lock("my app/../x")
    assert lock.path.name == "deploy-my_app____x.lock"
    assert site_deploy_lock("").path.name == "deploy-_default.lock"
