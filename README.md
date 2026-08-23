<div align="center">

# 🧬 Soul Bot

### Tu clon de Telegram con personalidad propia

**Un agente de IA que aprende tu forma de escribir y responde como tú en Telegram.**

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-Chat-000000?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyIDJMMyAxMHYxMGgxOFYxMEwxMiAyeiIgZmlsbD0id2hpdGUiLz48L3N2Zz4=&logoColor=white)](https://deepseek.com)
[![Gemini](https://img.shields.io/badge/Gemini-Flash-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://gemini.google.com)
[![Pyrogram](https://img.shields.io/badge/Pyrogram-2.2-0088CC?style=for-the-badge&logo=telegram&logoColor=white)](https://docs.pyrogram.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=for-the-badge)](LICENSE)
[![Open Source](https://img.shields.io/badge/Open%20Source-❤️-green?style=for-the-badge)](LICENSE)

---

![Soul Bot Architecture](https://img.shields.io/badge/Architecture-Python%20%2B%20SQLite%20%2B%20Telegram%20API-blue?style=for-the-badge&logo=architecture&logoColor=white)

</div>

## ✨ ¿Qué hace Soul Bot?

Soul Bot es un **agente de cuenta de usuario** (no un bot de Telegram) que:

1. **Aprende** tu estilo de escritura analizando todos los mensajes que envías
2. **Genera** un `Soul.md` — tu perfil de personalidad digital
3. **Responde como tú** en grupos y chats privados que autorices
4. **Se actualiza solo** cada vez que acumula mensajes nuevos

> 💡 No es un chatbot genérico. Es **tu clon digital** que habla como tú, usa tus modismos, y mantiene tu tono.

---

## 🏗️ Arquitectura

```
┌─────────────────────────────────────────────────────┐
│                   TELEGRAM                          │
│              (Cuenta de usuario)                    │
└──────────────┬──────────────────────┬───────────────┘
               │                      │
               ▼                      ▼
┌──────────────────────┐  ┌───────────────────────────┐
│   soul_agent.py      │  │     backfill.py           │
│   (Pyrogram Client)  │  │   (Historical Scanner)    │
│   ┌──────────────┐   │  │   ┌───────────────────┐   │
│   │  Handlers    │   │  │   │ Dialog Iterator    │   │
│   │  Commands    │   │  │   │ Message Fetcher    │   │
│   │  Auto-Reply  │   │  │   │ Progress Reporter  │   │
│   └──────────────┘   │  │   └───────────────────┘   │
└──────────┬───────────┘  └───────────┬───────────────┘
           │                          │
           ▼                          ▼
┌─────────────────────────────────────────────────────┐
│              message_store.py                        │
│              (SQLite + WAL Mode)                     │
│   ┌──────────────────────────────────────────────┐  │
│   │ messages table: id, ts, chat_id, text, is_out│  │
│   │ analyzed_for_soul: 0 | 1                     │  │
│   └──────────────────────────────────────────────┘  │
└──────────┬──────────────────────────┬───────────────┘
           │                          │
           ▼                          ▼
┌──────────────────────┐  ┌───────────────────────────┐
│  soul_manager.py     │  │     responder.py           │
│  (AI Personality     │  │   (Auto-Reply Engine)      │
│   Generator)         │  │   ┌────────────────────┐   │
│  ┌────────────────┐  │  │   │ Context Builder    │   │
│  │ Soul.md Writer │  │  │   │ Decision Engine    │   │
│  │ Prompt Builder │  │  │   │ Reply Generator    │   │
│  └────────────────┘  │  │   └────────────────────┘   │
└──────────┬───────────┘  └───────────┬───────────────┘
           │                          │
           ▼                          ▼
┌─────────────────────────────────────────────────────┐
│                ai_client.py                          │
│          (OpenAI-Compatible API)                     │
│     DeepSeek · Gemini · GPT · Any Provider          │
└─────────────────────────────────────────────────────┘
```

---

## 🚀 Puesta en Marcha

### Requisitos

- Python 3.10+
- Cuenta de Telegram con API credentials ([my.telegram.org](https://my.telegram.org))
- API key de un proveedor de IA compatible con OpenAI (DeepSeek, Gemini, OpenAI, etc.)

### Instalación

```bash
# Clonar el repo
git clone https://github.com/Carlosdev-cod/Soul-Bot.git
cd Soul-Bot

# Crear entorno virtual
python3 -m venv venv
source venv/bin/activate

# Instalar dependencias
pip install -r requirements.txt

# Configurar credenciales
cp config.example.json config.json
# Edita config.json con tus credenciales
```

### Configuración

Edita `config.json`:

```json
{
  "telegram": {
    "api_id": 12345678,
    "api_hash": "tu-api-hash-de-telegram",
    "phone_number": "+5491155551234"
  },
  "ai": {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-tu-api-key",
    "chat_model": "deepseek-chat"
  },
  "auth": {
    "owner_user_id": 123456789,
    "authorized_group_ids": [-1001234567890]
  }
}
```

### Ejecutar

```bash
python soul_agent.py
```

La primera vez pedirá un código de confirmación de Telegram. Ábrelo en la app de Telegram y pégalo.

---

## 📋 Comandos

| Comando | Descripción |
|---|---|
| `/soul_help` | Lista completa de comandos |
| `/soul_status` | Estado del agente y métricas |
| `/soul_now` | Refrescar Soul.md ahora |
| `/soul_show` | Ver el Soul.md actual |
| `/soul_pause` | Pausar auto-respuestas |
| `/soul_resume` | Reanudar auto-respuestas |
| `/soul_scan` | Escanear TODO el historial |
| `/soul_scan <id>` | Escanear un chat específico |
| `/soul_stats` | Estadísticas del almacén |
| `/soul_delete` | Opciones para borrar mensajes |
| `/soul_delete <chat_id>` | Borrar mensajes de un chat |
| `/soul_delete --confirm` | Borrar todos tus mensajes |
| `/soul_auth_chat` | Autorizar chat actual |
| `/soul_set_mode mention` | Responder solo si te mencionan |
| `/soul_set_mode always` | Responder a todos |

---

## 🧠 ¿Cómo Aprende?

```
Tus mensajes → message_store.py → SQLite
                                        ↓
                              soul_manager.py (cada 6h)
                                        ↓
                              ┌─────────────────────┐
                              │  DeepSeek / Gemini   │
                              │  analiza tu estilo   │
                              └──────────┬──────────┘
                                         ↓
                                    Soul.md
                              (tu personalidad digital)
```

1. **Captura**: Cada mensaje que envías se almacena en SQLite
2. **Análisis**: Cada 6 horas, la IA analiza tus mensajes nuevos
3. **Generación**: Actualiza `Soul.md` con tu patrón de escritura
4. **Respuesta**: Cuando alguien te escribe, usa tu Soul.md como contexto

---

## 🛡️ Seguridad

- `config.json` con API keys **nunca** se sube al repo (`.gitignore`)
- Solo responde en chats que **tú autorices**
- Rate limiting integrado (máx 8 respuestas/minuto)
- No responde a bots ni comandos
- Soul.md es un archivo local, no se comparte

---

## 📁 Estructura

```
Soul-Bot/
├── soul_agent.py         # Cliente Pyrogram + handlers
├── ai_client.py          # Wrapper de API compatible con OpenAI
├── soul_manager.py       # Generación y refresh de Soul.md
├── responder.py          # Motor de auto-respuestas
├── message_store.py      # SQLite async para mensajes
├── auth_manager.py       # Gestión de autorizaciones
├── backfill.py           # Scanner de historial
├── progress.py           # Barras de progreso
├── config.example.json   # Plantilla de configuración
├── requirements.txt      # Dependencias Python
├── LICENSE               # MIT License
└── README.md             # Este archivo
```

---

## 🤝 Contribuir

Las contribuciones son bienvenidas. Abre un [issue](https://github.com/Carlosdev-cod/Soul-Bot/issues) o un [pull request](https://github.com/Carlosdev-cod/Soul-Bot/pulls).

1. Haz fork del repo
2. Crea una branch (`git checkout -b feature/nueva-funcion`)
3. Commit (`git commit -m 'Agregar nueva función'`)
4. Push (`git push origin feature/nueva-funcion`)
5. Abre un Pull Request

---

## 📄 Licencia

Este proyecto está bajo la licencia MIT. Ver [LICENSE](LICENSE) para detalles.

---

## ⚠️ Disclaimer

Usar una cuenta de usuario de Telegram para automatizar respuestas es responsabilidad del usuario. Respeta los Términos de Servicio de Telegram y las reglas de los chats donde uses Soul Bot.

---

<div align="center">

**Hecho con ❤️ y Python**

[![Python](https://img.shields.io/badge/Python-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Telegram](https://img.shields.io/badge/Telegram-26A5E4?style=flat-square&logo=telegram&logoColor=white)](https://telegram.org)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-000000?style=flat-square&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZD0iTTEyIDJMMyAxMHYxMGgxOFYxMEwxMiAyeiIgZmlsbD0id2hpdGUiLz48L3N2Zz4=&logoColor=white)](https://deepseek.com)
[![SQLite](https://img.shields.io/badge/SQLite-003B57?style=flat-square&logo=sqlite&logoColor=white)](https://sqlite.org)

</div>
