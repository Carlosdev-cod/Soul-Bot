"""
ai_client.py
============
Wrapper async sobre un endpoint OpenAI-compatible (https://vimax-ia.p.jo3.org/v1)
que usa el modelo Gemini 3.6 flash para:
  1. Generar / actualizar Soul.md a partir del historial de mensajes del usuario.
  2. Producir respuestas "como yo" en chats autorizados.
  3. Analizar imágenes (visión) cuando el endpoint lo soporte.

El wrapper detecta automáticamente si el endpoint soporta visión; si no la
soporta (caso actual del proxy), degrada elegantemente y responde usando solo
el caption/metadatos de la imagen.

También soporta function calling (tools): si el endpoint no acepta el
parámetro `tools`, se reintenta la petición sin él y se marca como no
soportado durante un intervalo (re-probe periódico), igual que la visión.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

log = logging.getLogger("soul.ai")


class AIError(RuntimeError):
    pass


@dataclass
class ChatResponse:
    text: str
    raw: dict
    model: str
    usage: dict


class AIClient:
    """Cliente minimalista para un endpoint OpenAI-compatible."""

    def __init__(self, cfg: dict):
        ai = cfg["ai"]
        self.base_url = ai["base_url"].rstrip("/")
        self.api_key = ai["api_key"]
        self.chat_model = ai.get("selected_model") or ai.get("chat_model", "gemini-3.6-flash")
        self.vision_model = ai.get("selected_model") or ai.get("vision_model", self.chat_model)
        self.vision_enabled = ai.get("vision_enabled", True)
        self.vision_fallback_to_caption = ai.get("vision_fallback_to_caption", True)
        self.timeout = ai.get("request_timeout_seconds", 120)
        self.temperature = ai.get("temperature", 0.7)
        self.max_tokens = ai.get("max_tokens", 400)
        self._vision_supported: bool | None = None
        self._vision_checked_at: float = 0.0
        self._vision_recheck_interval = 6 * 3600
        # Function calling: None = sin datos, True/False = sondado
        self._tools_supported: bool | None = None
        self._tools_checked_at: float = 0.0
        self._tools_recheck_interval = 6 * 3600
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(self.timeout, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_models(self) -> list[str]:
        """Devuelve los IDs de modelos publicados por el endpoint configurado."""
        try:
            r = await self._client.get("/models")
            r.raise_for_status()
            data = r.json()
            models = data.get("data", [])
            ids = {str(item.get("id")) for item in models if item.get("id")}
            return sorted(ids, key=str.casefold)
        except httpx.HTTPStatusError as e:
            log.error("AI models HTTP %s: %s", e.response.status_code,
                      e.response.text[:300])
            raise AIError(f"AI models endpoint returned {e.response.status_code}") from e
        except (httpx.RequestError, ValueError) as e:
            log.error("AI models request error: %s", e)
            raise AIError(f"Could not load AI models: {e}") from e

    def set_model(self, model: str) -> None:
        """Cambia el modelo activo en memoria y limpia la caché de visión."""
        self.chat_model = model
        self.vision_model = model
        self._vision_supported = None
        self._vision_checked_at = 0.0

    # -------------------------------------------------------------- tools
    def _tools_probe_due(self) -> bool:
        """True si toca re-probar el soporte de tools tras un fallo."""
        if self._tools_supported is None:
            return False  # aún no sondado: se intenta con tools directamente
        return (time.time() - self._tools_checked_at) >= self._tools_recheck_interval

    async def is_tools_enabled(self) -> bool:
        """Indica si el endpoint soporta function calling (tools).

        No lanza peticiones: refleja el estado de la última sonda.
        """
        return bool(self._tools_supported)

    def _strip_tools(self, extra: dict | None) -> dict | None:
        """Quita tools/tool_choice del payload si el endpoint no los soporta."""
        if not extra:
            return extra
        if "tools" not in extra and "tool_choice" not in extra:
            return extra
        cleaned = {k: v for k, v in extra.items()
                   if k not in ("tools", "tool_choice")}
        return cleaned or None

    # -------------------------------------------------------------- helpers
    def _payload(self, messages: list[dict], model: str | None = None,
                 temperature: float | None = None, max_tokens: int | None = None,
                 extra: dict | None = None) -> dict:
        p: dict[str, Any] = {
            "model": model or self.chat_model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        if extra:
            p.update(extra)
        return p

    async def _post_chat(self, payload: dict) -> dict:
        try:
            r = await self._client.post("/chat/completions", json=payload)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            log.error("AI HTTP %s: %s", e.response.status_code, e.response.text[:500])
            raise AIError(f"AI endpoint returned {e.response.status_code}") from e
        except httpx.RequestError as e:
            log.error("AI request error: %s", e)
            raise AIError(f"Network error: {e}") from e
        except Exception as e:
            log.exception("AI unexpected error")
            raise AIError(str(e)) from e

    # -------------------------------------------------------------- chat
    async def chat(self, messages: list[dict], *, model: str | None = None,
                   temperature: float | None = None, max_tokens: int | None = None,
                   extra: dict | None = None) -> ChatResponse:
        """POST /chat/completions con soporte opcional de `tools`.

        Degradación elegante de function calling: si el endpoint rechaza
        el payload con tools (error HTTP), se reintenta UNA vez sin tools,
        se marca `_tools_supported=False` (re-probe cada 6h) y se devuelve
        la respuesta de texto. Así el agente sigue funcionando igual en
        endpoints sin soporte de tools.
        """
        use_extra = dict(extra) if extra else None
        has_tools = bool(use_extra and use_extra.get("tools"))
        if has_tools and self._tools_supported is False and \
                not self._tools_probe_due():
            # Endpoint ya sondado sin soporte: ir directo a texto plano
            use_extra = self._strip_tools(use_extra)
            has_tools = False
        payload = self._payload(messages, model, temperature, max_tokens,
                                use_extra)
        try:
            data = await self._post_chat(payload)
        except AIError as e:
            if has_tools and "Network error" not in str(e):
                # Podría ser rechazo del parámetro tools: reintentar sin él
                log.warning("Chat con tools falló (%s); reintentando sin "
                            "tools (modo degradado)", e)
                self._tools_supported = False
                self._tools_checked_at = time.time()
                payload = self._payload(messages, model, temperature,
                                        max_tokens, self._strip_tools(use_extra))
                data = await self._post_chat(payload)
            else:
                raise
        else:
            if has_tools and self._tools_supported is not True:
                # La petición con tools funcionó: el endpoint los acepta
                # (aunque el modelo puede elegir no usarlos)
                self._tools_supported = True
                self._tools_checked_at = time.time()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise AIError(f"Unexpected AI response shape: {data}") from e
        return ChatResponse(
            text=text,
            raw=data,
            model=data.get("model", payload["model"]),
            usage=data.get("usage", {}),
        )

    async def chat_text(self, system: str, user: str, *, temperature: float | None = None,
                        max_tokens: int | None = None) -> str:
        """Atajo para interacción single-turn."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        resp = await self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        return resp.text.strip()

    # -------------------------------------------------------------- visión
    async def _check_vision_support(self) -> bool:
        """
        Envía una imagen de prueba (rojo sólido, embebida para no depender de
        internet) y comprueba si el modelo la describe con el color correcto.
        Detecta los patrones de fallo típicos:

        - 'no pude generar una respuesta' (gemini a través de proxy sin visión)
        - 'no se ha adjuntado ninguna imagen'
        - respuesta genérica que ignora el contenido visual

        Sólo se marca como soportada si el modelo nombra el color 'rojo' (o un
        sinónimo muy cercano) en su respuesta.
        """
        if not self.vision_enabled:
            self._vision_supported = False
            return False
        now = time.time()
        if self._vision_supported is not None and \
                (now - self._vision_checked_at) < self._vision_recheck_interval:
            return self._vision_supported
        # 4x4 PNG rojo puro embebido (no depende de internet)
        img_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAYAAACp8Z5+AAAAFklEQVR42mP8z8BQ"
            "z4AEYUgZJl7XfwAAAABJRU5ErkJggg=="
        )
        messages = [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "¿De qué color es esta imagen? Responde con UNA sola "
                         "palabra en español: el nombre del color."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            ],
        }]
        try:
            resp = await self.chat(messages, model=self.vision_model, max_tokens=20,
                                   temperature=0.0)
            text = (resp.text or "").lower().strip()
            red_words = ("rojo", "roja", "red", "escarlata", "carmesí", "colorado")
            supported = any(w in text for w in red_words) and \
                        "no pude" not in text and \
                        "no se ha adjuntado" not in text and \
                        "no se adjunt" not in text and \
                        "no tengo acceso" not in text
            if not supported:
                log.info("Vision probe response (rejected): %r", resp.text[:100])
        except AIError as e:
            log.warning("Vision probe failed: %s", e)
            supported = False
        self._vision_supported = supported
        self._vision_checked_at = now
        log.info("Vision support: %s", "ENABLED" if supported else "DISABLED (fallback)")
        return supported

    async def is_vision_enabled(self) -> bool:
        return await self._check_vision_support()

    async def describe_image(self, image_bytes: bytes, mime: str,
                             prompt: str | None = None) -> str | None:
        """Devuelve descripción textual de la imagen o None si no se soporta."""
        if not await self._check_vision_support():
            return None
        b64 = base64.b64encode(image_bytes).decode()
        content = []
        if prompt:
            content.append({"type": "text", "text": prompt})
        else:
            content.append({"type": "text",
                            "text": "Describe lo que ves en esta imagen en español, "
                                    "en 1-2 frases concretas."})
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
        try:
            resp = await self.chat(
                [{"role": "user", "content": content}],
                model=self.vision_model, max_tokens=200, temperature=0.3,
            )
            return resp.text.strip()
        except AIError as e:
            log.warning("Vision describe failed: %s", e)
            return None

    async def reply_with_image_context(
        self, *, system: str, conversation: list[dict],
        image_bytes: bytes | None, mime: str | None,
        caption: str | None,
    ) -> str:
        """
        Genera respuesta del agente considerando contexto + imagen opcional.

        - Si la visión está soportada y hay imagen, la incluye en el mensaje.
        - Si la visión no está soportada y vision_fallback_to_caption=True,
          inserta una nota descriptiva usando el caption disponible.
        """
        last_user = conversation[-1] if conversation else {"role": "user", "content": ""}
        if image_bytes and await self._check_vision_support():
            b64 = base64.b64encode(image_bytes).decode()
            user_content: list[dict] = []
            if caption:
                user_content.append({"type": "text",
                                     "text": f"(Mensaje con foto. Caption: {caption})"})
            user_content.append({"type": "text",
                                 "text": last_user.get("content", "")})
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
            messages = [{"role": "system", "content": system}]
            for m in conversation[:-1]:
                messages.append(m)
            messages.append({"role": "user", "content": user_content})
            resp = await self.chat(messages, temperature=self.temperature,
                                   max_tokens=self.max_tokens)
            return resp.text.strip()
        # fallback: solo texto + caption
        if image_bytes and caption and self.vision_fallback_to_caption:
            conversation = list(conversation)
            conversation[-1] = {
                **conversation[-1],
                "content": (conversation[-1].get("content", "") +
                            f"\n[Nota: el mensaje incluía una foto con el caption: \"{caption}\". "
                            "No se ha podido analizar visualmente; responde basándote en el caption "
                            "y el contexto.]"),
            }
        resp = await self.chat(
            [{"role": "system", "content": system}, *conversation],
            temperature=self.temperature, max_tokens=self.max_tokens,
        )
        return resp.text.strip()
