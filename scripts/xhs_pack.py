#!/usr/bin/env python3
# coding=utf-8
"""今日热点精选：按当天爆款风向从热榜里挑 10 条最像爆款的新闻，
照爆款源的风格二创成可直接发的文案，连原贴链接/配图打包成一个页面推飞书。

筛选规则不写死名单，而是**每天现算**：素材 = 当天两个爆款源（xbangdan + SoPilot）
量出来的风向（跑得动的开头 / 风格 / 高频题材词）+ 跨天累计的稳定项（见
hooksupdate.md 第 4 节的自动风向区）。

配图只认原贴的 og:image，抓不到就不配图（热榜链多是搜索页/列表页，本来就没图），
不生成替代图。

用法：
    python3 scripts/xhs_pack.py                # 全流程（要 AI key）
    python3 scripts/xhs_pack.py --no-ai        # 只筛选 + 打包，不调模型
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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_x_card as gx          # noqa: E402  复用解析 / 爆款源 / AI / 站点常量

# 爆款源提供的是「选什么题、用什么角度写」：今天风向里哪几类跑得动，就照这几类去
# 找有潜力的新闻。**不是**拿标题去跟爆款正文撞词——撞上了说明该直接搬爆款，
# 用不着费劲找新闻源。
_STYLE_HINT = [
    ("奇观猎奇", r"奇观|罕见|首次|首例|第一次|离谱|魔幻|奇葩|惊呆|破纪录|极限|第一人"),
    ("暴论观点", r"争议|吵|怼|怒|骂|反驳|质疑|不满|抵制|炮轰|打脸|开撕"),
    ("家庭日常", r"彩礼|婚|婆媳|丈母娘|夫妻|离婚|份子钱|带娃|爸妈"),
    ("民间共鸣", r"网友|热议|吐槽|投诉|维权|曝光|翻车|割韭菜|踩雷"),
    ("都市夜话", r"男子|女子|大爷|大妈|小伙|业主|邻居|室友|同事|顾客|外卖|司机"),
    # 「快讯播报」故意不列：通报体是照实写的那种，正是二创写不出东西的题
]

# 「写得出爆款吗」——这才是入选门槛。爆款的共同点是：有件具体的事、有反转或反差、
# 落在一个能引起共鸣的话题上。光热但没法讲的（政策、财报、发布会、提问帖、工具推荐）
# 一律不选。
# 这里**不**把「有没有具体的人」当标准：没有名字的事一样能选，名字由模型编出来的才要拦
# （见 gen_x_card.invented_names）。
_STORY = [
    ("有冲突", r"反转|翻车|被坑|被骗|维权|投诉|吐槽|怒|吵|起诉|罚款|赔|抵制|退款|撕|"
             r"举报|纠纷|闹|拦|抢|逃|骗"),
    ("有反差", r"断裂|断了|塌|崩|爆炸|炸裂|离谱|奇葩|罕见|首次|首例|第一次|奇观|魔幻|"
             r"惊|居然|竟然"),
    ("婚恋家庭", r"彩礼|结婚|离婚|婚|婆媳|丈母娘|夫妻|份子钱|相亲|带娃|养娃"),
    ("有具体数字", r"[0-9０-９一二三四五六七八九十]+[个条名辆次岁人元万块天年万亿]"),
]
_CONFLICT = re.compile(r"反转|翻车|被骗|被坑|罚款|赔偿|起诉|告上|怒|吵|塌房|退款|维权|抵制|冲突|争议|道歉|打|砸|逃")

# 提问帖 / 工具推荐：没有可讲的「事」，二创只能抄问题或照着说明书复述
_ASK = re.compile(r"^如何看待|^怎么看|^如何看|^如何评价|是什么体验|求推荐|值得买吗|"
                  r"哪个好|哪个工具|怎么用|如何配置|注册|订阅|教程|好用吗")
# 没有可讲的事的选题：落到「数据好看但写不出故事」
_FACT_ONLY = re.compile(r"签署|协议|财报|季度|同比|增长|指数|收盘|开盘|涨幅|招标|"
                        r"上线|发布|获批|印发|试点|规划|部署|架构|开源")


def score_news(items, fp, keywords, weights=None):
    """给每条新闻打分：够不够「爆款相」。分项全部写进 why，方便人一眼看出为什么选它。

    weights = 近 14 天从爆款源学出来的「爆火机制」权重（见 gen_x_card.bangdan_learn）。
    这一步才是「用爆款的逻辑推测新闻的潜在价值」：机制是同一张表，能同时套在爆款正文
    和新闻标题上，所以权重可以从一边学到、用在另一边。
    """
    styles = {s for s in gx._bd_named(fp.get("styles", []), 4, skip="其他")}
    out = []
    for it in items:
        t = it["title"]
        if gx._AD_HOT.search(t):          # 广告 / 开售稿直接踢，别再让它混进 TOP1
            continue
        why, sc = [], 0.0
        story = [name for name, pat in _STORY if re.search(pat, t)]
        if story:
            sc += 3 * len(story)
            why += story
        # 对上今天跑得动的风格：同样的事，风向里的角度更容易跑起来
        style = [name for name, pat in _STYLE_HINT if name in styles and re.search(pat, t)]
        sc += 2 * len(style)
        why += [f"贴「{n}」" for n in style]
        weights = weights or {}
        mech = [m for m in gx._mech_hits(t) if weights.get(m)]
        if mech:
            sc += sum(3 * weights[m] for m in mech)
            why += [f"爆款逻辑「{m}」" for m in mech]
        hit = sorted([w for w in keywords if w in t], key=len, reverse=True)
        if any(len(w) >= 3 for w in hit):      # 撞上爆款源的高频词算加分，但不是门槛
            sc += 2
            why.append("题材词 " + "/".join(hit[:2]))
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
            sc -= 4
            why.append("提问帖/工具贴（没故事可讲）")
        if _FACT_ONLY.search(t) and len(story) < 2:
            sc -= 3
            why.append("只有数据没有事")
        if gx._NEWSY_TITLE.search(t):
            sc -= 4
            why.append("新闻腔（只能照实写）")
        # 够格入选：至少两个「有故事」的信号，或者一个信号 + 对上今天的风向
        strong = [m for m in mech if weights.get(m, 0) >= 0.5]     # 学出来特别稳的机制
        fit = len(story) >= 2 or bool(story and (style or strong))
        out.append({"score": round(sc, 1), "why": why, "item": it, "fit": fit})
    out.sort(key=lambda x: -x["score"])
    return out


def _bigrams(text):
    """标题的二字窗集合，用来判断两条是不是同一件事。"""
    t = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _same_event(a, b, need=3):
    """两条标题共用 3 个以上二字窗就当成同一件事（婚礼/看病/离世 那种改写也算）。

    单靠「题材词相同」不够：标题被改写过一次就对不上了（「新郎婚礼当天去医院看病后离世」
    和「官方回应婚礼看病离世」一个关键词都不重合，结果同一个事占了两条）。
    """
    return len(a & b) >= need


def pick_top(scored, top, keywords):
    """同一个事件别连占位：题材词相同的最多 2 条，标题高度重叠的最多 1 条。"""
    sigs = [{w for w in keywords if w in r["item"]["title"]} for r in scored]
    bags = [_bigrams(r["item"]["title"]) for r in scored]
    picked, used, taken, seen = [], {}, set(), []
    for cap in (3, 4):
        for i, row in enumerate(scored):
            if i in taken or not row.get("fit"):
                continue          # 没有故事相的，热也不选
            if any(_same_event(bags[i], b) for b in seen):
                continue          # 同一个事，换了个说法也不行
            groups = {g for g in (next((w for w in gx._THEME_WORDS if w in row["item"]["title"]), None),
                                  row["item"]["group"] or "其他") if g}
            kws = {"kw:" + w for w in sigs[i]}
            room = (any(used.get(k, 0) < 2 for k in kws) if kws
                    else any(used.get(k, 0) < cap for k in groups))
            if not room:
                continue
            for k in groups | kws:
                used[k] = used.get(k, 0) + 1
            taken.add(i)
            seen.append(bags[i])
            picked.append(row)
            if len(picked) >= top:
                return picked
    # 兜底：够格的不到 10 条时按分数补位，但把「这条没有故事相」写进理由，
    # 免得跟真够格的混在一起，事后分不清哪些是硬凑的
    for i, row in enumerate(scored):
        if i in taken:
            continue
        if any(_same_event(bags[i], b) for b in seen):
            continue          # 补位也不让同一个事件再来一条
        seen.append(bags[i])
        row["why"].append("备选（没有故事相）")
        taken.add(i)
        picked.append(row)
        if len(picked) >= top:
            break
    return picked


_XHS_SYSTEM = """你在给一个中文社媒账号写热点短评。写法是「辣评」：先用事引出话题，再给出自己的角度。

口吻（最容易写错的一条）：
- 你是旁观者，不是当事人。绝对不许写「我最近」「我儿子」「我朋友」这种代入当事人的句子——
  写成朋友圈口吻就废了。
- 第一段只把题面写明的事讲一遍，**不许补题面没有的细节**：没写原因的别编原因，没写结果的
  别编结果，没写态度的别编态度。
- 接下来 2~3 段才是重点：点评。要刁钻——指出这件事里别人没注意到的利益关系、责任归属、
  荒诞之处或反常识的细节。

其余规矩：
1. 标题 ≤ 20 字，要有情绪 / 悬念 / 数字，可以带 1~2 个 emoji。
2. 正文 200~450 字，全口语、短句、多换行；拆成 2~4 段放进 paras，段间自动空一行。
3. 结尾单独一行给 6~10 个话题标签，纯词，不要 # 号。
4. 不写新闻稿。禁用语：记者、据报道、相关部门、引发热议、值得深思。
5. 点评角度必须避开烂大街的解读：「成年人世界没有容易二字」「都是内卷」「原生家庭」
   「资本」「世态炎凉」「科技是一把双刃剑」这类一律不要，写了等于没写。
6. 全角标点；引号用「」；不要英文引号。
7. 不许编事实。题面没写名字就不许起名字，一律用不具名说法（「当事女生」「这家店的老板」
   「一位业主」）；绝对不许出现「李强」「赵工」「张阿姨」「王先生」这种自己起的人名，
   公司名、机构名、数字也只用题面有的。信息不够就把篇幅写短，别硬凑细节。题面只说「回应」
   就别写成「调查结果已出」「真相曝光」；没定论的事不要写成已经有定论。
8. 不要加「网传」「据称」这种免责词；官方通报类照实写。
9. 最后一句给个有态度的收束，能从前面推出来；放到别的故事上也成立的就是废话，重写。

只输出 JSON 数组，按输入的编号顺序，不要解释、不要 ``` 包裹：
[{"i": 1, "title": "标题", "paras": ["第一段", "第二段"], "tags": ["话题1", "话题2"]}]"""


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


_XHS_BATCH = 5          # 一次要 10 条整篇正文，模型容易写到一半就断


def _xhs_parse(text):
    """小红书版解析：gen_x_card._ai_parse 只认「title + paras」那套字段，
    直接拿来用会把 body/tags 全丢掉（实测成稿 0 条）。这里按同一套容错思路
    自己解一遍：数组可能被围栏包着、也可能被输出上限截断，完整的那几条照用。
    """
    text = text or ""
    data, start = [], text.find("[")
    if start >= 0:
        try:
            data = json.loads(text[start:])
        except ValueError:
            dec, pos = json.JSONDecoder(), start + 1
            while True:
                nxt = text.find("{", pos)
                if nxt < 0:
                    break
                try:
                    obj, end = dec.raw_decode(text[nxt:])
                except ValueError:
                    pos = nxt + 1
                    continue
                data.append(obj)
                pos = nxt + end
    out = []
    for n, it in enumerate(data if isinstance(data, list) else [], 1):
        if not isinstance(it, dict):
            continue
        title = str(it.get("title", "")).strip()
        paras = it.get("paras") or it.get("body") or []
        if isinstance(paras, str):
            paras = paras.split("\n")
        paras = [str(x).strip() for x in paras if str(x).strip()]
        tags = it.get("tags") or []
        if isinstance(tags, str):
            tags = re.split(r"[、,，#\s]+", tags)
        tags = [str(x).strip().lstrip("#") for x in tags if str(x).strip()]
        if title and paras:
            try:
                idx = int(it.get("i") or n)
            except (TypeError, ValueError):
                idx = n
            out.append({"i": idx, "title": gx._normalize_quotes(title),
                        "body": "\n\n".join(gx._normalize_quotes(x) for x in paras),
                        "tags": tags})
    return out


def xhs_write(picked, fp, now=None):
    """交给模型写小红书文案。返回 [{i,title,body,tags}]，拿不到就返回空。"""
    key, base, model = gx._ai_config()
    if not key:
        print("  ⚠️ 没配 AI key，跳过二创，这轮只出筛选结果和封面")
        return []
    got, last = [], ""
    src_of = {k + j + 1: r["item"]["title"] for j, r in enumerate(picked)}
    # 分小批写：一次要 10 条长正文容易撞输出上限，也被限流一枪打死整批。
    for k in range(0, len(picked), _XHS_BATCH):
        chunk = picked[k:k + _XHS_BATCH]
        prompt, part = _xhs_prompt(chunk, fp), []
        # 模型十次里有几次只交一半（实测 5 条只回 1 条），再要一次基本就补齐了
        for _attempt in (1, 2):
            last = gx._ai_call(key, base, model, _XHS_SYSTEM, prompt)
            have = {x["i"] for x in part}
            for x in _xhs_parse(last):
                x["i"] += k                  # 分批后编号要从整批的序号起算
                if x["i"] in have:
                    continue
                bad = gx.invented_names(x["title"] + "\n" + x["body"], src_of.get(x["i"], ""))
                if bad:                      # 题面里没这个人，是模型自己编的，宁可不要这条
                    print("     ⚠️ 丢掉一条：里头有原文没写的名字 " + "/".join(bad))
                    continue
                part.append(x)
            # 只在「交了一半」时补要一次：整批为空基本是限流/超时，
            # 再要一次等于把 180 秒的等待翻倍（实测能把一个步骤拖到 40 分钟）
            if len(part) >= len(chunk) or not part:
                break
        print(f"    第 {k // _XHS_BATCH + 1} 批：{len(chunk)} 条 -> 成稿 {len(part)} 条")
        got += part
    print(f"  AI 二创：{len(picked)} 条 -> 成稿 {len(got)} 条")
    if not got:
        # 打一小段原文，省得下次还得翻整轮日志才知道模型交了什么
        print("  ⚠️ 模型没写出合格内容（多半是限流或太弱）："
              + " ".join((last or "(空)").split())[:200])
    return got


# ── 原贴配图：拿不到就不配图 ──────────────────────────────────────────
# 热榜链一半是搜索页/列表页（s.weibo.com、bbs.hupu.com、tieba 的 hottopic...），
# 实测 12 条里 0 条带 og:image。所以别在这儿生成替代图：抓到就用，抓不到就空着。
_OG_TAG = re.compile(r"<meta\b[^>]*>", re.I)
_OG_KEYS = ("og:image", "twitter:image")


def fetch_og_image(url, timeout=8):
    """从原贴页面取 og:image。慢、失败、本来就没有，都返回空串（不重试）。"""
    try:
        req = urllib.request.Request(url, headers=dict(gx._HEADERS))
        with gx._OPENER.open(req, timeout=timeout) as resp:
            page = resp.read(200_000).decode("utf-8", "ignore")
    except Exception:            # noqa: BLE001 - 抓不到很正常，不该拖慢整轮
        return ""
    for tag in _OG_TAG.findall(page):
        if not any(k in tag for k in _OG_KEYS):
            continue
        m = re.search(r'content=["\']([^"\']+)', tag, re.I)
        if m and m.group(1).startswith("http"):
            return m.group(1)
    return ""


def x_intent(title, body=""):
    """X 的预填发帖链接。正文长过 X 自己的上限时 X 会截，这里不替你删。"""
    text = f"{title}\n\n{body}" if body else title
    return "https://x.com/intent/post?text=" + quote(text)


def render_pack(picked, wrote, site, handle="", now=None):
    """打包页：封面图 + 标题 + 正文 + 话题，一条一条照抄就能发。"""
    now = now or datetime.now()
    by_i = {int(x.get("i") or 0): x for x in wrote}
    cards = []
    for n, row in enumerate(picked, 1):
        it = row["item"]
        orig = it.get("orig") or ""
        # 没图就不留空列：10 条里通常一条图都没有，那个 300px 的空档太显眼
        pic = (f'<div class="cov"><img src="{html.escape(orig)}" alt="原贴配图" loading="lazy"></div>'
               if orig else "")
        nopic = "" if orig else '<div class="why">原贴没给配图</div>' 
        w = by_i.get(n) or {}
        title = w.get("title") or it["title"]
        body = w.get("body") or "（这轮没调模型，只有筛选结果；配上 AI key 再跑一次就有正文）"
        tags = w.get("tags") or []
        tagline = " ".join("#" + re.sub(r"\s+", "", t).lstrip("#") for t in tags)
        cards.append(f"""<section class="card">
  {pic}
  <div class="txt">
    <div class="rank">0{n} · {'/'.join(it['platforms'])} 第{it['rank']}位
      <span class="sc">匹配度 {row['score']}</span></div>
    <h2>{html.escape(title)}</h2>
    <pre class="body">{html.escape(body)}</pre>
    <div class="tags">{html.escape(tagline)}</div>
    <div class="xpost"><a href="{html.escape(x_intent(title, w.get('body') or ''))}" target="_blank" rel="noreferrer">🐦 一键发 X</a></div>
    <div class="src">原文：<a href="{html.escape(it['url'])}" rel="noreferrer">{html.escape(it['url'][:90])}</a></div>
    <div class="why">{html.escape('｜'.join(row['why']))}</div>
    {nopic}
  </div>
</section>""")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>今日热点精选 · {now:%Y-%m-%d}</title>
<style>
 body{{margin:0;background:#141418;color:#e9e9ef;
      font-family:"Noto Sans CJK SC","Microsoft YaHei",system-ui,sans-serif}}
 header{{padding:26px 20px;border-bottom:1px solid #2a2a33}}
 h1{{margin:0;font-size:22px}} .sub{{color:#9a9aa6;font-size:14px;margin-top:6px}}
 .wrap{{max-width:1100px;margin:0 auto;padding:20px}}
 .card{{display:flex;gap:22px;background:#1c1c22;border:1px solid #2a2a33;border-radius:16px;
       padding:18px;margin-bottom:20px}}
 .cov{{flex:0 0 300px}} .cov img{{width:300px;border-radius:12px;display:block}}
 .cov.nopic{{color:#7f7f8c;font-size:13px;align-self:center;line-height:1.6}}
 .txt{{flex:1;min-width:0}} h2{{margin:6px 0 12px;font-size:26px;line-height:1.4}}
 pre.body{{white-space:pre-wrap;font:inherit;line-height:1.9;margin:0 0 14px;color:#dcdce4}}
 .rank{{font-size:13px;color:#9a9aa6}} .sc{{color:#ffd479;margin-left:8px}}
 .tags{{color:#6ec1ff;font-size:14px;margin-bottom:10px}}
 .xpost{{margin:10px 0 12px}} .xpost a{{display:inline-block;padding:8px 16px;border-radius:999px;
        background:#1d9bf0;color:#fff;text-decoration:none;font-size:14px;font-weight:600}}
 .src{{font-size:12px;color:#7f7f8c;word-break:break-all;margin-bottom:6px}}
 .why{{font-size:12px;color:#66d9a0}}
 a{{color:#6ec1ff}}
</style></head><body>
<header><h1>今日热点精选 · {now:%m-%d}（{len(picked)} 条）</h1>
<div class="sub">标题 + 正文 + 话题 + 原贴链接，一条一条照着发即可。{'@' + handle if handle else ''}</div></header>
<div class="wrap">{''.join(cards)}</div></body></html>"""


def push_feishu(site, n, first_title, with_pic=0, with_text=True):
    """推飞书。只递一个链接过去——飞书自定义机器人发不了外链图片，
    原贴配图只能在页面里看，所以「有几条带图」写进正文里说明白。"""
    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("  ⚠️ 没配 FEISHU_WEBHOOK_URL，跳过飞书推送")
        return
    import notify_x_card as nxc          # 复用已经跑通的卡片结构，别再写一遍
    # 首行固定带「热点」：飞书自定义机器人的关键词校验靠它过
    content = "\n".join([
        f"📮 今日热点精选（{n} 条）", "",
        ("标题 + 正文 + 话题 + 原贴配图链接都排好了，点开照着发："
         if with_text else
         "这轮写作模型被限流（429），只出了选题、没出正文，点开先看选题："),
        f"[📕 打开今日热点精选]({site}/xhs/pack.html)",
        f"（{with_pic}/{n} 条原贴带图，其余的热榜链接是搜索页/列表页，本来就没图）",
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
    assert sc[0]["fit"] and not sc[-1]["fit"], "「有故事」才该够格，政策稿不该"
    picked = pick_top(sc, 2, ["楼市", "翻车"])
    assert "备选（没有故事相）" in picked[1]["why"], picked[1]
    assert len(picked) == 2
    picked[0]["item"]["orig"] = "https://e.com/p.jpg"
    page = render_pack(picked, [{"i": 1, "title": "标题", "body": "正文", "tags": ["a"]}],
                       "https://example.com")
    assert 'src="https://e.com/p.jpg"' in page and "标题" in page
    assert "原贴没给配图" in page, "没有原图的那条不该配图"
    assert "cov nopic" not in page, "没图的条目不该留空列"
    assert "<script" not in page, "转义没做"
    # 一键发 X：每条都挂上，且 URL 里的换行/引号都编过码
    assert page.count("x.com/intent/post") == 2, "两条都该有发帖链接"
    assert " " not in x_intent("标题", "第一段\n\n第二段").split("text=")[1]
    assert x_intent("标题") == "https://x.com/intent/post?text=%E6%A0%87%E9%A2%98"
    # 自编人名：题面没有的名字要拦掉，题面有就放行
    assert gx.invented_names("李伟在互联网公司上班。李伟后来说，这活儿没法干。", "办卡送话费") \
        == ["李伟"]
    assert gx.invented_names("王奶奶把养老钱捂得紧紧的。", "") == ["王奶奶"]
    # 挑题不再看「有没有具体的人」：没有名字但没有冲突/反差的题不该被这条规则压分
    assert not any("有具体的人" in w for r in sc for w in r["why"]), "「有具体的人」还在打分"
    got = _xhs_parse('\u0060\u0060\u0060json\n[{"i":1,"title":"标题","paras":["第一段","第二段"],"tags":"#a、b"}]\n\u0060\u0060\u0060')
    assert got and got[0]["body"] == "第一段\n\n第二段" and got[0]["tags"] == ["a", "b"], got
    assert _xhs_parse("模型今天罢工了") == []
    assert _xhs_parse('[{"title":"t","paras":["a"],"i":"7"}]')[0]["i"] == 7
    print("selftest OK — 爆款相打分 / 去广告 / 挑故事 / 打包 / 配图 都正常")


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
    # 跨天学习：X 卡那步已经把今天这笔写进 docs/x/bangdan_history.json 了，直接读
    hist = gx._bd_history(Path(args.out_dir).parent / "x")
    weights = gx.bangdan_learn(hist)
    if weights:
        top = sorted(weights.items(), key=lambda kv: -kv[1])[:5]
        print("  近 14 天学出来的爆火机制：" + "、".join(f"{k} {v:.2f}" for k, v in top))
    scored = score_news(items, fp, keywords, weights)
    picked = pick_top(scored, args.top, keywords)
    print(f"新闻筛选：{len(items)} 条候选 -> 取 {len(picked)} 条")
    print(f"  今天的高频题材词：{'、'.join(keywords[:10])}")
    for n, row in enumerate(picked, 1):
        it = row["item"]
        print(f"  {n:>2}. [{row['score']:>4}] {it['title'][:40]}"
              + (f"   ← {'｜'.join(row['why'])}" if args.explain else ""))

    wrote = [] if args.no_ai else xhs_write(picked, fp)
    # 10 条串着抓、每条等 8 秒就是一分多钟；并发只花最长那一条的时间
    with ThreadPoolExecutor(max_workers=5) as pool:
        for row, pic in zip(picked, pool.map(lambda r: fetch_og_image(r["item"]["url"]), picked)):
            row["item"]["orig"] = pic
    got = sum(1 for r in picked if r["item"].get("orig"))
    print(f"  原贴配图：{got}/{len(picked)} 条有（热榜链大多是搜索页，没有就不配图）")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "pack.html").write_text(render_pack(picked, wrote, args.site, args.handle),
                                   encoding="utf-8")
    (out / "pack.json").write_text(json.dumps(
        {"date": f"{datetime.now():%Y-%m-%d}", "score": [{"i": n, "score": r["score"],
         "why": r["why"], "title": r["item"]["title"], "url": r["item"]["url"],
         "x": x_intent(r["item"]["title"])} for n, r in enumerate(picked, 1)],
         "notes": [dict(x, x_intent=x_intent(x["title"], x["body"])) for x in wrote]},
        ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"图文包：{out/'pack.html'}")
    # 没出正文也照推：不然你那边一整天静悄悄，还以为是流水线挂了
    if picked:
        push_feishu(args.site, len(picked), picked[0]["item"]["title"], got, bool(wrote))


if __name__ == "__main__":
    main()
