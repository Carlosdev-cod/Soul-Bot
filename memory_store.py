"""
memory_store.py
===============
Memoria persistente del agente (SQLite).

Guarda "recuerdos" que la IA crea mediante la tool `save_memory` y consulta
con `recall_memories`. A diferencia de Soul.md (que describe CÓMO escribe el
dueño), la memoria guarda DATOS útiles que el agente debe recordar entre
conversaciones: gustos de terceros, pendientes, contexto de chats, etc.

Esquema:
  memories(
    key TEXT PRIMARY KEY,          -- identificador corto, ej: "gusto_ana_cafe"
    content TEXT NOT NULL,         -- el recuerdo en sí
    created_at INTEGER,
    updated_at INTEGER,
    times_recalled INTEGER DEFAULT 0
  )

La búsqueda usa LIKE con escape de comodines y prioriza:
  1. coincidencia exacta de key
  2. key que CONTIENE el query
  3. contenido que contiene el query
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("soul.memory")


class MemoryStore:
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
            CREATE TABLE IF NOT EXISTS memories (
                key TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                times_recalled INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_mem_updated ON memories(updated_at);
            """)
            c.commit()

    # -------------------------------------------------------------- helpers
    def _exec_read(self, sql, params):
        with self._connect() as c:
            cur = c.execute(sql, params)
            return cur.description, cur.fetchall()

    def _exec_write(self, sql, params):
        with self._connect() as c:
            cur = c.execute(sql, params)
            c.commit()
            return cur

    @staticmethod
    def _row_to_dict(desc, row) -> dict:
        d = dict(zip([x[0] for x in desc], row))
        return d

    # -------------------------------------------------------------- API
    async def save(self, key: str, content: str) -> bool:
        """Crea o actualiza un recuerdo por key. Devuelve True si es nuevo."""
        key = (key or "").strip()
        content = (content or "").strip()
        if not key or not content:
            return False
        now = int(time.time())
        async with self._lock:
            loop = asyncio.get_running_loop()
            cur = await loop.run_in_executor(
                None, self._exec_write,
                """INSERT INTO memories(key, content, created_at, updated_at,
                                         times_recalled)
                   VALUES(?,?,?, ?,0)
                   ON CONFLICT(key) DO UPDATE SET
                       content=excluded.content,
                       updated_at=excluded.updated_at""",
                (key[:120], content[:4000], now, now))
            return cur.lastrowid is not None

    async def get(self, key: str) -> dict | None:
        sql = ("SELECT key, content, created_at, updated_at, times_recalled "
               "FROM memories WHERE key=?")
        async with self._lock:
            loop = asyncio.get_running_loop()
            desc, rows = await loop.run_in_executor(None, self._exec_read,
                                                    sql, (key.strip(),))
        return self._row_to_dict(desc, rows[0]) if rows else None

    async def recall(self, query: str, limit: int = 5) -> list[dict]:
        """Busca recuerdos por key o contenido (LIKE escapado)."""
        query = (query or "").strip()
        if not query:
            return await self.list_recent(limit=limit)
        esc = (query.replace("\\", "\\\\").replace("%", "\\%")
               .replace("_", "\\_"))
        pattern = f"%{esc}%"
        # Prioridad: key exacta > key contiene > contenido contiene
        sql = """
        SELECT key, content, created_at, updated_at, times_recalled
        FROM memories
        WHERE key = ?1
           OR key LIKE ?2 ESCAPE '\\'
           OR content LIKE ?2 ESCAPE '\\'
        ORDER BY CASE
                   WHEN key = ?1 THEN 0
                   WHEN key LIKE ?2 ESCAPE '\\' THEN 1
                   ELSE 2
                 END,
                 updated_at DESC
        LIMIT ?
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            desc, rows = await loop.run_in_executor(
                None, self._exec_read, sql, (query, pattern, int(limit)))
        results = [self._row_to_dict(desc, r) for r in rows]
        # Métrica de uso: los recuerdos consultados se marcan
        if results:
            keys = [r["key"] for r in results]
            await loop.run_in_executor(
                None, self._exec_write,
                "UPDATE memories SET times_recalled=times_recalled+1 "
                "WHERE key IN ({})".format(",".join("?" * len(keys))), keys)
        return results

    async def forget(self, key: str) -> bool:
        """Elimina un recuerdo por key exacta. Devuelve True si existía."""
        async with self._lock:
            loop = asyncio.get_running_loop()
            cur = await loop.run_in_executor(
                None, self._exec_write,
                "DELETE FROM memories WHERE key=?", (key.strip(),))
            return cur.rowcount > 0

    async def list_recent(self, limit: int = 20) -> list[dict]:
        sql = ("SELECT key, content, created_at, updated_at, times_recalled "
               "FROM memories ORDER BY updated_at DESC LIMIT ?")
        async with self._lock:
            loop = asyncio.get_running_loop()
            desc, rows = await loop.run_in_executor(None, self._exec_read,
                                                    sql, (int(limit),))
        return [self._row_to_dict(desc, r) for r in rows]

    async def count(self) -> int:
        async with self._lock:
            loop = asyncio.get_running_loop()
            _, rows = await loop.run_in_executor(
                None, self._exec_read, "SELECT COUNT(*) FROM memories", ())
        return rows[0][0] if rows else 0
