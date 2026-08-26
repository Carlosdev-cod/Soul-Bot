"""
Tests de SoulManager: transcript de contexto con saltos reales,
parsing de JSON del proveedor y helpers.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import pytest_asyncio

import soul_manager
from soul_manager import (_parse_context_json, _strip_codefence,
                           SoulManager, _format_my_messages_for_ai)
from message_store import MessageStore


class FakeAI:
    """Stub del cliente IA para no depender de red."""

    def __init__(self, response="{}"):
        self.response = response
        self.calls: list[dict] = []
        self.chat_model = "fake-model"

    async def chat_text(self, system, user, *, temperature=None,
                        max_tokens=None):
        self.calls.append({"system": system, "user": user})
        return self.response


def test_strip_codefence():
    assert _strip_codefence("```markdown\nhola\n```") == "hola"
    assert _strip_codefence("hola") == "hola"
    assert _strip_codefence("```\nabc\n```") == "abc"


def test_parse_context_json():
    ok = _parse_context_json('{"summary": "hablan de futbol"}')
    assert ok["summary"] == "hablan de futbol"
    # con fences
    ok2 = _parse_context_json('```json\n{"summary": "x"}\n```')
    assert ok2["summary"] == "x"
    # JSON embebido en texto
    ok3 = _parse_context_json('Claro, aquí va: {"summary": "y"} espero ayude')
    assert ok3["summary"] == "y"
    # inválido → dict vacío, no excepción
    assert _parse_context_json("no hay json aqui") == {}


def test_format_my_messages_filtra_urls():
    msgs = [
        {"ts": 1000, "chat_title": "G", "chat_type": "group", "text": None,
         "caption": None},
        {"ts": 1001, "chat_title": "G", "chat_type": "group",
         "text": "https://spam.com", "caption": None},
        {"ts": 1002, "chat_title": "G", "chat_type": "group",
         "text": "hola como estas", "caption": None},
    ]
    out = _format_my_messages_for_ai(msgs)
    assert "hola como estas" in out
    assert "spam.com" not in out


@pytest.mark.asyncio
async def test_refresh_chat_context_transcript_con_saltos(tmp_path):
    """Bug original: '\\n'.join(lines) mandaba TODO en una línea."""
    store = MessageStore(str(tmp_path / "t.db"))
    for i, txt in enumerate(["hola", "que tal", "bien"]):
        await store.add_message(ts=1000 + i, chat_id=1, chat_type="group",
                                chat_title="G", message_id=i, from_id=555,
                                from_name="Ana", is_out=0, text=txt)
    fake_ai = FakeAI('{"summary": "saludo", "topics": ["saludos"], '
                     '"keywords": ["hola"], "my_role": "amigo", '
                     '"tone": "informal"}')
    sm = SoulManager(store, fake_ai, {"soul_md_path": str(tmp_path / "S.md")})
    ctx = await sm.refresh_chat_context(1, force=True)
    # el prompt enviado a la IA debe contener saltos de línea reales
    # (cada línea del transcript empieza con "[") y NUNCA backslash-n literal
    user_prompt = fake_ai.calls[0]["user"]
    assert "Conversación reciente:\n[" in user_prompt
    assert "\n[" in user_prompt          # líneas separadas por \n real
    assert "\\n[" not in user_prompt     # sin backslash-n literal
    assert ctx["summary"] == "saludo"
    assert ctx["topics"] == ["saludos"]


@pytest.mark.asyncio
async def test_refresh_chat_context_cache_30min(tmp_path):
    """No regenera el resumen si el último tiene menos de 30 min."""
    import time as _t
    store = MessageStore(str(tmp_path / "t.db"))
    await store.add_message(ts=1000, chat_id=1, chat_type="group",
                            chat_title="G", message_id=1, from_id=555,
                            from_name="Ana", is_out=0, text="hola")
    fake_ai = FakeAI('{"summary": "s1"}')
    sm = SoulManager(store, fake_ai, {"soul_md_path": str(tmp_path / "S.md")})
    await sm.refresh_chat_context(1, force=True)
    n_calls = len(fake_ai.calls)
    # sin force y con summary_at reciente → no debe llamar a la IA
    await sm.refresh_chat_context(1)
    assert len(fake_ai.calls) == n_calls
