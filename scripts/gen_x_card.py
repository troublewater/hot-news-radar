#!/usr/bin/env python3
# coding=utf-8
"""从最新报告生成 X 竖版卡片（HTML）与推文文案。

产出：
    docs/x/card.html   1200x1500 竖版卡片，交给无头浏览器截图成 PNG
    docs/x/copy.txt    推文文案草稿

选条规则：多平台共振优先（同题在多平台同时上榜），再比平台内名次。
条目附带上榜平台、最高名次、排名涨跌、在榜次数 —— 这些报告里本来就有，不额外调 AI。

用法：
    python3 scripts/gen_x_card.py
    python3 scripts/gen_x_card.py --source docs/reports/latest/current.html --top 8
    python3 scripts/gen_x_card.py --selftest
"""

import argparse
import html
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
DEFAULT_SITE = "https://troublewater.github.io/hot-news-radar"

_SOURCE_RE = re.compile(r'<span class="source-name">([^<]+)</span>')
_RANK_RE = re.compile(r'<span class="rank-num[^"]*">([^<]+)</span>')
_COUNT_RE = re.compile(r'<span class="count-info">([^<]+)</span>')
_LINK_RE = re.compile(r'<a[^>]+href="(http[^"]+)"[^>]*class="news-link"[^>]*>(.{2,220}?)</a>', re.S)


def _rank_value(raw):
    """'1-2' -> 1，'15' -> 15；取该条出现过的最好名次。"""
    nums = [int(n) for n in re.findall(r"\d+", raw or "")]
    return min(nums) if nums else 9999


def _trend_of(chunk):
    """报告里用 trend-up / trend-down 标记名次变化。"""
    if "trend-up" in chunk:
        return "up"
    if "trend-down" in chunk:
        return "down"
    return ""


def parse_items(report_html):
    """解析报告 HTML -> [{'title','url','platforms','rank','trend','count'}]，同题合并。"""
    merged = {}
    for chunk in report_html.split('<div class="news-item')[1:]:
        link = _LINK_RE.search(chunk)
        if not link:
            continue
        url = link.group(1)
        title = html.unescape(re.sub(r"<[^>]+>", "", link.group(2))).strip()
        if len(title) < 4:
            continue
        platforms = [html.unescape(p).strip() for p in _SOURCE_RE.findall(chunk)]
        platforms = [p for p in dict.fromkeys(platforms) if p]
        if not platforms:
            continue  # 独立展示区/RSS 段没有平台归属，不属于热榜卡片
        rank_m = _RANK_RE.search(chunk)
        rank = _rank_value(rank_m.group(1)) if rank_m else 9999
        count_m = _COUNT_RE.search(chunk)
        count = count_m.group(1).strip() if count_m else ""
        trend = _trend_of(chunk)

        item = merged.get(title)
        if item is None:
            merged[title] = {
                "title": title, "url": url, "platforms": platforms,
                "rank": rank, "trend": trend, "count": count,
            }
            continue
        for p in platforms:
            if p not in item["platforms"]:
                item["platforms"].append(p)
        if rank < item["rank"]:          # 名次更好，涨跌也以它为准
            item["rank"] = rank
            item["trend"] = trend or item["trend"]
        if count and not item["count"]:
            item["count"] = count
    items = list(merged.values())
    items.sort(key=lambda it: (-len(it["platforms"]), it["rank"]))
    return items


CSS = """
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1200px;height:1500px;overflow:hidden}
body{font-family:"Microsoft YaHei","PingFang SC","Noto Sans SC",system-ui,sans-serif;color:#eef2ff;
padding:58px 68px 40px;display:flex;flex-direction:column;
background:radial-gradient(900px 620px at 92% -10%,rgba(255,92,92,.26),transparent 62%),
radial-gradient(860px 640px at 2% 108%,rgba(64,132,255,.28),transparent 64%),#0b1020}
header{border-bottom:1px solid rgba(255,255,255,.14);padding-bottom:24px}
h1{font-size:62px;font-weight:800;letter-spacing:4px;
background:linear-gradient(92deg,#fff 12%,#9fc0ff 62%,#ff9a9a 100%);
-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.meta{margin-top:14px;display:flex;justify-content:space-between;font-size:21px;color:#8f9dc6;letter-spacing:2px}
.meta b{color:#ffd166;font-weight:700}
ul{list-style:none;margin-top:16px;flex:1;display:flex;flex-direction:column;padding-bottom:14px}
li{display:flex;gap:22px;align-items:center;flex:1;min-height:0}
.no{width:52px;height:52px;border-radius:14px;flex:none;display:grid;place-items:center;
font-size:28px;font-weight:800;color:#0b1020}
.n1{background:linear-gradient(135deg,#ffd166,#ff9f43)}.n2{background:linear-gradient(135deg,#8ecae6,#4d96ff)}
.n3{background:linear-gradient(135deg,#a0e7a0,#34c759)}.n4{background:linear-gradient(135deg,#ffb3d1,#ff5c8a)}
.n5{background:linear-gradient(135deg,#d0b3ff,#8b5cf6)}
.n6{background:linear-gradient(135deg,#7ee8c0,#10b981)}
.n7{background:linear-gradient(135deg,#ffc48a,#f97316)}
.n8{background:linear-gradient(135deg,#a5b4fc,#6366f1)}
.body{flex:1;min-width:0}
.txt{font-size:36px;font-weight:700;line-height:1.28;letter-spacing:.5px;display:block}
.tags{margin-top:11px;display:flex;gap:10px;flex-wrap:wrap}
.tag{font-size:20px;color:#a8b8e4;border:1px solid rgba(255,255,255,.24);border-radius:999px;padding:4px 16px}
.tag.hot{color:#ffd166;border-color:rgba(255,209,102,.5)}
.tag.up{color:#5ee08a;border-color:rgba(94,224,138,.45)}
.tag.down{color:#ff8f8f;border-color:rgba(255,143,143,.45)}
footer{border-top:1px solid rgba(255,255,255,.14);padding-top:20px;display:flex;
justify-content:space-between;font-size:20px;color:#6f7da6;letter-spacing:1px}
"""


def _tag(cls, text):
    return f'<span class="tag {cls}">{html.escape(text)}</span>' if cls else f'<span class="tag">{html.escape(text)}</span>'


def render_card(items, site, now=None, top=8, handle=""):
    now = now or datetime.now()
    picked = items[:top]
    resonant = sum(1 for it in picked if len(it["platforms"]) > 1)

    rows = []
    for i, it in enumerate(picked, 1):
        tags = [_tag("", p) for p in it["platforms"]]
        if len(it["platforms"]) > 1:
            tags.append(_tag("hot", f"多平台共振 ×{len(it['platforms'])}"))
        if it["rank"] < 9999:
            tags.append(_tag("", f"第 {it['rank']} 位"))
        if it["trend"] == "up":
            tags.append(_tag("up", "↑ 上升"))
        elif it["trend"] == "down":
            tags.append(_tag("down", "↓ 下降"))
        if it["count"]:
            tags.append(_tag("", it["count"]))
        rows.append(
            f'<li><span class="no n{i if i <= 8 else 8}">{i}</span>'
            f'<span class="body"><span class="txt">{html.escape(it["title"])}</span>'
            f'<span class="tags">{"".join(tags)}</span></span></li>'
        )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>{CSS}</style></head><body>
<header><h1>今日热点 · 速览</h1>
<div class="meta"><span>{now.strftime('%Y.%m.%d')} {WEEKDAY[now.weekday()]}</span>
<span>{len(picked)} 条精选 · 共振 <b>{resonant}</b> 条</span></div></header>
<ul>{''.join(rows)}</ul>
<footer><span>11 个国内热榜 + 8 个国际 RSS</span><span>{html.escape(handle)}</span></footer>
</body></html>
"""


def render_copy(items, site, now=None, top=3):
    """X 文案草稿：中文按 2 字符计，普通账号上限 280，所以只放 3 条。"""
    now = now or datetime.now()
    lines = [f"今日热榜速览 {now.strftime('%m/%d')}", ""]
    for i, it in enumerate(items[:top], 1):
        src = "·".join(it["platforms"][:2])
        lines.append(f"{i}. {it['title']}（{src}）")
    lines += ["", f"完整榜单 👉 {site}", "#热点 #新闻"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="docs/reports/latest/current.html")
    ap.add_argument("--out-dir", default="docs/x")
    ap.add_argument("--site", default=DEFAULT_SITE)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--handle", default="", help="卡片右下角署名，如 @your_x_handle")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        fixture = Path("tests/golden/html_snapshot_A.html")
        items = parse_items(fixture.read_text(encoding="utf-8"))
        assert len(items) >= 3, f"解析到的条目太少：{len(items)}"
        assert all(it["title"] for it in items), "存在空标题"
        assert all(it["platforms"] for it in items), "存在没有平台归属的条目"
        assert any(it["trend"] for it in items), "没解析出任何排名涨跌"
        assert any(it["count"] for it in items), "没解析出任何在榜次数"
        card = render_card(items, args.site, handle="@demo")
        assert "今日热点" in card and len(card) > 2000
        # 标签渲染用构造数据验证：夹具里可能没有多平台共振条目
        demo = [{"title": "测试条目", "url": "https://example.com",
                 "platforms": ["微博", "知乎"], "rank": 1, "trend": "up", "count": "3次"}]
        card2 = render_card(demo, args.site, handle="@demo")
        assert "多平台共振 ×2" in card2, "多平台共振标签没渲染"
        assert "↑ 上升" in card2 and "3次" in card2, "涨跌/次数标签没渲染"
        assert "完整榜单" in render_copy(items, args.site)
        print(f"selftest OK — {len(items)} 条，首条：{items[0]['title'][:26]} "
              f"{items[0]['platforms']} 第{items[0]['rank']}位 "
              f"{items[0]['trend'] or '-'} {items[0]['count'] or '-'}")
        return

    src = Path(args.source)
    if not src.is_file():
        raise SystemExit(f"找不到报告：{src}（先跑一次爬虫，或指定 --source）")
    items = parse_items(src.read_text(encoding="utf-8", errors="ignore"))
    if not items:
        raise SystemExit(f"{src} 里没解析出任何条目，报告结构可能变了")

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    handle = args.handle or ("@" + args.site.split("//")[-1].split(".")[0])
    (out / "card.html").write_text(render_card(items, args.site, top=args.top, handle=handle), encoding="utf-8")
    (out / "copy.txt").write_text(render_copy(items, args.site), encoding="utf-8")
    print(f"卡片已生成：{out/'card.html'}（{len(items)} 条候选，取前 {args.top}）")
    for i, it in enumerate(items[: args.top], 1):
        print(f"  {i}. [{len(it['platforms'])}平台 第{it['rank']}位 {it['trend'] or '-'} {it['count'] or '-'}] {it['title'][:34]}")


if __name__ == "__main__":
    main()