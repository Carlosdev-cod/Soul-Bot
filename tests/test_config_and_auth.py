"""
Tests de configuración: config_store + AuthManager.

Verifica que las escrituras de config.json nunca pierdan claves escritas
por otros módulos (bug original: AuthManager pisaba selected_model,
excluded_chat_ids y group_reply_mode).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import config_store
from auth_manager import AuthManager


@pytest.fixture
def cfg_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "telegram": {"api_id": 1, "api_hash": "x", "phone_number": "+1",
                     "session_name": "s"},
        "ai": {"base_url": "http://x", "api_key": "k", "chat_model": "m1"},
        "auth": {"owner_user_id": 0, "authorized_group_ids": [],
                 "authorized_user_ids": []},
    }), encoding="utf-8")
    return str(path)


def test_read_config_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="config.example.json"):
        config_store.read_config(tmp_path / "nope.json")


def test_update_config_merges(cfg_path):
    def mutate(cfg):
        cfg.setdefault("scan", {})["excluded_chat_ids"] = [-100]
    config_store.update_config(cfg_path, mutate)
    data = json.loads(open(cfg_path).read())
    assert data["scan"]["excluded_chat_ids"] == [-100]
    assert data["ai"]["chat_model"] == "m1"  # no se pierde nada


def test_update_config_returns_fresh(cfg_path):
    out = config_store.update_config(cfg_path, lambda c: c.setdefault("x", 1))
    assert out["x"] == 1 and out["ai"]["api_key"] == "k"


def test_atomic_write_leaves_no_temp(cfg_path):
    cfg = json.loads(open(cfg_path).read())
    cfg["nuevo"] = True
    config_store.atomic_write(cfg_path, cfg)
    files = [f.name for f in Path(cfg_path).parent.iterdir()]
    assert files == ["config.json"], f"sobró un temporal: {files}"


def test_authmanager_does_not_lose_other_keys(cfg_path):
    """El bug original: AuthManager.save() reescribía con copia stale."""
    auth = AuthManager(cfg_path)
    # Otro escritor (p. ej. /models y /soul_exclude) actualiza el archivo
    config_store.update_config(cfg_path, lambda c: c["ai"].__setitem__(
        "selected_model", "gemini-flash"))
    config_store.update_config(cfg_path, lambda c: c.__setitem__(
        "scan", {"excluded_chat_ids": [-1001234]}))
    # AuthManager persiste un cambio de autorización
    assert auth.authorize_user(999) is True
    data = json.loads(open(cfg_path).read())
    assert data["ai"]["selected_model"] == "gemini-flash"
    assert data["scan"]["excluded_chat_ids"] == [-1001234]
    assert 999 in data["auth"]["authorized_user_ids"]


def test_authmanager_owner_and_revokes(cfg_path):
    auth = AuthManager(cfg_path)
    assert auth.is_owner(1) is False  # owner aún en 0
    auth.set_owner(42)
    assert auth.is_owner(42)
    auth.authorize_group(-100)
    assert auth.is_group_authorized(-100)
    assert auth.authorize_group(-100) is False  # ya estaba
    assert auth.revoke_group(-100) is True
    assert auth.revoke_group(-100) is False
    # persistido
    auth2 = AuthManager(cfg_path)
    assert auth2.owner_id == 42
    assert auth2.is_group_authorized(-100) is False  # fue revocado
    assert auth2.is_owner(42) and not auth2.is_owner(43)
