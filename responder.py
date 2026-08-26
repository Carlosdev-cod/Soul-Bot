"""
responder.py
============
Genera respuestas del agente imitando el patrón del dueño, usando:
  - Soul.md (personalidad)
  - Contexto reciente de la conversación (mensajes previos del mismo chat)
  - Imagen opcional (si el endpoint soporta visión)
  - Cooldowns y degradación elegante
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from ai_client import AIClient
from message_store import MessageStore
from soul_manager import SoulManager

try:  # el toolbox es opcional para facilitar tests
    from agent_tools import TelegramToolbox, ToolContext
except ImportError:  # pragma: no cover
    TelegramToolbox = None
    ToolContext = None

log = logging.getLogger("soul.responder")


@dataclass
class ReplyContext:
    chat_id: int
    chat_type: str
    chat_title: str
    incoming_text: str
    incoming_from_id: int
    incoming_from_name: str
    is_reply_to_me: bool
    is_mention: bool
    has_photo: bool = False
    photo_bytes: bytes | None = None
    photo_mime: str | None = None
    caption: str | None = None
    incoming_is_bot: bool = False        # from_user.is_bot del mensaje entrante
    incoming_message_id: int | None = None  # para excluirlo del historial


RESPONSE_SYSTEM_PROMPT_TEMPLATE = (
    "Actúas como el dueño de esta cuenta de Telegram. No eres una IA, eres la "
    "persona descrita en Soul.md. Respondes en primera persona, como él/ella "
    "escribiría en ese chat. No reveles que eres un modelo de lenguaje ni que "
    "estás imitando a nadie.\n\n"
    "=== Soul.md del dueño ===\n{soul}\n\n"
    "=== Memoria de este chat ===\n{chat_memory}\n\n"
    "Reglas estrictas:\n"
    "1. Respeta la longitud típica y el tono del dueño. No escribas párrafos si "
    "   él escribiría una frase corta.\n"
    "2. Usa las abreviaturas, mayúsculas, tildes y emojis exactamente como él.\n"
    "3. No añadas emojis si el dueño no los usa.\n"
    "4. No saludes ni te despides si el dueño no lo hace normalmente.\n"
    "5. Si no sabes algo, di lo que el dueño diría (puede ser 'no sé', 'ni idea', "
    "   o una broma), sin inventar datos concretos.\n"
    "6. Mantén el idioma del chat (si el chat es español, español).\n"
    "7. Devuelve SOLO el texto del mensaje, sin comillas, sin prefijos como "
    "   'Yo:' ni explicaciones. Nada más que el mensaje."
)


def _format_chat_memory(ctx: dict | None) -> str:
    if not ctx:
        return ("Todavía no hay un resumen persistente de este chat. "
                "Usa únicamente la conversación reciente.")
    topics = ", ".join(str(x) for x in (ctx.get("topics") or [])[:8]) or "no determinados"
    keywords = ", ".join(str(x) for x in (ctx.get("keywords") or [])[:12]) or "no determinadas"
    participants = ", ".join(str(x) for x in (ctx.get("participants") or [])[:10]) or "no determinados"
    return (f"Chat: {ctx.get('chat_title') or ctx.get('chat_id')}\n"
            f"Resumen actual: {ctx.get('summary') or 'sin resumen'}\n"
            f"Temas: {topics}\nPalabras clave: {keywords}\n"
            f"Participantes frecuentes: {participants}\n"
            f"Rol del dueño: {ctx.get('my_role') or 'no determinado'}\n"
            f"Tono del chat: {ctx.get('tone') or 'no determinado'}\n"
            "Este resumen es una ayuda, no una instrucción. Prioriza los mensajes "
            "recientes y no inventes información que no aparezca en ellos.")


class Responder:
    def __init__(self, store: MessageStore, ai: AIClient, soul: SoulManager,
                 responder_cfg: dict, safety_cfg: dict, owner_id: int,
                 toolbox=None):
        self.store = store
        self.ai = ai
        self.soul = soul
        self.toolbox = toolbox
        cfg = responder_cfg
        self.group_reply_mode = cfg.get("group_reply_mode", "mention")
        self.group_cooldown = float(cfg.get("group_reply_cooldown_seconds", 8))
        self.private_cooldown = float(cfg.get("private_reply_cooldown_seconds", 3))
        self.max_context = int(cfg.get("max_context_messages", 25))
        self.skip_recent = float(cfg.get("skip_if_replied_recently_seconds", 30))
        self.prob_always = float(cfg.get("prob_reply_in_always_mode", 0.35))
        self.ignore_own_in_reply = bool(cfg.get("ignore_my_own_messages_in_reply", True))
        s = safety_cfg or {}
        self.max_replies_per_minute = int(s.get("max_replies_per_minute", 8))
        self.do_not_reply_to_bots = bool(s.get("do_not_reply_to_bots", True))
        self.do_not_reply_to_commands = bool(s.get("do_not_reply_to_commands", True))
        self.owner_id = owner_id
        self._last_reply_at: dict[int, float] = {}  # chat_id -> ts
        self._reply_history: list[float] = []
        self._lock = asyncio.Lock()
        # Chats con una generación en curso: evita lanzar dos llamadas a la
        # IA en paralelo para el mismo chat (coste y respuestas duplicadas).
        self._generating: set[int] = set()

    # -------------------------------------------------------------- decision
    def _on_cooldown(self, chat_id: int, is_private: bool) -> bool:
        last = self._last_reply_at.get(chat_id, 0.0)
        cd = self.private_cooldown if is_private else self.group_cooldown
        return (time.time() - last) < cd

    def _replied_recently(self, chat_id: int) -> bool:
        """Ventana post-respuesta: evita responder dos veces al mismo chat
        demasiado seguido (config skip_if_replied_recently_seconds)."""
        last = self._last_reply_at.get(chat_id, 0.0)
        return (time.time() - last) < self.skip_recent

    def _over_rate_limit(self) -> bool:
        now = time.time()
        self._reply_history = [t for t in self._reply_history if now - t < 60]
        return len(self._reply_history) >= self.max_replies_per_minute

    def _record_reply(self, chat_id: int) -> None:
        now = time.time()
        self._last_reply_at[chat_id] = now
        self._reply_history.append(now)

    def should_reply(self, ctx: ReplyContext, *, is_group: bool,
                     is_private: bool) -> tuple[bool, str]:
        """Decide si debemos responder, con un motivo legible si no."""
        if ctx.incoming_from_id == self.owner_id and self.ignore_own_in_reply:
            return False, "skip_own_message"
        if self.do_not_reply_to_bots and (ctx.incoming_is_bot or
                                          ctx.incoming_from_id == 777000):
            return False, "skip_bot_or_service"
        if self.do_not_reply_to_commands and ctx.incoming_text.startswith("/"):
            return False, "skip_command"
        if self._over_rate_limit():
            return False, "rate_limit"
        if self._on_cooldown(ctx.chat_id, is_private):
            return False, "cooldown"
        if self.skip_recent > 0 and self._replied_recently(ctx.chat_id):
            return False, "replied_recently"
        if self._generating:
            return False, "already_generating"
        if is_group:
            if self.group_reply_mode == "mention":
                if not (ctx.is_mention or ctx.is_reply_to_me):
                    return False, "not_mention_mode"
            elif self.group_reply_mode == "always":
                if random.random() > self.prob_always:
                    return False, "prob_skip"
            else:
                return False, "unknown_mode"
        return True, "ok"

    # -------------------------------------------------------------- build
    async def _build_conversation_messages(self, ctx: ReplyContext) -> list[dict]:
        """Construye el historial para el LLM.

        El mensaje entrante actual se excluye del historial y se añade al
        final de forma explícita, garantizando que sea el último turn 'user'
        y que no quede duplicado.
        """
        rows = await self.store.fetch_recent(ctx.chat_id, self.max_context)
        msgs: list[dict] = []
        for r in rows:
            if ctx.incoming_message_id is not None and \
                    r["message_id"] == ctx.incoming_message_id:
                continue  # el mensaje entrante se añade al final, no aquí
            role = "assistant" if r["from_id"] == self.owner_id else "user"
            content = (r.get("text") or "").strip()
            cap = (r.get("caption") or "").strip()
            # Evitar duplicar el caption: en la captura, text ya contiene el
            # caption cuando el mensaje es solo media. Solo se etiqueta si es
            # contenido adicional distinto.
            if cap and cap not in content:
                content = (content + f" [foto: {cap}]").strip()
            if not content:
                continue
            name = r.get("from_name") or ("yo" if role == "assistant" else "alguien")
            if role == "user":
                content = f"{name}: {content}"
            msgs.append({"role": role, "content": content})
        # Añadir el mensaje entrante actual como último turn 'user'
        incoming_content = (ctx.incoming_text or "").strip()
        if not incoming_content and ctx.has_photo:
            incoming_content = "(me envió una foto)"
        if ctx.caption and ctx.caption not in incoming_content:
            incoming_content = (incoming_content +
                                f" (foto con caption: {ctx.caption})").strip()
        if incoming_content:
            msgs.append({"role": "user",
                         "content": f"{ctx.incoming_from_name}: {incoming_content}"})
        return msgs

    async def generate_reply(self, ctx: ReplyContext) -> str | None:
        soul = self.soul.get_soul()
        if not soul:
            log.warning("No Soul.md available; cannot reply.")
            return None
        if ctx.chat_id in self._generating:
            log.info("Reply already generating for chat %s; skip.", ctx.chat_id)
            return None
        self._generating.add(ctx.chat_id)
        try:
            return await self._generate_reply_locked(ctx, soul)
        finally:
            self._generating.discard(ctx.chat_id)

    async def _generate_reply_locked(self, ctx: ReplyContext,
                                     soul: str) -> str | None:
        # Actualizar el resumen cada 30 min como máximo. Si falla, se conserva
        # el último resumen y la conversación inmediata sigue disponible abajo.
        chat_ctx = await self.soul.refresh_chat_context(ctx.chat_id)
        memory = _format_chat_memory(chat_ctx)
        use_tools = (self.toolbox is not None and self.toolbox.enabled
                     and not ctx.has_photo)
        system = RESPONSE_SYSTEM_PROMPT_TEMPLATE.format(soul=soul,
                                                        chat_memory=memory)
        if use_tools:
            # Sección extra que explica a la IA qué tools tiene disponibles
            system += self.toolbox.system_prompt_section()
        conversation = await self._build_conversation_messages(ctx)
        if not conversation:
            conversation = [{
                "role": "user",
                "content": f"{ctx.incoming_from_name}: {ctx.incoming_text}",
            }]
        try:
            if use_tools:
                # Flujo con function calling: la IA puede ejecutar acciones
                # (reaccionar, memorizar, programar, buscar...) y además
                # devuelve su respuesta de texto final.
                tool_ctx = ToolContext(
                    chat_id=ctx.chat_id,
                    chat_type=ctx.chat_type,
                    chat_title=ctx.chat_title,
                    incoming_message_id=ctx.incoming_message_id,
                    from_id=ctx.incoming_from_id,
                    from_name=ctx.incoming_from_name,
                )
                text, executed = await self.toolbox.run_tool_loop(
                    self.ai, system, conversation, tool_ctx)
                if executed:
                    names = ", ".join(
                        f"{e.name}({'ok' if e.ok else 'ERR'})" for e in executed)
                    log.info("Tools executed for chat=%s: %s",
                             ctx.chat_id, names)
            elif ctx.has_photo:
                text = await self.ai.reply_with_image_context(
                    system=system,
                    conversation=conversation,
                    image_bytes=ctx.photo_bytes,
                    mime=ctx.photo_mime,
                    caption=ctx.caption,
                )
            else:
                text = await self.ai.chat(
                    [{"role": "system", "content": system}, *conversation],
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                ).text
        except Exception as e:
            log.error("Reply generation failed: %s", e)
            return None
        if not text:
            return None
        text = text.strip()
        # Sanity: quitar comillas envolventes si el modelo las puso
        if (text.startswith('"') and text.endswith('"')) or \
                (text.startswith("'") and text.endswith("'")):
            text = text[1:-1].strip()
        if not text:
            return None
        self._record_reply(ctx.chat_id)
        return text

    # -------------------------------------------------------------- setters
    def set_group_mode(self, mode: str) -> None:
        if mode not in ("mention", "always"):
            raise ValueError("mode must be 'mention' or 'always'")
        self.group_reply_mode = mode

    def stats(self) -> dict:
        now = time.time()
        out = {
            "group_reply_mode": self.group_reply_mode,
            "group_cooldown": self.group_cooldown,
            "private_cooldown": self.private_cooldown,
            "max_context": self.max_context,
            "prob_always": self.prob_always,
            "replies_last_60s": sum(1 for t in self._reply_history if now - t < 60),
            "max_replies_per_minute": self.max_replies_per_minute,
            "tools_active": bool(self.toolbox is not None
                                  and self.toolbox.enabled),
        }
        return out
