"""
auth_manager.py
==============
Maneja las listas de chats/grupos y usuarios privados autorizados, así como
el user_id del dueño. La persistencia se hace en config.json (sección 'auth').
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("soul.auth")


class AuthManager:
    def __init__(self, config_path: str):
        self.config_path = str(config_path)
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        with open(self.config_path, "r", encoding="utf-8") as f:
            self.cfg = json.load(f)
        a = self.cfg.get("auth", {})
        self.owner_id = int(a.get("owner_user_id") or 0)
        self.group_ids: set[int] = set(int(x) for x in a.get("authorized_group_ids", []))
        self.user_ids: set[int] = set(int(x) for x in a.get("authorized_user_ids", []))

    def save(self) -> None:
        with self._lock:
            self.cfg["auth"] = {
                "owner_user_id": self.owner_id,
                "authorized_group_ids": sorted(self.group_ids),
                "authorized_user_ids": sorted(self.user_ids),
            }
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2, ensure_ascii=False)
            log.info("Auth config persisted: groups=%s users=%s owner=%s",
                     len(self.group_ids), len(self.user_ids), self.owner_id)

    # ---------- owner
    def set_owner(self, owner_id: int) -> None:
        with self._lock:
            self.owner_id = int(owner_id)
            self.save()

    def is_owner(self, user_id: int) -> bool:
        return self.owner_id == int(user_id) and self.owner_id != 0

    # ---------- groups
    def authorize_group(self, chat_id: int) -> bool:
        with self._lock:
            if chat_id in self.group_ids:
                return False
            self.group_ids.add(int(chat_id))
            self.save()
            return True

    def revoke_group(self, chat_id: int) -> bool:
        with self._lock:
            if chat_id not in self.group_ids:
                return False
            self.group_ids.discard(int(chat_id))
            self.save()
            return True

    def is_group_authorized(self, chat_id: int) -> bool:
        return int(chat_id) in self.group_ids

    # ---------- users
    def authorize_user(self, user_id: int) -> bool:
        with self._lock:
            if user_id in self.user_ids:
                return False
            self.user_ids.add(int(user_id))
            self.save()
            return True

    def revoke_user(self, user_id: int) -> bool:
        with self._lock:
            if user_id not in self.user_ids:
                return False
            self.user_ids.discard(int(user_id))
            self.save()
            return True

    def is_user_authorized(self, user_id: int) -> bool:
        return int(user_id) in self.user_ids

    # ---------- snapshots
    def snapshot(self) -> dict:
        return {
            "owner_user_id": self.owner_id,
            "authorized_group_ids": sorted(self.group_ids),
            "authorized_user_ids": sorted(self.user_ids),
        }
