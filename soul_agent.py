"""
soul_agent.py
=============
Agente de Telegram (cuenta de usuario, no bot) que aprende tu patrón de
escritura, lo plasma en Soul.md y responde como tú en chats autorizados.

Requisitos:
  - pip install kurigram tgcrypto httpx
  - Editar config.json con tus creds de Telegram (my.telegram.org).
  - La primera vez pide el código de login por consola.

Comandos del dueño (solo él, en cualquier chat):
  /soul_status       — estado del agente
  /soul_now          — forzar refresh de Soul.md
  /soul_pause        — pausar respuestas automáticas
  /soul_resume       — reanudar respuestas automáticas
  /soul_auth_chat    — autorizar el chat actual para auto-respuesta
  /soul_unauth_chat  — quitar autorización del chat actual
  /soul_auth_user    — autorizar al usuario al que respondes (reply) o por ID
  /soul_unauth_user  — quitar autorización de usuario (reply o por ID)
  /soul_set_mode <mention|always>   — modo de respuesta en grupos
  /soul_show         — envía el Soul.md actual (en privado)
  /soul_stats        — estadísticas del almacén y del responder
  /soul_help         — lista de comandos
  /soul_refresh      — alias de /soul_now
  /soul_scan         — backfill de TODOS los chats (grupos+privados), con progreso
  /soul_scan_groups  — backfill solo de grupos
  /soul_scan_private — backfill solo de chats privados
  /soul_scan <id>    — backfill solo del chat indicado (id numérico)
  /soul_learn        — muestra el último "learning summary" generado por la IA

Toda la captura de mensajes es local (SQLite en data/), nada se envía fuera
salvo a tu endpoint de IA para análisis/respuestas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from pyrogram import Client, filters
from pyrogram.types import Message, User
from pyrogram.enums import ChatType

from ai_client import AIClient, AIError
from auth_manager import AuthManager
from backfill import BackfillRunner
from message_store import MessageStore
from responder import ReplyContext, Responder
from soul_manager import SoulManager

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

log = logging.getLogger("soul.main")


# =====================================================================
#  Logging setup
# =====================================================================
def setup_logging(cfg: dict) -> None:
    lc = cfg.get("logging", {})
    level = getattr(logging, lc.get("level", "INFO").upper(), logging.INFO)
    log_path = BASE_DIR / lc.get("log_file", "logs/soul_agent.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers.clear()
    root.addHandler(fh)
    root.addHandler(sh)


# =====================================================================
#  Loader
# =====================================================================
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# =====================================================================
#  SoulAgent — orquestador principal
# =====================================================================
class SoulAgent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.store = MessageStore(str(BASE_DIR / "data" / "messages.db"))
        self.ai = AIClient(cfg)
        self.auth = AuthManager(str(CONFIG_PATH))
        self.soul = SoulManager(self.store, self.ai, cfg.get("soul", {}))
        self.responder = Responder(
            self.store, self.ai, self.soul,
            cfg.get("responder", {}),
            cfg.get("safety", {}),
            self.auth.owner_id,
        )
        self.auto_reply_enabled = bool(cfg.get("responder", {}).get(
            "auto_reply_enabled", True))
        self.paused = False
        self._refresh_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # Telegram client
        t = cfg["telegram"]
        self.app = Client(
            name=t.get("session_name", "soul_agent"),
            api_id=t["api_id"],
            api_hash=t["api_hash"],
            phone_number=t.get("phone_number"),
            workdir=str(BASE_DIR / "session"),
            in_memory=False,
        )
        self.me: User | None = None

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        log.info("Soul Agent starting...")
        await self.app.start()
        self.me = await self.app.get_me()
        log.info("Logged in as: id=%s name=%s username=%s",
                 self.me.id, self.me.first_name, self.me.username)
        if not self.auth.owner_id:
            self.auth.set_owner(self.me.id)
            log.info("Owner auto-set to %s", self.me.id)
        self._register_handlers()
        # Intentar construir Soul.md inicial si ya hay mensajes previos
        await self.soul.maybe_initial_build()
        # Lanzar refresh loop
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        # Probe de visión
        vok = await self.ai.is_vision_enabled()
        log.info("Vision enabled: %s", vok)
        log.info("Soul Agent ready. Owner=%s. Groups=%s Users=%s",
                 self.auth.owner_id, len(self.auth.group_ids),
                 len(self.auth.user_ids))

    async def stop(self) -> None:
        log.info("Stopping Soul Agent...")
        self._stop_event.set()
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        await self.ai.aclose()
        try:
            await self.app.stop()
        except Exception:
            pass
        log.info("Stopped.")

    # -------------------------------------------------------------- loop
    async def _refresh_loop(self) -> None:
        log.info("Soul refresh loop started (interval=%ss)",
                 self.soul.refresh_interval)
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(60)  # check cada minuto
                await self.soul.maybe_initial_build()
                await self.soul.refresh_if_due()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Refresh loop error: %s", e)
                await asyncio.sleep(60)

    # -------------------------------------------------------------- handlers
    def _register_handlers(self) -> None:
        app = self.app
        me_id = self.me.id if self.me else 0

        # 1) Comandos del dueño (PRIORIDAD MÁXIMA) - filtro manual para cuentas de usuario
        @app.on_message(
            (filters.outgoing | filters.incoming) & ~filters.channel
        )
        async def _on_admin_cmd(client: Client, message: Message):
            text = (message.text or "").strip()
            if text.startswith("/soul_"):
                log.info("Admin command detected: %s", text)
                await self._handle_admin_command(client, message)
                return  # No procesar más handlers para este mensaje
            # Si no es comando, procesar normalmente
            await self._capture_message(message, is_out=bool(message.outgoing))
            if not message.outgoing:
                await self._maybe_reply(client, message)

        # 2) Captura de mensajes editados
        @app.on_edited_message(filters.outgoing)
        async def _on_edit_outgoing(client: Client, message: Message):
            await self._capture_message(message, is_out=True, is_edit=True)

        log.info("Handlers registered.")

    # -------------------------------------------------------------- capture
    async def _capture_message(self, message: Message, *, is_out: bool,
                                is_edit: bool = False) -> None:
        try:
            chat = message.chat
            chat_type = (chat.type.value if isinstance(chat.type, ChatType)
                         else str(chat.type))
            text = (message.text or message.caption or "").strip()
            has_media = bool(message.photo or message.video or message.voice
                              or message.sticker or message.animation
                              or message.document or message.audio)
            media_kind = ("photo" if message.photo
                          else "video" if message.video
                          else "voice" if message.voice
                          else "sticker" if message.sticker
                          else "animation" if message.animation
                          else "document" if message.document
                          else "audio" if message.audio
                          else None)
            caption = (message.caption or "").strip() if message.caption else None
            from_user = message.from_user
            from_id = from_user.id if from_user else 0
            from_name = (from_user.first_name if from_user else "") or ""
            if from_user and from_user.last_name:
                from_name = f"{from_name} {from_user.last_name}".strip()
            reply_to = message.reply_to_message
            reply_to_message_id = reply_to.id if reply_to else None
            reply_to_text = None
            if reply_to and (reply_to.text or reply_to.caption):
                rn = reply_to.from_user
                rn_name = (rn.first_name if rn else "?") or "?"
                reply_to_text = (reply_to.text or reply_to.caption or "").strip()
                reply_to_text = f"[{rn_name}] {reply_to_text}"
            await self.store.add_message(
                ts=message.date.timestamp() if message.date else time.time(),
                chat_id=chat.id,
                chat_type=chat_type,
                chat_title=chat.title or chat.first_name or chat.username or "",
                message_id=message.id,
                from_id=from_id,
                from_name=from_name,
                is_out=1 if is_out else 0,
                text=text,
                has_media=1 if has_media else 0,
                media_kind=media_kind,
                caption=caption,
                reply_to_message_id=reply_to_message_id,
                reply_to_text=reply_to_text,
                raw={"edit": is_edit},
            )
        except Exception as e:
            log.exception("Capture error: %s", e)

    # -------------------------------------------------------------- reply
    async def _maybe_reply(self, client: Client, message: Message) -> None:
        if self.paused or not self.auto_reply_enabled:
            return
        if not message.from_user:
            return
        chat = message.chat
        chat_type = (chat.type.value if isinstance(chat.type, ChatType)
                     else str(chat.type))
        is_group = chat_type in ("group", "supergroup")
        is_private = chat_type == "private"

        # Determinar autorización
        authorized = False
        if is_private and self.auth.is_user_authorized(message.from_user.id):
            authorized = True
        elif is_group and self.auth.is_group_authorized(chat.id):
            authorized = True
        if not authorized:
            return

        # Es reply a mí o mention?
        is_reply_to_me = False
        if message.reply_to_message and message.reply_to_message.from_user:
            is_reply_to_me = message.reply_to_message.from_user.id == self.me.id
        is_mention = self._is_mention_of_me(message)

        text = (message.text or message.caption or "").strip()
        # Capturar foto si la hay (para visión)
        photo_bytes = None
        photo_mime = None
        if message.photo and self.cfg.get("ai", {}).get("vision_enabled", True):
            try:
                # Tamaño 'small' es suficiente para describir
                photo = message.photo
                # Elegir el tamaño más grande disponible (último en la lista sizes)
                target = photo.thumbs[-1] if getattr(photo, "thumbs", None) else photo
                buf = await client.download_media(message, in_memory=True,
                                                   file_size_limit=2_000_000)
                if buf is not None:
                    photo_bytes = bytes(buf.getbuffer())
                    photo_mime = "image/jpeg"
            except Exception as e:
                log.warning("Could not download photo: %s", e)

        ctx = ReplyContext(
            chat_id=chat.id,
            chat_type=chat_type,
            chat_title=chat.title or chat.first_name or chat.username or "",
            incoming_text=text,
            incoming_from_id=message.from_user.id,
            incoming_from_name=(message.from_user.first_name or "") +
                               (message.from_user.last_name or ""),
            is_reply_to_me=is_reply_to_me,
            is_mention=is_mention,
            has_photo=bool(photo_bytes),
            photo_bytes=photo_bytes,
            photo_mime=photo_mime,
            caption=(message.caption or "").strip(),
        )
        ok, reason = self.responder.should_reply(ctx, is_group=is_group,
                                                    is_private=is_private)
        if not ok:
            log.debug("Skip reply in chat %s: %s", chat.id, reason)
            return
        log.info("Generating reply in chat=%s reason=ok mention=%s reply_to_me=%s",
                 chat.id, is_mention, is_reply_to_me)
        try:
            reply_text = await self.responder.generate_reply(ctx)
        except Exception as e:
            log.exception("Reply generation error: %s", e)
            return
        if not reply_text:
            return
        try:
            await message.reply(reply_text, disable_notification=True)
            log.info("Replied in chat=%s: %r", chat.id, reply_text[:80])
        except Exception as e:
            log.error("Failed to send reply: %s", e)

    def _is_mention_of_me(self, message: Message) -> bool:
        text = (message.text or message.caption or "")
        if not text:
            return False
        me = self.me
        if me.username:
            if f"@{me.username.lower()}" in text.lower():
                return True
        # mención por nombre
        if me.first_name and me.first_name.lower() in text.lower():
            return True
        # entities mention
        for ent in (message.entities or []):
            if ent.type and ent.type.value == "mention":
                mentioned = text[ent.offset: ent.offset + ent.length]
                if me.username and mentioned.lstrip("@").lower() == me.username.lower():
                    return True
        return False

    # -------------------------------------------------------------- admin
    async def _handle_admin_command(self, client: Client, message: Message) -> None:
        if not message.from_user or not self.auth.is_owner(message.from_user.id):
            return
        cmd = (message.text or "").split()[0].lstrip("/").lower()
        args = (message.text or "").split(maxsplit=1)[1] if " " in (message.text or "") else ""

        async def reply(text: str):
            try:
                await message.reply(text, quote=True)
            except Exception:
                pass

        if cmd in ("soul_help",):
            await reply(_HELP_TEXT)
        elif cmd in ("soul_status",):
            await reply(await self._format_status())
        elif cmd in ("soul_now", "soul_refresh"):
            await reply("🔄 Refrescando Soul.md ahora… (puede tardar 20-40s)")
            result = await self.soul.refresh_if_due(force=True)
            if result.ok:
                lines = [
                    "✅ Soul.md actualizado.",
                    f"📥 Mensajes analizados en este ciclo: {result.messages_analyzed}",
                ]
                if result.chat_types:
                    parts = [f"{k}={v}" for k, v in result.chat_types.items()]
                    lines.append(f"📊 Por tipo de chat: {', '.join(parts)}")
                if result.sample_first_ts and result.sample_last_ts:
                    lines.append(
                        "📅 Muestra: " +
                        time.strftime("%Y-%m-%d", time.gmtime(result.sample_first_ts)) +
                        " → " +
                        time.strftime("%Y-%m-%d", time.gmtime(result.sample_last_ts))
                    )
                if result.learning_summary:
                    lines.append("")
                    lines.append("🧠 Aprendido este ciclo:")
                    for ln in result.learning_summary.splitlines():
                        ln = ln.strip().lstrip("-•* ")
                        if ln:
                            lines.append(f"  • {ln}")
                await reply("\n".join(lines))
            else:
                await reply(f"⚠️ No se pudo refrescar: {result.error}")
        elif cmd == "soul_pause":
            self.paused = True
            await reply("⏸️ Respuestas automáticas pausadas.")
        elif cmd == "soul_resume":
            self.paused = False
            await reply("▶️ Respuestas automáticas reanudadas.")
        elif cmd == "soul_auth_chat":
            chat = message.chat
            ctype = (chat.type.value if isinstance(chat.type, ChatType) else "")
            if ctype in ("group", "supergroup"):
                added = self.auth.authorize_group(chat.id)
                await reply(
                    f"✅ Grupo '{chat.title}' (id={chat.id}) autorizado." if added
                    else f"ℹ️ El grupo '{chat.title}' (id={chat.id}) ya estaba autorizado."
                )
            elif ctype == "private":
                target = message.chat.id
                added = self.auth.authorize_user(target)
                await reply(
                    f"✅ Usuario {target} autorizado para auto-respuesta en privado." if added
                    else f"ℹ️ El usuario {target} ya estaba autorizado."
                )
            else:
                await reply("⚠️ Este comando solo aplica en grupos o privados.")
        elif cmd == "soul_unauth_chat":
            chat = message.chat
            ctype = (chat.type.value if isinstance(chat.type, ChatType) else "")
            if ctype in ("group", "supergroup"):
                removed = self.auth.revoke_group(chat.id)
                await reply(
                    f"✅ Grupo '{chat.title}' (id={chat.id}) desautorizado." if removed
                    else f"ℹ️ El grupo '{chat.title}' no estaba autorizado."
                )
            elif ctype == "private":
                target = message.chat.id
                removed = self.auth.revoke_user(target)
                await reply(
                    f"✅ Usuario {target} desautorizado." if removed
                    else f"ℹ️ El usuario {target} no estaba autorizado."
                )
        elif cmd == "soul_auth_user":
            target = None
            if args.strip().isdigit():
                target = int(args.strip())
            elif message.reply_to_message and message.reply_to_message.from_user:
                target = message.reply_to_message.from_user.id
            if not target:
                await reply("Uso: /soul_auth_user <user_id> o responde a un mensaje "
                            "del usuario con este comando.")
                return
            added = self.auth.authorize_user(target)
            await reply(
                f"✅ Usuario {target} autorizado." if added
                else f"ℹ️ El usuario {target} ya estaba autorizado."
            )
        elif cmd == "soul_unauth_user":
            target = None
            if args.strip().isdigit():
                target = int(args.strip())
            elif message.reply_to_message and message.reply_to_message.from_user:
                target = message.reply_to_message.from_user.id
            if not target:
                await reply("Uso: /soul_unauth_user <user_id> o responde a un mensaje "
                            "del usuario con este comando.")
                return
            removed = self.auth.revoke_user(target)
            await reply(
                f"✅ Usuario {target} desautorizado." if removed
                else f"ℹ️ El usuario {target} no estaba autorizado."
            )
        elif cmd == "soul_set_mode":
            mode = args.strip().lower()
            if mode not in ("mention", "always"):
                await reply("Uso: /soul_set_mode mention|always")
                return
            self.responder.set_group_mode(mode)
            self.cfg["responder"]["group_reply_mode"] = mode
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2, ensure_ascii=False)
            await reply(f"✅ Modo de respuesta en grupos: {mode}")
        elif cmd == "soul_show":
            soul = self.soul.get_soul() or "(Soul.md aún no generado)"
            if len(soul) > 4000:
                soul = soul[:4000] + "\n\n... (truncado)"
            try:
                await message.reply(soul, quote=True)
            except Exception as e:
                log.error("Failed to send Soul.md: %s", e)
                await message.reply("⚠️ No se pudo enviar Soul.md (revisa el log).",
                                    quote=True)
        elif cmd == "soul_stats":
            await reply(await self._format_stats())
        elif cmd == "soul_learn":
            summary = self.soul.last_learning_summary()
            await reply(summary if summary else
                        "⚠️ Aún no hay learning summary. Ejecuta /soul_now primero.")
        elif cmd in ("soul_scan", "soul_scan_groups", "soul_scan_private"):
            await self._handle_scan(client, message, cmd, args)
        elif cmd == "soul_exclude":
            await self._handle_exclude(message, args, add=True)
        elif cmd == "soul_unexclude":
            await self._handle_exclude(message, args, add=False)
        elif cmd == "soul_excluded":
            await self._handle_list_excluded(message)
        elif cmd == "soul_delete":
            await self._handle_delete(message, args)
        elif cmd == "soul_delete_analyzed":
            await self._handle_delete_analyzed(message)
        elif cmd == "soul_delete_unanalyzed":
            await self._handle_delete_unanalyzed(message)

    # -------------------------------------------------------------- scan
    async def _handle_scan(self, client: Client, message: Message,
                             cmd: str, args: str) -> None:
        """Ejecuta un backfill con reporte de progreso en consola + Telegram.
        
        Uso:
          /soul_scan                  — escanea TODOS los chats
          /soul_scan 123456           — solo el chat con id 123456
          /soul_scan 123,456,789      — solo esos 3 chats específicos
          /soul_scan_groups           — solo grupos
          /soul_scan_private          — solo privados
        """
        # Determinar scope
        scan_cfg = dict(self.cfg.get("scan", {}))
        if cmd == "soul_scan_groups":
            scan_cfg["scan_private"] = False
            scan_cfg["scan_groups"] = True
            scan_cfg["scan_channels"] = False
        elif cmd == "soul_scan_private":
            scan_cfg["scan_private"] = True
            scan_cfg["scan_groups"] = False
            scan_cfg["scan_channels"] = False
        
        # Parsear IDs: separados por comas o espacios
        only_chat_ids: list[int] = []
        if cmd == "soul_scan" and args.strip():
            # Limpiar y separar por comas o espacios
            raw = args.strip().replace(" ", ",")
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            for part in parts:
                clean = part.lstrip("-")
                if clean.isdigit():
                    only_chat_ids.append(int(part))
        
        if only_chat_ids:
            log.info("Scanning specific chats: %s", only_chat_ids)
        
        # Reporter de Telegram
        sent_msg = None

        async def send_fn(text: str):
            nonlocal sent_msg
            try:
                sent_msg = await message.reply(text, quote=True)
                return sent_msg
            except Exception as e:
                log.warning("progress send error: %s", e)
                return None

        async def edit_fn(msg_id: str, text: str):
            try:
                if sent_msg:
                    await sent_msg.edit_text(text)
            except Exception as e:
                log.debug("progress edit error: %s", e)

        count_text = f"{len(only_chat_ids)} chats específicos" if only_chat_ids else "todos los chats"
        await message.reply(f"🔍 Iniciando backfill de {count_text}…", quote=True)
        runner = BackfillRunner(
            app=client, store=self.store, owner_id=self.me.id,
            scan_cfg=scan_cfg,
            ai_chat_send=send_fn,
            ai_chat_edit=edit_fn,
        )
        try:
            stats = await runner.run(only_chat_ids=only_chat_ids or None)
        except Exception as e:
            log.exception("Backfill failed: %s", e)
            await message.reply(f"❌ Backfill falló: {e}", quote=True)
            return
        # Mensaje final detallado en Telegram
        report = "\n".join([
            "✅ **Backfill completado**",
            *stats.summary_lines(),
        ])
        try:
            await message.reply(report, quote=True)
        except Exception:
            pass
        # Tras backfill, sugerir refresh del Soul.md
        my_count = await self.store.count_my_messages()
        unan = await self.store.count_unanalyzed()
        await message.reply(
            f"📈 Tienes {my_count} mensajes míos capturados ({unan} sin analizar).\n"
            f"Ejecuta /soul_now para que la IA regenere el Soul.md con estos datos.",
            quote=True
        )
        # Actualizar memoria contextual de los chats principales sin bloquear el
        # resultado del escaneo. Los demás chats se resumen bajo demanda.
        asyncio.create_task(self._refresh_contexts_after_scan())

    async def _refresh_contexts_after_scan(self) -> None:
        try:
            limit = int(self.cfg.get("scan", {}).get("context_refresh_limit", 50))
            updated = await self.soul.refresh_contexts_for_top_chats(limit=limit)
            log.info("Context memory refreshed after scan: %d chats", updated)
        except Exception as e:
            log.warning("Post-scan context refresh failed: %s", e)

    # -------------------------------------------------------------- exclusiones
    async def _handle_exclude(self, message: Message, args: str, add: bool) -> None:
        """Agrega o quita un chat de la lista de exclusiones."""
        if not args.strip():
            await message.reply(
                "Uso: /soul_exclude <chat_id> o /soul_unexclude <chat_id>",
                quote=True
            )
            return
        
        try:
            chat_id = int(args.strip())
        except ValueError:
            await message.reply("⚠️ ID inválido. Debe ser un número.", quote=True)
            return
        
        excluded = set(self.cfg.get("scan", {}).get("excluded_chat_ids", []))
        
        if add:
            if chat_id in excluded:
                await message.reply(f"ℹ️ El chat {chat_id} ya estaba excluido.", quote=True)
                return
            excluded.add(chat_id)
            action = "excluido"
        else:
            if chat_id not in excluded:
                await message.reply(f"ℹ️ El chat {chat_id} no estaba excluido.", quote=True)
                return
            excluded.discard(chat_id)
            action = "incluido"
        
        # Guardar en config
        if "scan" not in self.cfg:
            self.cfg["scan"] = {}
        self.cfg["scan"]["excluded_chat_ids"] = sorted(excluded)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, indent=2, ensure_ascii=False)
        
        # Actualizar runner si existe
        await message.reply(f"✅ Chat {chat_id} {action} del escaneo.", quote=True)

    async def _handle_list_excluded(self, message: Message) -> None:
        """Muestra la lista de chats excluidos."""
        excluded = self.cfg.get("scan", {}).get("excluded_chat_ids", [])
        if not excluded:
            await message.reply("📋 No hay chats excluidos del escaneo.", quote=True)
            return
        
        lines = ["📋 **Chats excluidos del escaneo:**\n"]
        for cid in excluded:
            lines.append(f"  • `{cid}`")
        lines.append(f"\nTotal: {len(excluded)} chats")
        lines.append("\nPara quitar: /soul_unexclude <chat_id>")
        await message.reply("\n".join(lines), quote=True)

    # -------------------------------------------------------------- delete
    async def _handle_delete(self, message: Message, args: str) -> None:
        """Elimina mensajes del dueño de la DB. No toca Soul.md."""
        arg = args.strip().lower()

        # Si tiene un chat_id numérico, borrar solo ese chat
        clean = args.strip().lstrip("-")
        if clean.isdigit():
            chat_id = int(args.strip())
            count_before = await self.store.count_owner_messages()
            deleted = await self.store.delete_owner_chat(chat_id)
            remaining = count_before - deleted
            if deleted == 0:
                await message.reply(
                    f"ℹ️ No se encontraron mensajes tuyos en el chat `{chat_id}`.",
                    quote=True
                )
            else:
                await message.reply(
                    f"🗑️ Eliminados **{deleted}** mensajes tuyos del chat `{chat_id}`.\n"
                    f"📈 Total restante en DB: {remaining} mensajes.\n"
                    f"ℹ️ Soul.md no fue modificado.",
                    quote=True
                )
            return

        # /soul_delete --confirm → borrar TODO
        if arg == "--confirm":
            total = await self.store.count_owner_messages()
            if total == 0:
                await message.reply("ℹ️ No hay mensajes tuyos en la base de datos.", quote=True)
                return
            deleted = await self.store.delete_owner_messages()
            await message.reply(
                f"🗑️ Eliminados **{deleted}** mensajes tuyos de la base de datos.\n"
                f"ℹ️ Soul.md no fue modificado.",
                quote=True
            )
        else:
            total = await self.store.count_owner_messages()
            await message.reply(
                f"⚠️ Estás a punto de eliminar **{total}** mensajes tuyos de la DB.\n\n"
                f"**Opciones:**\n"
                f"  • `/soul_delete --confirm` — borrar todos tus mensajes\n"
                f"  • `/soul_delete <chat_id>` — borrar solo de un chat específico\n\n"
                f"ℹ️ Soul.md **no** será modificado.",
                quote=True
            )

    async def _handle_delete_analyzed(self, message: Message) -> None:
        """Elimina mensajes ya analizados (incorporados a Soul.md)."""
        count = await self.store.count_analyzed()
        if count == 0:
            await message.reply(
                "ℹ️ No hay mensajes analizados para eliminar.", quote=True
            )
            return
        deleted = await self.store.delete_owner_by_analysis(analyzed=True)
        await message.reply(
            f"🗑️ Eliminados **{deleted}** mensajes analizados de la DB.\n"
            f"ℹ️ Soul.md no fue modificado (el perfil generado se mantiene intacto).",
            quote=True
        )

    async def _handle_delete_unanalyzed(self, message: Message) -> None:
        """Elimina mensajes pendientes de análisis (aún no en Soul.md)."""
        unan = await self.store.count_unanalyzed()
        if unan == 0:
            await message.reply(
                "ℹ️ No hay mensajes sin analizar para eliminar.", quote=True
            )
            return
        deleted = await self.store.delete_owner_by_analysis(analyzed=False)
        await message.reply(
            f"🗑️ Eliminados **{deleted}** mensajes sin analizar de la DB.\n"
            f"ℹ️ Soul.md no fue modificado.",
            quote=True
        )

    # -------------------------------------------------------------- status
    async def _format_status(self) -> str:
        s = self.soul.stats()
        r = self.responder.stats()
        snap = self.auth.snapshot()
        try:
            my_count = await self.store.count_my_messages()
            metrics = await self.store.my_metrics()
        except Exception:
            my_count = -1
            metrics = {}
        lines = [
            f"🪪 Soul Agent — estado",
            f"Owner: {snap['owner_user_id']}",
            f"Soul.md: {'✅ generado' if s['soul_md_exists'] else '⏳ pendiente'} "
            f"({s['soul_md_size']} bytes)",
            f"Mensajes míos capturados: {my_count}",
        ]
        if metrics:
            lines.append(
                f"📊 Grupos donde he escrito: {metrics.get('in_groups', 0)} | "
                f"Privados: {metrics.get('in_private', 0)} | "
                f"Total chats: {metrics.get('chats_touched', 0)}"
            )
            if metrics.get("first_ts") and metrics.get("last_ts"):
                first = time.strftime("%Y-%m-%d", time.gmtime(metrics["first_ts"]))
                last = time.strftime("%Y-%m-%d", time.gmtime(metrics["last_ts"]))
                lines.append(f"📅 Periodo cubierto: {first} → {last}")
            lines.append(
                f"✏️ Longitud media: {metrics.get('avg_length', 0):.1f} chars | "
                f"Máx: {metrics.get('max_length', 0)} | Mín: {metrics.get('min_length', 0)}"
            )
            lines.append(f"🖼️ Mensajes con media: {metrics.get('with_media', 0)}")
        lines.append(
            f"Último refresh: "
            f"{time.ctime(s['last_refresh_at']) if s['last_refresh_at'] else 'nunca'}"
        )
        lines.append(f"Intervalo refresh: {s['refresh_interval_seconds']/60:.0f} min")
        lines.append(
            f"Grupos autorizados: {len(snap['authorized_group_ids'])} -> "
            f"{snap['authorized_group_ids']}"
        )
        lines.append(
            f"Usuarios autorizados (privado): {len(snap['authorized_user_ids'])} -> "
            f"{snap['authorized_user_ids']}"
        )
        lines.append(f"Modo de respuesta en grupo: {r['group_reply_mode']}")
        lines.append(
            f"Cooldown grupo: {r['group_cooldown']}s | privado: {r['private_cooldown']}s"
        )
        lines.append(
            f"Replies en últimos 60s: {r['replies_last_60s']}/{r['max_replies_per_minute']}"
        )
        lines.append(f"Pausado: {'sí' if self.paused else 'no'}")
        lines.append(
            f"Visión: {'✅' if getattr(self.ai, '_vision_supported', False) else '❌'} "
            f"(probe en {time.ctime(getattr(self.ai, '_vision_checked_at', 0)) or 'nunca'})"
        )
        lines.append(
            f"Learning summary: "
            f"{'✅ disponible (/soul_learn)' if s.get('has_learning_summary') else '❌ sin generar'}"
        )
        return "\n".join(lines)

    async def _format_stats(self) -> str:
        try:
            my_count = await self.store.count_my_messages()
            unan = await self.store.count_unanalyzed()
            metrics = await self.store.my_metrics()
            top = await self.store.top_chats(limit=10)
        except Exception as e:
            return f"⚠️ Error obteniendo stats: {e}"
        lines = [
            "📊 Stats",
            f"Mensajes del dueño capturados: {my_count}",
            f"Mensajes sin analizar para Soul.md: {unan}",
            f"Mínimos para Soul.md inicial: {self.soul.initial_min_messages}",
            f"Sample size para análisis: {self.soul.sample_size}",
        ]
        if metrics:
            lines.append("")
            lines.append("📈 Cobertura de tu historial:")
            lines.append(f"  • Total chats donde he escrito: {metrics.get('chats_touched', 0)}")
            lines.append(f"  • Grupos: {metrics.get('in_groups', 0)}")
            lines.append(f"  • Privados: {metrics.get('in_private', 0)}")
            if metrics.get("first_ts") and metrics.get("last_ts"):
                first = time.strftime("%Y-%m-%d", time.gmtime(metrics["first_ts"]))
                last = time.strftime("%Y-%m-%d", time.gmtime(metrics["last_ts"]))
                lines.append(f"  • Periodo: {first} → {last}")
            lines.append(
                f"  • Longitud media: {metrics.get('avg_length', 0):.1f} chars "
                f"(mín {metrics.get('min_length', 0)} / máx {metrics.get('max_length', 0)})"
            )
            lines.append(f"  • Mensajes con media: {metrics.get('with_media', 0)}")
        if top:
            lines.append("")
            lines.append("🔥 Top 10 chats con más mensajes míos:")
            for i, t in enumerate(top, 1):
                title = (t.get("chat_title") or "?")[:40]
                ctype = (t.get("chat_type") or "?")
                lines.append(f"  {i}. [{ctype}] {title} — {t.get('count', 0)} msgs")
        return "\n".join(lines)


_HELP_TEXT = """🪪 **Soul Agent** — comandos del dueño

📊 Estado y stats:
/soul_status — estado completo del agente
/soul_stats — estadísticas del almacén + cobertura + top chats
/soul_learn — muestra el último resumen de aprendizaje de la IA

🧠 Soul.md:
/soul_now — refrescar Soul.md ahora (forzado, muestra resumen aprendido)
/soul_show — mostrar el Soul.md actual
/soul_pause — pausar respuestas automáticas (sigue capturando)
/soul_resume — reanudar respuestas automáticas

🔍 Backfill de historial:
/soul_scan — escanea TODOS los chats (grupos+privados) y guarda tus mensajes antiguos
/soul_scan 123 — solo el chat con id 123
/soul_scan 123,456,789 — esos 3 chats específicos
/soul_scan_groups — solo grupos
/soul_scan_private — solo privados

🗑️ Eliminar mensajes de la DB:
/soul_delete — ver opciones para borrar tus mensajes de la DB
/soul_delete --confirm — borrar TODOS tus mensajes de la DB
/soul_delete <chat_id> — borrar tus mensajes de un chat específico
/soul_delete_analyzed — borrar mensajes ya incorporados a Soul.md
/soul_delete_unanalyzed — borrar mensajes pendientes de análisis
ℹ️ Soul.md NO es afectado por estas operaciones

🚫 Chats excluidos del escaneo:
/soul_exclude <id> — excluir un chat del escaneo (nunca se escaneará)
/soul_unexclude <id> — quitar chat de exclusiones
/soul_excluded — ver lista de chats excluidos

🔐 Autorización:
/soul_auth_chat — autorizar el chat actual (grupo) o al usuario respondido (privado)
/soul_unauth_chat — quitar autorización del chat actual
/soul_auth_user <id> — autorizar un usuario (o responde a su mensaje con el comando)
/soul_unauth_user <id> — desautorizar un usuario

⚙️ Configuración:
/soul_set_mode mention — responder solo si te @mencionan o responden a ti (default)
/soul_set_mode always — responder a una fracción de mensajes en grupos autorizados

/soul_help — esta ayuda
"""


# =====================================================================
#  Main
# =====================================================================
async def _amain() -> None:
    cfg = load_config()
    setup_logging(cfg)
    log.info("Config loaded from %s", CONFIG_PATH)
    # Sanity check de creds
    t = cfg["telegram"]
    if not t.get("api_id") or not t.get("api_hash") or t.get("api_hash").startswith("PON_AQUI"):
        log.error("Telegram api_id/api_hash no configurados en config.json. Edítalo y reinicia.")
        sys.exit(1)
    agent = SoulAgent(cfg)
    # Capturar Ctrl-C
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, agent._stop_event.set)
        except NotImplementedError:
            pass
    try:
        await agent.start()
        await agent._stop_event.wait()
    finally:
        await agent.stop()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
