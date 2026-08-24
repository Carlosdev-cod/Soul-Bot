<div align="center">

# 🧬 Soul Bot

### Tu clon de Telegram con personalidad propia

**Un agente de IA que aprende tu forma de escribir y responde como tú en Telegram.**

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-Chat-000000?style=for-the-badge&logo=deepseek&logoColor=white)](https://deepseek.com)
[![Gemini](https://img.shields.io/badge/Gemini-Flash-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://gemini.google.com)
[![Kurigram](https://img.shields.io/badge/Kurigram-2.2-0088CC?style=for-the-badge&logo=telegram&logoColor=white)](https://docs.pyrogram.org)
[![SQLite](https://img.shields.io/badge/SQLite-WAL-003B57?style=for-the-badge&logo=sqlite&logoColor=white)](https://sqlite.org/)
[![HTTPX](https://img.shields.io/badge/HTTPX-Async-5A29E4?style=for-the-badge&logo=python&logoColor=white)](https://www.python-httpx.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)
[![Open Source](https://img.shields.io/badge/Open%20Source-❤️-green?style=for-the-badge)](LICENSE)

---

![Soul Bot Architecture](https://img.shields.io/badge/Architecture-Python%20%2B%20SQLite%20%2B%20Telegram%20API-blue?style=for-the-badge&logo=architecture&logoColor=white)

### Stack tecnológico

| Tecnología | Uso en Soul Bot |
|---|---|
| 🐍 **Python 3.10+** | Orquestación del agente y módulos asíncronos |
| ✈️ **Kurigram / Pyrogram API** | Conexión con Telegram mediante cuenta de usuario |
| 🧠 **DeepSeek / Gemini / OpenAI-compatible** | Personalidad, contexto, respuestas y visión |
| 🗄️ **SQLite + WAL** | Mensajes, historial y memoria contextual por chat |
| ⚡ **HTTPX async** | Cliente HTTP para el endpoint de IA |
| 🔐 **Telegram Auth** | Autorización de grupos y usuarios |
| 📊 **Markdown / JSON** | Soul.md, configuración y perfiles legibles |

</div>


> ⚠️ Automatizar una cuenta de usuario puede incumplir reglas de Telegram. Úsalo bajo tu responsabilidad, con autorización de los participantes y respetando la privacidad.

## Qué hace

- Captura tus mensajes y los analiza para construir `Soul.md`.
- Escanea historial antiguo con `/soul_scan`.
- Mantiene el contexto inmediato de cada chat en SQLite.
- Resume temas, palabras clave, participantes frecuentes, tono y rol del dueño por chat.
- Responde usando tres capas de información:
  1. **Personalidad global:** `Soul.md`.
  2. **Memoria del chat:** resumen persistente de esa conversación.
  3. **Conversación inmediata:** los últimos mensajes del mismo chat.
- Responde únicamente en grupos/usuarios autorizados.
- Puede analizar imágenes si el endpoint de IA soporta visión.

## Arquitectura

```text
Telegram / cuenta de usuario
          │
          ▼
     soul_agent.py
   handlers + captura
      │          │
      ▼          ▼
message_store  responder
 SQLite         │
      │         ├── contexto reciente del chat
      │         ├── memoria contextual persistente
      │         └── Soul.md + reglas de personalidad
      ▼         ▼
  soul_manager ─── ai_client.py
  aprendizaje       API OpenAI-compatible
```

### Memoria por chat

La tabla `chat_context` guarda una memoria resumida por `chat_id`:

- `summary`: de qué se habla actualmente.
- `topics`: temas recurrentes.
- `keywords`: palabras clave.
- `participants`: participantes frecuentes.
- `my_role`: papel habitual del dueño en ese chat.
- `tone`: tono dominante.
- métricas y fecha de actualización.

El resumen se actualiza como máximo cada 30 minutos por chat. La conversación reciente se consulta en cada respuesta, por lo que el agente puede reaccionar al tema actual aunque la memoria persistente todavía no se haya refrescado.

Después de `/soul_scan`, se actualizan en segundo plano los chats principales —por defecto, los 50 con más mensajes tuyos—. Los demás se actualizan automáticamente cuando reciben una conversación autorizada.

El resumen es una ayuda para el modelo, no una instrucción: los mensajes recientes tienen prioridad y el agente debe evitar inventar información.

## Requisitos

- Python 3.10+ (recomendado 3.12).
- Credenciales de Telegram desde [my.telegram.org](https://my.telegram.org).
- API key de un proveedor compatible con `/v1/chat/completions`.
- Dependencias de `requirements.txt`.

## Instalación

```bash
git clone https://github.com/Carlosdev-cod/Soul-Bot.git
cd Soul-Bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Edita `config.json` y completa tus credenciales. No subas este archivo: contiene secretos y está incluido en `.gitignore`.

```json
{
  "telegram": {
    "api_id": 12345678,
    "api_hash": "tu-api-hash",
    "phone_number": "+123456789",
    "session_name": "soul_agent"
  },
  "ai": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "tu-api-key",
    "chat_model": "gpt-4o-mini",
    "vision_enabled": false
  },
  "auth": {
    "owner_user_id": 0,
    "authorized_group_ids": [],
    "authorized_user_ids": []
  }
}
```

También puedes dejar `owner_user_id` en `0`: al iniciar sesión se asigna automáticamente a la cuenta conectada.

## Ejecución

```bash
source venv/bin/activate
python soul_agent.py
```

La primera ejecución puede pedir el código de inicio de sesión de Telegram y, si está activado, la contraseña de verificación en dos pasos.

## Flujo recomendado para aprender tu estilo

1. Inicia el agente.
2. Ejecuta `/soul_scan` para importar tus mensajes históricos.
3. Ejecuta `/soul_now` para generar o actualizar `Soul.md`.
4. Autoriza un grupo con `/soul_auth_chat` o un usuario con `/soul_auth_user`.
5. Activa respuestas con `/soul_resume`.
6. El agente usará el contexto de ese chat junto con tu personalidad global.

`Soul.md` se actualiza automáticamente según `refresh_interval_minutes`, por defecto cada 6 horas.

## Escaneo de historial

```text
/soul_scan                  todos los chats permitidos por la configuración
/soul_scan <chat_id>        un chat concreto
/soul_scan 123,456           varios chats
/soul_scan_groups            solo grupos y supergrupos
/soul_scan_private           solo chats privados
```

Configuración relevante:

```json
"scan": {
  "backfill_limit_per_chat": 200,
  "scan_groups": true,
  "scan_private": true,
  "scan_channels": false,
  "excluded_chat_ids": [],
  "report_progress_to_telegram": true,
  "context_refresh_limit": 50
}
```

`backfill_limit_per_chat` limita cuántos mensajes se recorren por chat en cada escaneo. El escaneo es deduplicado por `chat_id + message_id`, así que repetir `/soul_scan` no debería duplicar tus mensajes.

## Comandos

### Estado y aprendizaje

- `/soul_help` — ayuda completa.
- `/soul_status` — estado, autorización, límites y Soul.md.
- `/soul_stats` — estadísticas del almacén y chats principales.
- `/soul_now` o `/soul_refresh` — fuerza el refresh de `Soul.md`.
- `/soul_show` — muestra el perfil actual.
- `/soul_learn` — último resumen de lo aprendido.

### Respuestas

- `/soul_pause` — pausa respuestas, mantiene la captura.
- `/soul_resume` — reanuda respuestas.
- `/soul_set_mode mention` — responde si mencionan o responden al dueño.
- `/soul_set_mode always` — responde aleatoriamente en grupos autorizados según `prob_reply_in_always_mode`.

### Modelos de IA

- `/models` — consulta `GET /models` del endpoint configurado y muestra sus IDs.
- `/models <model_id>` — valida y activa directamente un modelo, por ejemplo `/models gemini-3.6-flash`.
- `/models config` — elimina la selección manual y vuelve al modelo por defecto del archivo.
- El modelo seleccionado queda guardado como `ai.selected_model` en `config.json` y se usa para chat y visión sin reiniciar.
- Para volver al valor de `config.json`, elimina `ai.selected_model` o usa el modelo definido en `ai.chat_model`.

### Autorización

- `/soul_auth_chat` — autoriza el grupo o privado actual.
- `/soul_unauth_chat` — revoca el chat actual.
- `/soul_auth_user <id>` — autoriza un usuario.
- `/soul_unauth_user <id>` — revoca un usuario.

### Historial y privacidad

- `/soul_exclude <chat_id>` — excluye un chat del escaneo.
- `/soul_unexclude <chat_id>` — elimina la exclusión.
- `/soul_excluded` — muestra exclusiones.
- `/soul_delete <chat_id>` — borra tus mensajes de un chat en la base local.
- `/soul_delete --confirm` — borra todos tus mensajes locales.
- `/soul_delete_analyzed` — borra mensajes tuyos ya incorporados al perfil.
- `/soul_delete_unanalyzed` — borra mensajes tuyos pendientes de análisis.

Borrar mensajes de la base no modifica automáticamente `Soul.md`; si quieres eliminar aprendizajes, regenera el archivo después de limpiar los datos.

## Estructura

```text
soul-agent/
├── soul_agent.py       # Cliente Telegram, handlers y ciclo principal
├── ai_client.py        # Cliente async OpenAI-compatible + visión
├── soul_manager.py     # Soul.md y memoria contextual por chat
├── responder.py        # Decisión, contexto y generación de respuestas
├── message_store.py    # SQLite WAL: mensajes y chat_context
├── auth_manager.py     # Autorizaciones persistentes
├── backfill.py         # Escaneo/deduplicación de historial
├── progress.py         # Progreso de consola y Telegram
├── config.example.json # Configuración de ejemplo
├── requirements.txt    # Dependencias
├── Soul.md             # Generado localmente, no se versiona
├── data/               # SQLite local, no se versiona
├── session/            # Sesión Telegram, no se versiona
└── logs/               # Logs, no se versiona
```

## Seguridad y privacidad

- `config.json`, `data/`, `session/`, `logs/` y `Soul.md` están ignorados por Git.
- Solo los mensajes necesarios para análisis/respuesta salen hacia el endpoint de IA.
- La memoria contextual evita enviar el historial completo: usa una ventana acotada y un resumen local.
- No guardes contraseñas, tokens o datos bancarios en mensajes que vayan a analizarse.
- Revisa y rota las credenciales si alguna vez compartes el directorio o el archivo ZIP.
- Usa exclusiones para chats sensibles antes de ejecutar un escaneo global.

## Desarrollo y verificación

Comprobar sintaxis sin iniciar Telegram:

```bash
python -m py_compile *.py
python -m json.tool config.example.json >/dev/null
```

Inspeccionar cambios:

```bash
git status
git diff --stat
git diff
```

## Limitaciones conocidas

- La calidad del clon depende de la cantidad y variedad de mensajes propios.
- El resumen por chat es aproximado y puede fallar si el proveedor de IA devuelve JSON inválido; en ese caso se conserva el resumen anterior.
- La respuesta puede tardar más cuando un chat necesita su primer resumen contextual.
- El contexto persistente se actualiza por ventanas y no reemplaza la lectura de los mensajes recientes.
- El escaneo de muchos chats puede tardar y consumir cuota de Telegram/IA.

## Licencia

MIT. Consulta `LICENSE`.
