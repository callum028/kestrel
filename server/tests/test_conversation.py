import pytest

from kestrel.conversation import BRAIN_NOT_CONNECTED, ConversationMessage, ConversationStore, Ref
from kestrel.events import EventKind


@pytest.fixture
def conversation(conn, log):
    return ConversationStore(conn, log)


async def test_posting_stores_the_user_message_and_returns_it(conversation):
    message = await conversation.post_user_message("what's running?")
    assert message.role == "user"
    assert message.text == "what's running?"
    assert message.id is not None


async def test_posting_returns_before_the_reply_is_generated(conversation):
    """The whole point of the brain step's async requirement: the reply is
    not there yet when post_user_message returns, only once the background
    task finishes."""
    await conversation.post_user_message("what's running?")
    assert [m.role for m in conversation.since()] == ["user"]
    assert conversation.thinking is True
    await conversation.wait_idle()
    assert conversation.thinking is False


async def test_the_stub_responder_replies_so_the_ui_is_testable_end_to_end(conversation):
    await conversation.post_user_message("what's running?")
    await conversation.wait_idle()
    messages = conversation.since()
    assert [m.role for m in messages] == ["user", "kestrel"]
    assert messages[1].text == BRAIN_NOT_CONNECTED


async def test_messages_come_back_oldest_to_newest(conversation):
    await conversation.post_user_message("first")
    await conversation.wait_idle()
    await conversation.post_user_message("second")
    await conversation.wait_idle()
    texts = [m.text for m in conversation.since()]
    assert texts == ["first", BRAIN_NOT_CONNECTED, "second", BRAIN_NOT_CONNECTED]


async def test_since_only_returns_messages_after_the_given_id(conversation):
    await conversation.post_user_message("first")
    await conversation.wait_idle()
    cursor = conversation.since()[-1].id
    await conversation.post_user_message("second")
    await conversation.wait_idle()
    fresh = conversation.since(after=cursor)
    assert [m.text for m in fresh] == ["second", BRAIN_NOT_CONNECTED]


async def test_refs_point_at_a_task_without_a_join_table(conversation):
    message = await conversation.post_user_message(
        "pick up KES-31", refs=[Ref(kind="task", handle="KES-31")]
    )
    assert message.refs == [Ref(kind="task", handle="KES-31")]
    assert message.to_dict()["refs"] == [{"kind": "task", "handle": "KES-31"}]


async def test_a_custom_responder_is_used_instead_of_the_stub(conn, log):
    class Echo:
        async def respond(self, text: str, history: list[ConversationMessage]) -> str:
            return f"echo: {text}"

    conversation = ConversationStore(conn, log, responder=Echo())
    await conversation.post_user_message("hello")
    await conversation.wait_idle()
    assert conversation.since()[-1].text == "echo: hello"


async def test_a_responder_that_raises_becomes_a_visible_system_message(conn, log):
    class Broken:
        async def respond(self, text: str, history: list[ConversationMessage]) -> str:
            raise RuntimeError("brain is down")

    conversation = ConversationStore(conn, log, responder=Broken())
    await conversation.post_user_message("hello")
    await conversation.wait_idle()
    reply = conversation.since()[-1]
    assert reply.role == "system"
    assert "brain is down" in reply.text
    assert [e.kind for e in log.since()] == [
        EventKind.CONVERSATION_MESSAGE,
        EventKind.BRAIN_CALL_FAILED,
        EventKind.CONVERSATION_MESSAGE,
    ]


async def test_two_messages_in_flight_both_land(conversation):
    """Nothing stops a second message arriving before the first reply is
    back - both must still land, not clobber each other."""
    await conversation.post_user_message("first")
    await conversation.post_user_message("second")
    await conversation.wait_idle()
    roles_and_texts = [(m.role, m.text) for m in conversation.since()]
    assert ("user", "first") in roles_and_texts
    assert ("user", "second") in roles_and_texts
    assert roles_and_texts.count(("kestrel", BRAIN_NOT_CONNECTED)) == 2
