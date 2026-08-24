"""
soul_manager.py
===============
Genera y mantiene actualizado el archivo Soul.md — la "personalidad" del dueño
extraída de su patrón de escritura real (mensajes enviados en grupos y privados).

Flujo:
  1. Captura de mensajes salientes del dueño (MessageStore).
  2. Cuando hay >= initial_min_messages se genera el primer Soul.md.
  3. Cada refresh_interval_minutes se analiza un lote de mensajes NO analizados
     aún (o los más recientes si no hay nuevos) y se fusionan en Soul.md.
  4. El Soul.md resultante se cachea en memoria para usar en respuestas.
  5. Tras cada análisis se genera un "learning summary" corto que describe qué
     aprendió la IA del dueño en este ciclo, apto para mostrar al usuario.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ai_client import AIClient, AIError
from message_store import MessageStore

log = logging.getLogger("soul.soul")


SOUL_SYSTEM_PROMPT = (
    "Eres un analista de personalidad experto. Recibirás una muestra de mensajes "
    "reales enviados por un usuario de Telegram (el 'dueño'), junto con su contexto "
    "(tipo de chat, con quién hablaba, si respondía a alguien, hora). Tu trabajo es "
    "producir un documento Markdown llamado 'Soul.md' que describa, en español, "
    "con PRECISIÓN y SIN INVENTAR, cómo escribe y cómo se comporta esa persona. "
    "IMPORTANTE: "
    "- NO incluyas contenido de páginas web, links, URLs o referencias externas. "
    "- NO describas tutoriales, guías o contenido técnico que no sea del dueño. "
    "- Enfócate ÚNICAMENTE en el patrón de escritura y personalidad del dueño. "
    "- Si el contenido parece ser de una página web y no un mensaje personal, IGNÓRALO. "
    "- No romantices, no añadas información no inferible. Si no hay datos, dilo. "
    "Devuelves SOLO el contenido del archivo Markdown, sin comentarios extra."
)

SOUL_SCHEMA_INSTRUCTIONS = """
El documento debe seguir esta estructura (usa los encabezados textualmente):

# Soul — <alias si se infiere, si no, 'El dueño'>

## 1. Estilo de escritura
- Longitud típica de mensajes (corto / medio / largo), con rango aproximado en palabras.
- Usa o no tildes, mayúsculas, signos de puntuación.
- Abreviaturas, faltas de ortografía intencionales, jerga.
- Uso de emojis (frecuencia, cuáles).
- Uso de mayúsculas para gritar/énfasis.

## 2. Vocabulario y giros
- Palabras o frases recurrentes (top 10, con ejemplos).
- Modismos o regionalismos detectados (con país/región si se infiere).
- Términos técnicos o de nicho.

## 3. Tono y personalidad
- Tono dominante (informal, sarcástico, cálido, distante, cómico, seco, etc.).
- Cómo reacciona ante preguntas, bromas, conflictos, agradecimientos.
- Nivel de formalidad y cómo cambia según el chat (grupo vs. privado).

## 4. Patrones conversacionales
- Suele responder con pregunta, afirmación corta, o desarrollo largo.
- Inicia o cierra conversaciones de alguna forma recurrente.
- Tiende a responder rápido con una palabra / reacción, o con párrafos.

## 5. Temas y preferencias
- Temas de los que habla con frecuencia.
- Temas que evita o le incomodan.
- Intereses, aficiones, trabajo (solo si se infieren).

## 6. Ejemplos representativos
- 5–8 mensajes textuales reales seleccionados como representativos, entrecomillados.

## 7. Reglas para imitar al dueño
- 6–10 reglas concretas y accionables que un agente IA debe respetar para responder
  como lo haría el dueño (longitud, tono, emojis, qué evitar, qué incluir).

## 8. Estadísticas observadas
- Número de mensajes analizados en este ciclo.
- Tipos de chat donde se observó (grupos, privados) y cuántos.
- Rango de fechas de la muestra.
- Longitud promedio / máxima / mínima observada (en caracteres).
"""

UPDATE_SYSTEM_PROMPT = (
    "Eres un analista de personalidad. Te entrego el Soul.md actual del dueño y "
    "una muestra de mensajes recientes suyos (que NO estaban analizados). "
    "Debes devolver una versión ACTUALIZADA del Soul.md que: "
    "(a) conserve lo válido del anterior, "
    "(b) afine o añada patrones nuevos observados en la muestra, "
    "(c) no invente nada que no se infiera de los mensajes. "
    "Mantén exactamente la misma estructura de encabezados (incluyendo la sección "
    "8 de Estadísticas observadas con los datos nuevos). "
    "Devuelves SOLO el contenido Markdown actualizado."
)

LEARNING_SUMMARY_PROMPT = (
    "Acabas de actualizar el Soul.md de un usuario con una muestra nueva de sus "
    "mensajes. Escribe un mini-resumen en español, en 4-6 bullets, de LO NUEVO que "
    "aprendiste o refinaste sobre esta persona en este ciclo. Sé concreto: "
    "menciona palabras o patrones específicos que añadiste o corregiste. No "
    "inventes. Si no hubo nada nuevo destacable, di 'Sin cambios significativos'. "
    "Devuelve SOLO los bullets, sin prefijo."
)


def _format_my_messages_for_ai(messages: list[dict]) -> str:
    """Convierte los mensajes capturados en un bloque legible para el LLM.
    
    Filtra mensajes que:
    - Son solo URLs o links
    - Contienen contenido de páginas web (http/https)
    - Son demasiado cortos (menos de 3 caracteres)
    - Son solo stickers o media sin texto
    """
    if not messages:
        return "(sin mensajes aún)"
    
    # Patrones de contenido basura
    url_pattern = re.compile(r'https?://\S+', re.IGNORECASE)
    link_only_pattern = re.compile(r'^[\s]*(https?://\S+[\s]*)+$', re.IGNORECASE)
    
    lines = []
    filtered_count = 0
    
    for m in reversed(messages):  # cronológico asc
        text = (m.get("text") or "").strip()
        caption = (m.get("caption") or "").strip()
        body = text or caption
        
        # Filtros de calidad
        if not body or len(body) < 3:
            filtered_count += 1
            continue
        
        # Si es solo una URL, skip
        if link_only_pattern.match(body):
            filtered_count += 1
            continue
        
        # Si el texto principal es solo URLs, usar caption si existe
        if url_pattern.search(body) and caption:
            body = caption
        
        # Si sigue siendo solo URLs, skip
        if link_only_pattern.match(body):
            filtered_count += 1
            continue
        
        ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(m["ts"]))
        chat = m.get("chat_title") or m.get("chat_type") or "?"
        kind = m.get("chat_type") or "?"
        
        # Limpiar URLs del texto para análisis
        clean_body = url_pattern.sub('[link]', body)
        
        media = m.get("media_kind")
        media_tag = f" [{media}]" if media else ""
        reply = m.get("reply_to_text")
        reply_tag = f" (respondiendo a: \"{reply[:50]}\")" if reply else ""
        
        lines.append(f"[{ts}] ({kind}/{chat}){media_tag}{reply_tag}: {clean_body[:300]}")
    
    if filtered_count > 0:
        log.debug("Filtered %d low-quality messages from AI input", filtered_count)
    
    return "\n".join(lines)


@dataclass
class AnalysisResult:
    ok: bool
    soul_md_size: int = 0
    messages_analyzed: int = 0
    learning_summary: str = ""
    chat_types: dict = field(default_factory=dict)
    sample_first_ts: float | None = None
    sample_last_ts: float | None = None
    error: str = ""

    def summary_lines(self) -> list[str]:
        if not self.ok:
            return [f"❌ {self.error}"]
        lines = [
            f"✅ Soul.md actualizado ({self.soul_md_size} bytes).",
            f"📥 Mensajes analizados: {self.messages_analyzed}",
        ]
        if self.chat_types:
            parts = []
            for k, v in self.chat_types.items():
                parts.append(f"{k}={v}")
            lines.append(f"📊 Por tipo de chat: {', '.join(parts)}")
        if self.sample_first_ts and self.sample_last_ts:
            lines.append(
                "📅 Muestra: " +
                time.strftime("%Y-%m-%d", time.gmtime(self.sample_first_ts)) +
                " → " +
                time.strftime("%Y-%m-%d", time.gmtime(self.sample_last_ts))
            )
        if self.learning_summary:
            lines.append("")
            lines.append("🧠 Aprendido este ciclo:")
            for ln in self.learning_summary.splitlines():
                ln = ln.strip()
                if ln:
                    lines.append(f"  • {ln.lstrip('-•* ')}")
        return lines


class SoulManager:
    def __init__(self, store: MessageStore, ai: AIClient, soul_cfg: dict):
        self.store = store
        self.ai = ai
        self.soul_md_path = Path(soul_cfg.get("soul_md_path", "Soul.md"))
        self.initial_min_messages = int(soul_cfg.get("initial_min_messages", 30))
        self.refresh_interval = int(soul_cfg.get("refresh_interval_minutes", 360)) * 60
        self.sample_size = int(soul_cfg.get("analysis_sample_size", 200))
        self._cached_soul: str | None = None
        self._cache_at: float = 0.0
        self._cache_ttl = 60  # segundos
        self._last_refresh_at: float = 0.0
        self._last_learning_summary: str = ""
        self._refreshing = asyncio.Lock()
        self._initial_done = False
        self._load_from_disk()

    # -------------------------------------------------------------- IO
    def _load_from_disk(self) -> None:
        if self.soul_md_path.exists():
            try:
                self._cached_soul = self.soul_md_path.read_text(encoding="utf-8")
                self._cache_at = time.time()
                self._initial_done = True
                log.info("Loaded Soul.md from disk (%d bytes)",
                         len(self._cached_soul))
            except Exception as e:
                log.warning("Could not load Soul.md: %s", e)

    def get_soul(self) -> str | None:
        """Devuelve Soul.md cacheado (recarga de disco si expiró)."""
        if self._cached_soul is not None and (time.time() - self._cache_at) < self._cache_ttl:
            return self._cached_soul
        if self.soul_md_path.exists():
            self._cached_soul = self.soul_md_path.read_text(encoding="utf-8")
            self._cache_at = time.time()
            return self._cached_soul
        return None

    def _save_soul(self, content: str) -> None:
        self.soul_md_path.parent.mkdir(parents=True, exist_ok=True)
        self.soul_md_path.write_text(content, encoding="utf-8")
        self._cached_soul = content
        self._cache_at = time.time()

    def _validate_soul_content(self, text: str) -> str | None:
        """Valida que el Soul.md generado no contenga contenido basura.
        
        Retorna None si es válido, o un string con el error si no lo es.
        """
        if not text or len(text) < 50:
            return "Demasiado corto para ser un Soul.md válido"
        
        text_lower = text.lower()
        
        # Patrones de contenido basura CRÍTICOS (rechazan)
        critical_patterns = [
            (r'<html|<body|<div|<p>', "Contiene HTML"),
            (r'kissing|romantic|bedroom|french kiss', "Contenido no relacionado"),
        ]
        
        for pattern, reason in critical_patterns:
            if re.search(pattern, text_lower):
                return reason
        
        # Verificar que tiene ALGUNA sección del esquema
        sections = ["escritura", "vocabulario", "tono", "personalidad", "reglas", "estilos"]
        has_section = any(s in text_lower for s in sections)
        if not has_section:
            return "No contiene secciones del Soul.md"
        
        return None

    # -------------------------------------------------------------- public
    async def maybe_initial_build(self) -> bool:
        """Genera Soul.md inicial si hay suficientes mensajes y aún no existe."""
        if self._initial_done and self.soul_md_path.exists():
            return True
        count = await self.store.count_my_messages()
        if count < self.initial_min_messages:
            log.info("Waiting for more messages to build Soul.md: %d/%d",
                     count, self.initial_min_messages)
            return False
        log.info("Building initial Soul.md from %d messages...", count)
        msgs = await self.store.fetch_my_messages(limit=self.sample_size,
                                                   only_unanalyzed=False)
        result = await self._do_initial_build(msgs)
        return result.ok

    async def _do_initial_build(self, msgs: list[dict]) -> AnalysisResult:
        corpus = _format_my_messages_for_ai(msgs)
        
        # Verificar que hay suficiente contenido válido
        if len(corpus) < 100:
            return AnalysisResult(ok=False, error="Muy poco contenido válido para generar Soul.md")
        
        user_prompt = (
            "Mensajes capturados del dueño (cronológico, más reciente al final):\n\n"
            f"{corpus}\n\n"
            "Genera el Soul.md siguiendo exactamente este esquema:\n"
            f"{SOUL_SCHEMA_INSTRUCTIONS}\n"
            "Devuelve SOLO el Markdown."
        )
        try:
            text = await self.ai.chat_text(SOUL_SYSTEM_PROMPT, user_prompt,
                                           temperature=0.4, max_tokens=8000)
        except AIError as e:
            log.error("Initial Soul.md generation failed: %s", e)
            return AnalysisResult(ok=False, error=str(e))
        text = _strip_codefence(text)
        
        # Validar calidad del Soul.md generado
        validation_error = self._validate_soul_content(text)
        if validation_error:
            log.warning("Soul.md validation failed: %s", validation_error)
            return AnalysisResult(ok=False, error=f"Soul.md inválido: {validation_error}")
        
        self._save_soul(text)
        await self.store.mark_analyzed([m["message_id"] for m in msgs])
        self._initial_done = True
        self._last_refresh_at = time.time()
        log.info("Soul.md generated (%d bytes).", len(text))
        # Generar learning summary
        learn = await self._gen_learning_summary(text, msgs, is_initial=True)
        self._last_learning_summary = learn
        return AnalysisResult(
            ok=True,
            soul_md_size=len(text),
            messages_analyzed=len(msgs),
            learning_summary=learn,
            chat_types=_count_chat_types(msgs),
            sample_first_ts=msgs[0]["ts"] if msgs else None,
            sample_last_ts=msgs[-1]["ts"] if msgs else None,
        )

    async def refresh_if_due(self, force: bool = False) -> AnalysisResult:
        """Refresca Soul.md si ha pasado el intervalo o si force=True."""
        now = time.time()
        if not force and (now - self._last_refresh_at) < self.refresh_interval:
            return AnalysisResult(ok=False, error="Aún no toca refresh")
        async with self._refreshing:
            if not self._initial_done:
                ok = await self.maybe_initial_build()
                if not ok:
                    return AnalysisResult(ok=False, error="Faltan mensajes iniciales")
            # priorizar mensajes no analizados; si no hay, usar los más recientes
            unanalyzed = await self.store.count_unanalyzed()
            if unanalyzed < 10 and not force:
                return AnalysisResult(
                    ok=False,
                    error=f"Pocos mensajes nuevos para refrescar ({unanalyzed})"
                )
            msgs = await self.store.fetch_my_messages(limit=self.sample_size,
                                                      only_unanalyzed=True)
            if len(msgs) < 5:
                msgs = await self.store.fetch_my_messages(limit=self.sample_size,
                                                          only_unanalyzed=False)
            if not msgs:
                return AnalysisResult(ok=False, error="Sin mensajes que analizar")
            log.info("Refreshing Soul.md with %d messages (unanalyzed=%d)...",
                     len(msgs), unanalyzed)
            current = self.get_soul() or ""
            corpus = _format_my_messages_for_ai(msgs)
            user_prompt = (
                f"=== Soul.md actual ===\n{current}\n\n"
                f"=== Muestra de mensajes nuevos del dueño ===\n{corpus}\n\n"
                "Genera el Soul.md actualizado. Mantén la misma estructura. "
                "Devuelve SOLO el Markdown."
            )
            try:
                text = await self.ai.chat_text(UPDATE_SYSTEM_PROMPT, user_prompt,
                                               temperature=0.4, max_tokens=8000)
            except AIError as e:
                log.error("Soul.md refresh failed: %s", e)
                return AnalysisResult(ok=False, error=str(e))
            text = _strip_codefence(text)
            self._save_soul(text)
            await self.store.mark_analyzed([m["message_id"] for m in msgs])
            self._last_refresh_at = time.time()
            log.info("Soul.md refreshed (%d bytes).", len(text))
            learn = await self._gen_learning_summary(text, msgs, is_initial=False)
            self._last_learning_summary = learn
            return AnalysisResult(
                ok=True,
                soul_md_size=len(text),
                messages_analyzed=len(msgs),
                learning_summary=learn,
                chat_types=_count_chat_types(msgs),
                sample_first_ts=msgs[0]["ts"] if msgs else None,
                sample_last_ts=msgs[-1]["ts"] if msgs else None,
            )

    async def _gen_learning_summary(self, soul_md: str, msgs: list[dict],
                                     is_initial: bool) -> str:
        """Genera el mini-resumen de 'qué aprendí en este ciclo'."""
        # Pre: si hay muy pocos mensajes, no molestar a la IA
        if len(msgs) < 3:
            return "Pocos mensajes para sacar un resumen de aprendizaje."
        sample = "\n".join(
            (m.get("text") or "")[:200] for m in msgs[-20:] if m.get("text")
        )
        user_prompt = (
            f"=== Soul.md actualizado (recorte) ===\n{soul_md[:1500]}\n\n"
            f"=== Muestra de mensajes nuevos analizados ===\n{sample}\n\n"
            f"{'Es la PRIMERA vez que se construye el Soul.md.' if is_initial else 'Es una actualización del Soul.md anterior.'}\n"
            "Escribe 4-6 bullets con lo más relevante que se aprendió/refinó."
        )
        try:
            text = await self.ai.chat_text(LEARNING_SUMMARY_PROMPT, user_prompt,
                                            temperature=0.3, max_tokens=300)
            return text.strip()
        except AIError as e:
            log.warning("Learning summary generation failed: %s", e)
            return ""

    async def refresh_chat_context(self, chat_id: int, *, force: bool = False,
                                   max_messages: int = 80) -> dict | None:
        """Resume el contexto reciente de un chat y lo guarda en SQLite.

        Se usa una ventana acotada para que cada respuesta conozca el tema actual
        sin reenviar todo el historial del chat al proveedor de IA.
        """
        current = await self.store.get_chat_context(chat_id)
        now = int(time.time())
        # No regenerar el resumen en cada mensaje: el historial reciente sigue
        # entrando directamente en Responder y el resumen se refresca cada 30 min.
        if current and not force and current.get("summary_at") and now - int(current["summary_at"]) < 1800:
            return current
        rows = await self.store.fetch_recent(chat_id, limit=max_messages)
        if not rows:
            return current
        lines = []
        participants = {}
        for m in rows:
            name = (m.get("from_name") or "alguien").strip()
            participants[name] = participants.get(name, 0) + 1
            body = (m.get("text") or m.get("caption") or "").strip()
            if not body:
                body = f"[{m.get('media_kind') or 'media'}]"
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("ts") or now))
            role = "YO" if m.get("is_out") else name
            lines.append(f"[{ts}] {role}: {body[:500]}")
        transcript = "\\n".join(lines)
        prompt = f"""Analiza el contexto de esta conversación de Telegram.
Devuelve SOLO JSON válido, sin markdown, con estas claves:
summary (resumen concreto de 3-6 frases sobre lo que se está hablando ahora),
topics (lista de hasta 8 temas), keywords (lista de hasta 12 palabras clave),
my_role (rol del dueño en este chat: amigo, soporte, ventas, técnico, etc.),
tone (tono dominante).
No inventes datos. Distingue hechos de bromas y no incluyas secretos, contraseñas,
tokens ni URLs completas.

Conversación reciente:
{transcript}"""
        try:
            raw = await self.ai.chat_text(
                "Eres un analista de contexto conversacional preciso y conservador.",
                prompt, temperature=0.2, max_tokens=700,
            )
            data = _parse_context_json(raw)
            if not data.get("summary"):
                return current
            first_ts = min((m.get("ts") for m in rows if m.get("ts")), default=None)
            last_ts = max((m.get("ts") for m in rows if m.get("ts")), default=None)
            await self.store.upsert_chat_context(
                chat_id,
                chat_type=rows[-1].get("chat_type"),
                chat_title=rows[-1].get("chat_title"),
                participants=sorted(participants, key=participants.get, reverse=True)[:20],
                topics=data.get("topics", [])[:8],
                keywords=data.get("keywords", [])[:12],
                summary=str(data["summary"])[:2500],
                my_role=str(data.get("my_role", ""))[:120],
                tone=str(data.get("tone", ""))[:120],
                messages_total=len(rows),
                my_messages_total=sum(1 for m in rows if m.get("is_out")),
                first_ts=first_ts,
                last_ts=last_ts,
                summary_at=now,
                summary_model=getattr(self.ai, "chat_model", ""),
            )
            return await self.store.get_chat_context(chat_id)
        except Exception as e:
            log.warning("Chat context refresh failed for %s: %s", chat_id, e)
            return current

    async def refresh_contexts_for_top_chats(self, limit: int = 50) -> int:
        """Actualiza contexto de los chats con más mensajes propios.

        Se limita deliberadamente para controlar coste/latencia del proveedor IA.
        Los demás chats se actualizarán bajo demanda cuando llegue un mensaje.
        """
        chats = await self.store.top_chats(limit=max(1, int(limit)))
        updated = 0
        for chat in chats:
            try:
                ctx = await self.refresh_chat_context(int(chat["chat_id"]), force=True)
                if ctx and ctx.get("summary"):
                    updated += 1
            except Exception as e:
                log.warning("Context batch failed for %s: %s", chat.get("chat_id"), e)
        return updated

    def last_learning_summary(self) -> str:
        return self._last_learning_summary

    def stats(self) -> dict:
        return {
            "soul_md_path": str(self.soul_md_path),
            "soul_md_exists": self.soul_md_path.exists(),
            "soul_md_size": self.soul_md_path.stat().st_size
                            if self.soul_md_path.exists() else 0,
            "last_refresh_at": self._last_refresh_at,
            "initial_done": self._initial_done,
            "refresh_interval_seconds": self.refresh_interval,
            "has_learning_summary": bool(self._last_learning_summary),
        }


def _strip_codefence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _parse_context_json(raw: str) -> dict:
    """Parsea JSON aunque el proveedor lo envuelva accidentalmente en fences."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    try:
        data = json.loads(text.strip())
        return data if isinstance(data, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _count_chat_types(msgs: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for m in msgs:
        k = m.get("chat_type") or "?"
        counts[k] = counts.get(k, 0) + 1
    return counts
