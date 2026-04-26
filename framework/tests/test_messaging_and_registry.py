"""Registry + inter-agent messaging + email-codes."""
import time

from framework.core import email_codes, messaging
from framework.core.registry import (
    AgentManifest, deregister_agent, discover_agents_from_dir, get_agent,
    list_agents, register_agent, update_agent,
)


def test_email_codes_round_trip():
    rid1 = email_codes.new_request_id()
    rid2 = email_codes.new_request_id()
    assert rid1 != rid2
    assert rid1.startswith("r-")
    # Sortable lexicographically
    assert rid1 < rid2

    subject = email_codes.encode_subject("seo-reporter", rid1, "[SEO:aisleprompt] run xyz")
    agent, req, original = email_codes.decode_subject(subject)
    assert agent == "seo-reporter"
    assert req == rid1
    assert "[SEO:aisleprompt]" in original

    # Reply prefix tolerated
    reply = "Re: " + subject
    agent2, req2, _ = email_codes.decode_subject(reply)
    assert agent2 == "seo-reporter"
    assert req2 == rid1


def test_confirmation_id_determinism():
    a = email_codes.new_confirmation_id("seo-deployer", "deploy_to_azure",
                                         "('20260426-1200',){}", "20260426T120000Z")
    b = email_codes.new_confirmation_id("seo-deployer", "deploy_to_azure",
                                         "('20260426-1200',){}", "20260426T120000Z")
    assert a == b  # same inputs → same id (so re-running an agent finds the same confirmation)


def test_registry_register_list_update_deregister(storage):
    m = AgentManifest(id="probe", name="Probe Agent", category="ops",
                       cron_expr="0 4 * * *")
    register_agent(m, storage=storage)
    fetched = get_agent("probe", storage=storage)
    assert fetched is not None
    assert fetched.id == "probe"
    assert fetched.name == "Probe Agent"

    update_agent("probe", {"cron_expr": "0 5 * * *", "enabled": False}, storage=storage)
    again = get_agent("probe", storage=storage)
    assert again.cron_expr == "0 5 * * *"
    assert again.enabled is False

    listed = list_agents(storage=storage)
    assert any(a.id == "probe" for a in listed)

    assert deregister_agent("probe", storage=storage, delete_storage=True) is True
    assert get_agent("probe", storage=storage) is None


def test_messaging_send_and_receive(storage):
    m_id = messaging.send_message(
        from_agent="alice",
        to_agents=["bob", "carol"],
        kind="info",
        subject="hi",
        body={"data": [1, 2, 3]},
        storage=storage,
    )
    assert m_id.startswith("m-")

    bob_inbox = messaging.list_inbox("bob", storage=storage)
    assert len(bob_inbox) == 1
    assert bob_inbox[0]["body"]["data"] == [1, 2, 3]
    assert bob_inbox[0]["from"] == "alice"

    # Acknowledge
    assert messaging.mark_read("bob", m_id, storage=storage) is True
    bob_unread = messaging.list_inbox("bob", unread_only=True, storage=storage)
    assert len(bob_unread) == 0

    # Carol still has it as unread
    carol_unread = messaging.list_inbox("carol", unread_only=True, storage=storage)
    assert len(carol_unread) == 1


def test_messaging_thread(storage):
    m1 = messaging.send_message(
        from_agent="a", to_agents="b", subject="hello", body={}, storage=storage,
    )
    m2 = messaging.send_message(
        from_agent="b", to_agents="a", subject="re: hello", body={},
        in_reply_to=m1, storage=storage,
    )
    thread = messaging.list_thread(m2, storage=storage)
    assert len(thread) == 2
    assert thread[0]["message_id"] == m1
    assert thread[1]["message_id"] == m2


def test_discover_agents_from_dir(tmp_path, storage):
    # Create two fake agent dirs with manifests
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "manifest.json").write_text(
        '{"id":"alpha","name":"Alpha","category":"misc","cron_expr":"0 0 * * *"}'
    )
    (tmp_path / "alpha" / "AGENT.md").write_text("# alpha runbook")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "manifest.json").write_text('{"id":"beta","name":"Beta"}')
    # Skip dirs without manifest
    (tmp_path / "no-manifest").mkdir()

    result = discover_agents_from_dir(str(tmp_path), storage=storage)
    assert result["discovered"] == 2

    # Re-discover — should be 0 new, 2 updated
    result2 = discover_agents_from_dir(str(tmp_path), storage=storage)
    assert result2["discovered"] == 0
    assert result2["updated"] == 2

    fetched = get_agent("alpha", storage=storage)
    assert fetched is not None
    assert "AGENT.md" in fetched.runbook_path
