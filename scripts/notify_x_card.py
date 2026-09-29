#!/usr/bin/env python3
# coding=utf-8
"""把生成的 X 卡片（文案 + 图片）推到飞书群机器人。

为什么不复用项目自带的推送框架：那套有分批、多账号、报告渲染，
对「一张卡片 + 一段文案」是过度设计。这里直连 webhook。

默认只在推送窗口内发送（早 07:30-08:30、晚 20:30-21:30，与 timeline.yaml 一致），
避免每半小时刷屏；--force 可忽略窗口。

用法：
    FEISHU_WEBHOOK_URL=xxx python3 scripts/notify_x_card.py
    python3 scripts/notify_x_card.py --force
    python3 scripts/notify_x_card.py --selftest
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CN_TZ = timezone(timedelta(hours=8))
WINDOWS = [(7 * 60 + 30, 8 * 60 + 30), (20 * 60 + 30, 21 * 60 + 30)]


def in_window(now):
    minute = now.hour * 60 + now.minute
    return any(start <= minute < end for start, end in WINDOWS)


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
        parts += ["", f"[打开站点]({site_url})"]
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
    ap.add_argument("--force", action="store_true", help="忽略推送时间窗口")
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
        assert "![" not in el["content"], "不能再用 markdown 图片语法，飞书会拒收外链"
        assert json.dumps(payload, ensure_ascii=False), "payload 无法序列化"
        assert in_window(datetime(2026, 9, 29, 8, 0, tzinfo=CN_TZ)), "早窗口判定错"
        assert not in_window(datetime(2026, 9, 29, 12, 0, tzinfo=CN_TZ)), "午间不应在窗口内"
        assert in_window(datetime(2026, 9, 29, 21, 0, tzinfo=CN_TZ)), "晚窗口判定错"
        print("selftest OK — payload 结构与窗口判定都正确")
        return

    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        raise SystemExit("缺少 FEISHU_WEBHOOK_URL 环境变量")

    if not args.force and not in_window(now):
        print(f"[{now:%H:%M}] 不在推送窗口内（07:30-08:30 / 20:30-21:30），跳过。加 --force 可强制发送")
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
    print(f"已推送飞书（{now:%H:%M}），配图：{image_url or '无'}")


if __name__ == "__main__":
    main()