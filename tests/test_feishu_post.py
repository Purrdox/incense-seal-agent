"""飞书富文本（msg_type=post）解析：编号列表被客户端转成 post 后不能丢消息。"""
from __future__ import annotations

import json

from qqlife.platform.feishu import post_parts


def _post(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False)


def test_ordered_list_lines():
    text, keys = post_parts(_post({
        "title": "",
        "content": [
            [{"tag": "text", "text": "1. 不用，网络协议的组织形式比较松散"}],
            [{"tag": "text", "text": "2. 这几个暂时不用管"},
             {"tag": "a", "text": "链接", "href": "http://example.com"}],
        ],
    }))
    assert text == "1. 不用，网络协议的组织形式比较松散\n2. 这几个暂时不用管链接"
    assert keys == []


def test_locale_wrapper_title_and_image():
    text, keys = post_parts(_post({
        "zh_cn": {"title": "标题", "content": [
            [{"tag": "text", "text": "正文"}],
            [{"tag": "img", "image_key": "img_1"}],
        ]},
    }))
    assert text == "标题\n正文"
    assert keys == ["img_1"]


def test_at_and_ignored_tags():
    text, keys = post_parts(_post({
        "title": "",
        "content": [
            [{"tag": "at", "user_name": "张三"}, {"tag": "text", "text": " 请你看一下"}],
            [{"tag": "hr"}, {"tag": "emotion", "emoji_type": "SMILE"}],
        ],
    }))
    assert text == "@张三 请你看一下"
    assert keys == []


def test_broken_or_empty_content():
    assert post_parts("not json") == ("", [])
    assert post_parts("") == ("", [])
    assert post_parts(_post({"title": "", "content": []})) == ("", [])
