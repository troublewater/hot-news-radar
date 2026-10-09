#!/usr/bin/env python3
# coding=utf-8
"""从最新报告生成两个板块的内容。

板块一（官方消息 -> 卡片图 -> 飞书）
    docs/x/card.html   1200x1500 竖版卡片，交给无头浏览器截图成 PNG
    排序：多平台共振优先（同题在多平台同时上榜），再比平台内名次。

板块二（非官方段子 -> 纯文字 -> X）
    docs/x/copy.txt    单条发帖素材（12 条，分隔线隔开）
    docs/x/sources.txt 抓来的原始素材：出处 + 原文链接 + 正文，查重和补细节用
    先抓一批原贴当「事实毛坯」（公众号走搜狗微信、虎扑步行街、百度贴吧热议），
    再把当天热搜里的钩子话题 + 这批毛坯交给模型，改写成隔断式小故事（不求真实）。
    这样每条都是「有人有事」的完整故事，而不是标题 + 摘要 + 几个热评拼出来的碎片。
    模型不可用（没配 AI_API_KEY / 调用失败）时退回原贴原样发出，不开天窗。

两个板块靠标题特征区分（见 is_gossip 上面的 _GOSSIP_* 规则）。
条目附带上榜平台、最高名次、排名涨跌、在榜次数 —— 这些报告里本来就有，不额外调 AI。

用法：
    python3 scripts/gen_x_card.py
    python3 scripts/gen_x_card.py --source docs/reports/latest/current.html --top 8
    python3 scripts/gen_x_card.py --selftest
"""

import argparse
import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
            # 报告里的链接是 HTML 转义过的（贴吧那条带 &amp;topic_id），不反转义点开就断
            url = html.unescape(link.group(1))
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


# 「XX 开售，4999 元」这类是电商稿，不是热点。用户被小米冰箱的稿子坑过。
# 以后再遇到新的带货句式，往这一组词里加就行（判定只看标题）。
_AD_HOT = re.compile(
    r"开售|预售|首销|上架|开启预约|预约开启|正式开卖|新品首发|直降|立减|领券|券后|"
    r"到手价|秒杀|包邮|清仓|满减|限时购|优惠力度")


def split_channels(items):
    """把条目切成 (官方, 段子)。判定规则见上面的 _GOSSIP_* 。"""
    items = [it for it in items if not _AD_HOT.search(it["title"])]
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
.txt{font-size:36px;font-weight:700;line-height:1.28;letter-spacing:.5px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tags{margin-top:11px;display:flex;gap:10px;flex-wrap:nowrap;overflow:hidden}
.tag{font-size:20px;color:#a8b8e4;border:1px solid rgba(255,255,255,.24);border-radius:999px;padding:4px 16px}
.tag.hot{color:#ffd166;border-color:rgba(255,209,102,.5)}
.tag.up{color:#5ee08a;border-color:rgba(94,224,138,.45)}
.tag.down{color:#ff8f8f;border-color:rgba(255,143,143,.45)}
.tag.solo{color:#98a4c6;border-color:rgba(152,164,198,.42)}
.mx{display:flex;gap:6px}
.mx b{font-weight:500;font-size:19px;color:#4c5678;border-radius:8px;padding:3px 10px;
background:rgba(255,255,255,.06);white-space:nowrap}
.mx b.on{color:#0b1020;font-weight:700;background:linear-gradient(135deg,#ffd166,#ff9f43)}
footer{border-top:1px solid rgba(255,255,255,.14);padding-top:20px;display:flex;
justify-content:flex-end;font-size:20px;color:#6f7da6;letter-spacing:1px}
"""


def _tag(cls, text):
    return f'<span class="tag {cls}">{html.escape(text)}</span>' if cls else f'<span class="tag">{html.escape(text)}</span>'


def render_card(items, site, now=None, top=8, handle=""):
    now = now or datetime.now()
    picked = items[:top]
    resonant = sum(1 for it in picked if len(it["platforms"]) > 1)
    # 频道矩阵：列固定为「本批出现最多的几个频道」，上下几行的方块才能对齐着看，
    # 哪条是全网共振、哪条只是单频道热门，扫一眼就知道，好决定搬哪条。
    seen = {}
    for it in items:                       # 按整张榜统计，不只看前 8 条
        for p in it["platforms"]:
            seen[p] = seen.get(p, 0) + 1
    cols = [p for p, _ in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))][:6]

    rows = []
    for i, it in enumerate(picked, 1):
        # 矩阵格子 = 本批的频道列；亮起的格子 = 这条在该频道上过榜
        names = it["platforms"]
        cells = "".join(
            f'<b class="{"on" if c in names else ""}">{html.escape(c)}</b>' for c in cols)
        tags = [f'<span class="mx">{cells}</span>']
        if len(names) > 1:
            tags.append(_tag("hot", f"多平台共振 ×{len(names)}"))
        elif cols:
            tags.append(_tag("solo", "单频道热门"))
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


# ── 段子正文从哪来 ─────────────────────────────────────────────────────
# 榜单接口每条只有 id/title/url，没有正文，所以「标题 + 内容」里的内容只能另找。
# 搜狗微信是唯一免登录、还能同时拿到「标题 + 文章首段摘要」的公众号入口；
# 已实测 GitHub runner（美国机房）可直连。知乎热榜从 CI 过去是 403，别再试。
def _abs_sogou(href):
    """搜狗吐出来的 href 里带空格（搜索词里有空格时），不编码直接请求会报 InvalidURL。"""
    return "https://weixin.sogou.com" + re.sub(r"\s+", "%20", html.unescape(href))


# 三组：文章链接、标题、首段摘要。链接只是跳板，正文补全会换成正主地址。
_SOGOU_PAT = re.compile(
    r'(?s)<div class="txt-box">.*?<h3>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'<p class="txt-info"[^>]*>(.*?)</p>')

# 用户要的是「脑洞大开」，不是「儿女情长」。两组词各轮一半，
# 光靠排序调不动配比——池子里本来就是家长里短多，得从搜索词源头掰。
_STORY_WEIRD = [
    "我有一个朋友", "细思极恐", "社死 现场", "离谱 经历", "万万没想到",
    "神操作", "整活 翻车", "奇葩 室友", "奇葩 同事", "反转 故事",
    "AI 整活", "程序员 翻车", "买家秀 翻车", "迷惑 行为",
]

# 争议/对比：用户点名要的「AMD 和 Intel」那一类，吵得起来的题目
_STORY_FIGHT = [
    "AMD Intel", "苹果 安卓", "预制菜", "国产 进口", "AI 取代",
    "电车 油车", "小米 华为", "学历 贬值",
]

# 按天轮换，一次只问 12 个：搜狗按 IP 限流，一口气问太多会被拉黑。
_STORY_KEYWORDS = [
    "彩礼涨价", "相亲翻车", "婆婆 房子", "大姑姐", "扶弟魔", "婚闹",
    "退婚 不退钱", "亲戚借钱", "家长群", "份子钱", "同学会", "月子仇",
    "婆媳 矛盾", "小舅子", "婚后 工资上交", "婚前 房子加名",
    "同事 甩锅", "老板 画饼", "邻居 噪音", "物业 业主", "装修 被坑",
    "楼上 漏水", "借钱 不还", "教育 内卷",
]

# 题材归类：一批素材里同一题材最多 2 条，否则 30 条里六七条都是份子钱，一眼机器。
# 词表要够宽，漏掉的题材（份子钱、扶弟魔）根本没机会被压。
_THEME_WORDS = [
    "彩礼", "嫁妆", "相亲", "订婚", "退婚", "离婚", "出轨", "前任", "婚闹",
    "婆婆", "公公", "大姑", "小姑", "小叔", "舅子", "扶弟", "月子", "婆媳",
    "份子钱", "随礼", "借钱", "还钱", "拆迁", "房子", "房贷", "学区房",
    "工资", "老板", "同事", "加班", "裁员", "邻居", "物业", "装修",
    "孩子", "家长群", "学校", "老师", "医院", "养老", "父母", "亲戚",
    "网购", "快递", "外卖", "手机", "游戏",
]

# 上面这些里属于「儿女情长婚嫁」的，单独算配额
_FAMILY_WORDS = {
    "彩礼", "嫁妆", "相亲", "订婚", "退婚", "离婚", "出轨", "前任", "婚闹",
    "婆婆", "公公", "大姑", "小姑", "小叔", "舅子", "扶弟", "月子", "婆媳",
    "份子钱", "随礼",
}

# 脑洞/离谱/反转：用户点名要这一类，排序上给最高优先
_STORY_FUN = re.compile(
    r"脑洞|离谱|奇葩|沙雕|神操作|骚操作|反转|万万没想到|整活|笑死|抽象|逆天|绝了|"
    r"赛博|科幻|黑客|脑回路|迷惑|破防|离奇|魔幻|神评|细思极恐|社死|"
    r"我(有)?一个朋友")

# 婚嫁家事：这类够多了，排序往后放，配额也单独卡
_STORY_HOT = re.compile(
    r"彩礼|嫁妆|婆婆|公公|大姑|小姑|小叔|舅子|扶弟|婚闹|相亲|订婚|退婚|离婚|出轨|"
    r"房子|房产|加名|工资|存款|借钱|份子钱|月子|学区房|法院|起诉|报警")

# 连载小说、引流广告都不是真事，别混进帖子里
# 盘点/大赏是「一年沙雕新闻合集」那种二手汇编，不是单个故事，混进来全是陈年旧闻
_STORY_NOISE = re.compile(
    r"（上）|（下）|\(上\)|\(下\)|第[一二三四五六七八九十\d]+章|合集|推荐阅读|点击阅读原文|"
    r"推荐上集|上集阅读|下集阅读|未完待续|盘点|大赏|年度|十大|榜单|图集|网友直呼")
_STORY_AD = re.compile(
    r"个人微信|微信号|加我微信|扫码关注|点击上方|淡斑|祛斑|减肥|瘦身|养生|偏方|根治|特效|包邮|"
    r"㎡|洋房|楼盘|户型|首付|总价|样板间")
# 标题里带这些的，正文写得再像样也是「一整年沙雕新闻合集」或软广，不是单个故事
_STORY_NOISE_TITLE = re.compile(
    r"沙雕|奇葩新闻|盘点|大赏|出炉|来袭|年度|十大|榜单|图集|^\d{4}年|"
    r"设备|厂家|批发|招商|代理|定制|供应|语音盒|海量|包邮")
# 百科词条式的开头（「彩礼，中国旧时婚礼程序之一」）不是故事
_STORY_WIKI = re.compile(r".{0,16}(又称|也称|是一种|是指|释义)")


def _clean_text(s):
    """搜狗把中文标点统一换成了半角，直接发出去很出戏。"""
    s = html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
    # 虎扑正文里的 <img> 属性带 > 时，上面的标签正则切不干净，会剩下一截图片 URL
    s = re.sub(r"\S*(?:x-oss-process|/quality/|ignore-error)\S*", " ", s)
    s = re.sub(r"[.．]{2,}$", "…", s)                 # 先收尾，否则句号会被拆成「。…」
    s = re.sub(r'^[”"’]+', "", s)                     # 摘要常从半句引号中间切进来
    # 闭引号、右括号跟在中文后面时也算「中文语境」，否则「问题”.「下一句」会漏掉
    cjk = r"[\u4e00-\u9fff”’】）》\]]"
    s = re.sub(rf"(?<={cjk})[,](?!\d)", "，", s)
    s = re.sub(rf"(?<={cjk})\.(?![a-zA-Z0-9])", "。", s)
    s = re.sub(rf"(?<={cjk})\?", "？", s)
    s = re.sub(rf"(?<={cjk})!", "！", s)
    s = re.sub(rf"(?<={cjk}):", "：", s)
    s = re.sub(rf"(?<={cjk});", "；", s)
    s = re.sub(r"(?<=[\u4e00-\u9fff])[—–\-](?=[\u4e00-\u9fff])", "，", s)   # 标题里的半角连字符
    s = re.sub(r"[!！]{2,}", "！", s)
    s = re.sub(r"[?？]{2,}", "？", s)
    return re.sub(r"\s+", " ", s).strip()


_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 搜狗给的是带会话的跳转链：先访问搜索页拿到 SNUID，再去换真实地址，
# 否则跳转页直接回验证码。整个流程共用一个 opener 就够了。
_COOKIES = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIES))


def _get(url, timeout=25, referer=""):
    """抓不到就返回空串——单个源挂了不该让整条流水线跟着挂。"""
    headers = dict(_HEADERS)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001 - 单个源失败不该中断整轮
        print(f"  ⚠️ 抓取失败 {url[:56]}：{type(exc).__name__}: {exc}")
        return ""


# ── 公众号正文 ────────────────────────────────────────────────────────
# 搜狗只给一百来字的摘要，还从半句中间切（读起来就是用户说的「断章取义」）。
# 但它的跳转页里藏着真实文章地址，只是被 JS 拆成小段防爬；拼回来就能打开原文，
# 从正文容器里取出从开头连续的几段。实测这条路走得通。
_ARTICLE_HEAD = re.compile(r"转自|公众号|点击上方|关注我们|来源[:：]|作者[:：]|微信号|阅读原文")
_ARTICLE_CHARS = 560          # 取多少字：够把「引子之后的正文」也包进来，再多就是搬运了
_HEAD_END = "）)▼】」"


def _wechat_url_from(page):
    """跳转页里 url 是一段段 += 拼起来的，拼回来。"""
    return "".join(re.findall(r"url \+= '([^']*)'", page))


def _wechat_url(sogou_url):
    return _wechat_url_from(_get(sogou_url, referer="https://weixin.sogou.com/"))


def _strip_article_head(text):
    """正文开头常是「本文转自公众号：xxx （ID：xxx）」，整句去掉。

    这些前言经常和正文粘在同一句里（中间没有句号），所以先在首句里截一刀。
    """
    sents = [x.strip() for x in re.split(r"(?<=[。！？…])", text) if x.strip()]
    while sents and _ARTICLE_HEAD.search(sents[0]):
        first = sents[0]
        cut = max(first.rfind(c) for c in _HEAD_END)
        if 0 < cut < len(first) - 6:               # 前言和正文粘在一起，从标记后面接着读
            sents[0] = first[cut + 1:].strip()
            break
        sents.pop(0)
    return "".join(sents)


def _cut_sentences(text, chars=_ARTICLE_CHARS):
    """按整句截断。从半句中间切就是「断章取义」，宁可少几个字。"""
    out = ""
    for sent in re.split(r"(?<=[。！？…])", text):
        if out and len(out) + len(sent) > chars:
            break
        out += sent
    return out or text[:chars]


_ARTICLE_BYTES = 1_200_000          # 正文容器在 500 KB 附近，读 400 KB 会正好切掉


def _drop_lead_dialogue(text):
    """公众号爱拿对话当引子：「阿达，太巧了吧！我家也是这样的！」

    没有人物、没有事，接在标题后面就像半截评论。开头连着几组引语就整组丢，
    直到露出正文为止。
    """
    for _ in range(4):                                  # 开头可能连着好几组
        m = re.match(r'\s*[“"]([^“”"]{0,60})[”"]\s*', text)
        if not m:
            break
        rest = text[m.end():]
        if not re.search(r"[\w\u4e00-\u9fff]", rest):
            break                                       # 后面没正文了，别把整篇吃光
        text = rest
    return text


# 公众号自家广告：一串没有句号的招生话术，后头直接粘正文（实测画室号最典型）。
# 命中密度不够就不动手，正常文章偶尔提一句「招生」不会被误伤。
_PROMO_WORDS = re.compile(
    r"画室|培训机构|辅导班|合格证|状元|招生|报名咨询|咨询热线|扫码|关注我们|点击上方|"
    r"微信号|粉丝群|优惠|限时|原价|现价|下单|购买|店铺|代理|加盟|领券|学员")
_AD_PROMO_MIN = 3


def _drop_ad_lead(text):
    """把开头那段自家广告切掉，从最后一个促销词所在的从句后面接正文。

    ponytail: 靠促销词密度认广告，碰到新话术就往 _PROMO_WORDS 里添词。
    """
    hits = list(_PROMO_WORDS.finditer(text[:400]))
    if len(hits) < _AD_PROMO_MIN:
        return text
    cut = text.find("，", hits[-1].end())
    if cut < 0 or cut > 400 or len(text) - cut < 20:
        return text                         # 别把整篇切没了
    return text[cut + 1:].lstrip()


def fetch_article(url, chars=_ARTICLE_CHARS):
    """公众号正文。文章页 3 MB 起（前半是脚本和样式），读到正文容器就够。"""
    headers = dict(_HEADERS)
    headers["Referer"] = "https://weixin.sogou.com/"
    try:
        with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=25) as resp:
            raw = resp.read(_ARTICLE_BYTES).decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001 - 抓不到就退回摘要
        print(f"  ⚠️ 正文抓取失败 {url[:48]}：{type(exc).__name__}: {exc}")
        return ""
    raw = re.sub(r"(?s)<section[^>]*display\s*:\s*none.*?</section>", " ", raw)   # 编辑器工具条
    block = re.search(r'(?s)id="js_content"[^>]*>(.*?)</div>\s*<div', raw)
    if not block:
        return ""
    body = _clean_text(re.sub(r"<[^>]+>", " ", block.group(1)))
    return _cut_sentences(_drop_ad_lead(_drop_lead_dialogue(_strip_article_head(body))), chars)


_FETCH_WORKERS = 6        # 小池子：够快，又不至于把搜狗/微信惹毛


def _one_fulltext(st, chars):
    """一条素材换原文。换不到返回 None。"""
    real = _wechat_url(st["url"])
    if not real:                               # 连着换十几次地址会被限流，歇一下再来一遍
        time.sleep(3)
        real = _wechat_url(st["url"])
    body = fetch_article(real, chars) if real else ""
    if not body:
        return None                            # 摘要版读不通，不要
    st["desc"] = body
    st["url"] = real                           # 换成正主地址，搜狗那个跳转是有时效的
    return st


def enrich_fulltext(stories, chars=_ARTICLE_CHARS):
    """把公众号素材换成原文正文；换不到原文的整条丢掉。

    搜狗只给一百来字的摘要，还从半句中间切。拿它当发帖素材就是「掐头去尾」，
    后半句接上钩子根本读不通。宁肯少几条，也不发半截话。

    并发做：38 条串着来、每条都在等超时，实测吃掉 13 分钟；并发之后几十秒。
    """
    todo, out = [], [None] * len(stories)
    for i, st in enumerate(stories):
        if st.get("src") == "公众号" and st.get("url"):
            todo.append((i, st))
        else:
            out[i] = st
    got = 0
    if todo:
        with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
            for (i, _), res in zip(todo, pool.map(lambda p: _one_fulltext(p[1], chars), todo)):
                if res is not None:
                    out[i] = res
                    got += 1
    print(f"  正文补全：{got}/{len(stories)} 条拿到原文，其余丢弃")
    return [x for x in out if x]


def fetch_stories(keywords=None, now=None, limit=12, per_keyword=6):
    """搜狗微信 -> [{'title','desc'}]。抓不到返回 []，调用方回退到榜单标题。"""
    now = now or datetime.now()
    if keywords:
        kws = list(keywords)
    else:
        # 每天换一批词（顺带避开限流）；三组用不同步长取，免得天天同一套组合。
        # 家事从主力降成补充——池子里本来就家长里短最多，用户要的是段子。
        pools = [(_STORY_WEIRD, round(limit * 0.4)), (_STORY_FIGHT, round(limit * 0.3))]
        pools.append((_STORY_KEYWORDS, limit - sum(n for _, n in pools)))
        kws = []
        for i, (pool, n) in enumerate(pools):
            start = (now.toordinal() * (3 + 2 * i)) % len(pool)
            kws += (pool + pool)[start:start + n]

    stories, seen = [], set()
    for kw in kws:
        page = _get("https://weixin.sogou.com/weixin?type=2&query=" + urllib.parse.quote(kw))
        if not page:
            continue
        for href, raw_title, raw_desc in _SOGOU_PAT.findall(page)[:per_keyword]:
            title, desc = _clean_text(raw_title), _clean_text(raw_desc)
            if not desc or title in seen or "\ufffd" in title:
                continue
            if (_STORY_NOISE.search(title) or _STORY_NOISE_TITLE.search(title)
                    or _STORY_AD.search(title) or _STORY_AD.search(desc)
                    or not _story_ok(desc)):
                continue
            seen.add(title)
            stories.append({"title": title, "desc": desc, "src": "公众号",
                            "kw": kw, "url": _abs_sogou(href)})
        time.sleep(1.5)                               # 搜狗按 IP 限流，问太快会被拦
    return stories


# 虎扑步行街：段子和争议故事的老窝，帖子页有完整首帖正文，值几次请求。
_HUPU_LIST = "https://bbs.hupu.com/bxj"
_HUPU_BODY = re.compile(r'(?s)class="thread-content-detail">(.*?)</div>')
# 虎扑、贴吧的官方活动帖（发帖赢好礼那一套）是运营广告，不是网友故事
_PROMO = re.compile(r"活动正式开启|赢官方|官方好礼|带话题发贴|名额|速来|点击报名")


def _paragraphs(text, budget=52, max_par=6):
    """切成「一到三行一段」的短段落。

    这是那条爆帖的精髓：句子短、段与段之间空一行，读起来有呼吸感。
    先按句末标点断句，超预算的长句再按逗号断，最后按字数预算拼回段落
    （太碎的一行一段反而像机器人在数数）。
    """
    pieces = []
    for sent in re.split(r"(?<=[。！？…])", text):
        # 断句断在引号里时，下一片会以孤零零的右引号开头，先把这尾巴摘掉
        sent = sent.strip().lstrip("”’\"」）】").strip()
        if not sent or not re.search(r"[\w\u4e00-\u9fff]", sent):
            continue                                   # 纯标点的碎片不算一段
        if len(sent) <= budget:
            pieces.append(sent)
            continue
        buf = ""
        for seg in re.split(r"(?<=[，,；;])", sent):
            if buf and len(buf) + len(seg) > budget:
                pieces.append(buf)
                buf = seg
            else:
                buf += seg
        if buf:
            pieces.append(buf)

    # 每两片拼一段：一到三行的长度，太碎的一行一段反而像机器人在数数
    paras = ["".join(pieces[i:i + 2]) for i in range(0, len(pieces), 2)]
    return paras[:max_par]


def _hupu_material(detail, max_replies=3):
    """首帖正文 + 前几条热评。步行街的乐子一半在评论里，只看首帖经常只有一句话。

    同一个帖子页里 seo-dom 会把首帖再抄一遍，所以先去重。
    """
    seen, blocks = set(), []
    for raw in _HUPU_BODY.findall(detail):
        # </p> 之间没有换行，直接去标签会把整篇糊成一行
        text = _clean_text(re.sub(r"</p>", " ", raw))
        # 属性值里带 > 时，去标签会留下半截 href="..."，这种块整块不要
        if "href=" in text or 'target="_blank"' in text:
            continue
        if len(text) >= 6 and text not in seen:
            seen.add(text)
            blocks.append(text)
    if not blocks:
        return "", []
    return blocks[0], blocks[1:1 + max_replies]


# 列表行自带「回复 / 浏览」，按回复数挑热的抓，命中率比顺着列表抓高得多：
# 一千多条里绝大多数首帖只有一句话，热闹的帖子才有人把前因后果写出来。
_HUPU_ROW = re.compile(
    r'(?s)<div class="post-title">\s*<a href="(/\d+\.html)"[^>]*>(.*?)</a>.*?'
    r'<div class="post-datum">\s*(\d+)\s*/\s*(\d+)\s*</div>')


def fetch_hupu(limit=4, probe=12):
    """步行街列表 -> 回复最多的几个帖子的首帖 + 热评。素材太短的（水贴）直接丢。"""
    page = _get(_HUPU_LIST)
    if not page:
        return []
    rows = []
    for href, raw, replies, _views in _HUPU_ROW.findall(page):
        title = _clean_text(raw)
        if len(title) < 8:
            continue
        rows.append((int(replies), href, title))
    seen, out = set(), []
    for _replies, href, title in sorted(rows, reverse=True)[:probe]:
        if href in seen:
            continue
        seen.add(href)
        detail = _get("https://bbs.hupu.com" + href)
        body, replies = _hupu_material(detail) if detail else ("", [])
        if len(body) < 60 or _PROMO.search(title + body):
            # 前者：首帖没正文，剩下的热评没头没尾。后者：官方活动广告
            continue
        out.append({"title": title, "desc": body, "replies": replies,
                    "src": "虎扑步行街", "url": "https://bbs.hupu.com" + href})
        if len(out) >= limit:
            break
        time.sleep(0.8)
    return out


_TIEBA_API = "https://tieba.baidu.com/hottopic/browse/topicList"


def fetch_tieba(limit=3):
    """贴吧热议：话题名 + 官方摘要。热榜性质的，只当补充，不指望出段子。"""
    page = _get(_TIEBA_API)
    if not page:
        return []
    try:
        rows = json.loads(page)["data"]["bang_topic"]["topic_list"]
    except Exception:  # noqa: BLE001 - 接口改版就老实跳过这一路
        return []
    out = []
    for row in rows[: limit * 3]:
        title = _clean_text(row.get("topic_name", ""))
        desc = _clean_text(row.get("abstract") or row.get("topic_desc") or "")
        if not title or not _story_ok(desc) or _PROMO.search(title + desc):
            continue
        out.append({"title": title, "desc": desc, "src": "百度贴吧",
                    "url": row.get("topic_url", "")})
        if len(out) >= limit:
            break
    return out


def collect_stories(top):
    """汇总三个源并按题材去重。交错着取，免得一个源把另一个挤没。"""
    # 三个源互不相干，串行等于把三家的等待时间加起来
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(fn, **kw) for fn, kw in (
            (fetch_stories, {"limit": 20}), (fetch_hupu, {"limit": 20, "probe": 36}),
            (fetch_tieba, {"limit": 6}))]
        groups = []
        for f in futs:
            try:
                groups.append(f.result() or [])
            except Exception as exc:        # noqa: BLE001 - 一个源挂了还有别的
                print(f"  ⚠️ 素材源失败：{type(exc).__name__}: {exc}")
                groups.append([])
    pool = []
    for i in range(max((len(x) for x in groups), default=0)):
        pool += [x[i] for x in groups if i < len(x)]
    # 先多挑几条再补正文：搜狗那迪经常换不到原文，换不到的整条要丢，
    # 不多留点余量就凑不满 top 条。
    return _pick_stories(enrich_fulltext(_pick_stories(pool, top + 8)), top)


def _story_ok(desc):
    """退回的是「没内容」的东西：图片集、连载目录、排版烂到没有标点的长句。"""
    if len(desc) < 30 or "▼" in desc:
        return False
    if re.match(r"^\d+", desc):                       # 网文章节开头都是「07 婆婆把…」
        return False
    if len(re.findall(r"\d+\s*[、.]", desc)) >= 3:
        return False                                  # 「27、…28、…29、…」是连载目录
    if _STORY_AD.search(desc) or _STORY_NOISE.search(desc):
        return False
    if _STORY_WIKI.search(desc[:26]):
        return False
    # 正常白话大约每 20 字一个标点；整段几乎没有标点的是复制粘贴的烂排版
    if len(re.findall(r"[。，！？、；：]", desc)) * 45 < len(desc):
        return False
    return True


def _pick_stories(stories, top):
    """同一题材先最多 2 条；凑不够 top 再放宽到 3 条、4 条。

    宁可要 25 条不重样的，也不要 30 条里六七条都是份子钱。
    """
    family_budget = max(3, top // 4)               # 婚嫁家事最多占四分之一
    ordered, picked, taken = sorted(stories, key=_story_key), [], set()
    used, family_used = {}, 0
    for cap in (2, 3, 4):
        for idx, st in enumerate(ordered):
            if idx in taken:
                continue
            theme = next((w for w in _THEME_WORDS if w in st["title"]), "")
            if theme and used.get(theme, 0) >= cap:
                continue                       # 没归上类的不卡，否则虎扑的帖子全被挤掉
            # 同一个搜索词翻出来的东西天然是一个题材（「AI 整活」一次能出五条），
            # 按词再卡一道，光靠题材词表堵不住。
            kw = "kw:" + st["kw"] if st.get("kw") else ""
            if kw and used.get(kw, 0) >= cap:
                continue
            if theme in _FAMILY_WORDS:
                if family_used >= family_budget:
                    continue
                family_used += 1
            if theme:
                used[theme] = used.get(theme, 0) + 1
            if kw:
                used[kw] = used.get(kw, 0) + 1
            taken.add(idx)
            picked.append(st)
            if len(picked) >= top:
                return picked
    return picked


def _story_key(s):
    """排序：脑洞/离谱/反转的排前面，婚嫁家事的往后放。

    之前按 _STORY_HOT（彩礼婆婆那一套）加权，结果前排全是家长里短；
    用户要的是脑洞，所以反过来：脑洞优先，婚嫁只做减法。
    """
    text = s["title"] + s["desc"]
    fun = 0 if _STORY_FUN.search(text) else 1
    family = 1 if _STORY_HOT.search(text) else 0
    n = len(s["desc"])
    short = max(0, 300 - n) // 60                    # 拿到原文的（400 字上下）排前面，
                                                     # 只有搜狗摘要的往后放
    return (fun, family, short)


# 单条开火用的钩子：一条帖子只讲一个故事，「第 N 条」那种说法用不上。
# 先自曝一句再提问——光提问像机器人，光感叹像营销号。
# 每句都必须对任何故事都成立：押中具体情节（比如「等个反转」）会一眼假。
_HOOK_SELF = [
    "看这种事总觉得离谱，但想想身边的人，又不觉得奇怪。",
    "我一般不爱评这种事，这条实在没忍住。",
    "这要搁我身上，我大概率处理不好。",
    "类似的瓜我吃过，但每次的细节都不一样。",
    "评论区估计又要吵起来，我先说我的看法。",
]

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
# 单条帖子的追问：上面那组是「几选一」的口气，一条帖子只讲一个故事，用不了。
_ASK_ONE = [
    "你们身边有类似的吗？",
    "换你你会怎么办？",
    "这事儿你站谁？评论区聊聊。",
    "你们那儿也这样吗？",
    "评论区聊聊你的看法。",
]
_MARKS = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
          "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳",
          "㉑", "㉒", "㉓", "㉔", "㉕", "㉖", "㉗", "㉘", "㉙", "㉚"]


# ── AI 改编：把热搜里的钩子话题写成「隔断式」小故事 ───────────────────
# 之前素材是「标题 + 原贴摘要 + 几个热评」，读起来断章取义（用户原话：词不达意、
# 上下文不连贯）。用户要的是：从热门/争议里拎出话题，再改编成有人有事的小故事，
# 不求真实，只要好看。这活儿规则做不了，交给模型。
# key / base / model 沿用仓库已有的 AI_* 配置（用户已在 Secrets 里配好）。
_AI_BASE = "https://open.bigmodel.cn/api/paas/v4"
# 写故事这个活儿对模型有要求：glm-4-flash 只会把新闻复述一遍、还经常只写一段。
# 按强弱依次试，哪个能回话就用哪个（配额/改名都不至于整批作废）。
_AI_MODEL_FALLBACKS = ["glm-4.7-flash", "glm-4.5-flash", "glm-4-flash"]
_AI_MODEL = _AI_MODEL_FALLBACKS[0]


def _fallbacks(base):
    """降级链跟着端点走：拿智谱的模型名去问 DeepSeek，只会白烧一分钟的请求。"""
    if "deepseek" in base:
        return ["deepseek-chat", "deepseek-reasoner"]
    if "bigmodel" in base:
        return _AI_MODEL_FALLBACKS
    return []                     # 自建/中转站：模型名无从猜，先信配置里那一个
_AI_BATCH = 4                    # 一次 4 条：一次要太多会撞输出上限，JSON 被截断就整批作废
_AI_TIMEOUT = 180
_FREQ_PATH = Path(__file__).resolve().parent.parent / "config" / "frequency_words.txt"

# 钩子话题：热搜标题里出现这些词，就是「有故事 / 能吵起来」的题
_HOOK_TOPIC = re.compile(
    r"彩礼|结婚|离婚|婚房|相亲|恋|分手|出轨|前任|婆媳|婆婆|丈母娘|岳父|"
    r"孩子|女儿|儿子|父亲|母亲|老人|夫妻|老公|老婆|"
    r"同事|老板|领导|裁员|加班|年终奖|跳槽|工资|薪|借钱|亲戚|邻居|室友|同学|"
    r"买房|房价|房贷|租房|学区|装修|车位|物业|份子钱|"
    r"油车|电车|新能源|苹果|安卓|华为|小米|AMD|Intel|骁龙|显卡|"
    r"预制菜|外卖|食堂|奶茶|养生|体检|医保|"
    r"AI|人工智能|大模型|程序员|考公|考研|学历|海归|"
    r"彩票|中奖|捡到|偷|骗|判|赔|起诉|退一赔三|"
    r"男子|女子|大爷|大妈|小伙|姑娘|网友"
)

_AI_SYSTEM = """你在给一个中文社媒账号写「话题小故事」。账号人设是爱看热闹的普通网友，转述身边事，不说教、不端着、不站队。

铁律：
1. 写故事，不写新闻。不许出现「记者」「据报道」「相关部门」「引发热议」「值得深思」这类腔调。
2. 除最后一段外，每段都得是事情本身。不许引用别人的话（「一位顾问告诉她」），不许替读者总结。
3. 每条都必须有：一个具体的主体（有身份；原文没给名字就用「新郎」「这位博主」这种不具名的说法）、一个具体数字（金额、天数、次数）、一个转折。
4. 叙事必须交代清楚：谁做了什么、对方什么反应、为什么难堪或为什么反转。不许甩「你…你…朋友面红耳赤」这种没头没尾的对话——读者看不懂就等于没写。
5. 可以改编讲法和节奏，但**不许出现原文没有的人名、地名、公司名**。原文没写名字就不许起名字，一律用不具名说法（「新郎」「当事女生」「这家店的老板」）；绝对不许写「李强」「李伟」「王奶奶」「张阿姨」这种自己编的人。数字也只用原文有的，不够就把篇幅写短。不许加「网传」「据称」这种免责词。
6. 排版是隔断式：每段 1~3 句，段与段之间空一行，正文 4~6 段，全文 200~350 字。
7. 标点全用中文全角。引号一律用「」或“”，不许用英文的 \" 和 '。
8. 不要 emoji，不要 #话题标签，不要小标题，不要序号。
9. 绝对不许复用范文里的人、事、数字，范文只示范写法。

最后一段是全文的落点：一句贴着这件事的冷峻判断，可以稍微夸大，但要能从前面的细节里推出来。
自检：那句话如果换成别的故事也成立，就是废话，重写。
落点写得好的例子：职场上暗流涌动，传到你耳朵里的秘密，可能全公司只有你还蒙在鼓里。
落点写得差的例子（一律禁止）：这些小秘密，让我对这个职场有了更深的了解。／科技竞争的本质不是企业间的对抗，而是创造者们对真理的执着追求。——这种放到哪儿都成立的人生道理，等于什么都没说。"""

_AI_FEWSHOT = """范文一：
这场婚姻，73天就谈到了1500万。
上海一位父亲，儿子结婚前拿出2400万给他买房。本以为房子能让小两口日子过得更稳，结果领证两个多月，儿媳就提出离婚，并要求分割房产。
事情眼看要变成“结婚73天，分走上千万”，父亲直接把儿子和儿媳一起告了。
因为买房那2400万，并不是白送的。父亲早就让儿子写了借条，房款在法律关系上属于借款。如今婚姻要散，他干脆要求两人共同偿还这笔2400万债务。
儿媳这边还在算房子能分多少，公公那边已经把整笔房款摆上了债务清单。
本来是一套婚房，最后先变成了一张2400万的欠条。

范文二：
火车上明明坐着上千人，为什么几十份盒饭经常都卖不完？
因为很多人搞反了一个逻辑：列车配盒饭，首先是为了保证“有人饿了能买到”，不是为了靠盒饭赚钱。
对车上工作人员来说，卖10份还是卖50份，收入基本没区别，但卖得越多，推车、加热、清理、处理垃圾的活儿反而越多。
所以盒饭价格高，有时反而能起到筛选需求的作用。大多数人会自带食物、吃泡面或者零食，真正有需要的人再买。
这东西最重要的不是销量，而是一直有。
看起来像生意，其实更像一项必须保留的服务。"""


def _yaml_ai():
    """从 config/config.yaml 的 ai: 段里捞 api_key / model / api_base。

    写故事这个活儿对模型要求不低：免费的 glm-4-flash 只会把新闻复述一遍。
    用户想换模型不必去动 GitHub Secrets，改 config.yaml 里的 ai.model / ai.api_base 就行。
    """
    cfg = {}
    try:
        text = (Path(__file__).resolve().parent.parent / "config" / "config.yaml").read_text(
            encoding="utf-8", errors="ignore")
    except OSError:
        return cfg
    for key in ("api_key", "model", "api_base", "x_writer_model", "x_writer_api_base",
                "x_writer_api_key"):
        m = re.search(r"^\s*" + key + r":\s*[\"']?([^\"'\s#]+)", text, re.M)
        if m:
            cfg[key] = m.group(1).strip()
    return cfg


def _ai_config():
    """环境变量优先，其次 config.yaml，最后退回智谱默认。

    板块二可以单独接一家供应商（ai.x_writer_api_key + x_writer_api_base）：换 DeepSeek
    不用动 AI_API_KEY，爬虫自己的分析/翻译照旧，两边互不影响。
    """
    y = _yaml_ai()
    wkey = os.environ.get("X_WRITER_API_KEY", "").strip() or y.get("x_writer_api_key", "")
    wbase = y.get("x_writer_api_base", "")
    if wkey:
        key = wkey
        base = os.environ.get("X_WRITER_API_BASE", "").strip() or wbase or _AI_BASE
        # 专属 key 在场时以 x_writer_model 为准（AI_MODEL 是爬虫那边的设置）
        model = y.get("x_writer_model", "") or _AI_MODEL
    else:
        # 端点配了别家、专属 key 还没填：拿旧 key 去撞就是 401，整批文案全空。
        # 所以这里宁可先用原供应商，只提醒一句。
        key = os.environ.get("AI_API_KEY", "").strip() or y.get("api_key", "")
        base = os.environ.get("AI_API_BASE", "").strip() or y.get("api_base", "") or _AI_BASE
        if wbase:
            # 配了别家端点却没给 key：这时候 x_writer_model 是那家的模型名，
            # 拿它去撞原供应商只会 404，所以连模型一起退回原供应商。
            print("  ⚠️ 配了 ai.x_writer_api_base 但没配 ai.x_writer_api_key，这轮仍用原供应商")
            model = (os.environ.get("AI_MODEL", "").strip()
                     or y.get("model", "") or _AI_MODEL)
        else:
            # 写故事可以单独指定模型（ai.x_writer_model）：爬虫的 AI 分析用什么跟这里无关
            model = (os.environ.get("AI_MODEL", "").strip()
                     or y.get("x_writer_model", "") or y.get("model", "") or _AI_MODEL)
    if model.startswith("openai/"):              # LiteLLM 前缀，直连时要去掉
        model = model.split("/", 1)[1]
    return key, base.rstrip("/"), model


# 题材型母题白名单（词库里的分组名）。其余分组是修饰词（「离谱」「网友」「热搜」），
# 拿它们当选题方向讲不出故事，只会变成新闻复述。
_TOPIC_GROUPS = [
    "彩礼婚恋", "家庭伦理", "男女对立", "翻车塌房", "离谱现场",
    "科技之争", "数码翻车", "车圈", "消费维权", "职场", "钱与房",
    "教育", "医疗", "娱乐圈", "网红直播",
]

# 新闻腔标题不进选题池：复述政策/财报/发布会读起来就是搬运，不是故事
_NEWSY_TITLE = re.compile(
    r"新政|政策|发布|获批|印发|通知|方案|条例|规定|试点|上线|指数|GDP|"
    r"财报|涨停|跌超|收盘|细则|征求意见|解读|发布会|通告|公告|宣布")


def _load_topic_words():
    """从 config/frequency_words.txt 读分组词：{组名: [词]}。正则词只取里面的中文片段。"""
    groups, name = {}, ""
    try:
        text = _FREQ_PATH.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return groups
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1] if line[1:-1] in _TOPIC_GROUPS else ""
            groups.setdefault(name, [])
            continue
        if not name or not line or line.startswith("#"):
            continue
        words = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", line)
        groups[name] += [w for w in words if len(w) >= 2]
    return {k: v for k, v in groups.items() if k and v}


# 母题词表缓存在模块级：要读文件，一晚上要问几十次
_TOPIC_WORDS = _load_topic_words()


def _bang_boost(bang):
    """今天每类母题在爆款里被提到几次。没抓到爆款源就返回空字典（等于不加权）。"""
    if not bang:
        return {}
    return {g: sum(1 for r in bang[:150] if any(w in r.get("text", "") for w in words))
            for g, words in _TOPIC_WORDS.items()}


def _hot_topic(title, boost, floor=3):
    """这条标题踩中了今天爆款里最热的哪个母题。没踩中返回空。

    floor 是「算热」的门槛：爆款一天上百条，随便一个词都可能被提一次，
    踩不到 3 次的不值得往前排。
    """
    if not title or not boost:
        return "", 0
    hits = [(c, g) for g, c in boost.items()
            if c >= floor and g in _TOPIC_GROUPS
            and any(w in title for w in _TOPIC_WORDS.get(g, []))]
    return max(hits)[::-1] if hits else ("", 0)


def _hook_topics(items, limit, bang=None):
    """今天哪几个母题在发酵：返回 [(母题, [今天的风向标题...])]。

    给模型的不是「照这个标题写」，而是「这个方向今天有人在聊」，免得写成新闻复述。
    bang 是爆款源样本（fetch_bangdan 的结果），可以不给。
    """
    groups = _load_topic_words()
    hits = {}
    for it in items:
        t = it["title"].strip()
        if not t or len(t) > 42 or _NEWSY_TITLE.search(t) or _STORY_NOISE_TITLE.search(t):
            continue
        for g, words in groups.items():
            if any(w in t for w in words):
                hits.setdefault(g, []).append((len(it["platforms"]), t))
    # 爆款源反向加权：哪几类题材今天在 X 上正跑得动，对应母题就往前排。
    # 这是「爆款源 → 新闻源」那一半闭环：选题方向跟着爆款风向走，不是拍脑袋。
    boost = _bang_boost(bang)
    out = []
    # 权重是拍的：爆款一天上百条、热搜一个母题撑死几条，不放大完全主导不了排序。
    def rank(kv):
        g, rows = kv
        return (-(min(boost.get(g, 0), 20) * 3 + len(rows) * 4), g)

    for g, rows in sorted(hits.items(), key=rank):
        rows = sorted(set(rows), reverse=True)[:2]
        out.append((g, [t for _, t in rows]))
    return out[:limit]


# ── 爆款源：xbangdan.com（X 中文区数据门户）────────────────────────────
# 用户的设定：hot-news-radar 是「新闻源」，xbangdan.com 是「爆款源」。
# 每天抓一次爆款源的真实数据（只抓 /hot.json：24 小时曝光榜 + 增速榜各 200 条、
# 免鉴权），量出「今天 X 上跑得动的是哪几类开头、什么节奏、哪些题材」，然后：
#   1. 用这批真开头和钩子分布约束板块二改写（爆款源 → 飞书文案）
#   2. 用爆款题材分布给母题加权，决定今天优先写哪几类（爆款源 → 新闻源选题）
#   3. 落一份 x/bangdan.md：风向 + 对标账号 + 词库建议，照着更新 frequency_words.txt
# 抓不到不能让整条流水线挂掉，所以这一整段都是 fail-soft。
#
# 识别表来自 hooksupdate.md 第五节的「X 榜单爆款钩子 / 内容风格」两批 20+8 类。
# 只做规则匹配、不调模型分类：分类这活儿模型不稳，还得额外花钱。
_BD_HOT = "https://xbangdan.com/hot.json"

# 备忘录自带的 15 类钩子：基本都是字面词，命中率最高，所以排在前面先匹。
# 实测（2026-10-06 抓的 200 条）：只按榜单那 20 类结构写，六成以上样本落到
# 「无钩子」——真实推文没那么多套路，靠这批字面钩子才量得出分布。
_BD_HOOKS = [
    ("求科普", r"不懂就问|我想问|想问下|求科普|求解答|这是什么"),
    ("冷知识反差", r"冷知识|热知识|第一次知道|第一次见"),
    ("没想明白", r"没想明白|想不明白|搞不懂|没搞懂"),
    ("我查了一下", r"我查了一下|查了下|特意查"),
    ("谁懂啊", r"谁懂啊|谁懂"),
    ("不敢信", r"不敢信"),
    ("震惊质疑", r"卧槽|太恐怖|真的假的"),
    ("暴论", r"暴论"),
    ("破防", r"破防"),
    ("呆住", r"呆住|愣住了|看愣了"),
    ("网传", r"网传"),
    ("据说", r"据说|听说|朋友发给我的|有人说"),
    ("来源背书", r"^(据|纽约时报|路透|彭博|华尔街日报|新华社|财新)"),
    ("场景代入", r"^(我|朋友|同事|昨天|那天|有次|上一?次|一位|一名|某)"),
    ("暴论反问", r"(为什么|凭什么|怎么还).*[？?]\s*$"),
    ("行情快报", r"(重回|站上|跌破|涨破|新高|新低).{0,8}\d"),
    ("数字盘点", r"^(我(整理|汇总)了|\d+\s*(种|个|条|款|张))"),
    ("连载续集", r"续集|上集|后续来了"),
    ("求做求购", r"哪里有卖|大佬.{0,6}(做|搞)一?[个下]|怎么做的|如何(盈利|赚钱)"),
    ("顿悟断言", r"当你(意识到|明白)"),
    ("极端定性", r"绝对|史上(最|第一)|最(惨|离谱|牛|强|恶心)"),
    ("金句开路", r"你是?为(了)?.{0,10}还是"),
    ("事件+情绪", r"太(无耻|离谱|恶心|震撼|过分)了|太过分"),
    ("反差反转", r"结果|没想到|本以为"),
    ("民间情绪", r"谁说|都这样|现在的人|说了跟没说一样|憋半天"),
]

# 内容风格 8 类（同一节）。今天哪类占比高，板块二改写就往哪类靠。
_BD_STYLES = [
    ("行情喊单", r"美元|比特币|BTC|ETH|涨|跌|仓位|上车"),
    ("家庭日常", r"我妈|我爸|老婆|老公|孩子|儿子|女儿|侄女|婆婆|丈母娘"),
    ("快讯播报", r"据|报道|记者|发布会|官宣|宣布"),
    ("奇观猎奇", r"保养|现场|画面|照片|奇观|罕见|第一次见"),
    ("都市夜话", r"^我|听说|朋友|同事|那天|有次"),
    ("暴论观点", r"为什么|凭什么|根本|其实|本质上"),
    ("金句感悟", r"你是?为(了)?|人生|世界|意义"),
    ("民间共鸣", r"都这样|现在的人|大家|谁不|普通人"),
]


# 爆款「为什么火」的机制。同一张表必须能同时套在爆款正文和新闻标题上——
# 两边用同一把尺子，才有可能拿爆款的逻辑去量新闻的潜力。
# 靠人工写死规则只能到这一步；真正决定权重的是 bangdan_learn()：每天从累计的
# 爆款源里重算一遍，哪个机制这段时间既稳定出现又真能带量，它就自动变重要。
_MECHANISMS = [
    ("反差反转", r"结果|没想到|反而|竟然|居然|本来.{0,10}(却|结果)|反转|翻车|打脸|说好的"),
    ("共鸣代入", r"我也|我家|我家那|身边|朋友|同事|你们|是不是|有没有|懂的都懂|谁不"),
    ("争议对立", r"争议|凭什么|该不该|吵|怼|骂|反对|支持|两极|站队|不配"),
    ("好奇猎奇", r"罕见|第一次|首次|奇观|离谱|魔幻|惊|真相|居然|竟然|最.{0,6}的"),
    ("金钱账本", r"\d+\s*[万块元亿]|[0-9]+万|花[了掉]|赔|赚|亏|彩礼|工资|存款|房价"),
    ("身份反差", r"\d+岁|[A-Z]\d{1,2}|大哥|大爷|大妈|老板|局长|医生|老师|博士|清华|北大|月薪"),
    ("时间紧迫", r"当天|当晚|最后一|一晚上|三分钟|立刻|马上|刚刚|才.{0,6}就"),
    ("人情冲突", r"婆婆|丈母娘|亲戚|邻居|同事|闺蜜|兄弟|分手|离婚|彩礼|份子钱|彩礼钱"),
]


def _mech_hits(text):
    """这段文字用了哪些爆火机制。"""
    return [name for name, pat in _MECHANISMS if re.search(pat, text or "")]


def fetch_bangdan(timeout=45):
    """抓 X 中文区当日爆款。返回 [{handle,name,text,v,r,l,age,cat}]，失败返回 []。"""
    try:
        req = urllib.request.Request(_BD_HOT, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as exc:        # noqa: BLE001 - 抓不到就走老路，不能拖垮流水线
        print(f"  ⚠️ 爆款源没抓到（{type(exc).__name__}: {exc}）")
        return []
    rows, seen = [], set()
    for key in ("rate", "views"):   # 增速榜 + 曝光榜，去重后按曝光排
        for it in data.get(key) or []:
            text = re.sub(r"https?://\S+", "", it.get("t") or "").strip()
            if len(text) < 6 or text[:6] in seen:
                continue
            seen.add(text[:6])      # 两榜大量重叠，同一段开头只留一条
            rows.append({"handle": it.get("h", ""), "name": it.get("n", ""), "text": text,
                         "v": int(it.get("v") or 0), "r": int(it.get("r") or 0),
                         "l": int(it.get("l") or 0), "age": it.get("age") or 0,
                         "cat": it.get("c") or "none"})
    rows.sort(key=lambda x: -x["v"])
    if rows:
        print(f"  ✅ 爆款源去重后 {len(rows)} 条，最高 {rows[0]['v']:,} 曝光")
    else:
        print("  ⚠️ 爆款源返回空")
    return rows


# SoPilot 爆帖榜：公开页，不用登录。只取文案样本——曝光数在页面另一处、而且
# 标题行已经给了足够的风格信号，这里不折腾配对，免得解析一改版就碎。
_SOPILOT = "https://sopilot.net/zh/rank/tweets?range={range}"
_SOPILOT_ANCHOR = re.compile(
    r'<a[^>]+href="(https://x[.]com/[^"?]+?/status/\d+)"[^>]*>(.*?)</a>', re.S)


def fetch_sopilot(ranges=("24h", "yesterday"), timeout=45):
    """抓 SoPilot 爆帖榜，返回与 fetch_bangdan 同形状的行（曝光一律 0）。

    链接文本就是推文文案（图片链接内部只有 <img>、没有文本，所以这样能区分），
    带 ? 的是互动链接（?sid= / ?quoto=），不算文案。跟 xbangdan 合并后再量一次
    钩子/风格指纹：样本翻倍，结论更稳。
    """
    rows, seen = [], set()
    for rng in ranges:
        try:
            req = urllib.request.Request(_SOPILOT.format(range=rng), headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                page = resp.read().decode("utf-8", "ignore")
        except Exception as exc:        # noqa: BLE001 - 少一个源不该拖垮流水线
            print(f"  ⚠️ SoPilot {rng} 没抓到（{type(exc).__name__}: {exc}）")
            continue
        for url, inner in _SOPILOT_ANCHOR.findall(page):
            text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", inner))).strip()
            if len(text) < 12 or text[:6] in seen:
                continue
            seen.add(text[:6])
            rows.append({"handle": url.split("/")[3], "name": "", "text": text,
                         "v": 0, "r": 0, "l": 0, "age": rng, "cat": "none"})
    if rows:
        print(f"  ✅ SoPilot 去重后 {len(rows)} 条文案样本")
    return rows


def fetch_hot_sources(timeout=45):
    """两个爆款源交错合并。交错很关键：fingerprint 只采样前 60 条，
    直接拼接的话后一个源一条都轮不上。

    两个源互相独立，串行就是白等一家的超时（实测光这一步 4 分钟），并发拉。
    两边各自吞异常，这里不用再兜。
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        fb = pool.submit(fetch_bangdan, timeout)
        fs = pool.submit(fetch_sopilot, timeout=timeout)
        bd, sp = fb.result() or [], fs.result() or []
    out = []
    for i in range(max(len(bd), len(sp))):
        if i < len(bd):
            out.append(bd[i])
        if i < len(sp):
            out.append(sp[i])
    return out


def _bd_named(common, n, skip="无钩子"):
    """钩子榜里剔掉「无钩子」再取前 n 个——量不出来就别写进风向。"""
    return [h for h, _ in common if h != skip][:n]


def _bd_first(text, table):
    for name, pat in table:
        if re.search(pat, text):
            return name
    return ""


def bangdan_fingerprint(rows, sample=60):
    """今天的爆款长什么样：钩子分布 / 风格分布 / 正文长度 / 真开头。"""
    rows = [r for r in (rows or []) if r.get("text")][:sample]
    hooks, styles, cats = Counter(), Counter(), Counter()
    for r in rows:
        hooks[_bd_first(r["text"], _BD_HOOKS) or "无钩子"] += 1
        styles[_bd_first(r["text"], _BD_STYLES) or "其他"] += 1
        cats[r.get("cat") or "none"] += 1
    mech, mexp = Counter(), Counter()
    for r in rows:
        for name in _mech_hits(r["text"]):
            mech[name] += 1
            mexp[name] += r["v"]        # SoPilot 那批 v=0，只当出现次数，不加曝光
    lens = sorted(len(r["text"]) for r in rows) or [0]
    return {"n": len(rows), "hooks": hooks.most_common(), "styles": styles.most_common(),
            "cats": cats.most_common(), "len_mid": lens[len(lens) // 2],
            "mech": mech.most_common(), "exp": mexp.most_common(),
            "openers": [r["text"][:40].replace("\n", " ") for r in rows[:6]],
            "tops": [r for r in rows if r["v"] > 0][:5]}   # SoPilot 那批没曝光数，不进对标榜


# 高频词统计要拦掉的常见字，不拦的话三字窗全是「你可以」「这就是」这种
_BD_STOP = ("我们你们他们这个那个就是不是没有可以什么怎么因为所以但是真的现在自己"
            "一个两个很多知道其实已经觉得直接结果然后而且甚至当时居然到底东西事情"
            "好几出来到了今天视频使用订单少钱牛逼句话各种")


def _bd_terms(rows, top=12):
    """爆款正文里的高频词。没有分词器，就用 2~4 字滑窗计数，再把「碎片」扔掉。

    长词会给自己的每个子窗都记一次（字节跳动 -> 字节跳/节跳动，紧急刹车 -> 紧急刹/急刹车）。
    不去掉的话榜单全是碎片，新闻标题闭着眼都能撞上一个（实测「免费升」「夏新闻」全在榜），
    筛选规则就等于摆设，所以：只留没有被「差不多同样多的更长词」包住的那几个。
    """
    cnt = Counter()
    for r in (rows or [])[:120]:
        t = r["text"]
        for n in (2, 3, 4):
            for i in range(len(t) - n + 1):
                g = t[i:i + n]
                if re.fullmatch(r"[\u4e00-\u9fff]{%d}" % n, g) and not any(c in _BD_STOP for c in g):
                    cnt[g] += 1
    # 两字词里混着「直接 / 结果 / 觉得」这种口水词，门槛抬高一点；三字以上一般就是实词了
    cands = [g for g, c in cnt.most_common(400) if c >= (5 if len(g) == 2 else 3)]
    keep, seen = [], set()
    for g in cands:
        # 更长且占了它一半以上出现的词在场，就当 g 是那个词的碎片
        if any(h != g and g in h and cnt[h] * 2 >= cnt[g] for h in cands):
            continue
        if g not in seen:
            seen.add(g)
            keep.append(g)
    try:
        library = _FREQ_PATH.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        library = ""
    return [(g, cnt[g]) for g in keep if g not in library][:top]


def _bangdan_prompt(fp):
    """把爆款指纹压成给模型看的几行：今天跑得动的类型 + 几条真开头。"""
    return [
        "", "今天的爆款风向（xbangdan.com 抓的 X 中文区实时数据）——照这个口味写，"
            "但人、事、数字必须是新编的：",
        "- 今天跑得动的开头类型：" + "、".join(_bd_named(fp["hooks"], 5)),
        "- 爆款正文中位 %d 字：短句、短段，越短越猛" % fp["len_mid"],
        "- 真开头（只学口气和节奏，不许复用里面的人和事）：",
    ] + ["    " + t for t in fp["openers"][:4]]


def render_bangdan(rows, now=None):
    """爆款风向备忘：给模型看的那套东西，也给人留一份（x/bangdan.md）。"""
    now = now or datetime.now()
    fp = bangdan_fingerprint(rows)
    out = [f"# 爆款风向 · {now:%Y-%m-%d}", "",
           f"来源：xbangdan.com + sopilot.net（X 中文区 24 小时曝光/增速榜，采样 {fp['n']} 条）", ""]
    if not fp["n"]:
        return "\n".join(out + ["⚠️ 今天没抓到爆款源，这一轮没参考它（不影响出稿）。"]) + "\n"
    top_hooks = "、".join(_bd_named(fp["hooks"], 4)) or "没量出明显套路"
    out += [f"风向：X 上今天跑得动的是 {top_hooks} 这类开头；爆款正文中位 {fp['len_mid']} 字。", "",
            "## 今天的真开头（照这个口气写，别抄内容）"]
    out += [f"- {t}" for t in fp["openers"][:6]]
    out += ["", "## 开头钩子分布"] + [f"- {h} × {n}" for h, n in fp["hooks"][:6]]
    out += ["", "## 内容风格分布"] + [f"- {s} × {n}" for s, n in fp["styles"][:5]
                             if s != "其他"]
    out += ["", "## 今天曝光最高的（对标）"]
    out += [f"- {r['name'] or r['handle']} @{r['handle']}：{r['v']:,} 曝光 / "
            f"{r['l']:,} 赞 — {r['text'][:40]}" for r in fp["tops"]]
    terms = _bd_terms(rows)
    if terms:
        out += ["", "## 词库建议（爆款里高频、frequency_words.txt 里还没有的）",
                "看哪个组合适就加进去，下一轮抓取就会跟着走：",
                "- " + "  ".join(f"{g}({c})" for g, c in terms)]
    return "\n".join(out) + "\n"


# 每条帖子分一个开头钩子（hooksupdate.md 的钩子库）。一条一个、批内不重复、
# 按日期轮换起始点——天天同一个起手式就是机器味。
def _load_hook_table():
    """钩子表以 hooksupdate.md 为准——飞书豆包 / WorkBuddy / CodeBuddy 读的是同一份。

    只认 <!-- MACHINE:HOOKS ... --> 里的「钩子名|起手式」行。文件没了或没标记就返回空，
    由下面那份内置默认值兜底——缺个 md 不该把整条流水线搞挂。
    """
    try:
        text = (Path(__file__).resolve().parent.parent / "hooksupdate.md").read_text(
            encoding="utf-8", errors="ignore")
    except OSError:
        return []
    m = re.search(r"<!--\s*MACHINE:HOOKS\b(.*?)-->", text, re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        name, eg = (s.strip() for s in line.split("|", 1))
        if name and eg:
            out.append((name, eg))
    return out


_WRITER_HOOKS = _load_hook_table() or [
    ("不懂就问", "不懂就问：这到底合不合规？"),
    ("冷知识", "冷知识：这样操作是合法的。"),
    ("热知识", "热知识：这事儿其实有明文规定。"),
    ("暴论", "暴论：这事没人管才怪。"),
    ("没想明白", "没想明白，为什么会这样？"),
    ("网传", "网传……我先摆这儿。"),
    ("据说", "据说当时就是这么处理的。"),
    ("第一次知道", "第一次知道，原来还能这么干。"),
    ("我查了一下", "我查了一下，这事有出处。"),
    ("谁懂啊", "谁懂啊，这种操作真的离谱。"),
    ("不敢信", "不敢信，这事是真的。"),
    ("破防", "破防了，就为这点事。"),
    ("呆住", "看完呆住，半天没说话。"),
    ("求科普", "求科普，这算谁的责任？"),
    ("数字盘点", "我给这事算了笔账。"),
    ("反差反转", "本来以为就这么过去了，结果……"),
    ("震惊质疑", "卧槽，真的假的？"),
    ("事件+情绪", "这事儿办得太离谱了。"),
    ("奇观直给", "原来这事是这么办的。"),
    ("都市怪谈", "听说那天就没人管，我是不太信。"),
]


def _ai_call(key, base, model, system, user):
    """一次 chat/completions。返回正文文本，失败返回空串。"""
    body = json.dumps({
        "model": model, "temperature": 1.0, "max_tokens": 4096,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        base + "/chat/completions", data=body, method="POST",
        headers={**_HEADERS, "Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    for attempt in (1, 2, 3):                    # 429/超时多半是抖动，退避几秒再来一次
        try:
            with urllib.request.urlopen(req, timeout=_AI_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            # 免费档限流是常态：这一轮前面几步也在这条 key 上跑，额度是被自己用掉的，
            # 所以 429 要给足退避时间，不能跟网络抖动一样隔 6 秒就撞第二下。
            if attempt == 3 or exc.code not in (429, 500, 502, 503, 504):
                print(f"  ⚠️ AI 改写失败：{type(exc).__name__}: {exc}")
                return ""
            time.sleep(15 * attempt)
        except Exception as exc:                 # noqa: BLE001 - 模型挂了就退回原贴
            if attempt == 3:
                print(f"  ⚠️ AI 改写失败：{type(exc).__name__}: {exc}")
                return ""
            time.sleep(6)
    return ""


def _normalize_quotes(text):
    """模型爱用英文直引号（"…" / '…'），中文里看着别扭，成对换成 “” 和 ‘’。

    成对替换靠奇偶交替，不依赖模型自己配对（它一向配得很随意）。
    """
    out, opening = [], {"\"": True, "'": True}
    for ch in text:
        if ch in opening:
            out.append(({"\"": "“", "'": "‘"} if opening[ch] else {"\"": "”", "'": "’"})[ch])
            opening[ch] = not opening[ch]
        else:
            out.append(ch)
    return "".join(out)


def _ai_ok(item):
    """弱模型爱交差：只写一段、或者四段每段一句话，读起来跟标题没区别。

    宁可少几条，也别把这种残次品塞进文案（用户之前就嫌「看不到内容」）。"""
    paras = item.get("paras") or []
    total = sum(len(p) for p in paras)
    return len(paras) >= 3 and total >= 150


def _title_bare(title):
    """标题只把钩子例句抄了一遍（「不懂就问：这到底合不合规？」），等于没写事。

    实测 glm-4.5-flash 就爱这么交差：提示词里给它一条起手式例句，它原样搬来当标题。
    这种标题发到 X 上读者什么也看不到，宁可当这条没写成，让下一个模型重试。
    """
    t = (title or "").strip().rstrip("。？！?!")
    if len(t) < 12:
        return True
    return any(t in eg.rstrip("。？！") for _, eg in _WRITER_HOOKS)


def _ai_parse(text):
    """模型爱加说明、```json 围栏，还可能写到一半被输出上限截断。

    所以不留着 json.loads 一把梭：先找数组起点，再逐个对象 raw_decode，
    完整的那几条照样能用，被截断的尾巴丢掉。
    """
    text = text or ""
    start = text.find("[")
    data = []
    if start >= 0:
        try:
            data = json.loads(text[start:])          # 正常情况：一次到底
        except ValueError:
            dec = json.JSONDecoder()
            pos = start + 1
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
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title", "")).strip()
        paras = it.get("paras") or []
        if isinstance(paras, str):
            paras = [x for x in paras.split("\n") if x.strip()]
        paras = [str(x).strip() for x in paras if str(x).strip()]
        if title and paras:
            out.append({"title": _normalize_quotes(title),
                        "paras": [_normalize_quotes(x) for x in paras]})
    return out


_AI_PROBE_MODELS = ["glm-4.7-flash", "glm-4.5-flash", "glm-4-flash", "glm-4.6-flash",
                    "glm-4-plus", "glm-4.7", "deepseek-chat", "deepseek-v4-pro"]


def ai_probe():
    """拿当前 key 挨个问一遍，看哪些模型真能用。"""
    key, base, model = _ai_config()
    if not key:
        print("没配 API Key（AI_API_KEY / ai.x_writer_api_key），没法探测")
        return
    print(f"接口：{base}")
    # 先试真正会用到的那条降级链，再捎带试几个常见的名字
    for model in dict.fromkeys([model] + _fallbacks(base) + _AI_PROBE_MODELS):
        body = json.dumps({"model": model, "max_tokens": 16,
                           "messages": [{"role": "user", "content": "回一个字：好"}]}).encode()
        req = urllib.request.Request(
            base + "/chat/completions", data=body, method="POST",
            headers={**_HEADERS, "Authorization": "Bearer " + key,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            print(f"  ✅ {model}：{data['choices'][0]['message']['content'].strip()[:20]}")
        except Exception as exc:                 # noqa: BLE001 - 探测就是要看各种失败
            detail = ""
            if hasattr(exc, "read"):
                try:
                    detail = exc.read().decode("utf-8", "ignore")[:120]
                except Exception:                # noqa: BLE001
                    detail = ""
            print(f"  ❌ {model}：{type(exc).__name__} {detail}")


# 百家姓。模型最爱给主角起名（李伟、王奶奶、张阿姨），读者一对原文就露馅：原文根本没这个人。
# 判据只取高精度的两条，宁可漏也不敢误杀——「马路」「白天」这种姓+字的常用词通常只出现一次：
#   1) 姓+1~2 字，且在正文里出现 ≥2 次（编出来的主角一定会被反复提起）
#   2) 姓+称呼（李先生、王奶奶、张阿姨），出现 1 次就算
_SURNAME = ("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章"
            "云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐"
            "于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪"
            "舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支"
            "柯昝管卢莫房裘解应宗丁宣邓郁单杭洪包诸左石崔吉龚程邢裴陆荣翁荀羊惠甄曲家封芮储靳段"
            "富巫乌焦巴弓牧山谷车侯全班仰秋仲伊宫宁仇栾暴甘厉戎祖武符刘景詹束龙叶幸司韶黎薄印宿"
            "白怀蒲鄂索咸赖卓蔺屠蒙池乔胥苍双闻莘党翟谭贡劳姬申扶堵冉宰雍桑桂牛通边燕冀浦尚农温"
            "庄晏柴瞿阎充慕连茹习艾鱼容向古易慎戈廖庾终居衡步都耿满弘匡国文寇广禄阙东欧沃利蔚越"
            "夔隆师巩聂晁勾敖融冷辛阚那简饶空曾沙鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公")
# 名字后面紧跟着「在 / 说 / 决定」这类主谓标志，或者是「王奶奶」这种称呼，
# 才算真的人名。只靠「姓+字」的形状会把「平台」「程序员」也吃进去。
_PTITLE = ("先生", "女士", "小姐", "阿姨", "大爷", "大妈", "奶奶", "爷爷",
           "叔叔", "老师", "老板")
_PVERB = ("说", "表示", "告诉", "觉得", "认为", "发现", "决定", "是", "在", "把", "和", "跟",
          "被", "让", "给", "问", "答", "却", "也", "还", "就", "已", "每", "从", "向",
          "想", "看到", "直接", "默默", "当场", "后来", "本来", "当时", "每天", "已经", "选择",
          "坚持", "拿", "花", "哭", "骂", "拒", "回", "签", "辞", "带", "走")


# 「姓+字」长成词的常用词。这些词在一段里出现两次很平常，但都不是人名，别误杀。
_COMMON = {
    "路上", "路边", "路口", "马路", "白天", "白色", "白云", "石头", "石油", "王国", "王牌", "张开",
    "张望", "张扬", "张力", "陈述", "陈旧", "陈年", "程度", "程序", "江南", "江湖", "周围", "周末",
    "分钟", "行李", "何时", "何必", "胡同", "胡子", "高兴", "高度", "高级", "高速", "高铁", "黄色",
    "黄金", "许多", "许可", "于是", "方便", "方案", "方向", "方式", "方法", "方面", "万一", "金钱",
    "金属", "熊猫", "计划", "成功", "成为", "成本", "成年", "谈话", "庞大", "舒服", "项目", "杜绝",
    "蓝色", "季节", "麻烦", "平安", "平时", "和平", "常常", "常见", "常规", "唐诗", "房子", "房间",
    "房价", "房东", "解决", "应该", "管理", "顾客", "胡说", "冷静", "叶子", "幸福", "印象", "怀疑",
    "焦点", "车站", "武器", "符合", "景点", "司机", "关于", "关系", "关注", "检查", "后来", "那么",
    "都是", "容易", "习惯", "简单", "相信", "满意", "温度", "牛奶", "堵车", "全部", "全国", "单位",
    "居然", "终于", "越来越", "师傅", "辛苦", "曾经", "权力", "游戏", "游客", "相连", "相关",
    "平台", "程序员", "黄牛", "封禁", "时候", "管理", "管子", "起来", "厉害", "难受",
    "石头", "石油", "王朝", "王子", "江苏", "江西", "包括", "包装", "左右", "陆续", "曲线",
    "封闭", "焦虑", "车子", "车牌", "全家", "秋天", "宁愿", "暴力", "暴雨", "武装", "龙头",
    "怀念", "劳动", "申请", "连续", "连忙", "步骤", "满足", "欧洲", "利益", "那些", "那个",
    "丰富", "关键", "相同", "调查", "后面", "权利", "到底", "将军", "美好", "行人", "行人",
    "效果", "方式", "日子", "早上", "晚上", "夜里", "楼下", "楼上", "家里", "店里", "街上",
    "公司", "公园", "公开", "公平", "公安", "公里", "公民", "属于", "居民", "局长", "所以",
}


def _name_cands(text):
    """「姓+1 字」「姓+2 字」的候选，只收句子开头那种。

    前面的字也是汉字就跳过：「大家表示」里的「家」、「行李却被」里的「李」、
    「对方父母」里的「方」、「最后只」里的「后」——全是词的碎片，不是人名。
    代价是「爱豆贺峻霖」这种夹在词里的漏掉，这个能忍：宁可漏，不能误删。
    """
    out = []
    for m in re.finditer(f"[{_SURNAME}]", text or ""):
        i = m.start()
        if i and "\u4e00" <= text[i - 1] <= "\u9fff":
            continue
        for ln in (2, 3):
            cand = text[i:i + ln]
            if cand[:2] in _COMMON:
                continue                  # 「石头是」「路上车」是从常用词长出来的
            if len(cand) == ln and all("\u4e00" <= ch <= "\u9fff" for ch in cand):
                out.append((cand, i + ln))
    return out


def invented_names(text, src=""):
    """正文里疑似编出来的人名：原文没有、出现在句首、后面还跟着动作或称呼。

    只认这一种形状——宁可漏掉一条，也不要把普通词误杀成名字（模型写错了顶多被读者看出来，
    误杀是直接少一条稿）。
    """
    text, src = text or "", src or ""
    out = []
    for cand, end in _name_cands(text):
        if cand in src or cand in _COMMON:
            continue
        if text.startswith(_PTITLE, end) or text.startswith(_PVERB, end):
            out.append(cand)
    # 「贺峻霖」和「贺峻」是同一个人的长短两个候选，只留长的
    out.sort(key=len, reverse=True)
    keep = []
    for n in out:
        if not any(k.startswith(n) for k in keep):
            keep.append(n)
    return keep


def _drop_invented(x, src):
    """这条里出现原文没写的人名就丢掉。返回被丢掉的名字（空列表=留）。"""
    bad = invented_names((x.get("title") or "") + "\n" + "\n".join(x.get("paras") or []), src)
    if bad:
        print("     ⚠️ 丢掉一条：里头有原文没写的名字 " + "/".join(bad))
    return bad


def ai_write_stories(items, seeds, top, now=None, bang=None):
    """把当天热搜话题交给模型，写成能单条发的隔断式故事。拿不到就返回空列表。"""
    key, base, model = _ai_config()
    if not key:
        print("  ⚠️ 没配 AI_API_KEY，跳过改写，退回抓来的原贴")
        return []
    topics = _hook_topics(items, min(40, max(16, top * 2)), bang=bang)
    if not topics:
        return []
    fp = bangdan_fingerprint(bang) if bang else {}
    models = [model] + [m for m in _fallbacks(base) if m != model]
    # 抓到的那批素材当「事实毛坯」递过去：模型改编时手上有细节，不至于全靠编
    seeds = [{"标题": s["title"], "正文": s["desc"][:400]} for s in (seeds or [])[:top]]
    wrote, fails = [], 0
    for start in range(0, top, _AI_BATCH):
        want = min(_AI_BATCH, top - len(wrote))
        if want <= 0:
            break
        # 一条对一个母题。之前直接拿热搜标题当题，模型就照着标题复述，成了新闻搬运。
        if not topics:
            break
        batch = [topics[(start + n) % len(topics)] for n in range(want)]
        user = [
            "今天的风向（母题 + 今天相关的热搜标题）。标题只说明这个方向今天有人在聊，"
            "不要复述它，也别出现里面的机构名、产品名、政策名，写这一类的普通人故事：",
        ]
        for n, (group, winds) in enumerate(batch, 1):
            user.append(f"{n}. {group}：" + "；".join(winds))
        # 判「是不是原文里的名字」只能用母题 + 素材：范文里的人正是明令禁止复用的
        src = " ".join([g for g, _ in batch] + [w for _, ws in batch for w in ws]
                       + [s["标题"] + s["正文"] for s in seeds])
        if seeds and start == 0:
            # 只在第一批给素材：每批都给的话，模型会拿同一条素材写出好几篇一样的
            user += ["", "今天从公众号 / 虎扑 / 贴吧抓到的原始素材（有用就取细节，没用就丢掉）：",
                     json.dumps(seeds, ensure_ascii=False)]
        elif wrote:
            user += ["", "已经发过的选题（换个角度写，别重复）：" + "、".join(x["title"] for x in wrote)]
        user += [
            "", f"把上面这 {len(batch)} 条母题各写成一条独立帖子，一条一个，顺序对应。",
            "- title 是「钩子 + 这件事」：把下面分到的起手式接上具体事端，一到两句。",
            "  只抄起手式本身（例如只写「不懂就问：这到底合不合规？」）不算一条帖子。",
        ]
        if fp.get("n"):
            user += _bangdan_prompt(fp)
        # 每条分一个开头钩子：按日期轮换起始点，批内不重复。
        # 之前只在提示词里列一排起手式让模型自己挑，结果十条有八条都是「不懂就问」。
        hoff = (now or datetime.now()).toordinal()
        picks = [_WRITER_HOOKS[(hoff + start + n) % len(_WRITER_HOOKS)]
                 for n in range(len(batch))]
        user += [
            f"- 这 {len(batch)} 条各分一个开头钩子，按顺序用，不许换、不许重复：",
            "    " + "；".join(f"{n}. {h}（写成「{eg}」这个口气）"
                               for n, (h, eg) in enumerate(picks, 1)),
            "- paras 是后面 4~6 段，每段 1~3 句，隔断式。",
            "", _AI_FEWSHOT,
            "", '只输出 JSON 数组：[{"title": "开场钩子", "paras": ["第一段", "第二段"]}]，'
                "数组长度必须是 %d，不要任何解释，不要用 ``` 包裹。" % len(batch),
        ]
        got = []
        for cand in models:                     # 第一个有回话的模型认下来，之后不再换
            raw = _ai_call(key, base, cand, _AI_SYSTEM, "\n".join(user))
            got = [x for x in _ai_parse(raw) if _ai_ok(x) and not _title_bare(x["title"])
                   and not _drop_invented(x, src)]
            if got:
                model, models = cand, [cand]     # 认下这个模型，后面的批次不再换
                break
        if not got:
            # 一批挂掉不该把后面的批全废掉：限流是随机的，接着试还能捞回来。
            # 但连挂两批就认输，别把配额烧光（实测 glm-4.5-flash 经常只出 1/4）。
            fails += 1
            print(f"  ⚠️ 第 {start // _AI_BATCH + 1} 批没写出合格内容（要求：至少 3 段、150 字）")
            print("     → 多半是模型太弱或被限流。可改 config/config.yaml 的 ai.x_writer_model")
            if fails >= 2:
                break
            continue
        fails = 0
        wrote += got
        print(f"  AI 改写：{len(batch)} 条话题 -> 成稿 {len(got)} 条")
    return [{"title": g["title"], "desc": "\n\n".join(g["paras"]), "src": "AI 改编",
             "url": "", "replies": [], "ai": True} for g in wrote[:top]]


def render_pool(stories, now=None, top=12, sep="-" * 18):
    """每条都是能**单独发**的一条帖子：标题 + 正文 + 钩子。

    用户是攒一批素材、一条一条发，不是一次发一大篇，所以不做整体开头结尾，
    每条自带钩子；分隔线只为人肉复制，复制单条时别带上。
    """
    now = now or datetime.now()
    seed = now.toordinal()
    blocks = []
    for i, st in enumerate(stories[:top], 1):
        mark = _MARKS[i - 1] if i <= len(_MARKS) else f"{i}."
        lines = [f"{mark} {st['title']}", ""]
        # AI 改编的故事自带分段（隔断式）和收尾句，再套模板钩子就成机器味了
        paras = ([x for x in st["desc"].split("\n\n") if x.strip()]
                 if st.get("ai") else _paragraphs(st["desc"]))
        for para in paras:
            lines += [para, ""]
        # 虎扑的热评单独成段，天然就是「网友说」的对话感
        for reply in st.get("replies", []):
            lines += [f"网友：{reply[:90]}", ""]      # 完整热评留在 sources.txt
        if not st.get("ai"):
            lines += [_HOOK_SELF[(seed + i) % len(_HOOK_SELF)],
                      _ASK_ONE[(seed * 5 + i) % len(_ASK_ONE)]]
        blocks.append("\n".join(lines))
    return ("\n" + sep + "\n").join(blocks) + "\n"


def render_sources(stories, now=None):
    """二创素材：原样留着出处、原文链接和正文，改写给用户自己动手。"""
    now = now or datetime.now()
    out = [f"# 二创素材 · {now.strftime('%Y-%m-%d')}",
           f"（{len(stories)} 条，来自公众号 / 虎扑步行街 / 百度贴吧）", ""]
    if stories and stories[0].get("ai"):
        out[1] = f"（{len(stories)} 条，AI 按当天热搜话题改编；下面是抓来的原始素材，查重和补细节用）"
    for i, st in enumerate(stories, 1):
        out.append(f"## {i}. {st['title']}")
        out.append(f"来源：{st.get('src', '未知')}")
        if st.get("url"):
            out.append(f"原文：{st['url']}")
        out.append("")
        out.append(st["desc"])          # 素材要原样，不做段落切分
        for reply in st.get("replies", []):
            out.append(f"热评：{reply}")
        out.append("")
    return "\n".join(out)


def render_links(items, bang=None, top=30, now=None):
    """热榜原始链接：给飞书里的豆包当二创素材（我们出链接，它按 hooksupdate.md 改写）。

    排序沿用板块二那套（共振 + 排他性），再叠爆款源的风向加权：今天 X 上跑得动的
    题材往前排并标火——这就是「爆款源反过来调热门链接」那一环。
    链接抓来什么样就什么样，不改写、不删减，挑哪条由豆包自己决定。
    """
    now = now or datetime.now()
    boost = _bang_boost(bang)
    rows = []
    for it in items:
        if not it.get("url") or _AD_HOT.search(it["title"]):
            continue                       # 没链接的、电商稿，都不算素材
        hot = bool(_hot_topic(it["title"], boost)[1])
        rows.append((0 if hot else 1, _sort_key(it), it, hot))
    rows.sort(key=lambda r: (r[0], r[1]))
    out = ["# 热榜原始链接 · %s" % now.strftime("%Y-%m-%d"),
           "（%d 条 / 共 %d 条；%s=今天 X 上跑得动的题材，%d 条）"
           % (min(len(rows), top), len(rows), '🔥', sum(1 for r in rows if r[3])), ""]
    for n, (_p, _k, it, hot) in enumerate(rows[:top], 1):
        meta = " · ".join(x for x in ((it.get("group") or ""),
                                      "+".join(it["platforms"][:4])) if x)
        out.append("%d. %s%s" % (n, '🔥' + " " if hot else "", it["title"]))
        out.append("   " + meta)
        out.append("   " + it["url"])
    return "\n".join(out) + "\n"


def render_copy(items, site, now=None, top=25):
    """板块二 X 文案：一篇帖子，不是三条。

    条目按 _sort_key 排（含段子度和排他性权重），不照抄榜一。
    site 参数保留仅为兼容旧调用，文案里不再出现站点链接和话题标签。

    注意长度：条数是 --copy-top，默认 25。中文按 2 字符计，
    20 条左右就远超普通账号的 280 上限了——这是给 X Premium 长文或拆成串用的。
    """
    now = now or datetime.now()
    seed = now.toordinal()

    picked = sorted(items, key=_sort_key)[:top]
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
    ap.add_argument("--link-top", type=int, default=30,
                    help="飞书里发几条热榜原始链接（给豆包当二创素材）")
    ap.add_argument("--story-top", type=int, default=30,
                    help="素材池放几条（都带正文，用户自己挑着一条条发）")
    ap.add_argument("--no-stories", action="store_true", help="不抓公众号故事，退回榜单标题")
    ap.add_argument("--no-bangdan", action="store_true", help="不参考 xbangdan.com 爆款源")
    ap.add_argument("--no-ai", action="store_true", help="不做 AI 改编，直接发抓来的原贴")
    ap.add_argument("--ai-top", type=int, default=12, help="AI 改编几条（要 30 条得有人工挑）")
    ap.add_argument("--handle", default="", help="卡片右下角署名，如 @your_x_handle")
    ap.add_argument("--ai-probe", action="store_true", help="探测当前 key 能用哪些模型")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.ai_probe:
        ai_probe()
        return

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
        # 频道矩阵：列取本批出现最多的频道，亮格 = 这条在该频道上过榜。
        # 单频道的单独标出来，一眼能挑出全网共振的那几条。
        mx = render_card([
            {"title": "两频道", "url": "u", "platforms": ["微博", "知乎"], "rank": 1,
             "trend": "", "count": ""},
            {"title": "单频道", "url": "u", "platforms": ["微博"], "rank": 2,
             "trend": "", "count": ""},
        ], args.site, handle="@demo")
        assert mx.count('class="mx"') == 2, "每条都该有频道矩阵"
        assert '<b class="on">微博</b>' in mx, "矩阵亮格没渲染"
        assert '<b class="">知乎</b>' in mx, "矩阵暗格没渲染"
        assert "单频道热门" in mx, "单频道的条目没被单独标出来"
        assert card3.count('class="mx"') == 1, "卡片没带频道矩阵"
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
        # 链接里的 HTML 转义要还原，否则贴吧那条会点成坏链
        ent = parse_items(
            '<div class="word-group"><div class="word-name">微博热搜</div>'
            '<div class="news-item"><span class="source-name">微博</span>'
            '<a href="https://tieba.baidu.com/x?topic_name=a&amp;topic_id=7" '
            'class="news-link">测试标题四个字</a></div></div>')
        assert ent[0]["url"] == "https://tieba.baidu.com/x?topic_name=a&topic_id=7", \
            ent[0]["url"]
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
        # 电商稿（「XX 开售，4999 元」）不是热点，不能占卡片位置
        assert _AD_HOT.search("小米“米家冰箱 Pro 439L 法式自动制冰”开售，4999 元"), "电商稿没被识别"
        assert not _AD_HOT.search("OpenAI 应战 Meta：发布个人 AI 助手 Dots"), "别把真新闻当广告"
        mixed = [{"title": "米家冰箱开售，4999 元", "url": "u", "platforms": ["微博"],
                  "rank": 1, "trend": "", "count": ""},
                 {"title": "真新闻一条测试标题", "url": "u", "platforms": ["微博"],
                  "rank": 2, "trend": "", "count": ""}]
        assert [x["title"] for x in split_channels(mixed)[0]] == ["真新闻一条测试标题"], \
            "电商稿没从卡片板块里剔掉"

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
        # 故事正文：搜狗摘要是半角标点，还要能渲染成「标题 + 缩进正文」
        assert _clean_text("彩礼给了,婚没结成.打了一年官司...") == "彩礼给了，婚没结成。打了一年官司…"
        assert "x-oss-process" not in _clean_text(
            "网友：这6次1次没高潮 /quality/50/ignore-error/1?x-oss-process=image/resize,w_225\"/>"), \
            "热评里的图片标签残渣没洗干净"
        assert _PROMO.search("「国庆回血计划」活动正式开启！"), "官方活动广告没被拦"
        fake_html = ('<div class="txt-box"><h3><a href="/link?url=abc">岳母彩礼涨价逼跑新郎</a></h3>'
                     '<p class="txt-info">彩礼钱我和你爸出,不用你们还.</p></div>')
        got = _SOGOU_PAT.findall(fake_html)
        assert got and _clean_text(got[0][1]) == "岳母彩礼涨价逼跑新郎", got
        assert got[0][0] == "/link?url=abc", "文章链接没解析出来"
        assert _clean_text(got[0][2]) == "彩礼钱我和你爸出，不用你们还。", got[0][2]
        # 单条素材：标题 + 正文 + 钩子，一条一个钩子，之间用分隔线
        stories = [{"title": "岳母彩礼涨价逼跑新郎", "desc": "彩礼钱我和你爸出，不用你们还。",
                    "src": "公众号", "url": "https://example.com/a"},
                   {"title": "大姑姐打起了我家五百万学区房的主意", "desc": "d" * 60,
                    "src": "虎扑步行街", "url": "https://example.com/b"}]
        scopy = render_pool(stories, top=5, now=datetime(2026, 9, 29, 8, 0))
        assert "① 岳母彩礼涨价逼跑新郎" in scopy and "② 大姑姐" in scopy, "单条编号没排对"
        assert "彩礼钱我和你爸出" in scopy, "故事正文没进文案"
        assert "http" not in scopy and "#" not in scopy, "发帖素材里不该有链接"
        assert sum(h in scopy for h in _HOOK_SELF) == 2, "两条素材没各带自己的钩子"
        # 隔断式：一到三行一段、段间空行；虎扑热评要单独成段
        paras = _paragraphs("第一句。第二句。第三句。第四句。第五句。第六句。第七句。")
        assert len(paras) == 4 and all(len(p) <= 60 for p in paras), paras
        assert _paragraphs("。") == [] and _paragraphs("") == [], "空输入不该切出段落"
        # 断句断在引号里时，下一段会以孤零零的右引号开头，要摘掉
        assert not any(p.startswith("”") for p in _paragraphs(
            "“阿达，太巧了吧！我家也是这样的！” 后面讲的是正文。又一句。")), "段落开头留了孤零零的右引号"
        assert _paragraphs("一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十，后半句"), \
            "没有句号的长句也要能断"
        hupu = [{"title": "虎扑一条", "desc": "楼主说了一句话。又补了一句。",
                 "replies": ["第一条回复", "第二条回复"], "src": "虎扑步行街", "url": ""}]
        hcopy = render_pool(hupu, top=1, now=datetime(2026, 9, 29, 8, 0))
        assert "网友：第一条回复" in hcopy and "网友：第二条回复" in hcopy, "热评没单独成段"
        assert "\n\n" in hcopy, "段落之间没有空行，不是隔断式"
        # 正文补全：搜狗跳转页里真实地址是 JS 拼的；前言要能从首句里切掉
        fake_js = ("<script>var url='';url += 'https://mp.';url += 'weixin.qq.c';"
                   "url += 'om/s?src=11';</script>")
        assert _wechat_url_from(fake_js) == "https://mp.weixin.qq.com/s?src=11", _wechat_url_from(fake_js)
        assert _strip_article_head("本文转自公众号：路上读书 （ID：x） ▼ 这几天都在追剧。第二句。") \
            == "这几天都在追剧。第二句。"
        # 对话引子（没人物没事）要整组丢掉，露出来的才是正文
        assert _drop_lead_dialogue(
            "“阿达，太巧了吧！我家也是这样的！” “不是吧，你老妈也是这样？” 每当新疆人聊到妈妈，大家总有话说。"
        ).startswith("每当新疆人聊到妈妈"), "对话引子没被丢掉"
        assert _drop_lead_dialogue("正文开头就是正文。第二句。") == "正文开头就是正文。第二句。", \
            "没有引子时不该乱切"
        assert _drop_lead_dialogue("“短”") == "“短”", "整篇都是引语时不该吃光"
        assert _drop_ad_lead("正文偶尔提一句招生，不算广告。") == "正文偶尔提一句招生，不算广告。", \
            "促销词太少就别动手"
        assert _drop_ad_lead(
            "某某画室连续斩获状元 共取得合格证 1034 张 画室学员报名咨询电话 123，"
            "AI绘画这几年发展迅猛，工具越来越好用，网友的脑洞也一发不可收拾。"
        ).startswith("AI绘画这几年发展迅猛"), "广告头要切掉"
        assert _drop_ad_lead("画室 合格证 状元") == "画室 合格证 状元", "切完没正文就别切"
        # AI 改编：解析要能扛住模型加说明 / 套 ```json 围栏
        assert _ai_parse("好的：\n```json\n[{\"title\": \"T\", \"paras\": [\"a\", \"b\"]}]\n```").__len__() == 1
        assert _ai_parse("不听话，没有 JSON") == []
        assert _normalize_quotes('他说"行"就走了') == '他说“行”就走了', _normalize_quotes('他说"行"就走了')
        assert _normalize_quotes("他嫌'杂牌'不好") == "他嫌‘杂牌’不好", _normalize_quotes("他嫌'杂牌'不好")
        # 撞输出上限被截断时，完整的那几条要能捞出来
        cut = _ai_parse('[{"title":"A","paras":["a1"]},{"title":"B","paras":["b1"]},{"title":"C","par')
        assert [x["title"] for x in cut] == ["A", "B"], cut
        assert _ai_parse('[{"title":"只有标题"}]') == []
        ai_st = {"title": "开场钩子", "desc": "第一段。\n\n第二段。", "ai": True, "src": "AI 改编", "url": ""}
        apool = render_pool([ai_st], top=1, now=datetime(2026, 9, 30, 8, 0))
        assert "\n\n第二段。" in apool, "AI 故事的分段被 _paragraphs 重排了"
        assert not any(h in apool for h in _HOOK_SELF), "AI 故事不该再套模板钩子"
        assert "网友：" not in apool
        # 标题只抄例句的，等于什么都没写，得拦掉（弱模型爱这么交差）
        assert len(_load_hook_table()) >= 15, \
            "hooksupdate.md 的 MACHINE:HOOKS 没读到，钩子表退回内置默认值了"
        assert _title_bare("不懂就问：这到底合不合规？"), "抄例句的标题没被拦"
        assert _title_bare("卧槽，真的假的？"), "短得没信息的标题没被拦"
        # 风向自动区 + 跨天累计：站点那份 hooksupdate.md 靠这两块每天刷新
        auto = _splice_auto("A<!-- AUTO:BANGDAN -->\n旧\n<!-- /AUTO:BANGDAN -->B", "新")
        assert "新" in auto and "旧" not in auto, auto
        assert _splice_auto("没有标记", "新") == "没有标记"
        rows4 = [{"text": "暴论：这彩礼还能临时涨价，男方当场就走了", "cat": "none",
                  "name": "x", "handle": "x", "v": 100, "l": 1}] * 3
        h4 = [{"d": "2026-10-04", "hooks": [["暴论", 4]], "styles": [["暴论观点", 3]],
               "terms": []},
              {"d": "2026-10-05", "hooks": [["暴论", 4]], "styles": [["暴论观点", 3]],
               "terms": []}]
        body = render_bangdan_auto(bangdan_fingerprint(rows4), [("彩礼", 5)], h4,
                                   now=datetime(2026, 10, 6))
        assert "暴论" in body and "彩礼" in body and "2/2 天" in body, body
        assert not _title_bare("不懂就问：楼下装修三年没人管，物业说管不了"), \
            "钩子接上事由的正常标题被误杀了"
        # 自编人名：原文没有的名字一律拦掉，常用词不能误杀
        assert invented_names("李伟在互联网公司上班。后来李伟辞职了。", "彩礼") == ["李伟"]
        assert invented_names("王奶奶把养老钱捂得紧紧的。", "") == ["王奶奶"]
        assert invented_names("刘浩哭了。刘浩说再也不碰了。", "刘浩是个体育老师") == []
        assert invented_names("马路上车很多，马路两边都停满了，白天也没人管。", "") == [], \
            "「马路」「白天」被当成名字了"
        # 一批挂掉不该把后面的批全废掉：限流是随机的，接着试还能捞回来
        gg = globals()
        calls = {"n": 0}
        dead = len(_AI_MODEL_FALLBACKS) + 1     # 第一批会挨个试完所有模型

        def flaky(key, base, model, system, user):
            calls["n"] += 1
            if calls["n"] <= dead:
                return ""
            return json.dumps([{"title": "不懂就问：楼下装修三年没人管",
                                "paras": ["甲" * 120, "乙" * 60, "丙" * 60]}],
                              ensure_ascii=False)

        keep_call, keep_cfg = gg["_ai_call"], gg["_ai_config"]
        # 夹具里全是新闻标题（一个母题都不命中），拼一条有母题的进去才能跑到改写
        hot8 = items + [{"title": "女子相亲被要20万彩礼", "platforms": ["微博"], "rank": 1}]
        # base 得是智谱那家，降级链才有 3 个模型（_fallbacks 按端点给链）
        gg["_ai_call"], gg["_ai_config"] = flaky, lambda: (
            "k", "https://open.bigmodel.cn/api/paas/v4", "m")
        try:
            salvaged = ai_write_stories(hot8, [], 8)
        finally:
            gg["_ai_call"], gg["_ai_config"] = keep_call, keep_cfg
        assert len(salvaged) == 1 and salvaged[0]["ai"], salvaged
        # 钩子话题：只有标题里带钩子的才算，且按共振数排热度
        hot = [{"title": "男子讨薪偷老板6千元被抓", "platforms": ["微博", "知乎"], "rank": 1},
               {"title": "某公司发布新款服务器", "platforms": ["IT之家"], "rank": 1},
               {"title": "彩礼谈崩了", "platforms": ["微博"], "rank": 5},
               {"title": "Mate90 系列新品发布会", "platforms": ["微博"], "rank": 1}]
        tops = _hook_topics(hot, 5)
        assert {g for g, _ in tops} >= {"职场", "彩礼婚恋"}, tops
        assert "发布会" not in "".join(w for _, ws in tops for w in ws), "新闻腔标题不该进选题"
        assert all(w for _, ws in tops for w in ws)
        _tw = _load_topic_words()
        assert "彩礼" in _tw.get("彩礼婚恋", []), _tw.get("彩礼婚恋")
        assert _ai_config()[1].startswith("http"), "AI base 没配出默认值"
        # 换供应商只认「专属 key」：端点改了、key 没填时不能拿旧 key 去撞 401
        os.environ.pop("X_WRITER_API_KEY", None)
        no_wkey = _ai_config()
        if _yaml_ai().get("x_writer_api_base"):       # 配置里确实指了别家才谈得上回退
            assert "deepseek" not in no_wkey[1], f"没专属 key 却把端点切走了：{no_wkey[1]}"
        os.environ["X_WRITER_API_KEY"] = "sk-test-only"
        try:
            w = _ai_config()
        finally:
            os.environ.pop("X_WRITER_API_KEY", None)
        assert w[0] == "sk-test-only" and w[1].startswith("http"), w
        assert _fallbacks("https://api.deepseek.com")[0] == "deepseek-chat"
        assert _fallbacks("https://open.bigmodel.cn/api/paas/v4")[0] == "glm-4.7-flash"
        assert _fallbacks("https://my-relay.example.com/v1") == []
        assert _ARTICLE_CHARS >= 500, "正文取太短就只剩引子了"
        assert _cut_sentences("第一句。第二句。第三句。", chars=10) == "第一句。第二句。", \
            _cut_sentences("第一句。第二句。第三句。", chars=10)
        assert _story_key({"title": "脑洞故事", "desc": "d" * 100}) \
            < _story_key({"title": "彩礼纠纷", "desc": "d" * 100}), "脑洞没排在婚嫁前面"
        assert "你刷到哪条" not in scopy and "这几条" not in scopy, "单条素材不该用「几选一」的口气提问"
        assert any(a in scopy for a in _ASK_ONE), "单条素材没带追问"
        assert _story_ok("彩礼，中国旧时婚礼程序之一，又称财礼、聘礼等。") is False, "百科词条不算故事"
        # 标题层：整年合集和软广，正文再像样也得拦在门外
        assert _STORY_NOISE_TITLE.search("2019年最后的沙雕新闻正式出炉！"), "年度合集没在标题层被拦"
        assert _STORY_AD.search("万万没想到，手指上竟然隐藏着这个淡斑开关"), "标题里的软广没被拦"
        # 换不到原文的公众号素材要整条丢掉：半截摘要接上钩子根本读不通
        g = globals()
        keep_wechat, keep_fetch = g["_wechat_url"], g["fetch_article"]
        g["_wechat_url"] = lambda url: "https://mp.weixin.qq.com/s/x"
        g["fetch_article"] = lambda url, chars=0: ""
        try:
            left = enrich_fulltext([
                {"title": "只有摘要的", "desc": "d" * 40, "src": "公众号", "url": "u"},
                {"title": "虎扑的", "desc": "d" * 40, "src": "虎扑步行街", "url": "u"},
            ])
        finally:
            g["_wechat_url"], g["fetch_article"] = keep_wechat, keep_fetch
        assert [x["title"] for x in left] == ["虎扑的"], f"拿不到原文的没被丢掉：{left}"
        assert scopy.count("-" * 18) == 1, "两条之间应该正好一条分隔线"
        ssrc = render_sources(stories, now=datetime(2026, 9, 29, 8, 0))
        assert "二创素材" in ssrc and "https://example.com/a" in ssrc, "素材文件没带原文链接"
        assert render_pool([], top=5).strip() == "", "空素材不该吐东西"
        # 热榜原始链接：给飞书里的豆包当二创素材，得带链接、带平台、拦电商稿
        lk_items = [
            {"title": "女子相亲被要20万彩礼", "url": "https://e.com/a", "group": "彩礼婚恋",
             "platforms": ["微博", "知乎"], "rank": 1, "trend": "", "count": ""},
            {"title": "小米冰箱开售，4999 元", "url": "https://e.com/b", "group": "",
             "platforms": ["微博"], "rank": 1, "trend": "", "count": ""},
        ]
        lk = render_links(lk_items, now=datetime(2026, 10, 6, 8, 0))
        assert "https://e.com/a" in lk and "女子相亲被要20万彩礼" in lk, lk
        assert "4999" not in lk, "电商稿不该进链接清单"
        assert "微博+知乎" in lk, "平台没带上"
        # 爆款风向要给链接加权：今天 X 上全在聊彩礼，带彩礼那条就得往前排
        bd3 = [{"handle": "x", "name": "x", "text": "这彩礼又涨了", "v": 1,
                "r": 0, "l": 0, "cat": "none"}] * 5
        lk2 = render_links(lk_items + [
            {"title": "公司裁员不给赔偿", "url": "https://e.com/c", "group": "职场",
             "platforms": ["微博", "贴吧", "知乎"], "rank": 1, "trend": "up", "count": ""},
        ], bang=bd3, now=datetime(2026, 10, 6, 8, 0))
        assert lk2.index("女子相亲被要20万彩礼") < lk2.index("公司裁员不给赔偿"), lk2
        assert "\U0001F525" in lk2, "爆款题材没标出来"
        assert "1." not in render_links([], now=datetime(2026, 10, 6, 8, 0)), \
            "没素材时不该列出条目"
        assert _story_key({"title": "彩礼纠纷", "desc": "d" * 100}) \
            > _story_key({"title": "无关故事", "desc": "d" * 100}), "婚嫁家事没被排到后面"
        assert _story_ok("彩礼涨价了") is False, "太短的摘要不该算故事"
        assert _story_ok("07 婆婆把房子留给她的事，胡莎早就知道") is False, "网文小节不该算故事"
        assert _story_ok("总攻目标吹起冲锋号下面来自真实网友的相亲翻车经历仅供单身族群参考张裕自大学起接触电脑之后就成了这样一段没有任何标点的长句") is False, \
            "没有标点的烂排版不该算故事"
        assert _story_ok("近日有女顾客在专柜消费二十万，因赠品寄错引发争议，品牌已致歉。") is True
        dup = [{"title": "彩礼涨价逼跑新郎", "desc": "d" * 40},
               {"title": "彩礼又涨了", "desc": "d" * 40},
               {"title": "彩礼谈崩了", "desc": "d" * 40},
               {"title": "邻居装修吵翻天", "desc": "d" * 40}]
        got2 = _pick_stories(dup, 3)
        assert len(got2) == 3 and sum("彩礼" in s["title"] for s in got2) == 2, \
            f"同题材没被压到 2 条：{[s['title'] for s in got2]}"
        # 同一个搜索词翻出来的东西天然同题材，按词再卡一道
        dupkw = [{"title": f"AI整活第{i}条", "desc": "d" * 40, "kw": "AI 整活"} for i in range(4)]
        dupkw.append({"title": "无关的一条帖子", "desc": "d" * 40})
        got3 = _pick_stories(dupkw, 3)
        assert len(got3) == 3 and sum(x.get("kw") == "AI 整活" for x in got3) == 2, \
            f"同一个搜索词的素材没被限流：{[x['title'] for x in got3]}"
        # 换个日期应该换一套说法，不然天天一个味
        # 爆款源：风向全是规则量出来的，量错了整套改写方向跟着错，钉住它
        fake_bd = [
            {"handle": "a", "name": "甲", "text": "卧槽！这个也太离谱了", "v": 900,
             "r": 1, "l": 1, "cat": "none"},
            {"handle": "b", "name": "乙", "text": "冷知识：充电线还可以当鞋带", "v": 800,
             "r": 1, "l": 1, "cat": "none"},
            {"handle": "c", "name": "丙", "text": "为什么商场里的书店不倒闭？", "v": 700,
             "r": 1, "l": 1, "cat": "finance"},
        ]
        bfp = bangdan_fingerprint(fake_bd)
        assert bfp["n"] == 3 and bfp["openers"], bfp
        assert dict(bfp["hooks"]) == {"震惊质疑": 1, "冷知识反差": 1, "暴论反问": 1}, bfp["hooks"]
        assert dict(bfp["cats"]) == {"none": 2, "finance": 1}, bfp["cats"]
        bmd = render_bangdan(fake_bd, now=datetime(2026, 10, 6, 8, 0))
        assert "风向：" in bmd and "@a" in bmd and "震惊质疑" in bmd, bmd[:300]
        # 抓不到爆款源要能正常出稿，不能抛异常
        assert "没抓到" in render_bangdan([], now=datetime(2026, 10, 6, 8, 0))
        assert bangdan_fingerprint(None)["n"] == 0
        # 爆款题材反向加权：爆款里全在聊彩礼，彩礼婚恋就该压过热搜更多的职场
        news2 = [{"title": "男子讨薪偷老板6千元被抓", "platforms": ["微博"], "rank": 1},
                 {"title": "公司裁员不给赔偿", "platforms": ["微博"], "rank": 2},
                 {"title": "年终奖缩水了", "platforms": ["微博"], "rank": 3},
                 {"title": "彩礼谈崩了", "platforms": ["微博"], "rank": 9}]
        plain = [g for g, _ in _hook_topics(news2, 5)]
        bd2 = [{"handle": "x", "name": "x", "text": "这彩礼还能临时涨价", "v": 1,
                "r": 0, "l": 0, "cat": "none"}] * 8
        weighted = [g for g, _ in _hook_topics(news2, 5, bang=bd2)]
        assert plain[0] == "职场" and weighted[0] == "彩礼婚恋", (plain, weighted)
        # 钩子按日期轮换、批内不重复：批内撞车就是「十条八条不懂就问」那个毛病
        d0 = datetime(2026, 10, 6).toordinal()
        picks0 = [_WRITER_HOOKS[(d0 + n) % len(_WRITER_HOOKS)][0] for n in range(12)]
        assert len(set(picks0)) == 12 and len(_WRITER_HOOKS) >= 15, picks0
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
    # 板块二：故事素材池 -> 每条单独发。正文来自 collect_stories（公众号 / 虎扑 / 贴吧）；
    # 抓不到才退回榜单标题——那样只有标题，信息量和卡片图没区别。
    stories = [] if args.no_stories else collect_stories(args.story_top)
    # 爆款源：X 中文区的实时风向。抓不到就当没有，不影响出稿。
    bang = [] if args.no_bangdan else fetch_hot_sources()
    fp = bangdan_fingerprint(bang)
    terms = _bd_terms(bang)
    (out / "bangdan.md").write_text(render_bangdan(bang), encoding="utf-8")
    # 记一笔历史（跨天累计看这个），再把当日风向刷进规范里那块 AUTO:BANGDAN
    hist = _bd_history(out)
    if fp["n"]:
        _bd_save_history(out, hist, fp, terms)
    # 二创规范（hooksupdate.md）也复制一份到站点：豆包那边读同一个 URL，两边就同一份。
    # 站点那份额外把「今日爆款风向」刷成今天的，豆包不用再去翻 bangdan.md。
    memo = Path(__file__).resolve().parent.parent / "hooksupdate.md"
    if memo.is_file():
        (out / "hooksupdate.md").write_text(
            _splice_auto(memo.read_text(encoding="utf-8", errors="ignore"),
                        render_bangdan_auto(fp, terms, hist)), encoding="utf-8")
    # collect_stories 里已经补过正文（换不到原文的已丢掉），这里不用再补一次。
    # 板块二优先走 AI 改编：抓来的原贴读起来断章取义（摘要 + 几个热评拼在一起），
    # 用户要的是「热搜话题 -> 有人有事的小故事」。改写失败就退回原贴，不开天窗。
    ai_n = 0 if args.no_ai else args.ai_top
    ai_stories = ai_write_stories(items, stories, ai_n, bang=bang) if ai_n else []
    posted = ai_stories or stories
    (out / "copy.txt").write_text(
        render_pool(posted, top=args.story_top) if posted
        else render_copy(items, args.site, top=args.copy_top), encoding="utf-8")
    (out / "sources.txt").write_text(render_sources(stories or ai_stories), encoding="utf-8")
    (out / "links.txt").write_text(
        render_links(items, bang=bang, top=args.link_top), encoding="utf-8")

    print(f"板块一 官方·图  {len(official):>3} 条 -> {out/'card.html'}，取前 {args.top}")
    for i, it in enumerate(official[: args.top], 1):
        print(f"  {i}. [{it['group']}] {it['title'][:38]}")
    copy_text = (out / "copy.txt").read_text(encoding="utf-8")
    # 现在是一批「单条帖子」，长度要按最长的一条算，不是整份文件加起来
    posts = [b for b in copy_text.split("-" * 18) if b.strip()]
    per = max((sum(2 if ord(c) > 127 else 1 for c in b) for b in posts), default=0)
    warn = "（超 280，得靠 Premium）" if per > 280 else "（普通账号也发得下）"
    if ai_stories:
        src_note = f"AI 改编 {len(ai_stories):>2} 条（原始素材 {len(stories)} 条已留档）"
    elif stories:
        mix = " / ".join(f"{k} {sum(1 for s in stories if s.get('src') == k)}"
                         for k in dict.fromkeys(s.get("src", "?") for s in stories))
        src_note = f"故事 {len(stories):>2} 条（{mix}）"
    else:
        src_note = "⚠️ 没抓到故事，退回榜单标题"
    print(f"板块二 素材      {src_note} -> {out/'copy.txt'}"
          f"，最长一条 {per} 字符 {warn}")
    if fp["n"]:
        print(f"爆款源 风向      {fp['n']:>3} 条样本 -> {out/'bangdan.md'}"
              f"，跑得动：{'、'.join(_bd_named(fp['hooks'], 4))}"
              f"，历史 {len(hist)} 天")
    else:
        print("爆款源 风向      ⚠️ 没抓到，这轮只按新闻源出稿")
    shown = (posted or sorted(items, key=_sort_key))[: args.story_top if posted else args.copy_top]
    for i, it in enumerate(shown, 1):
        print(f"  {i:>2}. {it['title'][:44]}")


# ── 爆款风向的「累计」：每天记一笔，hooksupdate.md 里那块自动风向才有跨天数据 ──
_BD_HIST_FILE = "bangdan_history.json"    # 落在 docs/x/ 下，跟着 reports 分支走
_BD_HIST_DAYS = 30


def _bd_history(out_dir):
    """读历史风向。流水线先把 reports 分支上那份恢复到 out/，读不到就是第一次跑。"""
    try:
        data = json.loads((Path(out_dir) / _BD_HIST_FILE).read_text(
            encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)][-_BD_HIST_DAYS:]


def _bd_save_history(out_dir, hist, fp, terms, now=None):
    """把今天这笔写进去，同一天只留最新一条。hist 原地更新，调用方接着拿它渲染。

    一天要跑几十轮，按轮次追加的话 30 条记录连一天都盖不住，「近 N 天」实际是
    「近 N 轮」——跨天学习就成了看最近几次运行，白学。
    """
    now = now or datetime.now()
    day = f"{now:%Y-%m-%d}"
    hist[:] = [h for h in hist if h.get("d") != day]
    hist.append({"d": day, "n": fp["n"], "mid": fp["len_mid"],
                 "hooks": fp["hooks"][:8], "styles": fp["styles"][:6],
                 "mech": (fp.get("mech") or [])[:8], "exp": (fp.get("exp") or [])[:8],
                 "terms": terms or []})
    del hist[:-_BD_HIST_DAYS]
    (Path(out_dir) / _BD_HIST_FILE).write_text(
        json.dumps(hist, ensure_ascii=False), encoding="utf-8")


_AUTO_RE = re.compile(
    r"(<!--\s*AUTO:BANGDAN\s*-->)(.*?)(<!--\s*/AUTO:BANGDAN\s*-->)", re.S)


def _splice_auto(text, body):
    """把「今日爆款风向」塞进 hooksupdate.md 的 AUTO:BANGDAN 区块（站点那份每天刷新）。"""
    return _AUTO_RE.sub(lambda m: m.group(1) + "\n" + body.strip() + "\n" + m.group(3),
                        text, count=1)


def bangdan_learn(hist, days=14):
    """从跨天的爆款源里学「哪套机制现在跑得动」——这就是每天自我更迭的那部分。

    单天样本会骗人（今天尽是彩礼，不代表彩礼一直行），所以看近 N 天的累计：
    出现天数多 = 稳定有效；累计曝光高 = 真能带量。两者合成权重，筛新闻时按这个打分。
    权重每次运行都重算，机制名单一变、样本一变，结果就跟着变，不需要人去改代码。
    """
    day_hit, exp_sum = Counter(), Counter()
    for day in hist[-days:]:
        for name, n in day.get("mech") or []:
            if n >= 2:                     # 当天只露一次的不算，噪声
                day_hit[name] += 1
        for name, v in day.get("exp") or []:
            exp_sum[name] += v
    names = set(day_hit) | set(exp_sum)
    if not names:
        return {}
    def norm(d):
        top = max(d.values()) or 1
        return {k: v / top for k, v in d.items()}
    nd, ne = norm(day_hit), norm(exp_sum)
    # 只有一天样本时「出现天数」完全没区分度（出现的都是 1），把重量压到曝光上
    wd = 0.6 if len({d.get("d") for d in hist[-days:] if d.get("mech")}) >= 2 else 0.2
    return {n: round(wd * nd.get(n, 0) + (1 - wd) * ne.get(n, 0), 3) for n in names}


def _bd_days(hist, key, skip, need=2):
    """近 7 天里某个开头 / 风格出现了几天。跨天还在 = 不是偶然，才值得写进规范。"""
    week = hist[-7:]
    cnt = Counter()
    for day in week:
        for name, n in day.get(key) or []:
            if name not in skip and n >= need:      # 当天只露一次的不算
                cnt[name] += 1
    return cnt, len(week)


def render_bangdan_auto(fp, terms, hist, now=None):
    """hooksupdate.md 里「今日爆款风向」那一块：今天什么样 + 近 7 天一直什么样。"""
    now = now or datetime.now()
    if not fp.get("n"):
        return "（今天没抓到爆款源，这一块保持上一次的内容，不影响出稿。）"
    w = bangdan_learn(hist)
    out = [f"**{now:%Y-%m-%d}** —— X 上今天跑得动的是 "
           f"{'、'.join(_bd_named(fp['hooks'], 4)) or '没量出明显套路'} 这类开头，"
           f"爆款正文中位 {fp['len_mid']} 字。今天优先用这几个钩子。", "",
           "真开头（只学口气和节奏，人和事必须新编）："]
    out += [f"- {t}" for t in fp["openers"][:4]]
    hc, days = _bd_days(hist, "hooks", {"无钩子"})
    sc, _ = _bd_days(hist, "styles", {"其他"})
    if days > 1 and hc:
        out += ["", f"近 {days} 天反复跑得动的开头（跨天还在，优先用）："
                    + "、".join(f"{k} {v}/{days} 天" for k, v in hc.most_common(6))]
    if days > 1 and sc:
        out += [f"近 {days} 天反复跑得动的风格："
                + "、".join(f"{k} {v}/{days} 天" for k, v in sc.most_common(4))]
    if w:
        top = sorted(w.items(), key=lambda kv: -kv[1])[:5]
        out += ["", "近 14 天学出来的爆火机制权重（筛选新闻就按这个排，每天重算）："
                    + "、".join(f"{k} {v:.2f}" for k, v in top)]
    if terms:
        out += ["今天爆款里的高频题材词（想加就加进 config/frequency_words.txt）："
                + "  ".join(f"{g}({c})" for g, c in terms[:8])]
    return "\n".join(out)


if __name__ == "__main__":
    main()
