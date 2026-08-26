"""
Tests del sistema de tools (function calling Kurigram <-> IA):
  - MemoryStore: guardar / recuperar / olvidar / upsert
  - MessageScheduler: add / cancel / envío vencido con reintentos
  - TelegramToolbox: esquema OpenAI, guards de seguridad, ejecución
    de tools y loop agéntico completo con una IA falsa.
  - MessageStore.search_messages: búsqueda con escape de comodines.
  - AIClient: degradación elegante cuando el endpoint rechaza tools.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from agent_tools import (REACTION_EMOJIS, TelegramToolbox, ToolContext,
                         _extract_tool_calls)
from memory_store import MemoryStore
from message_store import MessageStore
from scheduler import MessageScheduler


# --------------------------------------------------------------- fixtures
class FakeAuth:
    def __init__(self, owner=1, groups=(), users=()):
        self.owner_id = owner
        self._groups = set(groups)
        self._users = set(users)

    def is_group_authorized(self, cid):
        return cid in self._groups

    def is_user_authorized(self, uid):
        return uid in self._users


class FakeClient:
    """Cliente de pyrogram falso que registra las llamadas."""

    def __init__(self):
        self.reactions = []
        self.sent = []
        self.chats = {}

    async def send_reaction(self, chat_id, message_id, emoji, big=False):
        if emoji not in REACTION_EMOJIS:
            raise ValueError("REACTION_INVALID")
        self.reactions.append((chat_id, message_id, emoji))
        return True

    async def send_message(self, chat_id, text, reply_to_message_id=None):
        self.sent.append((chat_id, text, reply_to_message_id))
        return None

    async def get_chat(self, chat_id):
        if chat_id not in self.chats:
            raise ValueError("chat not found")
        return self.chats[chat_id]

    class _T:
        def __init__(self, v):
            self.value = v

    class _Chat:
        def __init__(self, title, ctype, members=None):
            self.title = title
            self.type = FakeClient._T(ctype)
            self.username = None
            self.members_count = members


class FakeAI:
    """IA falsa que devuelve una secuencia programada de respuestas."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, messages, **kw):
        self.calls.append((list(messages), kw))
        if not self.responses:
            raise AssertionError("FakeAI: no quedan respuestas programadas")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class _Resp:
    def __init__(self, text, tool_calls=None):
        self.text = text
        self.raw = {"choices": [{"message": {
            "content": text,
            "tool_calls": tool_calls or None,
        }}]}


def _tc(name, args):
    return {"id": f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def make_ctx(**over):
    base = dict(chat_id=100, chat_type="group", chat_title="G",
                incoming_message_id=555, from_id=777, from_name="Ana")
    base.update(over)
    return ToolContext(**base)


@pytest.fixture
def store(tmp_path):
    return MessageStore(str(tmp_path / "msg.db"))


@pytest.fixture
def memory(tmp_path):
    return MemoryStore(str(tmp_path / "mem.db"))


@pytest.fixture
def scheduler(tmp_path):
    sent = []

    async def send_fn(chat_id, text, reply_to):
        sent.append((chat_id, text, reply_to))

    s = MessageScheduler(str(tmp_path / "sched.json"), send_fn)
    s._test_sent = sent
    return s


@pytest.fixture
def toolbox(store, memory, scheduler):
    client = FakeClient()
    tb = TelegramToolbox(
        client=client, store=store, memory=memory, scheduler=scheduler,
        auth=FakeAuth(owner=1, groups={100}, users={200}),
        soul_provider=lambda: "Soul de prueba",
        owner_id=1, cfg={})
    tb._client = client
    return tb


# --------------------------------------------------------------- MemoryStore
async def test_memory_save_and_recall(memory):
    assert await memory.save("ana_cafe", "Ana prefiere café solo") is True
    rows = await memory.recall("café")
    assert len(rows) == 1
    assert rows[0]["key"] == "ana_cafe"
    assert "café solo" in rows[0]["content"]


async def test_memory_upsert_updates_content(memory):
    await memory.save("k", "v1")
    await memory.save("k", "v2")
    assert await memory.count() == 1
    rows = await memory.recall("k")
    assert rows[0]["content"] == "v2"


async def test_memory_exact_key_priority(memory):
    await memory.save("clave", "contenido sobre python")
    await memory.save("clave_larga", "otro recuerdo")
    rows = await memory.recall("clave", limit=5)
    assert rows[0]["key"] == "clave"


async def test_memory_forget(memory):
    await memory.save("k", "v")
    assert await memory.forget("k") is True
    assert await memory.forget("k") is False
    assert await memory.count() == 0


async def test_memory_recall_empty_returns_recent(memory):
    await memory.save("a", "1")
    await memory.save("b", "2")
    rows = await memory.recall("", limit=5)
    assert len(rows) == 2


async def test_memory_recall_escapes_wildcards(memory):
    await memory.save("pct", "100% seguro")
    rows = await memory.recall("100%")
    assert len(rows) == 1
    rows = await memory.recall("no_existe_")
    assert rows == []


# --------------------------------------------------------------- Scheduler
async def test_scheduler_add_and_pending(scheduler):
    t = await scheduler.add(100, "hola", time.time() + 3600)
    assert t["status"] == "pending"
    assert len(await scheduler.pending()) == 1


async def test_scheduler_cancel(scheduler):
    t = await scheduler.add(100, "hola", time.time() + 3600)
    assert await scheduler.cancel(t["id"]) is True
    assert await scheduler.cancel(t["id"]) is False
    assert await scheduler.pending_count() == 0


async def test_scheduler_sends_due_task(scheduler):
    await scheduler.add(100, "ya", time.time() - 1)
    await scheduler._tick()
    assert len(scheduler._test_sent) == 1
    pend = await scheduler.pending()
    assert pend == []  # marcada como sent


async def test_scheduler_not_due_not_sent(scheduler):
    await scheduler.add(100, "todavia no", time.time() + 600)
    await scheduler._tick()
    assert scheduler._test_sent == []


async def test_scheduler_retry_on_failure(tmp_path):
    attempts = []

    async def failing_send(chat_id, text, reply_to):
        attempts.append(1)
        raise RuntimeError("flood")

    s = MessageScheduler(str(tmp_path / "s2.json"), failing_send)
    t = await s.add(100, "x", time.time() - 1)
    await s._tick()
    # no marcada como sent: reintenta con backoff
    assert t["status"] == "pending"
    assert t["attempts"] == 1
    assert t["send_at"] > time.time()  # reprogramada al futuro


async def test_scheduler_persistencia(tmp_path):
    async def send_fn(c, t, r):
        pass

    s1 = MessageScheduler(str(tmp_path / "s3.json"), send_fn)
    await s1.add(100, "persist", time.time() + 60)
    # nueva instancia lee el mismo estado
    s2 = MessageScheduler(str(tmp_path / "s3.json"), send_fn)
    assert await s2.pending_count() == 1


# --------------------------------------------------------------- Toolbox
def test_specs_openai_format(toolbox):
    specs = toolbox.specs()
    assert len(specs) == 12
    for s in specs:
        assert s["type"] == "function"
        fn = s["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"
        assert fn["name"] in toolbox._handlers


async def test_react_tool(toolbox):
    ctx = make_ctx()
    res = await toolbox._t_react_to_message(ctx, emoji="👍")
    assert res["ok"] is True
    assert toolbox._client.reactions == [(100, 555, "👍")]


async def test_react_invalid_emoji_rejected(toolbox):
    with pytest.raises(Exception):
        await toolbox._t_react_to_message(make_ctx(), emoji="🚀🚀🚀")


async def test_send_message_to_current_chat(toolbox):
    res = await toolbox._t_send_message(make_ctx(), text="hola")
    assert res["ok"] is True
    assert toolbox._client.sent == [(100, "hola", None)]


async def test_send_message_to_authorized_chat(toolbox):
    res = await toolbox._t_send_message(make_ctx(), text="hola", chat_id=200)
    assert res["ok"] is True


async def test_send_message_to_unauthorized_blocked(toolbox):
    with pytest.raises(Exception, match="no está autorizado"):
        await toolbox._t_send_message(make_ctx(), text="spam", chat_id=999)


async def test_send_message_to_owner_allowed(toolbox):
    res = await toolbox._t_send_message(make_ctx(), text="nota", chat_id=1)
    assert res["ok"] is True


async def test_send_message_empty_text_rejected(toolbox):
    with pytest.raises(Exception):
        await toolbox._t_send_message(make_ctx(), text="   ")


async def test_schedule_message_bounds(toolbox):
    ctx = make_ctx()
    with pytest.raises(Exception, match="delay mínimo"):
        await toolbox._t_schedule_message(ctx, delay_seconds=2, text="x")
    with pytest.raises(Exception, match="delay máximo"):
        await toolbox._t_schedule_message(ctx, delay_seconds=10**7, text="x")
    res = await toolbox._t_schedule_message(ctx, delay_seconds=60, text="x")
    assert res["ok"] is True
    assert res["task_id"]


async def test_schedule_message_max_tasks(toolbox):
    ctx = make_ctx()
    for i in range(toolbox.max_scheduled_messages):
        await toolbox._t_schedule_message(ctx, delay_seconds=60 + i,
                                          text=f"t{i}")
    with pytest.raises(Exception, match="máximo"):
        await toolbox._t_schedule_message(ctx, delay_seconds=60, text="extra")


async def test_memory_tools_roundtrip(toolbox):
    ctx = make_ctx()
    await toolbox._t_save_memory(ctx, key="prueba", content="contenido")
    res = await toolbox._t_recall_memories(ctx, query="prueba")
    assert res["count"] == 1
    assert res["memories"][0]["content"] == "contenido"
    res2 = await toolbox._t_forget_memory(ctx, key="prueba")
    assert res2["removed"] is True


async def test_search_history_tool(toolbox, store):
    await store.add_message(ts=1000, chat_id=100, chat_type="group",
                            chat_title="G", message_id=1, from_id=1,
                            from_name="yo", is_out=1, text="hola mundo")
    await store.add_message(ts=2000, chat_id=100, chat_type="group",
                            chat_title="G", message_id=2, from_id=777,
                            from_name="Ana", is_out=0, text="qué tal todo")
    res = await toolbox._t_search_history(make_ctx(), query="mundo")
    assert res["count"] == 1
    assert res["results"][0]["text"] == "hola mundo"
    assert res["results"][0]["mine"] is True


async def test_get_chat_info_tool(toolbox):
    toolbox._client.chats[100] = FakeClient._Chat("Mi grupo", "supergroup", 42)
    res = await toolbox._t_get_chat_info(make_ctx())
    assert res["ok"] is True
    assert res["title"] == "Mi grupo"
    assert res["members_count"] == 42


async def test_read_soul_tool(toolbox):
    res = await toolbox._t_read_soul(make_ctx())
    assert res["ok"] is True
    assert "Soul de prueba" in res["soul_md"]


async def test_execute_safe_bad_json(toolbox):
    tc = {"id": "x", "type": "function",
          "function": {"name": "save_memory", "arguments": "{invalido"}}
    ex = await toolbox._execute_safe(tc, make_ctx())
    assert ex.ok is False
    assert "JSON" in ex.error


async def test_execute_safe_unknown_tool(toolbox):
    tc = _tc("no_existe", {})
    ex = await toolbox._execute_safe(tc, make_ctx())
    assert ex.ok is False
    assert "desconocida" in ex.error


# --------------------------------------------------------------- Tool loop
async def test_tool_loop_full_cycle(toolbox):
    """La IA pide una tool, recibe el resultado y produce texto final."""
    ai = FakeAI([
        _Resp("", tool_calls=[_tc("react_to_message", {"emoji": "🔥"})]),
        _Resp("jajaja qué bueno"),
    ])
    text, executed = await toolbox.run_tool_loop(
        ai, "SYSTEM", [{"role": "user", "content": "Ana: mira esto"}],
        make_ctx())
    assert text == "jajaja qué bueno"
    assert len(executed) == 1 and executed[0].ok
    assert toolbox._client.reactions == [(100, 555, "🔥")]
    # segunda llamada incluye el resultado de la tool
    msgs2 = ai.calls[1][0]
    assert any(m.get("role") == "tool" for m in msgs2)
    assert any(m.get("role") == "assistant" and "tool_calls" in m
               for m in msgs2)


async def test_tool_loop_no_tools_needed(toolbox):
    ai = FakeAI([_Resp("respuesta directa")])
    text, executed = await toolbox.run_tool_loop(
        ai, "SYSTEM", [{"role": "user", "content": "hola"}], make_ctx())
    assert text == "respuesta directa"
    assert executed == []


async def test_tool_loop_budget_exceeded(toolbox):
    """Si el modelo pide más tools del presupuesto, se corta con error
    informado y el flujo continúa hasta el texto final."""
    toolbox.max_executions_per_reply = 1
    toolbox.max_rounds = 1
    calls = [_tc("recall_memories", {}) for _ in range(3)]
    ai = FakeAI([
        _Resp("", tool_calls=calls),
        _Resp("basta de tools"),
    ])
    text, executed = await toolbox.run_tool_loop(
        ai, "S", [{"role": "user", "content": "x"}], make_ctx())
    assert len(executed) == 1
    assert text == "basta de tools"


async def test_tool_loop_error_degrades_to_none(toolbox):
    ai = FakeAI([RuntimeError("boom")])
    text, executed = await toolbox.run_tool_loop(
        ai, "S", [{"role": "user", "content": "x"}], make_ctx())
    assert text is None


async def test_tool_loop_tool_error_fed_back(toolbox):
    """Una tool que falla devuelve {"ok": false} y el loop continúa."""
    ai = FakeAI([
        _Resp("", tool_calls=[_tc("react_to_message", {"emoji": "🚀🚀"})]),
        _Resp("ok igualmente"),
    ])
    text, executed = await toolbox.run_tool_loop(
        ai, "S", [{"role": "user", "content": "x"}], make_ctx())
    assert text == "ok igualmente"
    assert executed[0].ok is False
    msgs2 = ai.calls[1][0]
    tool_msg = next(m for m in msgs2 if m.get("role") == "tool")
    payload = json.loads(tool_msg["content"])
    assert payload["ok"] is False


def test_extract_tool_calls_variants():
    assert _extract_tool_calls({}) == []
    assert _extract_tool_calls({"choices": []}) == []
    raw = {"choices": [{"message": {"content": None, "tool_calls": [
        _tc("x", {})]}}]}
    assert len(_extract_tool_calls(raw)) == 1


def test_system_prompt_section_mentions_tools():
    s = TelegramToolbox.system_prompt_section()
    assert "Herramientas" in s
    assert "save_memory" in s


# --------------------------------------------------------------- search store
async def test_store_search_escapes_like(store):
    await store.add_message(ts=1000, chat_id=1, chat_type="group",
                            chat_title="G", message_id=1, from_id=1,
                            from_name="yo", is_out=1, text="100% real")
    rows = await store.search_messages("100%")
    assert len(rows) == 1
    rows = await store.search_messages("zzz")
    assert rows == []


async def test_store_search_scoped_to_chat(store):
    await store.add_message(ts=1000, chat_id=1, chat_type="group",
                            chat_title="G", message_id=1, from_id=1,
                            from_name="yo", is_out=1, text="secreto")
    rows = await store.search_messages("secreto", chat_id=2)
    assert rows == []
    rows = await store.search_messages("secreto", chat_id=1)
    assert len(rows) == 1


# --------------------------------------------------------------- AIClient degradation
class _FakeResp:
    def __init__(self, status=400, payload=None):
        self.status_code = status
        self.text = json.dumps(payload or {})


def test_ai_strip_tools():
    from ai_client import AIClient
    cli = AIClient.__new__(AIClient)
    assert cli._strip_tools(None) is None
    assert cli._strip_tools({"temperature": 1}) == {"temperature": 1}
    out = cli._strip_tools({"tools": [1], "tool_choice": "auto"})
    assert not out  # vacío (None o {}): no quedan claves extra


async def test_ai_chat_retries_without_tools(monkeypatch):
    from ai_client import AIClient, AIError
    cli = AIClient.__new__(AIClient)
    cli.base_url = "http://x"
    cli.api_key = "k"
    cli.chat_model = "m"
    cli.temperature = 0.7
    cli.max_tokens = 10
    cli.timeout = 5
    cli._vision_supported = None
    cli._vision_checked_at = 0.0
    cli._vision_recheck_interval = 3600
    cli._tools_supported = None
    cli._tools_checked_at = 0.0
    cli._tools_recheck_interval = 3600
    cli._client = None  # no se usa: se parchea _post_chat

    calls = []

    async def fake_post(payload):
        calls.append(payload)
        if "tools" in payload:
            raise AIError("AI endpoint returned 400")
        return {"choices": [{"message": {"content": "respuesta texto"}}],
                "model": "m", "usage": {}}

    monkeypatch.setattr(cli, "_post_chat", fake_post)
    resp = await cli.chat([{"role": "user", "content": "hi"}],
                          extra={"tools": [{"type": "function"}]})
    assert resp.text == "respuesta texto"
    assert len(calls) == 2          # con tools -> fallo -> sin tools
    assert "tools" in calls[0]
    assert "tools" not in calls[1]
    assert cli._tools_supported is False  # marcado como no soportado


async def test_ai_chat_skips_tools_after_marked(monkeypatch):
    from ai_client import AIClient
    cli = AIClient.__new__(AIClient)
    cli.base_url = "http://x"
    cli.api_key = "k"
    cli.chat_model = "m"
    cli.temperature = 0.7
    cli.max_tokens = 10
    cli.timeout = 5
    cli._vision_supported = None
    cli._vision_checked_at = 0.0
    cli._vision_recheck_interval = 3600
    cli._tools_supported = False
    cli._tools_checked_at = time.time()  # recién sondado
    cli._tools_recheck_interval = 3600
    cli._client = None

    calls = []

    async def fake_post(payload):
        calls.append(payload)
        return {"choices": [{"message": {"content": "ok"}}],
                "model": "m", "usage": {}}

    monkeypatch.setattr(cli, "_post_chat", fake_post)
    await cli.chat([{"role": "user", "content": "hi"}],
                   extra={"tools": [{"type": "function"}]})
    assert len(calls) == 1
    assert "tools" not in calls[0]  # se envió directo sin tools
