"""Regression tests for the hero-image curators' short-circuit gate.

Encodes two traps found on 2026-08-28, both of which let an agent report
success(100%) for days while doing nothing:

  1. A backoff-only tick still records a *successful* run whose signal hash is
     {"pending": N, "searxng_up": True}. Once the cooldowns expire the hash is
     unchanged, so `signals()` short-circuits and the pending articles can
     never be retried. 47 articles stranded by the SearXNG outage deadlocked
     exactly this way.
  2. SEARXNG_URL is decided by the installed systemd unit / secrets.env, not by
     the agent's run.sh. A dead URL baked into a unit silently starves the
     search step, and the agent still reports success.

Run: python3 -m pytest tests/test_hero_curator_gate.py -q
"""
import re, unittest
from pathlib import Path

_AISLE = Path("/home/voidsstr/development/aisleprompt/agents/article-hero-image-curator/agent.py")
_UNIT_DIR = Path.home() / ".config/systemd/user"
_SECRETS = Path.home() / ".reusable-agents/secrets.env"

_DEAD_HOST = "searxng.aisleprompt.com"


class TestSignalsDeadlock(unittest.TestCase):
    def test_gate_is_cooldown_aware(self):
        """The fix: never short-circuit while an article is workable now.

        Old-buggy value: the gate returned {"pending", "searxng_up"} only, so a
        run that skipped everything for backoff froze the hash forever.
        """
        src = _AISLE.read_text()
        gate = src.split("def signals", 1)[1].split("def run", 1)[0]
        self.assertIn("cooling", gate,
                      "signals() must fold the retry-backoff set into its hash")
        self.assertRegex(
            gate, r"if any\(i not in cooling for i in ids\):\s*\n\s*return None",
            "signals() must return None (never short-circuit) while any pending "
            "article is outside its cooldown")

    def test_gate_still_returns_none_when_dependency_down(self):
        """A dead search backend must keep re-asserting, not decay to green."""
        gate = _AISLE.read_text().split("def signals", 1)[1].split("def run", 1)[0]
        self.assertRegex(gate, r"if not self\._searxng_up\(\):\s*\n\s*return None")


class TestSearxngWiring(unittest.TestCase):
    def test_no_unit_bakes_the_dead_host(self):
        """The installed unit — not run.sh — decides the backend."""
        offenders = [p.name for p in _UNIT_DIR.glob("agent-*.service")
                     if _DEAD_HOST in p.read_text()]
        self.assertEqual(offenders, [],
                         f"units still pinned to the dead SearXNG origin: {offenders}")

    def test_central_secrets_point_at_a_live_backend(self):
        if not _SECRETS.exists():
            self.skipTest("secrets.env not present")
        m = re.search(r"^SEARXNG_URL=['\"]?([^'\"\n]+)", _SECRETS.read_text(), re.M)
        self.assertIsNotNone(m, "SEARXNG_URL must be set centrally")
        self.assertNotIn(_DEAD_HOST, m.group(1))


if __name__ == "__main__":
    unittest.main()
