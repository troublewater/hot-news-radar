#!/usr/bin/env python3
# coding=utf-8
"""把生成的 X 卡片（文案 + 图片）推到飞书群机器人。

为什么不复用项目自带的推送框架：那套有分批、多账号、报告渲染，
对「一张卡片 + 一段文案」是过度设计。这里直连 webhook。

定时任务什么时候真的跑起来很不确定，所以时段放得很宽（早 05:00-13:00、
晚 17:00-次日01:00），再靠「当天这个时段已经推过就跳过」防止重复。
--force 可忽略这两层判断。

用法：
    FEISHU_WEBHOOK_URL=xxx python3 scripts/notify_x_card.py
    python3 scripts/notify_x_card.py --force
    python3 scripts/notify_x_card.py --selftest
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CN_TZ = timezone(timedelta(hours=8))
# 为什么不像以前那样把窗口钉在半小时里：GitHub 的 cron 有 0~30 分钟延迟，
# 爬虫本身还要跑十来分钟，两者一叠加就跑偏。窗口卡得越准，整天漏推的概率越高。
# 所以改成上下午各一大段，重复推送交给下面那个记录文件去拦。
SLOTS = [("am", 5 * 60, 13 * 60), ("pm", 17 * 60, 25 * 60)]
DEFAULT_STATE = ".x-push-state"


def slot_of(now):
    """当前落在哪一段（am/pm），不在任何一段返回空串。"""
    minute = now.hour * 60 + now.minute
    for name, start, end in SLOTS:
        if start <= minute < end:
            return name
    return ""


def in_window(now):
    return bool(slot_of(now))


def pushed_before(state_path, now, slot):
    """今天这一段推过没有。记录文件丢了就当没推过——多推一条总比漏推一天强。"""
    try:
        return Path(state_path).read_text(encoding="utf-8").strip() == f"{now:%Y-%m-%d} {slot}"
    except OSError:
        return False


def mark_pushed(state_path, now, slot):
    if not slot:
        return
    try:
        Path(state_path).write_text(f"{now:%Y-%m-%d} {slot}", encoding="utf-8")
    except OSError as exc:      # 写不上就算了，不影响主流程
        print(f"⚠️ 推送记录没写上：{exc}")


def build_content(copy_text, image_url, site_url, now=None):
    now = now or datetime.now(CN_TZ)
    # 首行固定含「热点」：飞书自定义机器人的关键词校验可用 批次,热点 两个词兜住
    parts = [f"📮 今日热点 · X 素材（{now.strftime('%m/%d')}）", ""]
    if image_url:
        # 不能用 ![](url)：飞书把 markdown 图片当 img_key 校验，只认自家上传的图，
        # 传外链会被拒收（11246 / invalid image keys）。自定义机器人拿不到 img_key，
        # 所以退成普通链接，点开即可看/下载。
        parts += ["【板块一 · 官方消息｜图】", f"[🖼 点此查看卡片图]({image_url})", ""]
    parts += ["【板块二 · 段子｜X 文案】", copy_text.strip()]
    if site_url:
        # 素材池是「一条一条发」用的，原文和出处单独放一份，方便自己改写
        parts += ["", f"[📄 二创素材（原文 + 出处）]({site_url}/x/sources.txt)",
                  "", f"[打开站点]({site_url})"]
    return "\n".join(parts)


def build_payload(content):
    # 与 trendradar/notification/senders.py 里线上跑通的飞书卡片结构保持一致
    return {
        "msg_type": "interactive",
        "card": {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": content}]}},
    }


def post(webhook_url, payload, timeout=30):
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8") or "{}")
    ok = body.get("code") == 0 or body.get("StatusCode") == 0
    return ok, body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--copy", default="docs/x/copy.txt", help="文案文件")
    ap.add_argument("--site", default="", help="站点地址，用于图片链接和页脚链接")
    ap.add_argument("--image-url", default="", help="卡片 PNG 的完整地址，留空则不发图")
    ap.add_argument("--force", action="store_true", help="忽略时段和当天去重，强制发送")
    ap.add_argument("--state", default=DEFAULT_STATE, help="推送记录文件，防止同一时段重复推送")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    now = datetime.now(CN_TZ)

    if args.selftest:
        content = build_content("今日热榜速览 09/29\n\n1. 测试条目（微博）", "https://example.com/x/card.png", "https://example.com")
        payload = build_payload(content)
        assert payload["msg_type"] == "interactive", "消息类型不对"
        assert payload["card"]["schema"] == "2.0", "卡片版本不对"
        el = payload["card"]["body"]["elements"][0]
        assert el["tag"] == "markdown" and "热点" in el["content"], "关键词兜底字样丢了"
        assert "[🖼 点此查看卡片图](https://example.com/x/card.png)" in el["content"], "图片链接没生成"
        assert "https://example.com/x/sources.txt" in el["content"], "二创素材链接没生成"
        assert "![" not in el["content"], "不能再用 markdown 图片语法，飞书会拒收外链"
        assert json.dumps(payload, ensure_ascii=False), "payload 无法序列化"
        assert slot_of(datetime(2026, 9, 29, 8, 0, tzinfo=CN_TZ)) == "am", "上午时段判定错"
        assert slot_of(datetime(2026, 9, 29, 21, 0, tzinfo=CN_TZ)) == "pm", "晚间时段判定错"
        assert not in_window(datetime(2026, 9, 29, 3, 0, tzinfo=CN_TZ)), "凌晨不该推"
        assert not in_window(datetime(2026, 9, 29, 15, 0, tzinfo=CN_TZ)), "下午三点不该推"
        # 定时任务落点漂移很大，光靠时段会漏推；去重记录保证「一天两次」而不是「一天零次」
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "state")
            morning = datetime(2026, 9, 29, 8, 0, tzinfo=CN_TZ)
            assert not pushed_before(state, morning, "am")
            mark_pushed(state, morning, "am")
            assert pushed_before(state, datetime(2026, 9, 29, 10, 0, tzinfo=CN_TZ), "am"), \
                "同一时段第二次没被挡住"
            assert not pushed_before(state, datetime(2026, 9, 29, 21, 0, tzinfo=CN_TZ), "pm"), \
                "晚上不该被早上的记录挡住"
            assert not pushed_before(state, datetime(2026, 9, 30, 8, 0, tzinfo=CN_TZ), "am"), \
                "隔天不该被头一天的记录挡住"
        print("selftest OK — payload 结构、时段判定、当天去重都正确")
        return

    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        raise SystemExit("缺少 FEISHU_WEBHOOK_URL 环境变量")

    slot = slot_of(now)
    if not args.force:
        if not slot:
            print(f"[{now:%H:%M}] 不在推送时段内（05:00-13:00 / 17:00-次日01:00），跳过")
            return
        if pushed_before(args.state, now, slot):
            print(f"[{now:%H:%M}] 今天这个时段已经推过了，跳过")
            return

    copy_path = Path(args.copy)
    if not copy_path.is_file():
        raise SystemExit(f"找不到文案文件：{copy_path}（先跑 scripts/gen_x_card.py）")
    copy_text = copy_path.read_text(encoding="utf-8")

    site = args.site.rstrip("/")
    image_url = args.image_url or (f"{site}/x/card.png?v={now:%m%d%H}" if site else "")

    payload = build_payload(build_content(copy_text, image_url, site, now))
    try:
        ok, body = post(webhook, payload)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"飞书返回 HTTP {e.code}：{e.read().decode(errors='ignore')[:300]}")
    except Exception as e:
        raise SystemExit(f"发送失败：{e}")

    if not ok:
        raise SystemExit(f"飞书拒收：{json.dumps(body, ensure_ascii=False)[:300]}")
    mark_pushed(args.state, now, slot)
    print(f"已推送飞书（{now:%H:%M}）{'（--force 强制）' if args.force else ''}，配图：{image_url or '无'}")


if __name__ == "__main__":
    main()
