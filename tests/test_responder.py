"""
Tests de Responder: decisiones should_reply, formateo de memoria,
construcción de conversación y guard anti-concurrencia.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import pytest_asyncio

from message_store import MessageStore
from responder import ReplyContext, Responder, _format_chat_memory


def make_responder(store=None, **overrides):
    cfg = {
        "group_reply_mode": "mention",
        "group_reply_cooldown_seconds": 0,
        "private_reply_cooldown_seconds": 0,
        "max_context_messages": 25,
        "skip_if_replied_recently_seconds": 30,
        "prob_reply_in_always_mode": 1.0,
        "ignore_my_own_messages_in_reply": True,
    }
    safety = {"max_replies_per_minute": 8, "do_not_reply_to_bots": True,
              "do_not_reply_to_commands": True}
    cfg.update(overrides)
    r = Responder.__new__(Responder)
    r.store = store
    r.group_reply_mode = cfg["group_reply_mode"]
    r.group_cooldown = float(cfg["group_reply_cooldown_seconds"])
    r.private_cooldown = float(cfg["private_reply_cooldown_seconds"])
    r.max_context = int(cfg["max_context_messages"])
    r.skip_recent = float(cfg["skip_if_replied_recently_seconds"])
    r.prob_always = float(cfg["prob_reply_in_always_mode"])
    r.ignore_own_in_reply = cfg["ignore_my_own_messages_in_reply"]
    r.max_replies_per_minute = safety["max_replies_per_minute"]
    r.do_not_reply_to_bots = safety["do_not_reply_to_bots"]
    r.do_not_reply_to_commands = safety["do_not_reply_to_commands"]
    r.owner_id = 1
    r._last_reply_at = {}
    r._reply_history = []
    r._generating = set()
    return r


def make_ctx(**overrides):
    base = dict(chat_id=10, chat_type="group", chat_title="G",
                incoming_text="hola", incoming_from_id=555,
                incoming_from_name="Ana", is_reply_to_me=True,
                is_mention=False)
    base.update(overrides)
    return ReplyContext(**base)


# --------------------------------------------------------------- should_reply
def test_skip_own_message():
    r = make_responder()
    ok, reason = r.should_reply(make_ctx(incoming_from_id=1),
                                is_group=True, is_private=False)
    assert not ok and reason == "skip_own_message"


def test_skip_bots_reales():
    """Bug original: solo se filtraba el 777000, no from_user.is_bot."""
    r = make_responder()
    ok, reason = r.should_reply(make_ctx(incoming_is_bot=True),
                                is_group=True, is_private=False)
    assert not ok and reason == "skip_bot_or_service"
    ok, _ = r.should_reply(make_ctx(incoming_from_id=777000),
                           is_group=True, is_private=False)
    assert not ok


def test_skip_commands():
    r = make_responder()
    ok, reason = r.should_reply(make_ctx(incoming_text="/start"),
                                is_group=True, is_private=False)
    assert not ok and reason == "skip_command"


def test_skip_replied_recently():
    """Config skip_if_replied_recently_seconds ahora se respeta."""
    r = make_responder()
    ok, _ = r.should_reply(make_ctx(), is_group=True, is_private=False)
    assert ok
    r._record_reply(10)
    ok, reason = r.should_reply(make_ctx(), is_group=True, is_private=False)
    assert not ok and reason == "replied_recently"


def test_rate_limit():
    r = make_responder(skip_if_replied_recently_seconds=0)
    r.max_replies_per_minute = 2
    reason = ""
    for i in range(5):
        ok, reason = r.should_reply(make_ctx(chat_id=100 + i),
                                    is_group=True, is_private=False)
        if not ok:
            break
        r._record_reply(100 + i)
    assert reason == "rate_limit"


def test_mention_mode():
    r = make_responder(group_reply_mode="mention")
    ok, reason = r.should_reply(make_ctx(is_reply_to_me=False, is_mention=False),
                                is_group=True, is_private=False)
    assert not ok and reason == "not_mention_mode"
    ok, _ = r.should_reply(make_ctx(is_mention=True),
                           is_group=True, is_private=False)
    assert ok


def test_generating_guard():
    r = make_responder()
    r._generating.add(10)
    ok, reason = r.should_reply(make_ctx(), is_group=True, is_private=False)
    assert not ok and reason == "already_generating"


# --------------------------------------------------------------- memory format
def test_format_chat_memory_saltos_reales():
    """Bug original: \\n literal (backslash+n) en el prompt del sistema."""
    mem = _format_chat_memory({
        "chat_title": "Amigos", "summary": "Hablamos de fútbol",
        "topics": ["fútbol"], "keywords": ["gol"], "participants": ["Ana"],
        "my_role": "amigo", "tone": "informal",
    })
    assert "\n" in mem
    assert "\\n" not in mem
    assert "Chat: Amigos" in mem


def test_format_chat_memory_vacia():
    mem = _format_chat_memory(None)
    assert "Todavía no hay" in mem


# --------------------------------------------------------------- conversation
@pytest.mark.asyncio
async def test_build_conversation_entrante_ultimo_sin_duplicar(tmp_path):
    store = MessageStore(str(tmp_path / "t.db"))
    await store.add_message(ts=100, chat_id=10, chat_type="group",
                            chat_title="G", message_id=1, from_id=555,
                            from_name="Ana", is_out=0, text="hola")
    await store.add_message(ts=200, chat_id=10, chat_type="group",
                            chat_title="G", message_id=2, from_id=1,
                            from_name="yo", is_out=1, text="qué tal")
    # el mensaje entrante ya capturado (mismo message_id del contexto)
    await store.add_message(ts=300, chat_id=10, chat_type="group",
                            chat_title="G", message_id=3, from_id=555,
                            from_name="Ana", is_out=0, text="todo bien?")
    r = make_responder(store=store)
    ctx = make_ctx(incoming_text="todo bien?", incoming_message_id=3,
                   incoming_from_name="Ana")
    msgs = await r._build_conversation_messages(ctx)
    textos = [m["content"] for m in msgs]
    # no debe aparecer dos veces el mensaje entrante
    assert sum("todo bien?" in t for t in textos) == 1
    # y debe ser el último, con rol user
    assert msgs[-1]["role"] == "user"
    assert "todo bien?" in msgs[-1]["content"]
    # el propio del dueño con rol assistant
    assert any(m["role"] == "assistant" and "qué tal" in m["content"]
               for m in msgs)


@pytest.mark.asyncio
async def test_build_conversation_foto_sin_texto(tmp_path):
    """Foto sin caption: placeholder explícito para el LLM."""
    store = MessageStore(str(tmp_path / "t.db"))
    r = make_responder(store=store)
    ctx = make_ctx(incoming_text="", has_photo=True, incoming_message_id=99,
                   incoming_from_name="Ana")
    msgs = await r._build_conversation_messages(ctx)
    assert msgs, "debe generar al menos el mensaje entrante"
    assert "(me envió una foto)" in msgs[-1]["content"]


@pytest.mark.asyncio
async def test_build_conversation_no_duplica_caption(tmp_path):
    """En captura, text ya contiene el caption de medios: no se duplica."""
    store = MessageStore(str(tmp_path / "t.db"))
    cap = "mi perro"
    await store.add_message(ts=100, chat_id=10, chat_type="group",
                            chat_title="G", message_id=1, from_id=555,
                            from_name="Ana", is_out=0, text=cap, caption=cap)
    r = make_responder(store=store)
    ctx = make_ctx(incoming_text="bonita foto", incoming_message_id=2,
                   incoming_from_name="Ana")
    msgs = await r._build_conversation_messages(ctx)
    historial = msgs[0]["content"]
    assert historial.count(cap) == 1
