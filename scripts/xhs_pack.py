#!/usr/bin/env python3
# coding=utf-8
"""小红书图文包：按今日爆款风向从热榜里挑 10 条最像爆款的新闻，
照爆款源的风格二创成小红书文案，每条配一张封面图，打包成可直接发的页面。

筛选规则不写死名单，而是**每天现算**：素材 = 当天两个爆款源（xbangdan + SoPilot）
量出来的风向（跑得动的开头 / 风格 / 高频题材词）+ 跨天累计的稳定项（见
hooksupdate.md 第 4 节的自动风向区）。

用法：
    python3 scripts/xhs_pack.py                # 全流程（要 AI key）
    python3 scripts/xhs_pack.py --no-ai        # 只筛选 + 出封面，不调模型
    python3 scripts/xhs_pack.py --top 10 --explain   # 打印每条的得分理由
    python3 scripts/xhs_pack.py --selftest
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_x_card as gx          # noqa: E402  复用解析 / 爆款源 / AI / 站点常量

# 爆款风格 -> 标题里长什么样的更贴。风向说今天哪几类跑得动，就照这几类加分。
_STYLE_HINT = [
    ("奇观猎奇", r"奇|怪|罕见|首个|第一|最|破|极限|离谱|神|魔幻|惊"),
    ("暴论观点", r"争议|吵|怼|怒|骂|回应|反驳|质疑|不满|抵制|炮轰"),
    ("家庭日常", r"婚|彩礼|婆|妈|爸|孩子|夫妻|亲|份子钱|房|家"),
    ("民间共鸣", r"网友|热议|吐槽|投诉|维权|曝光|翻车|坑|割韭菜"),
    ("都市夜话", r"男子|女子|大爷|大妈|小伙|业主|邻居|室友|同事|顾客"),
    ("快讯播报", r"通报|回应|警方|官方|声明|辟谣"),
]
# 有冲突才有故事性：平淡的正经消息在小红书没人点
_CONFLICT = re.compile(
    r"反转|翻车|被骗|被坑|罚款|赔偿|起诉|告上|怒|吵|塌房|退款|维权|抵制|冲突|争议|道歉|打|砸|逃")

# 提问帖没有可写的"事"，二创只能抄问题，压下去
_ASK = re.compile(r"^如何看待|^怎么看|^如何看|^如何评价|是什么体验|求推荐|值得买吗")


def score_news(items, fp, keywords):
    """给每条新闻打分。分项全部写进 why，方便人一眼看出为什么选它。"""
    styles = {s for s in gx._bd_named(fp.get("styles", []), 4, skip="其他")}
    out = []
    for it in items:
        t = it["title"]
        if gx._AD_HOT.search(t):          # 广告 / 开售稿直接踢，别再让它混进 TOP1
            continue
        why, sc = [], 0.0
        hit = [w for w in keywords if w in t]
        if hit:
            sc += 4 * min(len(hit), 2)
            why.append("题材词 " + "/".join(hit[:3]))
        for name, pat in _STYLE_HINT:
            if name in styles and re.search(pat, t):
                sc += 3
                why.append(f"贴「{name}」")
        if _CONFLICT.search(t):
            sc += 2
            why.append("有冲突")
        n = len(it["platforms"])
        if n >= 2:
            sc += 2
            why.append(f"{n} 平台共振")
        if n >= 4:
            sc += 2
        if it["rank"] <= 3:
            sc += 2
            why.append("榜内前三")
        if _ASK.search(t):
            sc -= 3
            why.append("提问帖（难二创）")
        if gx._NEWSY_TITLE.search(t):
            sc -= 4
            why.append("新闻腔（只能照实写）")
        out.append({"score": round(sc, 1), "why": why, "item": it})
    out.sort(key=lambda x: -x["score"])
    return out


def pick_top(scored, top, keywords):
    """同题材 / 同高频词别连占位：先每组最多 2 条，不够再放宽到 3、4。"""
    sigs = [{w for w in keywords if w in r["item"]["title"]} for r in scored]
    picked, used, taken = [], {}, set()
    for cap in (2, 3, 4):
        for i, row in enumerate(scored):
            if i in taken:
                continue
            keys = {t for t in (next((w for w in gx._THEME_WORDS if w in row["item"]["title"]), None),
                                row["item"]["group"] or "其他") if t}
            keys |= {"kw:" + w for w in sigs[i]}
            if all(used.get(k, 0) >= cap for k in keys):
                continue
            for k in keys:
                used[k] = used.get(k, 0) + 1
            taken.add(i)
            picked.append(row)
            if len(picked) >= top:
                return picked
    return picked


_XHS_SYSTEM = """你在给一个小红书账号写笔记，素材是当天的热点新闻。

小红书的规矩（跟推文不一样，别写成推文）：
1. 标题 ≤ 20 字，要有情绪 / 悬念 / 数字，可以带 1~2 个 emoji。
2. 正文 200~450 字，全口语、短句、多换行；用 emoji 当小标题分 2~4 点。
3. 结尾单独一行给 6~10 个话题标签，纯词，不要 # 号。
4. 写故事、写感受，不写新闻稿。禁用语：记者、据报道、相关部门、引发热议、值得深思。
5. 必须落到具体：一个具体的人、一个具体数字、一次转折。
6. 全角标点；引号用「」；不要英文引号。
7. 可以自由改编、补细节、换人名，但不要加「网传」「据称」这种免责词。
   官方通报类照实写，不要编。
8. 最后一句给个有态度的收束，能从前面推出来；放到别的故事上也成立的就是废话，重写。

只输出 JSON 数组，按输入的编号顺序，不要解释、不要 ``` 包裹：
[{"i": 1, "title": "标题", "body": "正文", "tags": ["话题1", "话题2"]}]"""


def _xhs_prompt(picked, fp):
    user = ["今天 X 中文区跑得动的爆款风向（照这个口味写，人和事必须是新的）：",
            "- 跑得动的开头类型：" + "、".join(gx._bd_named(fp.get("hooks", []), 5)),
            "- 跑得动的风格：" + "、".join(gx._bd_named(fp.get("styles", []), 4, skip="其他")),
            f"- 爆款正文中位 {fp.get('len_mid', 0)} 字：短句、短段",
            "- 真开头（只学口气和节奏，不许复用里面的人和事）："]
    user += ["    " + t for t in (fp.get("openers") or [])[:4]]
    user += ["", "把这些热点各写成一条小红书笔记，一条一个，顺序对应："]
    for n, row in enumerate(picked, 1):
        it = row["item"]
        user.append(f"{n}. [{it['group']}] {it['title']}"
                    f"（{'/'.join(it['platforms'])} 第{it['rank']}位）")
    user += ["", f"数组长度必须是 {len(picked)}。"]
    return "\n".join(user)


def xhs_write(picked, fp, now=None):
    """交给模型写小红书文案。返回 [{i,title,body,tags}]，拿不到就返回空。"""
    key, base, model = gx._ai_config()
    if not key:
        print("  ⚠️ 没配 AI key，跳过二创，这轮只出筛选结果和封面")
        return []
    raw = gx._ai_call(key, base, model, _XHS_SYSTEM, _xhs_prompt(picked, fp))
    got = [x for x in gx._ai_parse(raw) if x.get("title") and x.get("body")]
    print(f"  AI 二创：{len(picked)} 条 -> 成稿 {len(got)} 条")
    if not got:
        print("  ⚠️ 模型没写出合格内容（多半是限流或太弱）")
    return got


# ── 封面：小红书是竖版 3:4。每条一张，图文一一对应 ─────────────────────
def cover_html(idx, title, kicker, site):
    """每条新闻一张封面页。真正的「新闻原图」抓不到（热榜链接大多是搜索页），
    所以封面按标题生成——图文对应靠的是「这条文案配这张封面」。"""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
  html,body{{margin:0;padding:0}}
  .c{{width:1080px;height:1440px;box-sizing:border-box;padding:96px 84px;display:flex;
     flex-direction:column;justify-content:space-between;
     font-family:"Noto Sans CJK SC","Microsoft YaHei",sans-serif;
     background:linear-gradient(160deg,#1b1b22 0%,#2a2233 55%,#3a2430 100%);color:#fff}}
  .top{{display:flex;align-items:center;gap:18px;font-size:30px;color:#ffd479;font-weight:700}}
  .badge{{background:#ff2e4d;color:#fff;border-radius:999px;padding:8px 26px;font-size:28px}}
  h1{{font-size:96px;line-height:1.28;margin:0;font-weight:900;letter-spacing:1px;
     word-break:break-word}}
  .hl{{color:#ffd479}}
  .foot{{display:flex;justify-content:space-between;align-items:flex-end;
        font-size:28px;color:#c8c8d0;border-top:2px solid rgba(255,255,255,.14);padding-top:26px}}
  .n{{font-size:34px;color:#ff6b81;font-weight:800}}
</style></head><body>
<div class="c">
  <div class="top"><span class="badge">今日热议</span><span>{kicker}</span></div>
  <h1>{title}</h1>
  <div class="foot"><span>{site}</span><span class="n">0{idx}</span></div>
</div></body></html>"""


def render_pack(picked, wrote, site, handle="", now=None):
    """打包页：封面图 + 标题 + 正文 + 话题，一条一条照抄就能发。"""
    now = now or datetime.now()
    by_i = {int(x.get("i") or 0): x for x in wrote}
    cards = []
    for n, row in enumerate(picked, 1):
        it = row["item"]
        w = by_i.get(n) or {}
        title = w.get("title") or it["title"]
        body = w.get("body") or "（这轮没调模型，只有筛选结果；配上 AI key 再跑一次就有正文）"
        tags = w.get("tags") or []
        tagline = " ".join("#" + re.sub(r"\s+", "", t).lstrip("#") for t in tags)
        cards.append(f"""<section class="card">
  <div class="cov"><img src="cover/{n}.png" alt="封面 {n}" loading="lazy"></div>
  <div class="txt">
    <div class="rank">0{n} · {'/'.join(it['platforms'])} 第{it['rank']}位
      <span class="sc">匹配度 {row['score']}</span></div>
    <h2>{html.escape(title)}</h2>
    <pre class="body">{html.escape(body)}</pre>
    <div class="tags">{html.escape(tagline)}</div>
    <div class="src">原文：<a href="{html.escape(it['url'])}" rel="noreferrer">{html.escape(it['url'][:90])}</a></div>
    <div class="why">{html.escape('｜'.join(row['why']))}</div>
  </div>
</section>""")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小红书图文包 · {now:%Y-%m-%d}</title>
<style>
 body{{margin:0;background:#141418;color:#e9e9ef;
      font-family:"Noto Sans CJK SC","Microsoft YaHei",system-ui,sans-serif}}
 header{{padding:26px 20px;border-bottom:1px solid #2a2a33}}
 h1{{margin:0;font-size:22px}} .sub{{color:#9a9aa6;font-size:14px;margin-top:6px}}
 .wrap{{max-width:1100px;margin:0 auto;padding:20px}}
 .card{{display:flex;gap:22px;background:#1c1c22;border:1px solid #2a2a33;border-radius:16px;
       padding:18px;margin-bottom:20px}}
 .cov{{flex:0 0 300px}} .cov img{{width:300px;border-radius:12px;display:block}}
 .txt{{flex:1;min-width:0}} h2{{margin:6px 0 12px;font-size:26px;line-height:1.4}}
 pre.body{{white-space:pre-wrap;font:inherit;line-height:1.9;margin:0 0 14px;color:#dcdce4}}
 .rank{{font-size:13px;color:#9a9aa6}} .sc{{color:#ffd479;margin-left:8px}}
 .tags{{color:#6ec1ff;font-size:14px;margin-bottom:10px}}
 .src{{font-size:12px;color:#7f7f8c;word-break:break-all;margin-bottom:6px}}
 .why{{font-size:12px;color:#66d9a0}}
 a{{color:#6ec1ff}}
</style></head><body>
<header><h1>小红书图文包 · {now:%m-%d}（{len(picked)} 条）</h1>
<div class="sub">封面图 + 标题 + 正文 + 话题，一条一条照着发即可。{'@' + handle if handle else ''}</div></header>
<div class="wrap">{''.join(cards)}</div></body></html>"""


def push_feishu(site, n, first_title):
    """推飞书。小红书没有官方发帖接口，这里只把图文包链接递到手机上，发帖还是手动。"""
    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("  ⚠️ 没配 FEISHU_WEBHOOK_URL，跳过飞书推送")
        return
    import notify_x_card as nxc          # 复用已经跑通的卡片结构，别再写一遍
    # 首行固定带「热点」：飞书自定义机器人的关键词校验靠它过
    content = "\n".join([
        f"📮 今日热点 · 小红书图文包（{n} 条）", "",
        "封面图 + 标题 + 正文 + 话题都排好了，点开照着发：",
        f"[📕 打开图文包]({site}/xhs/pack.html)",
        "", f"首条：{first_title}",
    ])
    ok, body = nxc.post(webhook, nxc.build_payload(content))
    print("  飞书推送：" + ("✅ 已推送" if ok else f"❌ {body}"))


def selftest():
    items = [
        {"title": "商k不让碰，男子急眼了当场翻车", "platforms": ["虎扑", "贴吧"], "rank": 1,
         "group": "离谱现场", "url": "https://e.com/1"},
        {"title": "购房贷款贴息政策发布，对楼市拉动效果如何", "platforms": ["微博"], "rank": 5,
         "group": "钱与房", "url": "https://e.com/2"},
        {"title": "某新机开售直降领券到手价 2999", "platforms": ["微博"], "rank": 1,
         "group": "数码翻车", "url": "https://e.com/3"},
    ]
    fp = {"hooks": [("破防", 3), ("暴论", 2)], "styles": [("都市夜话", 5), ("奇观猎奇", 3)],
          "len_mid": 60, "openers": []}
    sc = score_news(items, fp, ["楼市", "翻车"])
    assert all("开售" not in r["item"]["title"] for r in sc), "广告稿没被踢掉"
    assert sc[0]["item"]["title"].startswith("商k"), sc[0]
    assert sc[-1]["item"]["title"].startswith("购房贷款"), "新闻腔没被压分"
    picked = pick_top(sc, 2, ["楼市", "翻车"])
    assert len(picked) == 2
    page = render_pack(picked, [{"i": 1, "title": "标题", "body": "正文", "tags": ["a"]}],
                       "https://example.com")
    assert "cover/1.png" in page and "标题" in page
    assert "<script" not in page, "转义没做"
    body = cover_html(1, "标题", "虎扑 第1位", "https://example.com")
    assert "1080px" in body and "标题" in body
    print("selftest OK — 打分 / 去广告 / 降新闻腔 / 挑选 / 打包 / 封面 都正常")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="docs/reports/latest/current.html")
    ap.add_argument("--out-dir", default="docs/xhs")
    ap.add_argument("--site", default=gx.DEFAULT_SITE)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--handle", default="")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--no-bangdan", action="store_true")
    ap.add_argument("--explain", action="store_true", help="打印每条的得分理由")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return

    src = Path(args.source)
    if not src.is_file():
        raise SystemExit(f"找不到报告：{src}")
    items = gx.parse_items(src.read_text(encoding="utf-8", errors="ignore"))
    bang = [] if args.no_bangdan else gx.fetch_hot_sources()
    fp = gx.bangdan_fingerprint(bang)
    if not fp.get("n"):
        print("⚠️ 今天没抓到爆款源，筛选只能按通用规则走（不推荐）")
    keywords = [t for t, _ in gx._bd_terms(bang, top=40)]
    scored = score_news(items, fp, keywords)
    picked = pick_top(scored, args.top, keywords)
    print(f"新闻筛选：{len(items)} 条候选 -> 取 {len(picked)} 条")
    print(f"  今天的高频题材词：{'、'.join(keywords[:10])}")
    for n, row in enumerate(picked, 1):
        it = row["item"]
        print(f"  {n:>2}. [{row['score']:>4}] {it['title'][:40]}"
              + (f"   ← {'｜'.join(row['why'])}" if args.explain else ""))

    wrote = [] if args.no_ai else xhs_write(picked, fp)
    out = Path(args.out_dir)
    (out / "cover").mkdir(parents=True, exist_ok=True)
    for n, row in enumerate(picked, 1):
        it = row["item"]
        (out / "cover" / f"{n}.html").write_text(
            cover_html(n, html.escape(it["title"]), f"{'/'.join(it['platforms'])} 第{it['rank']}位",
                       args.site), encoding="utf-8")
    (out / "pack.html").write_text(render_pack(picked, wrote, args.site, args.handle),
                                   encoding="utf-8")
    (out / "pack.json").write_text(json.dumps(
        {"date": f"{datetime.now():%Y-%m-%d}", "score": [{"i": n, "score": r["score"],
         "why": r["why"], "title": r["item"]["title"], "url": r["item"]["url"]}
         for n, r in enumerate(picked, 1)], "notes": wrote}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"图文包：{out/'pack.html'}（{len(picked)} 张封面待截图：{out/'cover'}）")
    if wrote and picked:
        push_feishu(args.site, len(picked), picked[0]["item"]["title"][:30])


if __name__ == "__main__":
    main()
