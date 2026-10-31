"""binding：身份指令族与回执文案。"""
from __future__ import annotations

import pytest

import qqlife.platform.identity as identity_mod
from qqlife.binding import BindingCommands, persona_label


class FakeOutbound:
    def __init__(self) -> None:
        self.probed: list[tuple[str, str]] = []

    def output_channels(self):
        return ["feishu", "bark"]

    async def probe(self, channel, remote_id, text=""):
        self.probed.append((channel, remote_id))
        return True


class FakeRegistry:
    def ensure(self, uid):
        return {"store": type("S", (), {"items": []})(), "persona_path": ""}


@pytest.fixture
def binder(tmp_path, monkeypatch):
    monkeypatch.setattr(identity_mod, "LEGACY_BINDINGS", tmp_path / "bindings.json")
    identity = identity_mod.Identity(users_root=tmp_path / "data")
    bundles = {"feishu": object(), "bark": type("B", (), {"output": object()})()}
    return BindingCommands(identity=identity, bundles=bundles,
                           outbound=FakeOutbound(), registry=FakeRegistry())


async def test_non_command_is_ignored(binder):
    assert await binder.handle("feishu", "u1", None, "你好") is None


async def test_create_creates_identity_and_binds(binder):
    reply = await binder.handle("feishu", "u1", None, ".create alice")
    assert "已创建身份并绑定" in reply
    assert "默认（内置 ROLE）" in reply
    assert "在办提醒 0 条" in reply
    assert "下一步：.persona" in reply


async def test_create_existing_is_rejected(binder):
    await binder.handle("feishu", "u1", None, ".create alice")
    reply = await binder.handle("feishu", "u2", None, ".create alice")
    assert "已存在" in reply


async def test_link_unknown_requires_create(binder):
    reply = await binder.handle("feishu", "u1", None, ".link alice")
    assert "不存在" in reply
    assert ".create" in reply


async def test_link_existing_identity(binder):
    await binder.handle("feishu", "u1", None, ".create alice")
    reply = await binder.handle("feishu", "u2", None, ".link alice")
    assert "已绑定" in reply


async def test_profile_shows_default_persona(binder):
    await binder.handle("feishu", "u1", None, ".create alice")
    reply = await binder.handle("feishu", "u1", "alice", ".profile")
    assert "当前身份：alice" in reply
    assert "人设：默认（内置 ROLE）" in reply


async def test_link_outbound_endpoint_and_confirm(binder):
    await binder.handle("feishu", "u1", None, ".create alice")
    reply = await binder.handle("feishu", "u1", "alice", ".link bark token")
    assert "测试消息已发出" in reply
    assert "已确认 1 个端点验证通过" in await binder.handle(
        "feishu", "u1", "alice", ".link ok")


async def test_create_confirm_when_already_bound(binder):
    await binder.handle("feishu", "u1", None, ".create alice")
    reply = await binder.handle("feishu", "u1", "alice", ".create bob")
    assert "确认新建并切到 bob" in reply
    reply = await binder.handle("feishu", "u1", "alice", ".create bob confirm")
    assert "已创建身份并绑定" in reply


async def test_old_command_names_are_not_handled(binder):
    for old in (".bind alice", ".whoami", ".unbind", ".idnew bob"):
        assert await binder.handle("feishu", "u1", None, old) is None


async def test_persona_label():
    assert persona_label("/tmp/alice/persona.md") == "persona.md"
    assert persona_label("") == "默认（内置 ROLE）"
