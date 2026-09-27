"""Tests for the reference relay.

Each test is a sentence from the pattern's README. The regression that matters most is
test_refire_after_resolve_posts_again: the bug it guards against passed review and ran in
production for months.
"""

import pytest

import relay


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Empty state, fixed maps, and a fake chat API that records every call."""
    calls: list[tuple[str, dict]] = []

    def fake_post(method: str, body: dict) -> dict:
        calls.append((method, body))
        return {"ok": True, "ts": f"ts-{len(calls)}"}

    monkeypatch.setattr(relay, "_post", fake_post)
    monkeypatch.setattr(relay, "ROUTES", {"k8s-live": "#alerts-live", "infra-noise": "#ops-noise"})
    monkeypatch.setattr(relay, "SENDERS", {"k8s-": {"name": "cluster", "icon": ":ship:"},
                                           "k8s-live": {"name": "live-cluster", "icon": ":fire:"}})
    relay.STATE.clear()
    return calls


def firing(name="PodCrashLooping", group="g1", **labels):
    return {"status": "firing", "groupKey": group,
            "alerts": [{"labels": {"alertname": name, **labels},
                        "annotations": {"summary": "it crashed"}}]}


def resolved(name="PodCrashLooping", group="g1", **labels):
    return {**firing(name, group, **labels), "status": "resolved"}


# --------------------------------------------------------------------------- routing

def test_unknown_route_is_rejected_and_nothing_is_sent(fresh):
    status, msg = relay.handle("nope", firing())
    assert status == 400
    assert "unknown route key" in msg
    assert fresh == []


def test_identity_uses_the_longest_matching_prefix():
    assert relay.identity_for("k8s-live")["name"] == "live-cluster"
    assert relay.identity_for("k8s-other")["name"] == "cluster"
    assert relay.identity_for("infra-noise")["name"] == "alerts"   # no prefix: the default


# --------------------------------------------------------------------------- episodes

def test_a_firing_episode_is_posted_once(fresh):
    assert relay.handle("k8s-live", firing())[1].startswith("posted")
    assert relay.handle("k8s-live", firing())[1] == "already posted"
    assert [m for m, _ in fresh] == ["chat.postMessage"]
    assert fresh[0][1]["channel"] == "#alerts-live"


def test_resolve_marks_the_original_message_instead_of_posting(fresh):
    relay.handle("k8s-live", firing())
    assert relay.handle("k8s-live", resolved())[1] == "resolved"
    assert [m for m, _ in fresh] == ["chat.postMessage", "reactions.add"]
    assert fresh[1][1]["timestamp"] == "ts-1"
    assert relay.handle("k8s-live", resolved())[1] == "already resolved"


def test_refire_after_resolve_posts_again(fresh):
    """The regression. A duplicate check that ignores status fails exactly here."""
    relay.handle("k8s-live", firing())
    relay.handle("k8s-live", resolved())
    assert relay.handle("k8s-live", firing())[1].startswith("posted")
    assert [m for m, _ in fresh] == ["chat.postMessage", "reactions.add", "chat.postMessage"]


def test_a_record_in_another_channel_does_not_suppress(fresh, monkeypatch):
    relay.handle("k8s-live", firing())
    monkeypatch.setitem(relay.ROUTES, "k8s-live", "#alerts-moved")   # the route moved
    assert relay.handle("k8s-live", firing())[1] == "posted to #alerts-moved"


def test_resolve_with_no_tracked_message_is_still_delivered(fresh):
    assert relay.handle("k8s-live", resolved())[1] == "resolved (untracked, posted)"
    assert fresh[0][0] == "chat.postMessage"
    assert fresh[0][1]["text"].startswith("*Resolved: ")


def test_each_entity_is_its_own_episode(fresh):
    """Two customers on one rule are two groups, so both are posted."""
    relay.handle("k8s-live", firing(group="g-customer-1", user="c1"))
    relay.handle("k8s-live", firing(group="g-customer-2", user="c2"))
    assert [m for m, _ in fresh] == ["chat.postMessage", "chat.postMessage"]


def test_without_a_group_key_the_labels_decide(fresh):
    a = {"status": "firing", "alerts": [{"labels": {"alertname": "X", "user": "c1"}}]}
    b = {"status": "firing", "alerts": [{"labels": {"alertname": "X", "user": "c2"}}]}
    assert relay.group_key("k8s-live", a) != relay.group_key("k8s-live", b)
    assert relay.group_key("k8s-live", a) == relay.group_key("k8s-live", dict(a))


# --------------------------------------------------------------------------- delivery

def test_transport_success_is_not_delivery(monkeypatch):
    monkeypatch.setattr(relay, "_post", lambda m, b: {"ok": False, "error": "channel_not_found"})
    with pytest.raises(relay.DeliveryFailed, match="channel_not_found"):
        relay.handle("k8s-live", firing())
    assert relay.STATE == {}      # nothing recorded for a message that never appeared


def test_already_reacted_is_not_a_failure(monkeypatch):
    relay.STATE["k8s-live:g1"] = relay.Record(ts="ts-0", channel="#alerts-live", status="open")
    monkeypatch.setattr(relay, "_post", lambda m, b: {"ok": False, "error": "already_reacted"})
    assert relay.handle("k8s-live", resolved())[1] == "resolved"
