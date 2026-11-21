"""Tripwire between agents/deployer/deployer.py and the affiliate-site kit.

The kit's deployer block (kits/affiliate-site/deploy/README.md, "Routine
deploys") leaves `rg` out of deploy.vars on purpose. With both `app` and `rg`,
the deployer reads the image of the app's active revision after a failed
deploy.cmd and records the deploy as a success when it ends with the new tag;
the kit's deploy.sh can exit 4 with the new image still serving (a smoke
failure whose automatic rollback itself failed), which must stay a failure.

If the deployer's post-flight condition changes, revisit that README block and
kits/affiliate-site/unit/deploy/deployerBlock.test.ts (the kit's own copy of
this check). Offline: reads two files, runs nothing.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOYER = ROOT / "agents" / "deployer" / "deployer.py"
KIT_README = ROOT / "kits" / "affiliate-site" / "deploy" / "README.md"


def test_post_flight_reads_the_serving_image_only_with_app_and_rg():
    src = DEPLOYER.read_text(encoding="utf-8")
    assert 'app = deploy_vars.get("app", "")' in src
    assert 'rg = deploy_vars.get("rg", "")' in src
    assert "if app and rg:" in src
    assert "if rc != 0 and not verified_via_revision:" in src
    # rg is read in that one place only.
    assert src.count('deploy_vars.get("rg"') == 1
    assert '["rg"]' not in src


def test_kit_deployer_block_leaves_rg_out():
    if not KIT_README.is_file():
        pytest.skip("kits/affiliate-site is not in this checkout")
    blocks = [b for b in re.findall(r"^```yaml\n(.*?)^```$", KIT_README.read_text(encoding="utf-8"), re.M | re.S)
              if b.startswith("deployer:\n")]
    assert len(blocks) == 1
    block = blocks[0]
    assert "{rg}" not in block
    assert not re.search(r"^\s*rg:", block, re.M)
    m = re.search(r"^    vars: \{(.*)\}$", block, re.M)
    assert m, "the deploy stage has no one-line vars mapping"
    assert sorted(kv.split(":", 1)[0].strip() for kv in m.group(1).split(",")) == ["app", "image"]
