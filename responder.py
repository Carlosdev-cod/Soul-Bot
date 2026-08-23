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


RESPONSE_SYSTEM_PROMPT_TEMPLATE = (
    "Actúas como el dueño de esta cuenta de Telegram. No eres una IA, eres la "
    "persona descrita en Soul.md. Respondes en primera persona, como él/ella "
    "escribiría en ese chat. No reveles que eres un modelo de lenguaje ni que "
    "estás imitando a nadie.\n\n"
    "=== Soul.md del dueño ===\n{soul}\n\n"
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


class Responder:
    def __init__(self, store: MessageStore, ai: AIClient, soul: SoulManager,
                 responder_cfg: dict, safety_cfg: dict, owner_id: int):
        self.store = store
        self.ai = ai
        self.soul = soul
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

    # -------------------------------------------------------------- decision
    def _on_cooldown(self, chat_id: int, is_private: bool) -> bool:
        last = self._last_reply_at.get(chat_id, 0.0)
        cd = self.private_cooldown if is_private else self.group_cooldown
        return (time.time() - last) < cd

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
        if self.do_not_reply_to_bots and ctx.incoming_from_id == 777000:
            return False, "skip_telegram_service"
        if self.do_not_reply_to_commands and ctx.incoming_text.startswith("/"):
            return False, "skip_command"
        if self._over_rate_limit():
            return False, "rate_limit"
        if self._on_cooldown(ctx.chat_id, is_private):
            return False, "cooldown"
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
        rows = await self.store.fetch_recent(ctx.chat_id, self.max_context)
        msgs: list[dict] = []
        for r in rows:
            role = "assistant" if r["from_id"] == self.owner_id else "user"
            content = (r.get("text") or "").strip()
            cap = (r.get("caption") or "").strip()
            if cap:
                content = (content + f" [foto: {cap}]").strip()
            if not content:
                continue
            name = r.get("from_name") or ("yo" if role == "assistant" else "alguien")
            if role == "user":
                content = f"{name}: {content}"
            msgs.append({"role": role, "content": content})
        # Reemplazar el último 'user' por el mensaje entrante actual sin
        # duplicar si ya está almacenado.
        # (El handler lo almacena antes de llamar al responder.)
        return msgs

    async def generate_reply(self, ctx: ReplyContext) -> str | None:
        soul = self.soul.get_soul()
        if not soul:
            log.warning("No Soul.md available; cannot reply.")
            return None
        system = RESPONSE_SYSTEM_PROMPT_TEMPLATE.format(soul=soul)
        conversation = await self._build_conversation_messages(ctx)
        if not conversation:
            conversation = [{
                "role": "user",
                "content": f"{ctx.incoming_from_name}: {ctx.incoming_text}",
            }]
        try:
            text = await self.ai.reply_with_image_context(
                system=system,
                conversation=conversation,
                image_bytes=ctx.photo_bytes if ctx.has_photo else None,
                mime=ctx.photo_mime,
                caption=ctx.caption,
            )
        except Exception as e:
            log.error("Reply generation failed: %s", e)
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
        return {
            "group_reply_mode": self.group_reply_mode,
            "group_cooldown": self.group_cooldown,
            "private_cooldown": self.private_cooldown,
            "max_context": self.max_context,
            "prob_always": self.prob_always,
            "replies_last_60s": sum(1 for t in self._reply_history if now - t < 60),
            "max_replies_per_minute": self.max_replies_per_minute,
        }
