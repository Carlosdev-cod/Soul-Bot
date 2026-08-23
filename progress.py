"""
progress.py
===========
Barra de progreso ligera para consola y notificaciones de Telegram.

- ConsoleProgressBar: imprime `█████░░░ 45% — mensaje` con \r.
- TelegramProgressReporter: edita un mensaje de Telegram para mostrar progreso
  (más resumen final).
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("soul.progress")


class ConsoleProgressBar:
    """Barra de progreso ASCII que se actualiza in-place en stdout."""

    def __init__(self, total: int, label: str = "", width: int = 30,
                 update_every: int = 1):
        self.total = max(1, int(total))
        self.label = label
        self.width = width
        self.update_every = max(1, update_every)
        self.current = 0
        self.last_render_at = 0.0
        self.start_ts = time.time()
        self._min_interval = 0.05  # 20 fps max en consola

    def update(self, n: int = 1, note: str = "") -> None:
        self.current += n
        now = time.time()
        if (self.current < self.total) and \
                (now - self.last_render_at) < self._min_interval and \
                self.current % self.update_every != 0:
            return
        self._render(note)
        self.last_render_at = now

    def _render(self, note: str = "") -> None:
        pct = min(100, int(self.current * 100 / self.total))
        filled = int(self.width * (pct / 100))
        bar = "█" * filled + "░" * (self.width - filled)
        elapsed = max(0.001, time.time() - self.start_ts)
        eta = (elapsed / max(1, self.current)) * (self.total - self.current)
        line = (f"\r{self.label} |{bar}| {pct:3d}% "
                f"({self.current}/{self.total}) "
                f"~{eta:0.1f}s" + (f" — {note}" if note else ""))
        # truncar a ancho terminal típico
        line = line[:120]
        sys.stdout.write(line)
        sys.stdout.flush()
        if self.current >= self.total:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def finish(self, note: str = "") -> None:
        self.current = self.total
        self._render(note)


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str = ""
    extras: dict = field(default_factory=dict)


class TelegramProgressReporter:
    """
    Edita un único mensaje de Telegram para mostrar progreso + resumen final.

    Uso:
        reporter = TelegramProgressReporter(reply_callback, total_steps=4)
        await reporter.start("Iniciando análisis de historial…")
        await reporter.step("Escaneando chats", detail="8 chats")
        await reporter.step("Descargando mensajes", detail="1200 mensajes")
        await reporter.finish("✅ Completado", summary_lines=[...])
    """

    def __init__(self, send_fn: Callable[[str], Awaitable],
                 edit_fn: Callable[[str, str], Awaitable] | None = None,
                 total_steps: int = 1, throttle_seconds: float = 1.0):
        self.send_fn = send_fn
        self.edit_fn = edit_fn
        self.total_steps = max(1, int(total_steps))
        self.throttle = throttle_seconds
        self.current_step = 0
        self._msg_id: str | None = None
        self._last_edit_at = 0.0
        self._buffer: str = ""
        self._lock = asyncio.Lock()
        self._start_time = time.time()
        self._chats_scanned = 0
        self._messages_found = 0
        self._new_inserted = 0

    async def _send(self, text: str) -> None:
        try:
            msg = await self.send_fn(text)
            self._msg_id = getattr(msg, "id", None) or str(msg.id if hasattr(msg, "id") else "")
        except Exception as e:
            log.debug("progress send error: %s", e)

    async def _edit(self, text: str) -> None:
        if not self._msg_id or not self.edit_fn:
            return
        now = time.time()
        if (now - self._last_edit_at) < self.throttle:
            return
        try:
            await self.edit_fn(self._msg_id, text)
            self._last_edit_at = now
        except Exception as e:
            log.debug("progress edit error: %s", e)

    async def start(self, title: str = "Procesando…") -> None:
        async with self._lock:
            self._start_time = time.time()
            self._buffer = self._format(title, 0)
            await self._send(self._buffer)

    async def step(self, name: str, detail: str = "",
                   chats_scanned: int = 0, messages_found: int = 0,
                   new_inserted: int = 0) -> None:
        async with self._lock:
            self.current_step = min(self.total_steps, self.current_step + 1)
            self._chats_scanned = chats_scanned
            self._messages_found = messages_found
            self._new_inserted = new_inserted
            self._buffer = self._format(name, self.current_step, detail)
            await self._edit(self._buffer)

    async def finish(self, title: str, summary_lines: list[str] | None = None) -> None:
        async with self._lock:
            self.current_step = self.total_steps
            elapsed = time.time() - self._start_time
            lines = [self._format(title, self.total_steps, "")]
            lines.append(f"⏱ Tiempo total: {elapsed:.1f}s")
            if summary_lines:
                lines.append("")
                lines.extend(summary_lines)
            final = "\n".join(lines)
            if self._msg_id and self.edit_fn:
                try:
                    await self.edit_fn(self._msg_id, final)
                except Exception:
                    await self._send(final)
            else:
                await self._send(final)

    def _format(self, current_action: str, step: int, detail: str = "") -> str:
        pct = int(step * 100 / self.total_steps)
        bar_w = 12
        filled = int(bar_w * pct / 100)
        bar = "█" * filled + "░" * (bar_w - filled)
        
        elapsed = time.time() - self._start_time
        if step > 0 and self.total_steps > 1:
            eta = (elapsed / step) * (self.total_steps - step)
            eta_str = f"~{eta:.0f}s" if eta > 1 else "<1s"
        else:
            eta_str = ""
        
        line = f"[{bar}] {pct:3d}% — {current_action}"
        if detail:
            line += f" ({detail})"
        if eta_str:
            line += f" ⏳{eta_str}"
        
        # Agregar estadísticas en tiempo real
        if self._chats_scanned > 0 or self._messages_found > 0:
            stats_parts = []
            if self._chats_scanned > 0:
                stats_parts.append(f"📊{self._chats_scanned} chats")
            if self._messages_found > 0:
                stats_parts.append(f"💬{self._messages_found} msgs")
            if self._new_inserted > 0:
                stats_parts.append(f"🆕{self._new_inserted} nuevos")
            if stats_parts:
                line += " | " + " ".join(stats_parts)
        
        return line
