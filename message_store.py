"""
message_store.py
================
Almacenamiento SQLite asincrónico para el corpus del agente.

Guarda TODOS los mensajes salientes del dueño (para construir Soul.md) y los
mensajes entrantes de chats autorizados (para contexto de respuesta).

Esquema:
  messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,                      -- epoch seconds
    chat_id INTEGER NOT NULL,
    chat_type TEXT,                           -- 'private' | 'group' | 'supergroup' | 'channel'
    chat_title TEXT,
    message_id INTEGER NOT NULL,
    from_id INTEGER NOT NULL,                  -- 0 si desconocido
    from_name TEXT,
    is_out INTEGER NOT NULL,                   -- 1 si fue enviado por el dueño
    text TEXT,
    has_media INTEGER NOT NULL,
    media_kind TEXT,                           -- 'photo' | 'video' | 'voice' | 'sticker' | ...
    caption TEXT,
    reply_to_message_id INTEGER,
    reply_to_text TEXT,
    analyzed_for_soul INTEGER DEFAULT 0,      -- 1 si ya fue incorporado a Soul.md
    raw TEXT                                   -- JSON de metadatos extra
  )
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("soul.store")


class MessageStore:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init_db(self) -> None:
        with self._connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                chat_type TEXT,
                chat_title TEXT,
                message_id INTEGER NOT NULL,
                from_id INTEGER NOT NULL,
                from_name TEXT,
                is_out INTEGER NOT NULL,
                text TEXT,
                has_media INTEGER NOT NULL DEFAULT 0,
                media_kind TEXT,
                caption TEXT,
                reply_to_message_id INTEGER,
                reply_to_text TEXT,
                analyzed_for_soul INTEGER DEFAULT 0,
                raw TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_chat_ts ON messages(chat_id, ts);
            CREATE INDEX IF NOT EXISTS idx_isout_ts ON messages(is_out, ts);
            CREATE INDEX IF NOT EXISTS idx_analyzed ON messages(analyzed_for_soul);
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            -- Memoria contextual por chat: qué se habla, con quién, qué temas.
            -- Permite al agente responder con conocimiento del contexto del chat
            -- actual, no solo de la personalidad global del dueño.
            CREATE TABLE IF NOT EXISTS chat_context (
                chat_id INTEGER PRIMARY KEY,
                chat_type TEXT,
                chat_title TEXT,
                participants TEXT,           -- JSON: lista de nombres/ids frecuentes
                topics TEXT,                 -- JSON: lista corta de temas recurrentes
                keywords TEXT,               -- JSON: palabras clave más usadas
                summary TEXT,                -- resumen en prosa (es) generado por IA
                my_role TEXT,                -- cómo actúa el dueño en ese chat (ej: soporte, broma, ventas)
                tone TEXT,                   -- tono dominante del chat (formal/informal/técnico/etc.)
                messages_total INTEGER DEFAULT 0,    -- total de mensajes capturados del chat
                my_messages_total INTEGER DEFAULT 0, -- mensajes del dueño en este chat
                first_ts INTEGER,
                last_ts INTEGER,
                summary_at INTEGER,          -- epoch del último resumen generado
                summary_model TEXT,          -- modelo usado
                summary_version INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_ctx_last ON chat_context(last_ts);
            CREATE INDEX IF NOT EXISTS idx_ctx_summary_at ON chat_context(summary_at);
            """)
            c.commit()

    # -------------------------------------------------------------- meta
    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._connect() as c:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if r is None:
                return default
            try:
                return json.loads(r[0])
            except Exception:
                return r[0]

    def set_meta(self, key: str, value: Any) -> None:
        v = json.dumps(value) if not isinstance(value, str) else value
        with self._connect() as c:
            c.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, v))
            c.commit()

    # -------------------------------------------------------------- write
    async def add_message(self, **fields) -> int:
        cols = (
            "ts", "chat_id", "chat_type", "chat_title", "message_id",
            "from_id", "from_name", "is_out", "text", "has_media",
            "media_kind", "caption", "reply_to_message_id", "reply_to_text",
            "analyzed_for_soul", "raw",
        )
        ts = int(fields.get("ts") or time.time())
        fields["ts"] = ts
        fields.setdefault("has_media", 0)
        fields.setdefault("analyzed_for_soul", 0)
        if "raw" in fields and not isinstance(fields["raw"], (str, type(None))):
            fields["raw"] = json.dumps(fields["raw"], ensure_ascii=False)
        values = [fields.get(c) for c in cols]
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT INTO messages({','.join(cols)}) VALUES({placeholders})"
        async with self._lock:
            loop = asyncio.get_running_loop()
            cur = await loop.run_in_executor(None, self._exec_write, sql, values)
            log.debug("Stored msg id=%s chat=%s is_out=%s text=%r",
                      cur.lastrowid, fields.get("chat_id"),
                      fields.get("is_out"), (fields.get("text") or "")[:50])
            return cur.lastrowid

    def _exec_write(self, sql, params):
        with self._connect() as c:
            cur = c.execute(sql, params)
            c.commit()
            return cur

    # -------------------------------------------------------------- read
    async def fetch_recent(self, chat_id: int, limit: int = 25) -> list[dict]:
        sql = ("SELECT ts, chat_id, chat_type, chat_title, message_id, from_id, "
               "from_name, is_out, text, has_media, media_kind, caption, "
               "reply_to_message_id, reply_to_text "
               "FROM messages WHERE chat_id=? ORDER BY ts DESC LIMIT ?")
        async with self._lock:
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, self._exec_read, sql, (chat_id, limit))
        rows = list(reversed(rows))  # cronológico asc
        return [dict(zip([d[0] for d in _CURSOR_DESC], r)) for r in rows]

    async def fetch_my_messages(self, limit: int = 2000,
                                only_unanalyzed: bool = False) -> list[dict]:
        where = "WHERE is_out=1"
        if only_unanalyzed:
            where += " AND analyzed_for_soul=0"
        sql = (f"SELECT ts, chat_id, chat_type, chat_title, message_id, from_id, "
               f"from_name, is_out, text, has_media, media_kind, caption, "
               f"reply_to_message_id, reply_to_text FROM messages {where} "
               f"ORDER BY ts DESC LIMIT ?")
        async with self._lock:
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, self._exec_read, sql, (limit,))
        return [dict(zip([d[0] for d in _CURSOR_DESC], r)) for r in rows]

    async def count_my_messages(self) -> int:
        sql = "SELECT COUNT(*) FROM messages WHERE is_out=1"
        async with self._lock:
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, self._exec_read, sql, ())
        return rows[0][0] if rows else 0

    async def count_unanalyzed(self) -> int:
        sql = "SELECT COUNT(*) FROM messages WHERE is_out=1 AND analyzed_for_soul=0"
        async with self._lock:
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, self._exec_read, sql, ())
        return rows[0][0] if rows else 0

    async def my_metrics(self) -> dict:
        """Métricas agregadas de los mensajes del dueño."""
        sql = """
        SELECT
            COUNT(*) AS total,
            COUNT(DISTINCT chat_id) AS chats,
            SUM(CASE WHEN chat_type='group' OR chat_type='supergroup' THEN 1 ELSE 0 END) AS in_groups,
            SUM(CASE WHEN chat_type='private' THEN 1 ELSE 0 END) AS in_private,
            SUM(CASE WHEN has_media=1 THEN 1 ELSE 0 END) AS with_media,
            MIN(ts) AS first_ts,
            MAX(ts) AS last_ts,
            AVG(LENGTH(COALESCE(text, caption))) AS avg_len,
            MAX(LENGTH(COALESCE(text, caption))) AS max_len,
            MIN(LENGTH(COALESCE(text, caption))) AS min_len
        FROM messages WHERE is_out=1 AND (
            (text IS NOT NULL AND text != '') OR
            (caption IS NOT NULL AND caption != '')
        )
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, self._exec_read, sql, ())
        if not rows or not rows[0]:
            return {}
        r = rows[0]
        return {
            "total_messages": int(r[0] or 0),
            "chats_touched": int(r[1] or 0),
            "in_groups": int(r[2] or 0),
            "in_private": int(r[3] or 0),
            "with_media": int(r[4] or 0),
            "first_ts": r[5],
            "last_ts": r[6],
            "avg_length": float(r[7] or 0),
            "max_length": int(r[8] or 0),
            "min_length": int(r[9] or 0),
        }

    async def top_chats(self, limit: int = 10) -> list[dict]:
        """Chats donde más mensajes ha enviado el dueño."""
        sql = """
        SELECT chat_id, chat_type, chat_title, COUNT(*) AS n
        FROM messages WHERE is_out=1
        GROUP BY chat_id, chat_type, chat_title
        ORDER BY n DESC LIMIT ?
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, self._exec_read, sql, (limit,))
        return [{"chat_id": r[0], "chat_type": r[1], "chat_title": r[2], "count": r[3]}
                for r in rows]

    async def my_messages_for_soul(self, limit: int = 200,
                                    only_unanalyzed: bool = False) -> list[dict]:
        """Atajo para que SoulManager no duplique la SQL."""
        return await self.fetch_my_messages(limit=limit,
                                             only_unanalyzed=only_unanalyzed)

    async def mark_analyzed(self, message_ids: Iterable[int]) -> None:
        ids = list(message_ids)
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        sql = f"UPDATE messages SET analyzed_for_soul=1 WHERE message_id IN ({placeholders}) " \
              f"AND is_out=1"
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._exec_write, sql, ids)

    # -------------------------------------------------------------- delete
    async def count_owner_messages(self) -> int:
        sql = "SELECT COUNT(*) FROM messages WHERE is_out=1"
        async with self._lock:
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, self._exec_read, sql, ())
        return rows[0][0] if rows else 0

    async def count_analyzed(self) -> int:
        sql = "SELECT COUNT(*) FROM messages WHERE is_out=1 AND analyzed_for_soul=1"
        async with self._lock:
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, self._exec_read, sql, ())
        return rows[0][0] if rows else 0

    async def delete_owner_messages(self) -> int:
        """Elimina TODOS los mensajes del dueño. Retorna cantidad eliminada."""
        sql = "DELETE FROM messages WHERE is_out=1"
        async with self._lock:
            loop = asyncio.get_running_loop()
            cur = await loop.run_in_executor(None, self._exec_write, sql, [])
            return cur.rowcount

    async def delete_owner_chat(self, chat_id: int) -> int:
        """Elimina mensajes del dueño en un chat específico."""
        sql = "DELETE FROM messages WHERE is_out=1 AND chat_id=?"
        async with self._lock:
            loop = asyncio.get_running_loop()
            cur = await loop.run_in_executor(None, self._exec_write, sql, [chat_id])
            return cur.rowcount

    async def delete_owner_by_analysis(self, analyzed: bool) -> int:
        """Elimina mensajes del dueño filtrados por estado de análisis."""
        val = 1 if analyzed else 0
        sql = "DELETE FROM messages WHERE is_out=1 AND analyzed_for_soul=?"
        async with self._lock:
            loop = asyncio.get_running_loop()
            cur = await loop.run_in_executor(None, self._exec_write, sql, [val])
            return cur.rowcount

    def _exec_read(self, sql, params):
        with self._connect() as c:
            cur = c.execute(sql, params)
            global _CURSOR_DESC
            _CURSOR_DESC = cur.description
            return cur.fetchall()

    # -------------------------------------------------------------- chat_context
    async def upsert_chat_context(self, chat_id: int, **fields) -> None:
        """
        Inserta o actualiza la fila de memoria contextual de un chat.
        Acepta cualquier subconjunto de columnas. Hace UPSERT por chat_id.
        """
        if not fields:
            return
        # Sanitizar: serializar listas/dicts a JSON
        json_cols = ("participants", "topics", "keywords")
        for c in json_cols:
            if c in fields and not isinstance(fields[c], str):
                fields[c] = json.dumps(fields[c], ensure_ascii=False)
        cols = list(fields.keys())
        placeholders = ",".join("?" * len(cols))
        # UPDATE clause: solo columnas que no sean chat_id
        update_cols = [c for c in cols if c != "chat_id"]
        update_clause = ",".join(f"{c}=excluded.{c}" for c in update_cols)
        sql = (f"INSERT INTO chat_context(chat_id, {','.join(cols)}) "
               f"VALUES(?, {placeholders}) "
               f"ON CONFLICT(chat_id) DO UPDATE SET {update_clause}")
        params = [int(chat_id)] + [fields[c] for c in cols]
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._exec_write, sql, params)

    async def get_chat_context(self, chat_id: int) -> dict | None:
        """Lee la memoria contextual de un chat. Devuelve None si no existe."""
        sql = ("SELECT chat_id, chat_type, chat_title, participants, topics, "
               "keywords, summary, my_role, tone, messages_total, "
               "my_messages_total, first_ts, last_ts, summary_at, "
               "summary_model, summary_version "
               "FROM chat_context WHERE chat_id=?")
        async with self._lock:
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, self._exec_read, sql, (chat_id,))
        if not rows:
            return None
        row = rows[0]
        desc = [d[0] for d in _CURSOR_DESC]
        out = dict(zip(desc, row))
        # Deserializar JSON donde corresponda
        for jc in ("participants", "topics", "keywords"):
            if out.get(jc):
                try:
                    out[jc] = json.loads(out[jc])
                except Exception:
                    pass
        return out

    async def get_all_chat_contexts(self, min_messages: int = 0) -> list[dict]:
        """Lista todas las memorias contextuales (para Soul.md / status)."""
        sql = ("SELECT chat_id, chat_type, chat_title, summary, my_role, tone, "
               "topics, keywords, my_messages_total, summary_at "
               "FROM chat_context WHERE my_messages_total >= ? "
               "ORDER BY last_ts DESC")
        async with self._lock:
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, self._exec_read, sql, (min_messages,))
        if not rows:
            return []
        desc = [d[0] for d in _CURSOR_DESC]
        out = []
        for r in rows:
            d = dict(zip(desc, r))
            for jc in ("topics", "keywords"):
                if d.get(jc):
                    try:
                        d[jc] = json.loads(d[jc])
                    except Exception:
                        pass
            out.append(d)
        return out

    async def count_chats_with_context(self) -> int:
        sql = "SELECT COUNT(*) FROM chat_context WHERE summary IS NOT NULL AND summary != ''"
        async with self._lock:
            loop = asyncio.get_running_loop()
            rows = await loop.run_in_executor(None, self._exec_read, sql, ())
        return rows[0][0] if rows else 0

    async def delete_chat_context(self, chat_id: int) -> bool:
        sql = "DELETE FROM chat_context WHERE chat_id=?"
        async with self._lock:
            loop = asyncio.get_running_loop()
            cur = await loop.run_in_executor(None, self._exec_write, sql, [chat_id])
            return cur.rowcount > 0


_CURSOR_DESC: list = []
