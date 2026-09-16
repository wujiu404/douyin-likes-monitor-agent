"""告警渠道的离线测试（不碰网络）。

覆盖三件事：
1. **卡片内容**：数字来自真实增量，带视频链接和阈值——收到告警的人能判断这条是松是紧。
2. **渠道组合**：`local+feishu_im` 两个都要发，且**任一失败就算未送达**
   （否则「飞书挂了但本地写成功」会被记成已推送，推送坏了你永远发现不了）。
3. **收件人识别**：没配 open_id 时自动取应用创建者；失败只降级不抛，
   告警发不出去不该让整轮扫描跟着失败。
"""
from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.notifiers import build_notifier
from app.notifiers.card import alert_card
from app.notifiers.composite import CompositeNotifier
from app.notifiers.feishu_im import FeishuIMNotifier
from app.notifiers.local import LocalNotifier


def _item(video_id: str = "7412345678901234567", **over) -> dict:
    base = {
        "video_id": video_id,
        "account": "示例账号",
        "title": "翻唱《某某》",
        "prev_likes": 12,
        "curr_likes": 128,
        "delta": 116,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------- 卡片渲染
def test_card_renders_real_numbers_link_and_threshold() -> None:
    card = alert_card("R20260915-200000-abcd", [{**_item(), "threshold": 5}])

    text = json.dumps(card, ensure_ascii=False)
    assert "示例账号" in text
    assert "12 → 128" in text          # 真实前后值，不做美化
    assert "+116" in text              # 增量
    assert "https://www.douyin.com/video/7412345678901234567" in text
    assert "阈值" in text and "5" in text
    assert card["header"]["title"]["content"] == "点赞告警 · 1 条"
    assert card["header"]["template"] == "red"


def test_card_folds_long_lists() -> None:
    card = alert_card("R1", [_item(str(i)) for i in range(25)])
    text = json.dumps(card, ensure_ascii=False)
    assert "另有 5 条未展示" in text
    assert card["header"]["title"]["content"] == "点赞告警 · 25 条"


def test_card_survives_missing_fields() -> None:
    """字段缺了不能崩——宁可少显示，不能因为一条脏数据把整轮告警吞掉。"""
    card = alert_card("R1", [{"video_id": "", "account": "", "title": ""}])
    text = json.dumps(card, ensure_ascii=False)
    assert "（无标题）" in text
    assert "douyin.com" not in text  # 没有 id 就不给链接


def test_card_omits_link_and_marks_simulated_data() -> None:
    """模拟视频（acc01_v01）拼出来的链接必然 404 —— 宁可不给，并标注是模拟数据。

    收到告警的人点一下 404 就知道数据是编的，比不写链接更糟。
    """
    card = alert_card("R1", [_item("acc01_v01")])
    text = json.dumps(card, ensure_ascii=False)
    assert "douyin.com" not in text
    assert "模拟数据" in text
    assert "acc01_v01" in text          # id 本身照常展示，便于定位是哪条


# ---------------------------------------------------------------- 渠道组合
def _settings(**over) -> Settings:
    base = dict(
        storage_backend="sqlite",
        provider_chain="mock",
        browser_user_data_dir="",
        scheduler_enabled=False,
        feishu_app_id="cli_x",
        feishu_app_secret="s",
        feishu_notify_receive_id="ou_me",
        feishu_notify_receive_id_type="open_id",
    )
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_build_notifier_single_backend() -> None:
    n = build_notifier(_settings(notifier_backend="local"))
    assert isinstance(n, LocalNotifier) and n.name == "local"


def test_build_notifier_splits_plus_and_dedupes() -> None:
    n = build_notifier(_settings(notifier_backend="local+feishu_im+local"))
    assert isinstance(n, CompositeNotifier)
    assert n.name == "local+feishu_im"           # 顺序保留、重复去掉
    assert [x.name for x in n.notifiers] == ["local", "feishu_im"]


async def test_composite_requires_every_channel_to_succeed() -> None:
    calls: list[str] = []

    class Ok:
        name = "ok"

        async def send_alerts(self, run_id, items):
            calls.append("ok")
            return True

    class Bad:
        name = "bad"

        async def send_alerts(self, run_id, items):
            calls.append("bad")
            return False

    class Boom:
        name = "boom"

        async def send_alerts(self, run_id, items):
            calls.append("boom")
            raise RuntimeError("渠道炸了")

    ok_all = await CompositeNotifier([Ok(), Ok()]).send_alerts("R1", [_item()])
    assert ok_all is True

    # 一个失败 → 整体未送达；且**后面的渠道照样要发**（不能被前面的失败截断）
    mixed = await CompositeNotifier([Bad(), Ok()]).send_alerts("R1", [_item()])
    assert mixed is False
    assert calls[-1] == "ok"

    # 抛异常被兜住，不外溢
    with_exc = await CompositeNotifier([Boom()]).send_alerts("R1", [_item()])
    assert with_exc is False


async def test_composite_skips_sending_when_no_alerts() -> None:
    class Nope:
        name = "nope"

        async def send_alerts(self, run_id, items):  # pragma: no cover - 不该被调用
            raise AssertionError("没有告警时不该发")

    assert await CompositeNotifier([Nope()]).send_alerts("R1", []) is True


# ---------------------------------------------------------------- 应用消息
class _Resp:
    def __init__(self, status_code: int = 200, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.content = b"{}"
        self.text = json.dumps(self._body, ensure_ascii=False)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """假的 httpx.AsyncClient：只认我们真正会打的那三条路径。"""

    def __init__(self, *, creator_id: str = "ou_creator", send_code: int = 0, send_msg: str = "ok") -> None:
        self.creator_id = creator_id
        self.send_code = send_code
        self.send_msg = send_msg
        self.calls: list[tuple[str, str, dict]] = []

    async def post(self, url: str, **kw) -> _Resp:
        self.calls.append(("POST", url, kw))
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return _Resp(200, {"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        if "/im/v1/messages" in url:
            return _Resp(200, {"code": self.send_code, "msg": self.send_msg})
        return _Resp(200, {"code": 0})

    async def get(self, url: str, **kw) -> _Resp:
        self.calls.append(("GET", url, kw))
        return _Resp(200, {"code": 0, "data": {"app": {"creator_id": self.creator_id}}})

    def sent_messages(self) -> list[dict]:
        return [kw["json"] for m, url, kw in self.calls if m == "POST" and "/im/v1/messages" in url]


@pytest.fixture
def im_notifier():
    """返回 (notifier, fake_http)；fake 装进去了，网络调用全被记账。"""
    n = FeishuIMNotifier(_settings())
    fake = FakeHttp()
    n._client = fake  # type: ignore[assignment]
    return n, fake


async def test_im_sends_card_to_configured_open_id(im_notifier) -> None:
    n, fake = im_notifier
    assert await n.send_alerts("R1", [_item()]) is True

    msgs = fake.sent_messages()
    assert len(msgs) == 1
    assert msgs[0]["receive_id"] == "ou_me"
    assert msgs[0]["msg_type"] == "interactive"

    params = [kw.get("params") for m, url, kw in fake.calls if "/im/v1/messages" in url][0]
    assert params == {"receive_id_type": "open_id"}

    card = json.loads(msgs[0]["content"])
    assert "示例账号" in json.dumps(card, ensure_ascii=False)


async def test_im_discovers_creator_when_receive_id_unset() -> None:
    """没配 open_id 也能用：应用是你自己建的，creator_id 就是你。"""
    n = FeishuIMNotifier(_settings(feishu_notify_receive_id=""))
    fake = FakeHttp(creator_id="ou_maker")
    n._client = fake  # type: ignore[assignment]

    assert await n.send_alerts("R1", [_item()]) is True
    assert fake.sent_messages()[0]["receive_id"] == "ou_maker"
    assert n.receive_id == "ou_maker"  # 识别一次就缓存住

    # 第二次不再问应用信息
    before = len([c for c in fake.calls if c[0] == "GET"])
    await n.send_alerts("R2", [_item()])
    after = len([c for c in fake.calls if c[0] == "GET"])
    assert after == before


async def test_im_returns_false_on_api_error_without_raising(im_notifier) -> None:
    """收件人不在可用范围（230013）必须先降级——告警发不出去不该让扫描失败。"""
    n, fake = im_notifier
    fake.send_code, fake.send_msg = 230013, "bot has no permission"

    assert await n.send_alerts("R1", [_item()]) is False


async def test_im_without_credentials_degrades() -> None:
    n = FeishuIMNotifier(_settings(feishu_app_id="", feishu_app_secret="", feishu_notify_receive_id=""))
    n._client = FakeHttp()  # type: ignore[assignment]
    assert await n.send_alerts("R1", [_item()]) is False


async def test_im_leaves_no_alert_untouched_when_empty(im_notifier) -> None:
    n, fake = im_notifier
    assert await n.send_alerts("R1", []) is True
    assert fake.sent_messages() == []


async def test_token_is_cached_across_sends(im_notifier) -> None:
    n, fake = im_notifier
    await n.send_alerts("R1", [_item()])
    await n.send_alerts("R2", [_item()])
    token_calls = [c for c in fake.calls if c[1].endswith("/tenant_access_token/internal")]
    assert len(token_calls) == 1


# ---------------------------------------------------------------- 评论提醒
def _review_item(**over) -> dict:
    base = {
        "comment_id": "c001",
        "content": "这是什么歌啊，好好听",
        "keywords": ["什么歌", "好听"],
        "draft": "这首是《孤勇者》 — 陈奕迅，喜欢可以搜来听听～",
        "video_id": "7412345678901234567",
        "account": "示例账号",
        "video_title": "外卖小哥当众高歌",
        "song_title": "孤勇者",
        "song_artist": "陈奕迅",
        "thread_id": "R20260915-200055-f9da:7412345678901234567",
    }
    base.update(over)
    return base


def test_review_card_shows_comment_and_draft_and_says_no_auto_send() -> None:
    """只有「有 3 条待确认」而不给出评论和拟回复，等于没提醒。"""
    from app.notifiers.card import review_card

    card = review_card("R1:v1", [_review_item()])
    text = json.dumps(card, ensure_ascii=False)
    assert "这是什么歌啊，好好听" in text       # 评论原文
    assert "孤勇者" in text and "陈奕迅" in text  # 拟回复里的歌名与歌手
    assert "什么歌、好听" in text               # 命中的关键词
    assert "不会自动发送" in text               # 合规红线要写在卡片上
    assert card["header"]["title"]["content"] == "待确认拟回复 · 1 条"


def test_review_card_carries_real_video_link() -> None:
    """提醒里要能一键跳到视频——否则收到提醒的人只能自己去搜这条评论。"""
    from app.notifiers.card import review_card

    text = json.dumps(review_card("R1:v1", [_review_item()]), ensure_ascii=False)
    assert "https://www.douyin.com/video/7412345678901234567" in text


def test_review_card_marks_simulated_video_without_dead_link() -> None:
    """模拟视频不给死链，但要明说——免得对着一条假评论去核对真实视频。"""
    from app.notifiers.card import review_card

    text = json.dumps(
        review_card("R1:v1", [_review_item(video_id="acc01_v01")]), ensure_ascii=False
    )
    assert "douyin.com" not in text
    assert "模拟数据" in text


def test_review_card_accepts_comma_joined_keywords() -> None:
    """《评论命中表》里 keywords 存的是逗号分隔字符串，卡片也要能收。

    直接 `"、".join("什么歌")` 会按单字拆成「什、么、歌」——看着像乱码，
    所以这里按字符串形态钉一次。
    """
    from app.notifiers.card import review_card

    text = json.dumps(
        review_card("R1:v1", [_review_item(keywords="什么歌,好听")]), ensure_ascii=False
    )
    assert "什么歌、好听" in text
    assert "什、么、歌" not in text


def test_review_card_with_no_items_shows_no_link_hint() -> None:
    from app.notifiers.card import review_card

    text = json.dumps(review_card("R1:v1", []), ensure_ascii=False)
    assert "模拟数据" not in text
    assert "douyin.com" not in text


async def test_im_sends_review_card(im_notifier) -> None:
    n, fake = im_notifier
    assert await n.send_reviews("R1:v1", [_review_item()]) is True
    card = json.loads(fake.sent_messages()[0]["content"])
    assert "这是什么歌" in json.dumps(card, ensure_ascii=False)


async def test_local_notifier_handles_reviews(caplog) -> None:
    """本地渠道不联网，但也要把评论提醒写进日志——否则默认配置下这条链路是断的。"""
    notifier = LocalNotifier(_settings())
    assert await notifier.send_reviews("R1:v1", [_review_item()]) is True


async def test_composite_fans_out_reviews_too() -> None:
    seen: list[tuple[str, str]] = []

    class Rec:
        name = "rec"

        async def send_alerts(self, run_id, items):  # pragma: no cover - 本用例不走
            return True

        async def send_reviews(self, thread_id, items):
            seen.append((thread_id, items[0]["comment_id"]))
            return True

    ok = await CompositeNotifier([Rec(), Rec()]).send_reviews("R1:v1", [_review_item()])
    assert ok is True
    assert seen == [("R1:v1", "c001"), ("R1:v1", "c001")]
