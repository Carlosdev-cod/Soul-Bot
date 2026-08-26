"""
scheduler.py
============
Mensajes programados del agente.

Permite a la IA (tool `schedule_message`) y al dueño (comandos) agendar
mensajes que se enviarán automáticamente en el futuro, p. ej. recordatorios
o respuestas diferidas ("te contesto en una hora").

Persistencia: JSON en data/scheduled_messages.json con escritura atómica
(tempfile + os.replace), igual que config_store.

Cada tarea:
  {
    "id": "a1b2c3d4",           -- uuid4 corto
    "chat_id": 12345,
    "text": "recuerda X",
    "send_at": 1730000000,      -- epoch seconds
    "reply_to_message_id": null,
    "created_at": 1730000000,
    "status": "pending",        -- pending | sent | failed | cancelled
    "attempts": 0
  }

El loop (run_loop) revisa cada N segundos las tareas vencidas y las envía
mediante un callback async provisto por el agente. Reintentos con backoff
hasta max_attempts; después se marcan failed y se conservan para auditoría.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("soul.scheduler")

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 60


class MessageScheduler:
    def __init__(self, state_path: str,
                 send_fn: Callable[[int, str, int | None], Awaitable[None]]):
        """send_fn(chat_id, text, reply_to_message_id) -> None (async).

        Debe lanzar excepción si el envío falla; el scheduler la captura
        y aplica backoff.
        """
        self.state_path = str(state_path)
        Path(self.state_path).parent.mkdir(parents=True, exist_ok=True)
        self._send_fn = send_fn
        self._tasks: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._loop_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._load()

    # -------------------------------------------------------------- persistencia
    def _load(self) -> None:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for t in data.get("tasks", []):
                if isinstance(t, dict) and t.get("id"):
                    self._tasks[t["id"]] = t
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("No se pudo cargar %s (%s); se arranca vacío: %s",
                        self.state_path, type(e).__name__, e)

    def _save(self) -> None:
        """Escritura atómica: nunca corrompe el estado ante un crash."""
        data = {"tasks": list(self._tasks.values())}
        try:
            fd, tmp = tempfile.mkstemp(dir=Path(self.state_path).parent,
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except Exception as e:
            log.error("No se pudo persistir scheduler state: %s", e)

    # -------------------------------------------------------------- API
    async def add(self, chat_id: int, text: str, send_at: float,
                  reply_to_message_id: int | None = None) -> dict:
        task = {
            "id": uuid.uuid4().hex[:8],
            "chat_id": int(chat_id),
            "text": str(text)[:4000],
            "send_at": float(send_at),
            "reply_to_message_id": reply_to_message_id,
            "created_at": time.time(),
            "status": "pending",
            "attempts": 0,
        }
        async with self._lock:
            self._tasks[task["id"]] = task
            self._save()
        log.info("Scheduled %s for chat=%s at=%s text=%r",
                 task["id"], chat_id, send_at, task["text"][:50])
        return task

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            t = self._tasks.get(task_id)
            if not t or t["status"] != "pending":
                return False
            t["status"] = "cancelled"
            self._save()
            return True

    async def pending(self) -> list[dict]:
        async with self._lock:
            return sorted(
                (t for t in self._tasks.values() if t["status"] == "pending"),
                key=lambda t: t["send_at"])

    async def pending_count(self) -> int:
        async with self._lock:
            return sum(1 for t in self._tasks.values()
                       if t["status"] == "pending")

    async def purge_finished(self, keep: int = 200) -> int:
        """Elimina tareas sent/failed/cancelled antiguas (hasta `keep`)."""
        async with self._lock:
            finished = [t for t in self._tasks.values()
                        if t["status"] != "pending"]
            finished.sort(key=lambda t: t.get("created_at", 0))
            n = 0
            for t in finished[:-keep] if keep else finished:
                self._tasks.pop(t["id"], None)
                n += 1
            if n:
                self._save()
            return n

    # -------------------------------------------------------------- loop
    async def run_loop(self, check_interval: float = 5.0) -> None:
        """Loop principal: envía tareas vencidas."""
        log.info("Scheduler loop started (interval=%.1fs)", check_interval)
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("Scheduler tick error: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=check_interval)
            except asyncio.TimeoutError:
                pass
        log.info("Scheduler loop stopped.")

    async def _tick(self) -> None:
        now = time.time()
        due: list[dict] = []
        async with self._lock:
            for t in self._tasks.values():
                if t["status"] == "pending" and t["send_at"] <= now:
                    due.append(t)
        for t in due:
            await self._attempt_send(t)

    async def _attempt_send(self, task: dict) -> None:
        try:
            await self._send_fn(task["chat_id"], task["text"],
                                task.get("reply_to_message_id"))
            async with self._lock:
                task["status"] = "sent"
                self._save()
            log.info("Scheduled message %s sent to chat=%s",
                     task["id"], task["chat_id"])
        except Exception as e:
            async with self._lock:
                task["attempts"] = int(task.get("attempts", 0)) + 1
                if task["attempts"] >= MAX_ATTEMPTS:
                    task["status"] = "failed"
                    log.error("Scheduled %s FAILED permanently: %s",
                              task["id"], e)
                else:
                    # backoff: reintenta en 60s * intento
                    task["send_at"] = time.time() + RETRY_BACKOFF_SECONDS * task["attempts"]
                    log.warning("Scheduled %s send failed (attempt %s): %s",
                                task["id"], task["attempts"], e)
                self._save()

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._stop.clear()
            self._loop_task = asyncio.create_task(self.run_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
