"""Identity：创建/绑定/改绑/解绑、单向端点验证、持久化与坏文件容错。"""
from __future__ import annotations

import json

import pytest

import qqlife.platform.identity as identity_mod


@pytest.fixture
def ident(tmp_path, monkeypatch):
    # 隔离旧全局 bindings.json，避免测试读到真实 data/ 并触发改名迁移
    monkeypatch.setattr(identity_mod, "LEGACY_BINDINGS", tmp_path / "bindings.json")
    return identity_mod.Identity(users_root=tmp_path / "data")


def test_create_and_bind_creates_identity_and_index(ident, tmp_path):
    status, _ = ident.create_and_bind("feishu", "u1", "alice")
    assert status == "ok"
    assert ident.resolve("feishu", "u1") == "alice"
    assert ident.uid_exists("alice")
    assert (tmp_path / "data" / "alice" / "uid.json").exists()


def test_create_profile_only(ident):
    status, msg = ident.create("alice")
    assert status == "ok"
    assert "未绑定端点" in msg
    assert ident.uid_exists("alice")
    assert ident.endpoints("alice") == []


def test_create_existing_rejected(ident):
    ident.create("alice")
    status, msg = ident.create_and_bind("feishu", "u1", "alice")
    assert status == "error"
    assert "已存在" in msg


def test_link_requires_existing_identity(ident):
    status, msg = ident.bind("feishu", "u1", "alice")
    assert status == "error"
    assert ".create" in msg
    assert not ident.uid_exists("alice")


def test_link_existing_identity(ident):
    ident.create("alice")
    status, _ = ident.bind("feishu", "u1", "alice")
    assert status == "ok"
    assert ident.resolve("feishu", "u1") == "alice"


def test_link_same_endpoint_is_idempotent(ident):
    ident.create("alice")
    ident.bind("feishu", "u1", "alice")
    status, msg = ident.bind("feishu", "u1", "alice")
    assert status == "ok"
    assert "无需重复绑定" in msg
    assert len(ident.endpoints("alice")) == 1


def test_rebind_requires_confirm_and_force_moves_endpoint(ident):
    ident.create("alice")
    ident.create("bob")
    ident.bind("feishu", "u1", "alice")
    status, _ = ident.bind("feishu", "u1", "bob")
    assert status == "confirm"
    assert ident.resolve("feishu", "u1") == "alice"

    status, _ = ident.bind("feishu", "u1", "bob", force=True)
    assert status == "ok"
    assert ident.resolve("feishu", "u1") == "bob"
    assert ident.endpoints("alice") == []
    assert len(ident.endpoints("bob")) == 1


def test_create_requires_confirm_when_endpoint_bound(ident):
    ident.create_and_bind("feishu", "u1", "alice")
    status, _ = ident.create_and_bind("feishu", "u1", "bob")
    assert status == "confirm"
    assert ident.resolve("feishu", "u1") == "alice"

    status, _ = ident.create_and_bind("feishu", "u1", "bob", force=True)
    assert status == "ok"
    assert ident.resolve("feishu", "u1") == "bob"
    assert ident.endpoints("alice") == []
    assert len(ident.endpoints("bob")) == 1


def test_invalid_uid_rejected(ident):
    for bad in ("Alice", "1abc", "", "a" * 17):
        status, _ = ident.bind("feishu", "u1", bad)
        assert status == "error"
        status, _ = ident.create_and_bind("feishu", "u1", bad)
        assert status == "error"


def test_unidirectional_channel_rejected(ident):
    status, _ = ident.bind("bark", "token", "alice")
    assert status == "error"
    status, _ = ident.create_and_bind("bark", "token", "alice")
    assert status == "error"


def test_outbound_endpoint_needs_confirmation(ident):
    ident.create_and_bind("feishu", "u1", "alice")
    status, _ = ident.bind_endpoint("alice", "bark", "token")
    assert status == "ok"
    verified = {e["remote_id"] for e in ident.endpoints("alice", verified_only=True)}
    assert verified == {"u1"}   # 单向端点待确认，不参与出站广播

    assert ident.confirm_pending("alice") == 1
    verified = {e["remote_id"] for e in ident.endpoints("alice", verified_only=True)}
    assert verified == {"u1", "token"}


def test_unbind_removes_index_and_endpoint(ident):
    ident.create_and_bind("feishu", "u1", "alice")
    ok, _ = ident.unbind("feishu", "u1")
    assert ok is True
    assert ident.resolve("feishu", "u1") is None
    assert ident.endpoints("alice") == []
    assert ident.unbind("feishu", "u1") == (False, "当前端点未绑定任何身份。")


def test_persistence_across_instances(ident, tmp_path, monkeypatch):
    ident.create_and_bind("feishu", "u1", "alice")
    monkeypatch.setattr(identity_mod, "LEGACY_BINDINGS", tmp_path / "bindings.json")
    again = identity_mod.Identity(users_root=tmp_path / "data")
    assert again.resolve("feishu", "u1") == "alice"
    assert again.uids() == ["alice"]


def test_corrupt_uid_json_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(identity_mod, "LEGACY_BINDINGS", tmp_path / "bindings.json")
    home = tmp_path / "data" / "alice"
    home.mkdir(parents=True)
    (home / "uid.json").write_text("{broken", encoding="utf-8")
    good = tmp_path / "data" / "bob"
    good.mkdir(parents=True)
    (good / "uid.json").write_text(json.dumps({
        "created": "2026-10-18", "endpoints": [{"channel": "feishu", "remote_id": "u2"}],
    }), encoding="utf-8")

    ident = identity_mod.Identity(users_root=tmp_path / "data")
    assert ident.uids() == ["bob"]
    assert ident.resolve("feishu", "u2") == "bob"
