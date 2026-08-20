import pytest

from kestrel.context import MEMORY_CORE_MAX, assemble
from kestrel.models import Message

BASE = dict(
    identity="You are Kestrel.",
    examples="> PC's off. Want me to queue it?",
    memory_core=["Works at a startup", "Prefers integration tests for auth"],
    attention_block="now: 2026-08-20T14:38:00\npresence: AT_DESK\nfocus: KES-31 / diff",
    task_index=["KES-31 running 22m, 2 unanswered questions"],
    conversation=[Message("user", "how's the auth one going?")],
)


def test_assembly_is_deterministic():
    assert assemble(**BASE).system == assemble(**BASE).system


def test_memory_core_cap_is_enforced():
    with pytest.raises(ValueError, match="cap is"):
        assemble(**{**BASE, "memory_core": [f"fact {i}" for i in range(MEMORY_CORE_MAX + 1)]})


def test_volatile_state_is_in_the_system_block_not_the_conversation():
    bundle = assemble(**BASE)
    assert "presence: AT_DESK" in bundle.system
    assert all("presence" not in m.content for m in bundle.messages)


def test_task_index_is_present_so_it_knows_what_to_ask_for():
    assert "KES-31" in assemble(**BASE).system


def test_dump_round_trips_for_diffing():
    import json

    dumped = json.loads(assemble(**BASE).dump())
    assert dumped["layers"]["memory_core"] == 2
    assert dumped["messages"][0]["role"] == "user"
