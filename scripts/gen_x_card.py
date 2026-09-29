#!/usr/bin/env python3
# coding=utf-8
"""从最新报告生成两个板块的内容。

板块一（官方消息 -> 卡片图 -> 飞书）
    docs/x/card.html   1200x1500 竖版卡片，交给无头浏览器截图成 PNG
    排序：多平台共振优先（同题在多平台同时上榜），再比平台内名次。

板块二（非官方段子 -> 纯文字 -> X）
    docs/x/copy.txt    推文文案草稿
    排序：同样是共振优先，但叠加「排他性」权重——各平台第 1 条是所有人都在抄的，
    给它降权，优先「多平台 + 还在上升 + 名次在中段」的条目。

两个板块靠标题特征区分（见 is_gossip 上面的 _GOSSIP_* 规则）。
条目附带上榜平台、最高名次、排名涨跌、在榜次数 —— 这些报告里本来就有，不额外调 AI。

用法：
    python3 scripts/gen_x_card.py
    python3 scripts/gen_x_card.py --source docs/reports/latest/current.html --top 8
    python3 scripts/gen_x_card.py --selftest
"""

import argparse
import html
import os
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_REPO = os.environ.get("GITHUB_REPOSITORY", "troublewater/hot-news-radar")
DEFAULT_SITE = (os.environ.get("SITE_URL") or f"https://{_REPO.replace('/', '.github.io/')}").rstrip("/")

_SOURCE_RE = re.compile(r'<span class="source-name">([^<]+)</span>')
_RANK_RE = re.compile(r'<span class="rank-num[^"]*">([^<]+)</span>')
_COUNT_RE = re.compile(r'<span class="count-info">([^<]+)</span>')
_LINK_RE = re.compile(r'<a[^>]+href="(http[^"]+)"[^>]*class="news-link"[^>]*>(.{2,220}?)</a>', re.S)
_GROUP_RE = re.compile(r'<div class="word-name">(.*?)</div>', re.S)


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
    """解析报告 HTML -> [{'title','url','platforms','rank','trend','count','group'}]，同题合并。

    group 是这条所属的关键词组名（报告里每个 word-group 的 word-name），
    卡片就是靠它分「官方 / 段子」两个板块的。
    """
    merged = {}
    # 报告先按关键词分组（word-group + word-name），组内才是 news-item。
    # 必须先切组，否则不知道每条属于哪个关键词，就没法分板块。
    for section in report_html.split('<div class="word-group"')[1:]:
        gm = _GROUP_RE.search(section)
        group = html.unescape(re.sub(r"<[^>]+>", "", gm.group(1))).strip() if gm else ""
        for chunk in section.split('<div class="news-item')[1:]:
            link = _LINK_RE.search(chunk)
            if not link:
                continue
            url = link.group(1)
            title = html.unescape(re.sub(r"<[^>]+>", "", link.group(2))).strip()
            if len(title) < 4:
                continue
        # 报告把同题的多平台并成一个 span，用 + 连接（如 "微博+知乎"），必须拆开，
        # 否则每条都只有 1 个"平台"，共振优先排序和共振标签全部失效。
            platforms = [
                html.unescape(p).strip()
                for raw in _SOURCE_RE.findall(chunk)
                for p in raw.split("+")
            ]
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
                    "rank": rank, "trend": trend, "count": count, "group": group,
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


# ── 板块划分 ──────────────────────────────────────────────────
# 候选池完全来自 frequency_words.txt（报告已经是按它筛过的），
# 这里只决定「这条进图、还是进文案」：
#   读起来像「有人、有故事」的段子 -> 板块二（X 文案）
#   其余 -> 板块一（官方消息，出图发飞书）
#
# 为什么不直接拿关键词组名分类：组名是给「筛进报告」用的，太糙。
# 试过按组切，像"4家公司今日官宣回购"会因为「官宣」被判进娱乐圈、
# "中国男子百米接力夺冠"会因为「男子」被判进街坊故事。
#
# 强特征词：标题里单独出现就算段子
_GOSSIP_HARD = re.compile(
    r"彩礼|嫁妆|相亲|催婚|催生|婆媳|上门女婿|二婚|出轨|小三|家暴|认亲|"
    r"塌房|翻车|人设|绯闻|丑闻|私生子|桃色|致歉|"
    r"吐槽|破防|绷不住|离谱|奇葩|无语|活久见|逆天|神操作|炸裂|"
    r"网红|主播|直播间|带货|打赏|摆拍|卖惨|蹭流量|"
    r"我有一个朋友|我有一个兄弟|闺蜜|室友|亲戚|妈宝|渣男|渣女|小仙女|捞女|双标|"
    r"讨薪|欠薪|摊主|街坊|保安|外卖员|快递员"
)
# 弱特征词：光有「男子/女子/网友」是体育和时政的常客，必须再配上「有故事」的动词才算
_GOSSIP_SOFT = re.compile(r"网友|男子|女子|大爷|大妈|小伙|姑娘|司机|乘客|路人|邻居")
_GOSSIP_STORY = re.compile(
    r"吐槽|回应|被抓|被拘|求助|围观|举报|维权|挨|骂|怼|吵|赔|罚|救|捡|骗|丢|抢|偷|哭|怒|质问"
)


def is_gossip(item):
    title = item["title"]
    return bool(_GOSSIP_HARD.search(title)) or bool(
        _GOSSIP_SOFT.search(title) and _GOSSIP_STORY.search(title)
    )


# 「段子度」：这几类词命中说明是真瓜，不是正能量好人好事，给板块二加分。
# 名单外的（认亲、致歉这类）也不删，只是排在后面。
_JUICY = re.compile(
    r"彩礼|嫁妆|相亲|催婚|婆媳|上门女婿|出轨|小三|家暴|"
    r"塌房|翻车|人设|绯闻|丑闻|离谱|奇葩|逆天|神操作|破防|"
    r"吐槽|讨薪|欠薪|被抓|被拘|偷|抢|骗|吵|骂|赔|哭|怒|质问|争论|中奖"
)


def _pick_score(it):
    """板块二（X 文案）的排他性 + 段子度权重。

    各平台第 1 条是每个搬运号都在抄的，抄它等于和别人发一样的东西，所以给榜首降权；
    优先「多平台 + 还在上升 + 名次在中段（4-15）」的条目 —— 热度够了，但还抄的人少。
    数值都是拍的，觉得挑得不对直接改这里的加减分。
    """
    score = len(it["platforms"]) * 10
    if _JUICY.search(it["title"]):
        score += 8          # 段子度：真瓜优先，权重略低于一个平台
    if it["trend"] == "up":
        score += 6
    rank = it["rank"]
    if rank == 1:
        score -= 5
    elif rank <= 3:
        score -= 2
    elif rank <= 15:
        score += 3
    # 长标题塞进正文像论坛提问，不是帖子该有的句子，压一压
    if len(it["title"]) > 26:
        score -= (len(it["title"]) - 26) // 2 + 1
    return score


def _sort_key(it):
    return (-_pick_score(it), -len(it["platforms"]), it["rank"])


def split_channels(items):
    """把条目切成 (官方, 段子)。判定规则见上面的 _GOSSIP_* 。"""
    official = [it for it in items if not is_gossip(it)]
    gossip = [it for it in items if is_gossip(it)]
    # 报告要是回到「全量模式」（只有一个「全部新闻」组），两类会一边倒成空，
    # 那就两边都用全量兜底，别让卡片或文案开天窗。
    if not official:
        official = list(items)
    if not gossip:
        gossip = list(items)
    return official, gossip


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
justify-content:flex-end;font-size:20px;color:#6f7da6;letter-spacing:1px}
"""


def _tag(cls, text):
    return f'<span class="tag {cls}">{html.escape(text)}</span>' if cls else f'<span class="tag">{html.escape(text)}</span>'


def render_card(items, site, now=None, top=8, handle=""):
    now = now or datetime.now()
    picked = items[:top]
    resonant = sum(1 for it in picked if len(it["platforms"]) > 1)

    rows = []
    for i, it in enumerate(picked, 1):
        # 平台名合成一个标签：逐平台一个标签会挤成 8~11 个，太长还会换行把下面
        # 的徽章顶到第二行。只列前 3 个，数量交给「多平台共振 ×N」表达。
        names = it["platforms"]
        tags = [_tag("", "+".join(names[:3]) + (" 等" if len(names) > 3 else ""))]
        if len(it["platforms"]) > 1:
            tags.append(_tag("hot", f"多平台共振 ×{len(it['platforms'])}"))
        if it["rank"] < 9999:
            tags.append(_tag("", f"第 {it['rank']} 位"))
        # 只标「上升」。上游把几乎整张榜都算成 down（实测 137 条里 23 down / 2 up），
        # 满屏「↓ 下降」像坏掉了，而榜单里本来都是往上走的话题，下行信息没有价值。
        if it["trend"] == "up":
            tags.append(_tag("up", "↑ 上升"))
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
<footer><span>{html.escape(handle)}</span></footer>
</body></html>
"""


# 板块二的文案：不是一串标题，而是一段「像人写的帖子」。
# 开头亮态度、中间把当天最像段子的几条串起来、结尾自己先表态再抛问题引讨论。
# 底线：包装词都是通用套话，绝不给标题加戏——不能编事实。
# 池子按日期轮换，所以每天读起来不一样，不会天天同一句。
_OPENERS = [
    "扒了一圈热搜，最没道理的是这几条：",
    "今天这几个事儿，一个比一个离谱：",
    "刷了一晚上，挑几条最让我想不通的：",
    "今天热搜挺有意思，挑几条说说：",
    "挑几条刚刷到的，你们感受一下：",
]
# 结尾先亮自己的态度、再抛问题，这是「钩子」那一环。
# 注意：每句都必须对任何选题都成立——通用套话一旦押中具体情节就会翻车，
# 比如「等个反转」配到认亲这种正能量新闻上，一眼假。
_TAKES = [
    "我个人的私心是第{n}条，蹲个后续。",
    "这几条里我最想看第{n}条怎么收场。",
    "别的先不说，第{n}条我盯上了。",
    "我自己先站第{n}条，等下文。",
]
_ASKS = [
    "你刷到哪条了？评论区聊聊。",
    "这几条你怎么看？",
    "你们身边有类似的吗？",
    "换你你会怎么办？",
]
_MARKS = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
          "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳",
          "㉑", "㉒", "㉓", "㉔", "㉕", "㉖", "㉗", "㉘", "㉙", "㉚"]


def render_copy(items, site, now=None, top=25):
    """板块二 X 文案：一篇帖子，不是三条。

    条目按 _sort_key 排（含段子度和排他性权重），不照抄榜一。
    site 参数保留仅为兼容旧调用，文案里不再出现站点链接和话题标签。

    注意长度：条数是 --copy-top，默认 25。中文按 2 字符计，
    20 条左右就远超普通账号的 280 上限了——这是给 X Premium 长文或拆成串用的。
    """
    now = now or datetime.now()
    picked = sorted(items, key=_sort_key)[:top]
    seed = now.toordinal()

    lines = [_OPENERS[seed % len(_OPENERS)], ""]
    for i, it in enumerate(picked):
        mark = _MARKS[i] if i < len(_MARKS) else f"{i + 1}."
        lines.append(f"{mark} {it['title']}")
    lines.append("")
    if picked:
        # 三个池子用不同的步长取，保证「每天都不一样」——
        # 之前用 seed//3、seed//5，连着的两天整除结果相同，出来的句子一模一样。
        take = _TAKES[(seed * 3) % len(_TAKES)].format(n=_MARKS[seed % len(picked)])
        lines.append(take)
        lines.append(_ASKS[(seed * 5) % len(_ASKS)])
    return "\n".join(lines)



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="docs/reports/latest/current.html")
    ap.add_argument("--out-dir", default="docs/x")
    ap.add_argument("--site", default=DEFAULT_SITE)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--copy-top", type=int, default=25, help="X 文案里列几条")
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
        down = dict(demo[0], trend="down")
        assert "下降" not in render_card([down], args.site, handle="@demo"), "下降标签该被去掉"
        long3 = [{"title": "测试条目", "url": "https://example.com",
                  "platforms": ["甲", "乙", "丙", "丁", "戊"], "rank": 9, "trend": "", "count": ""}]
        card3 = render_card(long3, args.site, handle="@demo")
        assert "甲+乙+丙 等" in card3 and "丁" not in card3, "长平台列表没截断"
        # 报告是多平台合并成一个 span（+ 连接），这条守住解析不再退化成 1 个平台
        joined = parse_items(
            '<div class="word-group"><div class="word-name">微博热搜</div>'
            '<div class="news-item new"><span class="source-name">微博+知乎+抖音</span>'
            '<a href="https://e.com/x" class="news-link">测试标题四个字</a></div></div>'
        )
        assert joined and joined[0]["platforms"] == ["微博", "知乎", "抖音"], (
            "多平台合并的 source-name 没拆开",
            joined and joined[0]["platforms"],
        )
        assert joined[0]["group"] == "微博热搜", "条目所属的关键词组名没解析出来"
        assert all(it["group"] for it in items), "报告里解析出了没有关键词组的条目"

        # 分板块：强特征词直接算段子；弱特征词要配上有故事的动词，别把体育/时政误伤
        fake = [
            {"title": "官方一条", "group": "", "platforms": ["甲"], "rank": 1, "trend": "", "count": ""},
            {"title": "女子相亲被要20万彩礼", "group": "", "platforms": ["乙"], "rank": 2, "trend": "up", "count": ""},
        ]
        off2, gos2 = split_channels(fake)
        assert [it["title"] for it in off2] == ["官方一条"], f"板块切分错：{off2}"
        assert [it["title"] for it in gos2] == ["女子相亲被要20万彩礼"], f"板块切分错：{gos2}"
        assert not is_gossip({"title": "中国男子百米接力夺冠"}), "光有「男子」不该算段子"
        assert not is_gossip({"title": "4家公司今日官宣回购"}), "「官宣」不该算段子"
        assert is_gossip({"title": "司机接到盲人乘客两人聊着聊着都哭了"}), "弱特征+故事动词没算成段子"

        # 排他性：同为 2 平台时，「上升 + 中段名次」要压过「榜首」
        top1 = {"title": "榜首", "group": "x", "platforms": ["甲", "乙"], "rank": 1, "trend": "", "count": ""}
        mid = {"title": "上升中段", "group": "x", "platforms": ["甲", "乙"], "rank": 7, "trend": "up", "count": ""}
        assert _sort_key(mid) < _sort_key(top1), "排他性权重没生效（上升+中段应排在榜首前面）"
        # 板块二文案：写成帖子，且不许再出现链接和话题标签
        copy = render_copy(items, "https://example.com", now=datetime(2026, 9, 29, 8, 0))
        assert any(copy.startswith(o) for o in _OPENERS), f"开头没走轮换池：{copy[:20]}"
        assert "① " in copy and items[0]["title"] in copy, "条目没串进文案"
        assert any(a in copy for a in _ASKS), "结尾没抛问题引讨论"
        assert "http" not in copy and "#" not in copy, "文案里不该再有链接或话题标签"
        # 换个日期应该换一套说法，不然天天一个味
        copy2 = render_copy(items, "https://example.com", now=datetime(2026, 9, 30, 8, 0))
        assert copy != copy2, "不同日期文案完全一样，等于没做变化"
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

    official, gossip = split_channels(items)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    handle = args.handle or "@DrAnswerMe"

    # 板块一：官方消息 -> 卡片图（飞书里的图）
    (out / "card.html").write_text(
        render_card(official, args.site, top=args.top, handle=handle), encoding="utf-8")
    # 板块二：整份榜单 -> 纯文字（发 X 用）。段子度/排他性只管排序，
    # 因为一篇要列几十条，光靠段子池（常常不到 10 条）凑不出来。
    (out / "copy.txt").write_text(render_copy(items, args.site, top=args.copy_top), encoding="utf-8")

    print(f"板块一 官方·图  {len(official):>3} 条 -> {out/'card.html'}，取前 {args.top}")
    for i, it in enumerate(official[: args.top], 1):
        print(f"  {i}. [{it['group']}] {it['title'][:38]}")
    copy_text = (out / "copy.txt").read_text(encoding="utf-8")
    weighted = sum(2 if ord(c) > 127 else 1 for c in copy_text)
    warn = "（超 280，需要 Premium 长文或拆串）" if weighted > 280 else "（普通账号发得下）"
    print(f"板块二 文案      {len(items):>3} 条 -> {out/'copy.txt'}，取前 {args.copy_top}"
          f"，加权 {weighted} 字符 {warn}")
    for i, it in enumerate(sorted(items, key=_sort_key)[:args.copy_top], 1):
        print(f"  {i:>2}. {it['title'][:44]}")


if __name__ == "__main__":
    main()