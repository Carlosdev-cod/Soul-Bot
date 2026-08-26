"""
Tests de MessageStore: deduplicación, mark_analyzed por chat,
chat_context y lecturas concurrentes.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import pytest_asyncio

from message_store import MessageStore


@pytest_asyncio.fixture
async def store(tmp_path):
    s = MessageStore(str(tmp_path / "test.db"))
    yield s
    # cerrar nada: sqlite se cierra por contexto en cada op


async def _add(store, chat_id, message_id, text="hola", ts=1000, is_out=1,
              from_id=1, caption=None):
    await store.add_message(ts=ts, chat_id=chat_id, chat_type="private",
                            chat_title=f"chat{chat_id}", message_id=message_id,
                            from_id=from_id, from_name="yo" if is_out else "otro",
                            is_out=is_out, text=text, caption=caption)


@pytest.mark.asyncio
async def test_upsert_no_duplica_edits(store):
    """Un mensaje editado (mismo chat_id+message_id) actualiza, no duplica."""
    await _add(store, 111, 50, text="original")
    await _add(store, 111, 50, text="editado")
    assert await store.count_my_messages() == 1
    rows = await store.fetch_recent(111, limit=5)
    assert rows[0]["text"] == "editado"


@pytest.mark.asyncio
async def test_mismo_message_id_en_distintos_chats(store):
    """message_id es por-chat: no debe confundirse entre chats."""
    await _add(store, 111, 100, text="de A")
    await _add(store, 222, 100, text="de B")
    assert await store.count_my_messages() == 2


@pytest.mark.asyncio
async def test_mark_analyzed_respeta_chat(store):
    """El bug original: marcaba por message_id global y contaminaba chats."""
    await _add(store, 111, 100, text="de A")
    await _add(store, 222, 100, text="de B")
    await store.mark_analyzed([(111, 100)])
    unan = await store.fetch_my_messages(limit=10, only_unanalyzed=True)
    assert len(unan) == 1 and unan[0]["chat_id"] == 222
    analyzed = await store.count_analyzed()
    assert analyzed == 1


@pytest.mark.asyncio
async def test_upsert_conserva_analyzed(store):
    """Al editar un mensaje ya analizado no se resetea su estado."""
    await _add(store, 111, 10, text="v1")
    await store.mark_analyzed([(111, 10)])
    await _add(store, 111, 10, text="v2 editado")
    assert await store.count_analyzed() == 1
    assert await store.count_unanalyzed() == 0


@pytest.mark.asyncio
async def test_fetch_recent_orden_cronologico(store):
    await _add(store, 1, 1, text="primero", ts=100)
    await _add(store, 1, 2, text="segundo", ts=200)
    await _add(store, 1, 3, text="tercero", ts=300)
    rows = await store.fetch_recent(1, limit=3)
    textos = [r["text"] for r in rows]
    assert textos == ["primero", "segundo", "tercero"]


@pytest.mark.asyncio
async def test_lecturas_concurrentes_columnas_correctas(store):
    """Sin _CURSOR_DESC global: consultas intercaladas no corrompen columnas."""
    await _add(store, 111, 1, text="msg", is_out=1)
    await store.upsert_chat_context(111, chat_type="private", chat_title="A",
                                    summary="resumen", topics=["t"],
                                    keywords=["k"], participants=["yo"],
                                    messages_total=1, my_messages_total=1,
                                    first_ts=1, last_ts=1, summary_at=1)
    for _ in range(50):
        t1 = asyncio.create_task(store.fetch_recent(111, limit=1))
        t2 = asyncio.create_task(store.get_chat_context(111))
        r1, r2 = await asyncio.gather(t1, t2)
        assert r1[0]["chat_type"] == "private"
        assert r1[0]["text"] == "msg"
        assert r2["summary"] == "resumen"


@pytest.mark.asyncio
async def test_chat_context_upsert_y_lectura(store):
    await store.upsert_chat_context(42, chat_type="group",
                                    chat_title="Grupo", summary="s1",
                                    topics=["a", "b"], keywords=["c"],
                                    participants=["x"], my_role="amigo",
                                    tone="informal", messages_total=10,
                                    my_messages_total=4, first_ts=1,
                                    last_ts=2, summary_at=3, summary_model="m")
    # segundo upsert parcial: no debe perder los campos del primero
    await store.upsert_chat_context(42, summary="s2", summary_at=4)
    ctx = await store.get_chat_context(42)
    assert ctx["summary"] == "s2"
    assert ctx["topics"] == ["a", "b"]
    assert ctx["chat_title"] == "Grupo"
    assert ctx["messages_total"] == 10


@pytest.mark.asyncio
async def test_message_exists(store):
    await _add(store, 111, 55)
    assert await store.message_exists(111, 55) is True
    assert await store.message_exists(222, 55) is False
    assert await store.message_exists(111, 56) is False


@pytest.mark.asyncio
async def test_delete_owner_chat(store):
    await _add(store, 111, 1)
    await _add(store, 222, 2)
    deleted = await store.delete_owner_chat(111)
    assert deleted == 1
    assert await store.count_my_messages() == 1


@pytest.mark.asyncio
async def test_metrics(store):
    await _add(store, 111, 1, text="hola que tal", is_out=1)
    await _add(store, 222, 2, text="x", is_out=1)
    m = await store.my_metrics()
    assert m["total_messages"] == 2
    assert m["chats_touched"] == 2
