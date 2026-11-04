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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_x_card as gx          # noqa: E402  复用解析 / 爆款源 / AI / 站点常量

# 爆款源提供的是「选什么题、用什么角度写」：今天风向里哪几类跑得动，就照这几类去
# 找有潜力的新闻。**不是**拿标题去跟爆款正文撞词——撞上了说明该直接搬爆款，
# 用不着费劲找新闻源。
# 选题只认两样东西：当天从范文池蒸馏出来的「内容类型」（learn_types）＋通用的
# 「这条有没有可讲的事」。这里不再写死任何题材偏好——婚恋、街坊、争议都不是加分项，
# 池子换了，选出来的题就跟着换。

# 提问帖 / 工具推荐：没有可讲的「事」，二创只能抄问题或照着说明书复述
_ASK = re.compile(r"^如何看待|^怎么看|^如何看|^如何评价|是什么体验|求推荐|值得买吗|"
                  r"哪个好|哪个工具|怎么用|如何配置|注册|订阅|教程|好用吗")
# 没有可讲的事的选题：落到「数据好看但写不出故事」
_FACT_ONLY = re.compile(r"签署|协议|财报|季度|同比|增长|指数|收盘|开盘|涨幅|招标|"
                        r"上线|发布|获批|印发|试点|规划|部署|架构|开源")


def score_news(items, fp, keywords, weights=None, types=None):
    """给每条新闻打分：够不够「爆款相」。分项全部写进 why，方便人一眼看出为什么选它。

    weights = 近 14 天从爆款源学出来的「爆火机制」权重（见 gen_x_card.bangdan_learn）。
    types   = 当天从范文池总结出来的内容类型（见 gen_x_card.learn_types），每类带识别词。
    这一步才是「用爆款的逻辑推测新闻的潜在价值」：机制和类型都是同一张表，能同时套在
    爆款正文和新闻标题上，所以可以从一边学到、用在另一边。
    """
    out = []
    for it in items:
        t = it["title"]
        if gx._AD_HOT.search(t):          # 广告 / 开售稿直接踢，别再让它混进 TOP1
            continue
        why, sc = [], 0.0
        # 撞上范文池总结出来的内容类型：题材对上了，就是「这类爆款在讲的事」
        ty = gx.match_types(t, types)
        if ty:
            sc += 3 * len(ty)
            why += [f"类型「{n}」" for n in ty]
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
        if _FACT_ONLY.search(t) and not ty:
            sc -= 3
            why.append("只有数据没有事")
        if gx._NEWSY_TITLE.search(t):
            sc -= 4
            why.append("新闻腔（只能照实写）")
        # 够格入选只看「撞没撞上池子里的类型」；没有类型（蒸馏失败）时全放行，
        # 宁可让 pick_top 按分数和去重去挑，也不要再塞一套写死的偏好进来
        fit = bool(ty) if types else True
        out.append({"score": round(sc, 1), "why": why, "item": it, "fit": fit,
                    "type": ty[0] if ty else ""})
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
                continue          # 没撞上池子里的类型，热也不选
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
    # 兜底：够格的不到 10 条时按分数补位，但把「这条没撞上类型」写进理由，
    # 免得跟真够格的混在一起，事后分不清哪些是硬凑的
    for i, row in enumerate(scored):
        if i in taken:
            continue
        if any(_same_event(bags[i], b) for b in seen):
            continue          # 补位也不让同一个事件再来一条
        seen.append(bags[i])
        row["why"].append("备选（未命中池里的类型）")
        taken.add(i)
        picked.append(row)
        if len(picked) >= top:
            break
    return picked


_XHS_SYSTEM = """你在给一个中文社媒账号写热点短评。**写法不固定**：照附在后面的「真爆款范文」写，
结构、段数、每段句数、长短、怎么开头、怎么收尾，全部以范文为准。范文池是每天从 X 爆款榜
累积的，风向变了范文就变，你跟着变——不要套任何固定模板，也不要自己发明一套招式。

不许这么写：
- 不写新闻。禁用语：记者、据报道、相关部门、引发热议、值得深思、有关部门。
- 不讲大道理。落点换到别的故事上也念得通的，就是废话，重写。
- 不要问卷式收尾：「你怎么看」「你怎么评价」「说说你的想法」一律不要，不管范文怎么收。
- 你不是当事人：绝对不许写「我最近」「我儿子」「我朋友」这种代入当事人的句子，写成朋友圈
  口吻就废了。范文里出现第一人称是范文的事，你不许跟着用。
- 不许堆形容词煽情（「太离谱了」「我真的服了」），读完得让人知道到底发生了什么。

只能这么写：
- 只把题面已经写明的事展开（补场景、动作、反应），不许新增题面没说的事件。
- 范文只示范写法：范文里的人、事、数字一个字都不许搬过来。要学得像，就用题面自己的料
  把那个节奏填满；料不够就写短，别硬凑。

不许编（这条会被程序硬拦，违反的整条作废）：
- 新闻里没写名字就用不具名说法（「当事女生」「这家店的老板」「一位业主」）；绝不允许出现
  自己编的人名（「李强」「赵工」「张阿姨」「王先生」这类）。
- 公司名、机构名、数字同理，只能用原文有的。
- 题面只说「回应」就别写成「调查结果已出」「真相曝光」；没定论的事不要写成已经有定论。

格式：
- 标题 <= 20 字，要有情绪 / 悬念 / 数字，可以带 1~2 个 emoji。
- paras 必须 3~5 段，**每段 60~110 字**（整条 200~450 字）。每段是一个完整场景/动作，
  不是一句话；有原文摘录就按摘录铺开，没有摘录就 150~250 字，别靠编细节凑长度。
- 结尾必须是陈述句，不许提问。
- 结尾的 tags 只放 6~10 个话题词，纯词，不要 # 号。
- 全角标点；引号一律「」；不要英文引号。

同一条还要给一份头条版（tt_*），跟 X 版分开写，别直接照搬：
- tt_title <= 30 字，不带 emoji、不带 #、不用「破防/笑死」这种网感词，像正经稿子的标题。
- tt_body 300~600 字、3~5 段：先讲清发生了什么（谁、在哪、做了什么、结果），最后一段给一句落点，
  可以带观点，但别喊口号、别提问、别编原文没有的人名细节。
- 全角标点，引号一律「」。

只输出 JSON 数组，按输入的编号顺序，不要解释、不要 ``` 包裹：
[{"i": 1, "title": "标题", "paras": ["第一段", "第二段"], "tags": ["话题1", "话题2"],
  "tt_title": "头条标题", "tt_body": "头条正文，段落之间空一行"}]"""


def _xhs_prompt(picked, fp, types=None, stats=None, skill=None):
    user = ["今天 X 中文区跑得动的爆款风向（照这个口味写，人和事必须是新的）：",
            "- 跑得动的开头类型：" + "、".join(gx._bd_named(fp.get("hooks", []), 5)),
            "- 跑得动的风格：" + "、".join(gx._bd_named(fp.get("styles", []), 4, skip="其他")),
            f"- 爆款正文中位 {fp.get('len_mid', 0)} 字：短句、短段"]
    if types:
        user += ["", "今天从范文池总结出来的内容类型（写法照对应那一条，别自己发明招式）："]
        user += [f"- {t['name']}：{t.get('desc', '')}｜写法：{t.get('how', '')}"
                 for t in types[:10]]
    if skill:
        # 这份是当天从整池范文蒸馏出来的「怎么写」，比只给类型更具体
        user += ["", "今天从范文池蒸馏出来的写法（结构/钩子/句型照这个来，别自己发明）："]
        for st in (skill.get("structures") or [])[:3]:
            share = f"（占 {round((st.get('share') or 0) * 100)}%）" if st.get("share") else ""
            user.append(f"- 骨架「{st.get('name')}」{st.get('skeleton')}{share}")
        hk = "；".join(f"{h.get('kind')}：{h.get('pattern')}"
                       for h in (skill.get("hooks") or [])[:5] if h.get("pattern"))
        if hk:
            user.append("- 开头钩子：" + hk)
        pt = "；".join(x.get("pattern") for x in (skill.get("patterns") or [])[:5]
                       if x.get("pattern"))
        if pt:
            user.append("- 高频句型：" + pt)
        ln = skill.get("length") or {}
        if ln.get("median"):
            user.append(f"- 篇幅：中位 {ln['median']} 字 / {ln.get('paras') or 3} 段左右")
        rl = [str(x) for x in (skill.get("rules") or [])[:5] if str(x).strip()]
        if rl:
            user.append("- 硬规则：" + "；".join(rl))
    if stats:
        user += ["", f"- 正文规模按 {stats['len']} 字上下写，别写成一行一行的碎片"]
    user += ["", "有原文摘录的条目，只用摘录里写明的事展开；没有摘录的只按标题写，宁可短，不许补细节。",
             "", "把这些热点各写成一条爆款推文，一条一个，顺序对应："]
    for n, row in enumerate(picked, 1):
        it = row["item"]
        tname = f"，爆款类型：{row['type']}" if row.get("type") else ""
        user.append(f"{n}. [{it['group']}] {it['title']}"
                    f"（{'/'.join(it['platforms'])} 第{it['rank']}位{tname}）")
        ex = (it.get("excerpt") or "").strip()
        if ex:
            user.append(f"   原文摘录：{ex[:400]}")
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
            tt = str(it.get("tt_body") or "").strip()
            out.append({"i": idx, "title": gx._normalize_quotes(title),
                        "body": "\n\n".join(gx._normalize_quotes(x) for x in paras),
                        "tags": tags,
                        "tt_title": gx._normalize_quotes(str(it.get("tt_title") or "").strip()),
                        "tt_body": gx._normalize_quotes(tt) if tt else ""})
    return out


def xhs_write(picked, fp, now=None, types=None, stats=None, skill=None):
    """交给模型写小红书文案。返回 [{i,title,body,tags}]，拿不到就返回空。"""
    key, base, model = gx._ai_config("xhs")
    if not key:
        print("  ⚠️ 没配 AI key，跳过二创，这轮只出筛选结果和封面")
        return []
    got, best, last = [], {}, ""
    # 分小批写：一次要 10 条长正文容易撞输出上限，也被限流一枪打死整批。
    for k in range(0, len(picked), _XHS_BATCH):
        chunk = picked[k:k + _XHS_BATCH]
        # 每条能拿来核对的「原文」只有热榜标题（原贴正文抓不到），查编造的名字就靠它
        src_of = {k + j + 1: r["item"]["title"] + "\n" + (r["item"].get("excerpt") or "")
                  for j, r in enumerate(chunk)}
        prompt, part = _xhs_prompt(chunk, fp, types, stats, skill), []
        note = ""
        # 模型十次里有几次只交一半（实测 5 条只回 1 条），再要一次基本就补齐了
        for _attempt in (1, 2):
            last = gx._ai_call(key, base, model, _XHS_SYSTEM, prompt + note)
            have = {x["i"] for x in part}
            for x in _xhs_parse(last):
                x["i"] += k                  # 分批后编号要从整批的序号起算
                if x["i"] in have:
                    continue
                bad = gx.invented_names(x["title"] + "\n" + x["body"], src_of.get(x["i"], ""))
                if bad:                      # 题面里没这个人，是模型自己编的，宁可不要这条
                    print("     ⚠️ 丢掉一条：里头有原文没写的名字 " + "/".join(bad))
                    continue
                if len(x["body"]) > len(best.get(x["i"], {}).get("body", "")):
                    best[x["i"]] = x         # 留最长的一版兜底
                if len(x["body"]) >= 120 and not x["body"].rstrip().endswith(("？", "?")):
                    part.append(x)
                # 太短 / 问句收尾的算没交，逼模型补写
            # 整批为空且模型没回内容，多半是限流/超时，再要一次只是白等
            if len(part) >= len(chunk) or not (last or "").strip():
                break
            note = ("\n\n上一版太短（有的只有一两句）或用问句收尾。这次每条必须 200 字以上、"
                    "3~5 段、每段 60~110 字，结尾用陈述句，不许提问。")
        # 补不齐就用写过最长的那版兜底，别让整条消失
        have = {x["i"] for x in part}
        for n in range(k + 1, k + 1 + len(chunk)):
            if n not in have and n in best:
                part.append(best[n])
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


# 站点自己的 logo / 头像 / 占位图一律不要：热榜页里这些比正文图还多，
# 混进来就是「图裂」和「配了张无关的图」。
_IMG_SKIP = re.compile(
    r"logo|icon|avatar|gravatar|emoji|qrcode|qr_|placeholder|default|blank|spacer|static|"
    r"header-image|def_man|badge|sprite|loading|advert|banner|ads?/", re.I)
# 只认「上传目录」里的正文图；认不出就不配图，宁缺毋滥
_IMG_OK = re.compile(
    r"blogfile|newspost|upload|article|content|photo|/pai/|/image/|/202[0-9]/|/news/", re.I)
_BLOCK = re.compile(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>")


def _page(url, timeout=15):
    """抓原贴页面。慢、失败都返回空串（不重试，别拖垮整轮）。"""
    try:
        req = urllib.request.Request(url, headers=dict(gx._HEADERS))
        with gx._OPENER.open(req, timeout=timeout) as resp:
            return resp.read(250_000).decode("utf-8", "ignore")
    except Exception:            # noqa: BLE001 - 抓不到很正常，不该拖慢整轮
        return ""


def _img_from(page):
    """优先 og:image；没有就退到正文第一张图（虎扑这类根本不放 og 标签）。"""
    for tag in _OG_TAG.findall(page):
        if not any(k in tag for k in _OG_KEYS):
            continue
        m = re.search(r'content=["\']([^"\']+)', tag, re.I)
        if m:
            src = html.unescape(m.group(1))
            if src.startswith("http") and not _IMG_SKIP.search(src):
                return src
    for raw in re.findall(r'<img[^>]+(?:data-src|data-original|src)=["\']([^"\']+)', page, re.I):
        src = html.unescape(raw)
        if src.startswith("//"):
            src = "https:" + src
        if not src.startswith("http") or _IMG_SKIP.search(src):
            continue
        wh = re.search(r"_w_(\d+)_h_(\d+)", src)
        if wh and int(wh.group(1)) < 300 and int(wh.group(2)) < 300:
            continue                      # 缩略图/头像，撑不起封面
        if _IMG_OK.search(src):
            return src
    return ""


def _text_from(page):
    """正文摘录：去标签后取较长行，去重，最多 900 字。"""
    text = _BLOCK.sub(" ", page)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", "\n", text))
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines()]
    lines = [x for x in lines if len(x) >= 22]
    return "\n".join(dict.fromkeys(lines))[:900]


def fetch_article(url, timeout=15):
    """一次请求同时拿配图和正文摘录；抓不到就都是空。"""
    page = _page(url, timeout)
    return {"img": _img_from(page) if page else "", "text": _text_from(page) if page else ""}


def fetch_og_image(url, timeout=15):
    return fetch_article(url, timeout)["img"]


def x_intent(title, body=""):
    """X 的预填发帖链接。正文长过 X 自己的上限时 X 会截，这里不替你删。"""
    text = f"{title}\n\n{body}" if body else title
    return "https://x.com/intent/post?text=" + quote(text)


def render_pack(picked, wrote, site, handle="", now=None):
    """打包页：封面图 + 标题 + 正文 + 话题，一条一条照抄就能发。"""
    # 跑在 GitHub Actions 上时机器是 UTC，直接 now() 会让北京凌晨的页面标成昨天
    now = now or datetime.now(timezone(timedelta(hours=8)))
    by_i = {int(x.get("i") or 0): x for x in wrote}
    cards = []
    for n, row in enumerate(picked, 1):
        it = row["item"]
        orig = it.get("orig") or ""
        # 没图就不留空列：10 条里通常一条图都没有，那个 300px 的空档太显眼
        pic = (f'<div class="cov"><img src="{html.escape(orig)}" alt="原贴配图" '
               f'referrerpolicy="no-referrer" loading="lazy"></div>'
               if orig else "")
        nopic = "" if orig else '<div class="why">原贴没给配图</div>' 
        w = by_i.get(n) or {}
        title = w.get("title") or it["title"]
        body = w.get("body") or "（这轮没调模型，只有筛选结果；配上 AI key 再跑一次就有正文）"
        tags = w.get("tags") or []
        tagline = " ".join("#" + re.sub(r"\s+", "", t).lstrip("#") for t in tags)
        # 今日头条没有公开发帖 API，只能给一份可复制的稿 + 发布页链接
        tt = ""
        if w.get("tt_body"):
            ttext = (w.get("tt_title") or title) + "\n\n" + w["tt_body"]
            tt = f"""<details class="tt"><summary>头条版（复制即发）</summary>
      <pre class="body" id="tt{n}">{html.escape(ttext)}</pre>
      <button class="cp" data-for="tt{n}" type="button">复制头条文案</button>
      <a href="https://mp.toutiao.com/profile_v4/graphic/publish" target="_blank" rel="noreferrer">打开头条发布页</a>
    </details>"""
        cards.append(f"""<section class="card">
  {pic}
  <div class="txt">
    <div class="rank">0{n} · {'/'.join(it['platforms'])} 第{it['rank']}位
      <span class="sc">匹配度 {row['score']}</span></div>
    <h2>{html.escape(title)}</h2>
    <pre class="body">{html.escape(body)}</pre>
    <div class="tags">{html.escape(tagline)}</div>
    <div class="xpost"><a href="{html.escape(x_intent(title, w.get('body') or ''))}" target="_blank" rel="noreferrer">🐦 一键发 X</a></div>
    {tt}
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
 details.tt{{margin:8px 0 14px;border:1px solid #2a2a33;border-radius:10px;padding:10px 12px}}
 details.tt summary{{cursor:pointer;font-size:13px;color:#9a9aa6}}
 details.tt pre{{margin:10px 0;font-size:13px;line-height:1.8}}
 .cp{{padding:6px 14px;border-radius:999px;border:1px solid #3a3a46;background:#242430;
      color:#e9e9ef;font-size:13px;cursor:pointer;margin-right:12px}}
 a{{color:#6ec1ff}}
</style></head><body>
<header><h1>今日热点精选 · {now:%m-%d}（{len(picked)} 条）</h1>
<div class="sub">标题 + 正文 + 话题 + 原贴链接，一条一条照着发即可。{'@' + handle if handle else ''}</div></header>
<div class="wrap">{''.join(cards)}</div>
<script>
document.querySelectorAll('button.cp').forEach(function (b) {{
  b.onclick = function () {{
    var pre = document.getElementById(b.dataset.for);
    if (!pre) return;
    navigator.clipboard.writeText(pre.textContent).then(function () {{
      b.textContent = '已复制';
      setTimeout(function () {{ b.textContent = '复制头条文案'; }}, 1500);
    }});
  }};
}});
</script></body></html>"""


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
        ("标题 + 正文 + 话题 + 头条版都排好了，点开照着发："
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
    types_demo = [{"name": "婚房算账", "desc": "x", "sign": ["彩礼", "婚房"], "hot": 0.2}]
    demo = [{"title": "彩礼涨到38万，男方当场走人", "url": "", "platforms": ["微博"],
             "rank": 1, "count": "", "trend": "", "group": "彩礼婚恋"}]
    typed = score_news(demo, {"styles": [], "hooks": []}, [], types=types_demo)
    assert typed[0]["type"] == "婚房算账" and "类型「婚房算账」" in typed[0]["why"], typed[0]
    pool_types = [{"name": "翻车现场", "sign": ["翻车"], "hot": 0.3}]
    sc = score_news(items, fp, ["楼市", "翻车"], types=pool_types)
    assert all("开售" not in r["item"]["title"] for r in sc), "广告稿没被踢掉"
    assert sc[0]["item"]["title"].startswith("商k"), sc[0]
    assert sc[-1]["item"]["title"].startswith("购房贷款"), "新闻腔没被压分"
    assert sc[0]["fit"] and not sc[-1]["fit"], "只有命中池里的类型才够格，政策稿不该"
    picked = pick_top(sc, 2, ["楼市", "翻车"])
    assert "备选（未命中池里的类型）" in picked[1]["why"], picked[1]
    assert len(picked) == 2
    picked[0]["item"]["orig"] = "https://e.com/p.jpg"
    page = render_pack(picked, [{"i": 1, "title": "标题", "body": "正文", "tags": ["a"],
                                 "tt_title": "头条标题", "tt_body": "头条正文"}],
                       "https://example.com")
    assert 'src="https://e.com/p.jpg"' in page and "标题" in page
    assert "原贴没给配图" in page, "没有原图的那条不该配图"
    assert "cov nopic" not in page, "没图的条目不该留空列"
    assert page.count("<script") == 1, "页面只该有自己那段复制脚本"
    assert "头条版（复制即发）" in page and "头条正文" in page, "头条版没出来"
    assert 'id="tt2"' not in page, "没写头条版的那条不该有复制块"
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
    # 写死的题材偏好必须已经清掉：婚恋类标题不该靠关键词自动加分
    none_types = score_news([{"title": "彩礼涨到38万，男方当场走人", "platforms": ["微博"],
                              "rank": 1, "group": "", "url": ""}], fp, [], types=None)
    assert none_types[0]["score"] == 2, none_types[0]      # 只剩榜内前三那 2 分
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
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--handle", default="")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--no-push", action="store_true", help="不推飞书（本地试跑用）")
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
    # 当天从范文池总结出来的内容类型：挑新闻和写作都用它（gen_x_card 那步已经写好）
    types = gx.load_types(Path(args.out_dir).parent / "x")
    if types:
        print(f"  内容类型 {len(types)} 类："
              + "、".join(t["name"] for t in types[:6]))
    if weights:
        top = sorted(weights.items(), key=lambda kv: -kv[1])[:5]
        print("  近 14 天学出来的爆火机制：" + "、".join(f"{k} {v:.2f}" for k, v in top))
    scored = score_news(items, fp, keywords, weights, types)
    picked = pick_top(scored, args.top, keywords)
    print(f"新闻筛选：{len(items)} 条候选 -> 取 {len(picked)} 条")
    print(f"  今天的高频题材词：{'、'.join(keywords[:10])}")
    for n, row in enumerate(picked, 1):
        it = row["item"]
        print(f"  {n:>2}. [{row['score']:>4}] {it['title'][:40]}"
              + (f"   ← {'｜'.join(row['why'])}" if args.explain else ""))

    pool = gx._bd_pool(Path(args.out_dir).parent / "x")
    stats = gx.bangdan_stats(pool)
    skill = gx.load_skill(Path(args.out_dir).parent / "x")
    print(f"  范文池累积 {len(pool)} 篇"
          + (f"，规模中位 {stats['len']} 字" if stats else "")
          + f"；按 {len(types)} 类内容类型匹配"
          + ("，写法规则已载入" if skill else ""))
    # 先把原文/配图抓回来：写作靠它撑篇幅，页面靠它配图（10 条并发，只花最长一条的时间）
    with ThreadPoolExecutor(max_workers=6) as pool:
        arts = list(pool.map(lambda r: fetch_article(r["item"]["url"]), picked))
    for row, art in zip(picked, arts):
        row["item"]["orig"] = art["img"]
        row["item"]["excerpt"] = art["text"]
    wrote = [] if args.no_ai else xhs_write(picked, fp, types=types, stats=stats, skill=skill)
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
        if not args.no_push:
            push_feishu(args.site, len(picked), picked[0]["item"]["title"], got, bool(wrote))
        else:
            print("  已跳过飞书推送（--no-push）")


if __name__ == "__main__":
    main()
