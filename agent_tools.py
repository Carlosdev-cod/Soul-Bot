"""
agent_tools.py
==============
Sistema de tools (function calling OpenAI-compatible) que conecta a la IA
con Telegram a través de Kurigram.

Hasta ahora el agente solo podía GENERAR texto de respuesta. Con este módulo
la IA puede además EJECUTAR acciones reales en Telegram durante la
generación de su respuesta:

  - reaccionar a mensajes con emojis          (react_to_message)
  - enviar mensajes (al chat actual o         (send_message)
    autorizados)
  - buscar en TODO tu historial capturado     (search_history)
  - leer el historial reciente de un chat     (get_chat_history)
  - guardar recuerdos persistentes            (save_memory / recall_memories
                                               / forget_memory)
  - programar mensajes futuros / avisos       (schedule_message /
                                               list_scheduled /
                                               cancel_scheduled)
  - consultar metadatos de un chat            (get_chat_info)
  - leer su propio Soul.md                    (read_soul)

Seguridad (todas auditadas en log):
  - `tools.enabled=false` desactiva todo el sistema.
  - send_message solo al chat actual salvo que el destino esté autorizado
    Y `tools.allow_send_to_authorized_chats=true`.
  - Presupuesto de ejecuciones por respuesta (evita bucles del modelo).
  - schedule_message: delay 10s..7 días y tope de tareas pendientes.
  - Cada tool devuelve un dict JSON; los errores se devuelven como
    {"ok": false, "error": "..."} para que el modelo se corrija solo.

Degradación elegante: si el endpoint de IA no soporta `tools`, AIClient
reintenta sin tools y el agente responde como siempre (solo texto).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ai_client import AIClient
from auth_manager import AuthManager
from memory_store import MemoryStore
from message_store import MessageStore
from scheduler import MessageScheduler

log = logging.getLogger("soul.tools")

# Emojis permitidos para reaccionar (los habituales en Telegram).
REACTION_EMOJIS = {
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤‍🔥", "💯", "🤣", "💔", "🙌", "😭",
    "😤", "😎", "👀", "🤝", "✍", "⚡", "🍰", "🫡", "🤌", "spam",
}


@dataclass
class ToolContext:
    """Contexto inmutable de la generación actual (una por mensaje)."""
    chat_id: int
    chat_type: str                 # private | group | supergroup
    chat_title: str
    incoming_message_id: int | None
    from_id: int                   # quien escribió el mensaje entrante
    from_name: str


@dataclass
class ToolExecution:
    name: str
    ok: bool
    result: Any = None
    error: str | None = None
    at: float = field(default_factory=time.time)


class ToolError(RuntimeError):
    """Error controlado de una tool: se devuelve al modelo, no rompe el loop."""


class TelegramToolbox:
    """Registro + ejecutor de tools con loop agéntico estándar de function
    calling (assistant tool_calls -> tool results -> ... -> texto final)."""

    def __init__(self, *, client, store: MessageStore, memory: MemoryStore,
                 scheduler: MessageScheduler, auth: AuthManager,
                 soul_provider: Callable[[], str | None],
                 owner_id: int, cfg: dict):
        self.client = client                      # pyrogram (kurigram) Client
        self.store = store
        self.memory = memory
        self.scheduler = scheduler
        self.auth = auth
        self.soul_provider = soul_provider        # -> str Soul.md
        self.owner_id = int(owner_id)
        tc = cfg or {}
        self.enabled = bool(tc.get("enabled", True))
        self.max_rounds = int(tc.get("max_tool_rounds", 3))
        self.max_executions_per_reply = int(tc.get("max_executions_per_reply", 6))
        self.allow_send_to_authorized = bool(
            tc.get("allow_send_to_authorized_chats", True))
        self.max_scheduled_messages = int(tc.get("max_scheduled_messages", 20))
        self.max_send_text_length = int(tc.get("max_send_text_length", 3500))
        # métricas para /soul_status
        self.total_executions = 0
        self.total_errors = 0
        self.last_executed: list[str] = []
        # registro nombre -> handler async (ctx, **kwargs) -> dict
        self._handlers: dict[str, Callable[..., Awaitable[dict]]] = {}
        self._register_tools()

    # =====================================================================
    #  Especificaciones (formato OpenAI tools)
    # =====================================================================
    def specs(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "react_to_message",
                    "description": (
                        "Reacciona con un emoji al mensaje que estás "
                        "contestando (o al que indiques). Úsalo solo cuando "
                        "una reacción sea más natural que un mensaje escrito."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "emoji": {
                                "type": "string",
                                "description": (
                                    "Emoji de reacción, ej: 👍 ❤ 🔥 😂. "
                                    "Debe ser uno de los emojis estándar "
                                    "de Telegram."),
                            },
                            "message_id": {
                                "type": "integer",
                                "description": (
                                    "Opcional: id del mensaje a reaccionar. "
                                    "Por defecto, el mensaje entrante actual."),
                            },
                        },
                        "required": ["emoji"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_message",
                    "description": (
                        "Envía un mensaje de Telegram como el dueño. Por "
                        "defecto al chat de la conversación actual. Úsalo "
                        "solo si aporta algo que no cubre tu respuesta normal."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "chat_id": {
                                "type": "integer",
                                "description": (
                                    "Destino. Por defecto el chat actual. "
                                    "Otros chats solo si están autorizados."),
                            },
                            "text": {"type": "string",
                                     "description": "Texto a enviar."},
                            "reply_to_message_id": {
                                "type": "integer",
                                "description": (
                                    "Opcional: id de mensaje a responder."),
                            },
                        },
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_history",
                    "description": (
                        "Busca en TODO el historial de mensajes capturado "
                        "del dueño (todos los chats). Ideal para recordar "
                        "qué se dijo, a quién, o cuándo."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string",
                                      "description": "Texto a buscar."},
                            "limit": {"type": "integer",
                                      "description": "Resultados (1-15). "
                                                     "Por defecto 5."},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_chat_history",
                    "description": (
                        "Devuelve los últimos mensajes de un chat capturado "
                        "localmente (por defecto, el chat actual)."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "chat_id": {
                                "type": "integer",
                                "description": "Por defecto el chat actual.",
                            },
                            "limit": {"type": "integer",
                                      "description": "Cantidad (1-40). "
                                                     "Por defecto 15."},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "save_memory",
                    "description": (
                        "Guarda un recuerdo persistente para futuras "
                        "conversaciones. Úsalo para datos importantes: "
                        "gustos, pendientes, contexto de personas."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": (
                                    "Identificador corto y estable, ej: "
                                    "'ana_prefiere_cafe' o 'pendiente_pago')."),
                            },
                            "content": {"type": "string",
                                        "description": "El recuerdo completo."},
                        },
                        "required": ["key", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "recall_memories",
                    "description": (
                        "Recupera recuerdos guardados. Sin query devuelve "
                        "los más recientes."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string",
                                      "description": "Texto a buscar."},
                            "limit": {"type": "integer",
                                      "description": "Por defecto 5."},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "forget_memory",
                    "description": (
                        "Elimina un recuerdo por su key exacta (cuando ya "
                        "no aplica o está desactualizado)."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                        },
                        "required": ["key"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "schedule_message",
                    "description": (
                        "Programa el envío futuro de un mensaje como el "
                        "dueño, en el chat actual. Ej: recordatorios o "
                        "'te contesto en una hora'."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "delay_seconds": {
                                "type": "integer",
                                "description": (
                                    "Segundos a esperar (mínimo 10, máximo "
                                    "604800 = 7 días)."),
                            },
                            "text": {"type": "string",
                                     "description": "Mensaje a enviar."},
                            "chat_id": {
                                "type": "integer",
                                "description": "Por defecto el chat actual.",
                            },
                        },
                        "required": ["delay_seconds", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_scheduled",
                    "description": (
                        "Lista los mensajes programados pendientes con su "
                        "id, chat y cuándo se enviarán."),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cancel_scheduled",
                    "description": (
                        "Cancela un mensaje programado pendiente por su id."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                        },
                        "required": ["task_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_chat_info",
                    "description": (
                        "Metadatos de un chat (título, tipo, nº de miembros "
                        "si es grupo). Por defecto el chat actual."),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "chat_id": {
                                "type": "integer",
                                "description": "Por defecto el chat actual.",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_soul",
                    "description": (
                        "Devuelve el Soul.md actual: quién eres, cómo "
                        "escribes. Útil para verificar tu identidad antes "
                        "de mensajes delicados."),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]

    # =====================================================================
    #  Registro de handlers
    # =====================================================================
    def _register_tools(self) -> None:
        self._handlers = {
            "react_to_message": self._t_react_to_message,
            "send_message": self._t_send_message,
            "search_history": self._t_search_history,
            "get_chat_history": self._t_get_chat_history,
            "save_memory": self._t_save_memory,
            "recall_memories": self._t_recall_memories,
            "forget_memory": self._t_forget_memory,
            "schedule_message": self._t_schedule_message,
            "list_scheduled": self._t_list_scheduled,
            "cancel_scheduled": self._t_cancel_scheduled,
            "get_chat_info": self._t_get_chat_info,
            "read_soul": self._t_read_soul,
        }

    # =====================================================================
    #  Loop agéntico (function calling)
    # =====================================================================
    async def run_tool_loop(self, ai: AIClient, system: str,
                            conversation: list[dict],
                            ctx: ToolContext) -> tuple[str | None,
                                                       list[ToolExecution]]:
        """Ejecuta el ciclo tools -> resultados -> ... -> texto final.

        Devuelve (texto_final, ejecuciones). Si el endpoint no soporta
        tools o algo falla, degrada a una llamada normal sin tools.
        """
        messages: list[dict] = [{"role": "system", "content": system},
                                *conversation]
        executed: list[ToolExecution] = []
        budget = self.max_executions_per_reply
        try:
            for round_no in range(self.max_rounds):
                resp = await ai.chat(
                    messages, extra={"tools": self.specs(),
                                     "tool_choice": "auto"})
                tool_calls = _extract_tool_calls(resp.raw)
                if not tool_calls:
                    return (resp.text or "").strip() or None, executed
                # El mensaje assistant con tool_calls se reenvía tal cual
                assistant_msg = _assistant_message(resp.raw)
                if assistant_msg:
                    messages.append(assistant_msg)
                for tc in tool_calls:
                    if budget <= 0:
                        messages.append(_tool_result_msg(
                            tc.get("id"),
                            {"ok": False,
                             "error": "límite de ejecuciones alcanzado; "
                                      "responde ya al usuario"}))
                        continue
                    ex = await self._execute_safe(tc, ctx)
                    executed.append(ex)
                    budget -= 1
                    self._note_execution(ex)
                    messages.append(_tool_result_msg(tc.get("id"),
                                                     ex.result if ex.ok
                                                     else {"ok": False,
                                                           "error": ex.error}))
            # Se agotaron las rondas: forzar texto final sin tools
            resp = await ai.chat(messages)
            return (resp.text or "").strip() or None, executed
        except Exception as e:
            log.exception("Tool loop failed: %s", e)
            return None, executed

    async def _execute_safe(self, tc: dict, ctx: ToolContext) -> ToolExecution:
        name = (tc.get("function", {}) or {}).get("name") or ""
        raw_args = (tc.get("function", {}) or {}).get("arguments") or "{}"
        try:
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) \
                    else (raw_args or {})
            except json.JSONDecodeError:
                raise ToolError(f"argumentos JSON inválidos: {raw_args[:120]}")
            handler = self._handlers.get(name)
            if handler is None:
                raise ToolError(f"tool desconocida: {name}")
            result = await handler(ctx, **(args or {}))
            return ToolExecution(name=name, ok=True, result=result)
        except ToolError as e:
            self.total_errors += 1
            return ToolExecution(name=name, ok=False, error=str(e))
        except TypeError as e:
            # kwargs incorrectos en la llamada
            self.total_errors += 1
            return ToolExecution(name=name, ok=False,
                                 error=f"argumentos inválidos: {e}")
        except Exception as e:
            self.total_errors += 1
            log.exception("Tool %s crashed: %s", name, e)
            return ToolExecution(name=name, ok=False,
                                 error=f"error interno: {e}")

    def _note_execution(self, ex: ToolExecution) -> None:
        self.total_executions += 1
        self.last_executed.append(f"{ex.name}:{'ok' if ex.ok else 'err'}")
        self.last_executed = self.last_executed[-10:]

    # =====================================================================
    #  Handlers de las tools
    # =====================================================================
    def _resolve_chat(self, ctx: ToolContext, chat_id: Any) -> int:
        if chat_id in (None, "", 0):
            return ctx.chat_id
        try:
            return int(chat_id)
        except (TypeError, ValueError):
            raise ToolError("chat_id debe ser un número")

    def _ensure_send_allowed(self, ctx: ToolContext, chat_id: int) -> None:
        """Envíos solo al chat actual o a chats autorizados (si está activo)."""
        if chat_id == ctx.chat_id:
            return
        if not self.allow_send_to_authorized:
            raise ToolError("envío a otros chats no permitido "
                            "(tools.allow_send_to_authorized_chats=false)")
        # owner_id se lee en tiempo de ejecución: puede auto-asignarse tras
        # el primer arranque (antes era 0 en config nueva)
        owner = self.auth.owner_id or self.owner_id
        authorized = (self.auth.is_group_authorized(chat_id)
                      or self.auth.is_user_authorized(chat_id)
                      or chat_id == owner)
        if not authorized:
            raise ToolError(f"el chat {chat_id} no está autorizado")

    async def _t_react_to_message(self, ctx: ToolContext, emoji: str = "",
                                  message_id: int | None = None) -> dict:
        emoji = (emoji or "").strip()
        if not emoji:
            raise ToolError("emoji requerido")
        if emoji not in REACTION_EMOJIS:
            raise ToolError(f"emoji no soportado como reacción: {emoji}")
        target = int(message_id) if message_id else ctx.incoming_message_id
        if not target:
            raise ToolError("no hay mensaje objetivo para reaccionar")
        await self.client.send_reaction(ctx.chat_id, target, emoji)
        log.info("Tool react: chat=%s msg=%s emoji=%s",
                 ctx.chat_id, target, emoji)
        return {"ok": True, "reacted": emoji, "message_id": target}

    async def _t_send_message(self, ctx: ToolContext, text: str = "",
                              chat_id: Any = None,
                              reply_to_message_id: int | None = None) -> dict:
        text = (text or "").strip()
        if not text:
            raise ToolError("text requerido")
        if len(text) > self.max_send_text_length:
            raise ToolError(f"texto demasiado largo (máx "
                            f"{self.max_send_text_length})")
        chat = self._resolve_chat(ctx, chat_id)
        self._ensure_send_allowed(ctx, chat)
        await self.client.send_message(chat, text,
                                       reply_to_message_id=reply_to_message_id)
        log.info("Tool send_message: chat=%s len=%d reply_to=%s",
                 chat, len(text), reply_to_message_id)
        return {"ok": True, "chat_id": chat, "sent_length": len(text)}

    async def _t_search_history(self, ctx: ToolContext, query: str = "",
                                limit: int = 5) -> dict:
        query = (query or "").strip()
        if not query:
            raise ToolError("query requerido")
        limit = max(1, min(int(limit or 5), 15))
        rows = await self.store.search_messages(query, limit=limit)
        return {"ok": True, "query": query,
                "results": [_row_brief(r) for r in rows],
                "count": len(rows)}

    async def _t_get_chat_history(self, ctx: ToolContext,
                                  chat_id: Any = None, limit: int = 15) -> dict:
        chat = self._resolve_chat(ctx, chat_id)
        limit = max(1, min(int(limit or 15), 40))
        rows = await self.store.fetch_recent(chat, limit)
        return {"ok": True, "chat_id": chat,
                "messages": [_row_brief(r) for r in rows],
                "count": len(rows)}

    async def _t_save_memory(self, ctx: ToolContext, key: str = "",
                             content: str = "") -> dict:
        key, content = (key or "").strip(), (content or "").strip()
        if not key or not content:
            raise ToolError("key y content requeridos")
        await self.memory.save(key, content)
        log.info("Tool save_memory: key=%r", key)
        return {"ok": True, "key": key, "saved": True}

    async def _t_recall_memories(self, ctx: ToolContext, query: str = "",
                                 limit: int = 5) -> dict:
        limit = max(1, min(int(limit or 5), 10))
        rows = await self.memory.recall((query or "").strip(), limit=limit)
        return {"ok": True, "memories": [
            {"key": r["key"], "content": r["content"]} for r in rows],
            "count": len(rows)}

    async def _t_forget_memory(self, ctx: ToolContext, key: str = "") -> dict:
        key = (key or "").strip()
        if not key:
            raise ToolError("key requerido")
        removed = await self.memory.forget(key)
        return {"ok": True, "key": key, "removed": removed}

    async def _t_schedule_message(self, ctx: ToolContext,
                                  delay_seconds: int = 0, text: str = "",
                                  chat_id: Any = None) -> dict:
        text = (text or "").strip()
        if not text:
            raise ToolError("text requerido")
        try:
            delay = int(delay_seconds)
        except (TypeError, ValueError):
            raise ToolError("delay_seconds debe ser entero")
        if delay < 10:
            raise ToolError("delay mínimo: 10 segundos")
        if delay > 7 * 24 * 3600:
            raise ToolError("delay máximo: 7 días (604800s)")
        chat = self._resolve_chat(ctx, chat_id)
        self._ensure_send_allowed(ctx, chat)
        if await self.scheduler.pending_count() >= self.max_scheduled_messages:
            raise ToolError(f"máximo de {self.max_scheduled_messages} tareas "
                            "pendientes alcanzado")
        task = await self.scheduler.add(chat, text, time.time() + delay)
        return {"ok": True, "task_id": task["id"],
                "send_at": task["send_at"], "chat_id": chat}

    async def _t_list_scheduled(self, ctx: ToolContext) -> dict:
        tasks = await self.scheduler.pending()
        now = time.time()
        return {"ok": True, "tasks": [
            {"task_id": t["id"], "chat_id": t["chat_id"],
             "text": t["text"][:120],
             "in_seconds": round(t["send_at"] - now)}
            for t in tasks], "count": len(tasks)}

    async def _t_cancel_scheduled(self, ctx: ToolContext,
                                  task_id: str = "") -> dict:
        task_id = (task_id or "").strip()
        if not task_id:
            raise ToolError("task_id requerido")
        cancelled = await self.scheduler.cancel(task_id)
        if not cancelled:
            raise ToolError(f"tarea {task_id} no encontrada o no pendiente")
        return {"ok": True, "task_id": task_id, "cancelled": True}

    async def _t_get_chat_info(self, ctx: ToolContext,
                               chat_id: Any = None) -> dict:
        chat = self._resolve_chat(ctx, chat_id)
        try:
            info = await self.client.get_chat(chat)
        except Exception as e:
            raise ToolError(f"no pude consultar el chat: {e}")
        members = getattr(info, "members_count", None)
        return {"ok": True, "chat_id": chat,
                "type": str(getattr(getattr(info, "type", None), "value",
                                    getattr(info, "type", ""))),
                "title": getattr(info, "title", None)
                         or getattr(info, "first_name", None)
                         or getattr(info, "username", None),
                "username": getattr(info, "username", None),
                "members_count": members}

    async def _t_read_soul(self, ctx: ToolContext) -> dict:
        soul = self.soul_provider()
        if not soul:
            raise ToolError("Soul.md aún no generado")
        return {"ok": True, "soul_md": soul[:6000]}

    # =====================================================================
    #  Prompt adicional para el system del responder
    # =====================================================================
    @staticmethod
    def system_prompt_section() -> str:
        return (
            "\n\n=== Herramientas (tools) disponibles ===\n"
            "Puedes EJECUTAR acciones en Telegram usando tool calls además de "
            "responder texto. Herramientas: reaccionar con emojis, enviar "
            "mensajes, buscar en todo tu historial, leer historial de chats, "
            "guardar/recuperar recuerdos persistentes, programar mensajes "
            "futuros y consultar info de chats.\n"
            "Criterios de uso (importantes):\n"
            "1. Úsalas SOLO cuando aporten valor real y sean algo que el "
            "dueño haría. Un mensaje de texto normal NO necesita tools.\n"
            "2. Si te piden recordar algo para el futuro, usa "
            "save_memory o schedule_message.\n"
            "3. Si te preguntan por algo dicho antes, usa search_history "
            "antes de responder.\n"
            "4. Una reacción (👍/❤/🔥) es mejor que responder de más.\n"
            "5. Tras ejecutar tools, tu texto final sigue siendo la "
            "respuesta al mensaje: devuélvela igualmente.\n"
        )

    # =====================================================================
    #  Estado / stats
    # =====================================================================
    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "tools_count": len(self._handlers),
            "max_rounds": self.max_rounds,
            "max_executions_per_reply": self.max_executions_per_reply,
            "total_executions": self.total_executions,
            "total_errors": self.total_errors,
            "last_executed": list(self.last_executed),
        }


# =====================================================================
#  Helpers estáticos del protocolo tool-calls
# =====================================================================
def _extract_tool_calls(raw: dict) -> list[dict]:
    try:
        msg = raw["choices"][0]["message"]
        calls = msg.get("tool_calls")
        return [c for c in calls if isinstance(c, dict)] if calls else []
    except (KeyError, IndexError, TypeError, AttributeError):
        return []


def _assistant_message(raw: dict) -> dict | None:
    """Reconstruye el mensaje assistant (con tool_calls) para reenviarlo."""
    try:
        msg = raw["choices"][0]["message"]
        out = {"role": "assistant",
               "content": msg.get("content") or None}
        if msg.get("tool_calls"):
            out["tool_calls"] = msg["tool_calls"]
        return out
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def _tool_result_msg(tool_call_id: str, result: dict) -> dict:
    return {"role": "tool", "tool_call_id": str(tool_call_id or ""),
            "content": json.dumps(result, ensure_ascii=False,
                                  default=str)[:3500]}


def _row_brief(r: dict) -> dict:
    """Resumen compacto de una fila de mensaje para el modelo."""
    when = r.get("ts")
    date = time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else "?"
    who = (r.get("from_name") or ("yo" if r.get("is_out") else "?"))
    return {"date": date, "chat": (r.get("chat_title") or
                                   str(r.get("chat_id"))),
            "chat_id": r.get("chat_id"),
            "from": who,
            "mine": bool(r.get("is_out")),
            "text": (r.get("text") or r.get("caption") or "")[:300]}
