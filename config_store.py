"""
config_store.py
===============
Lectura y escritura centralizada de config.json.

Problema que resuelve: antes, AuthManager y SoulAgent mantenían cada uno su
propia copia del archivo y lo reescribían por completo. Si el dueño ejecutaba
`/models <modelo>` y luego `/soul_auth_user <id>`, la segunda escritura
sobreescribía la primera con una copia desactualizada y la selección del
modelo se perdía silenciosamente.

Solución: toda escritura pasa por `update_config()`, que hace
leer-modificar-escribir bajo un lock de proceso y con reemplazo atómico
(archivo temporal + os.replace), de modo que:
  - Nunca se pierden claves escritas por otro módulo.
  - Un corte de energía a mitad de escritura no corrompe el archivo.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger("soul.config")

# Lock de proceso: serializa leer-modificar-escribir incluso entre hilos.
_lock = threading.RLock()


def read_config(path: str | Path) -> dict:
    """Lee config.json. Lanza FileNotFoundError con mensaje claro si falta."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"No se encontró {path}. Cópialo desde config.example.json y "
            "completa tus credenciales:  cp config.example.json config.json"
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write(path: str | Path, cfg: dict) -> None:
    """Escribe config.json de forma atómica (temporal + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp",
                               prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def update_config(path: str | Path, mutator: Callable[[dict], None]) -> dict:
    """Lee-modifica-escribe config.json de forma segura.

    `mutator` recibe el dict completo y lo modifica IN PLACE (p. ej.
    `cfg["ai"]["selected_model"] = x`). Devuelve la configuración fresca
    que quedó en disco, para que el llamador actualice su copia en memoria.
    """
    with _lock:
        cfg = read_config(path)
        mutator(cfg)
        atomic_write(path, cfg)
        return cfg
