"""
backfill.py
===========
Backfill del historial de Telegram: descarga mensajes propios desde la API
de Telegram (get_history) y los inserta en MessageStore, mostrando progreso.

- En consola: barra ASCII con \r.
- En Telegram: edita un único mensaje de progreso, muestra resumen final.

Comandos disponibles:
  /soul_scan            — backfill de TODOS los chats (grupos + privados) con
                          backfill_limit mensajes por chat. Usa config.scan
  /soul_scan_groups     — solo grupos
  /soul_scan_private    — solo privados
  /soul_scan <id_o_link> — solo el chat indicado

Por defecto respeta config.scan.backfill_limit (default 200) y evita re-
descargar mensajes ya en la BD (deduplica por chat_id+message_id).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from pyrogram import Client
from pyrogram.types import Chat, Message
from pyrogram.enums import ChatType

from message_store import MessageStore
from progress import ConsoleProgressBar, TelegramProgressReporter

log = logging.getLogger("soul.backfill")


@dataclass
class BackfillStats:
    chats_scanned: int = 0
    chats_with_my_messages: int = 0
    my_messages_found: int = 0
    already_in_store: int = 0
    new_inserted: int = 0
    failed_chats: int = 0
    by_chat_type: dict = field(default_factory=dict)
    chat_titles: list = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        lines = [
            f"📊 Chats escaneados: {self.chats_scanned}",
            f"💬 Chats con mensajes míos: {self.chats_with_my_messages}",
            f"📥 Mensajes míos encontrados: {self.my_messages_found}",
            f"🆕 Nuevos insertados: {self.new_inserted}",
            f"♻️ Ya estaban en BD: {self.already_in_store}",
            f"⚠️ Chats con error: {self.failed_chats}",
        ]
        if self.by_chat_type:
            parts = [f"{k}={v}" for k, v in self.by_chat_type.items()]
            lines.append(f"📂 Por tipo: {', '.join(parts)}")
        if self.chat_titles:
            sample = self.chat_titles[:5]
            lines.append("🔥 Top chats con mis mensajes:")
            for t in sample:
                lines.append(f"   • {t}")
            if len(self.chat_titles) > 5:
                lines.append(f"   … y {len(self.chat_titles) - 5} más")
        return lines


class BackfillRunner:
    """Recorre el historial de chats y descarga mensajes propios."""

    def __init__(self, app: Client, store: MessageStore, owner_id: int,
                 scan_cfg: dict, ai_chat_send: Callable | None = None,
                 ai_chat_edit: Callable | None = None):
        self.app = app
        self.store = store
        self.owner_id = owner_id
        self.backfill_limit = int(scan_cfg.get("backfill_limit_per_chat", 200))
        self.scan_groups = bool(scan_cfg.get("scan_groups", True))
        self.scan_private = bool(scan_cfg.get("scan_private", True))
        self.scan_channels = bool(scan_cfg.get("scan_channels", False))
        self.skip_empty_chats = bool(scan_cfg.get("skip_empty_chats", True))
        self.report_to_telegram = bool(scan_cfg.get("report_progress_to_telegram",
                                                     True))
        self.excluded_chat_ids: set[int] = set(
            int(x) for x in scan_cfg.get("excluded_chat_ids", [])
        )
        self.ai_chat_send = ai_chat_send
        self.ai_chat_edit = ai_chat_edit

    # -------------------------------------------------------------- API
    async def _list_dialogs(self) -> list[Chat]:
        """Lista todos los chats del usuario."""
        chats: list[Chat] = []
        async for dialog in self.app.get_dialogs():
            chats.append(dialog.chat)
        return chats

    async def _backfill_one_chat(self, chat: Chat, bar: ConsoleProgressBar,
                                   stats: BackfillStats) -> None:
        ctype = (chat.type.value if isinstance(chat.type, ChatType)
                 else str(chat.type))
        # Filtro por tipo
        if ctype == "private" and not self.scan_private:
            return
        if ctype in ("group", "supergroup") and not self.scan_groups:
            return
        if ctype == "channel" and not self.scan_channels:
            return
        chat_title = chat.title or chat.first_name or chat.username or "SinNombre"
        try:
            my_count_in_chat = 0
            already = 0
            inserted = 0
            async for message in self.app.get_chat_history(
                    chat.id, limit=self.backfill_limit):
                bar.update(1, note=f"{chat_title[:30]}")
                if not message.from_user or message.from_user.id != self.owner_id:
                    continue
                # Deduplicar por (chat_id, message_id)
                if await self.store.message_exists(chat.id, message.id):
                    already += 1
                    continue
                await self._store_message(chat, message)
                inserted += 1
                my_count_in_chat += 1
            stats.my_messages_found += my_count_in_chat + already
            stats.already_in_store += already
            stats.new_inserted += inserted
            if my_count_in_chat + already > 0:
                stats.chats_with_my_messages += 1
                stats.by_chat_type[ctype] = stats.by_chat_type.get(ctype, 0) + 1
                stats.chat_titles.append(
                    f"{chat_title} ({my_count_in_chat+already} míos)"
                )
            stats.chats_scanned += 1
        except Exception as e:
            log.warning("Backfill failed in chat %s: %s", chat.id, e)
            stats.failed_chats += 1

    async def _store_message(self, chat: Chat, message: Message) -> None:
        ctype = (chat.type.value if isinstance(chat.type, ChatType)
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
        await self.store.add_message(
            ts=message.date.timestamp() if message.date else time.time(),
            chat_id=chat.id,
            chat_type=ctype,
            chat_title=chat.title or chat.first_name or chat.username or "",
            message_id=message.id,
            from_id=self.owner_id,
            from_name="yo",
            is_out=1,
            text=text,
            has_media=1 if has_media else 0,
            media_kind=media_kind,
            caption=(message.caption or "").strip() if message.caption else None,
            analyzed_for_soul=0,
        )

    # -------------------------------------------------------------- run
    async def run(self, *, only_chat_ids: list[int] | None = None) -> BackfillStats:
        stats = BackfillStats()
        log.info("Backfill starting; listing dialogs…")
        chats = await self._list_dialogs()
        if only_chat_ids:
            id_set = set(only_chat_ids)
            chats = [c for c in chats if c.id in id_set]
            log.info("Filtered to %d specific chats", len(chats))
        
        # Filtrar chats excluidos
        if self.excluded_chat_ids:
            before = len(chats)
            chats = [c for c in chats if c.id not in self.excluded_chat_ids]
            excluded_count = before - len(chats)
            if excluded_count > 0:
                log.info("Excluded %d chats from scan (IDs: %s)",
                         excluded_count, self.excluded_chat_ids)
        
        # Estimar total de mensajes a recorrer para la barra (aprox)
        total_approx = len(chats) * self.backfill_limit
        bar = ConsoleProgressBar(total=total_approx,
                                  label="backfill", width=30)
        # Reporter de Telegram
        reporter: TelegramProgressReporter | None = None
        if self.report_to_telegram and self.ai_chat_send:
            reporter = TelegramProgressReporter(
                send_fn=self.ai_chat_send,
                edit_fn=self.ai_chat_edit,
                total_steps=max(1, len(chats) + 1),
                throttle_seconds=2.0,
            )
            await reporter.start(
                f"🔍 Escaneando {len(chats)} chats (limit={self.backfill_limit})…"
            )
        try:
            step_idx = 0
            for chat in chats:
                step_idx += 1
                ctype = (chat.type.value if isinstance(chat.type, ChatType)
                         else str(chat.type))
                if ctype == "private" and not self.scan_private:
                    continue
                if ctype in ("group", "supergroup") and not self.scan_groups:
                    continue
                if ctype == "channel" and not self.scan_channels:
                    continue
                if reporter:
                    title = chat.title or chat.first_name or chat.username or "?"
                    await reporter.step(
                        f"📂 {title[:35]}",
                        detail=f"{step_idx}/{len(chats)}",
                        chats_scanned=stats.chats_scanned,
                        messages_found=stats.my_messages_found,
                        new_inserted=stats.new_inserted,
                    )
                await self._backfill_one_chat(chat, bar, stats)
        finally:
            bar.finish(note=f"chats={stats.chats_scanned}")
        if reporter:
            await reporter.finish(
                "✅ Backfill completado",
                summary_lines=stats.summary_lines(),
            )
        log.info("Backfill done: %s", stats)
        return stats
