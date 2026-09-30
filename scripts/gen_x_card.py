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
import urllib.parse
import urllib.request
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


def enrich_fulltext(stories, chars=_ARTICLE_CHARS):
    """把公众号素材换成原文正文；换不到原文的整条丢掉。

    搜狗只给一百来字的摘要，还从半句中间切。拿它当发帖素材就是「掐头去尾」，
    后半句接上钩子根本读不通。宁肯少几条，也不发半截话。
    """
    got, kept = 0, []
    for st in stories:
        if st.get("src") != "公众号" or not st.get("url"):
            kept.append(st)
            continue
        real = _wechat_url(st["url"])
        if not real:                               # 连着换十几次地址会被限流，歇一下再来一遍
            time.sleep(3)
            real = _wechat_url(st["url"])
        body = fetch_article(real, chars) if real else ""
        if not body:
            continue                               # 摘要版读不通，不要
        st["desc"] = body
        st["url"] = real                           # 换成正主地址，搜狗那个跳转是有时效的
        kept.append(st)
        got += 1
        time.sleep(0.6)                            # 连着换地址容易被限流，间隔别抠
    print(f"  正文补全：{got}/{len(stories)} 条拿到原文，其余丢弃")
    return kept


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
    groups = [fetch_stories(limit=20), fetch_hupu(limit=20, probe=36), fetch_tieba(limit=6)]
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
_AI_MODEL = "glm-4-flash"
_AI_BATCH = 8                    # 一次要 8 条：要 30 条得拆几次，题目太长会越写越水
_AI_TIMEOUT = 180

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
2. 允许自由改编：补细节、换人名地名（张姐、老王、我表弟那个单位），把事情落到一个具体的人身上。不要求真实，也不许加「网传」「据称」这种免责词。
3. 排版必须是隔断式：每段 1~3 句，段与段之间空一行，正文 3~5 段，全文 150~350 字。
4. 结尾落在一句有余味的转折或反讽上。不要总结、不要升华、不要问「你怎么看」。
5. 不要 emoji，不要 #话题标签，不要小标题，不要序号。"""

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


def _ai_config():
    """key/base/model 走环境变量；没配就退回仓库 config.yaml 里的智谱默认。"""
    key = os.environ.get("AI_API_KEY", "").strip()
    base = os.environ.get("AI_API_BASE", "").strip() or _AI_BASE
    model = os.environ.get("AI_MODEL", "").strip() or _AI_MODEL
    if not key:                                  # 环境变量没给，从 yaml 里捞一把
        try:
            cfg = (Path(__file__).resolve().parent.parent / "config" / "config.yaml").read_text(
                encoding="utf-8", errors="ignore")
            m = re.search(r"^\s*api_key:\s*[\"']?([^\"\'\s#]+)", cfg, re.M)
            key = (m.group(1) if m else "").strip()
        except OSError:
            key = ""
    if model.startswith("openai/"):              # LiteLLM 前缀，直连时要去掉
        model = model.split("/", 1)[1]
    return key, base.rstrip("/"), model


def _hook_topics(items, limit):
    """热搜里挑「有故事」的题，按上榜平台数（共振）排热度。"""
    seen, out = set(), []
    ranked = sorted(items, key=lambda it: (-len(it["platforms"]), it["rank"]))
    for it in ranked:
        t = it["title"].strip()
        if not t or t in seen or not _HOOK_TOPIC.search(t):
            continue
        if len(t) > 42 or _STORY_NOISE_TITLE.search(t):
            continue
        seen.add(t)
        out.append((t, len(it["platforms"])))
        if len(out) >= limit:
            break
    return out


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
    try:
        with urllib.request.urlopen(req, timeout=_AI_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        return data["choices"][0]["message"]["content"]
    except Exception as exc:                     # noqa: BLE001 - 模型挂了就退回抽取式素材
        print(f"  ⚠️ AI 改写失败：{type(exc).__name__}: {exc}")
        return ""


def _ai_parse(text):
    """模型爱加说明和 ```json 围栏，只抠出那个 JSON 数组。"""
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
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
            out.append({"title": title, "paras": paras})
    return out


def ai_write_stories(items, seeds, top, now=None):
    """把当天热搜话题交给模型，写成能单条发的隔断式故事。拿不到就返回空列表。"""
    key, base, model = _ai_config()
    if not key:
        print("  ⚠️ 没配 AI_API_KEY，跳过改写，退回抓来的原贴")
        return []
    topics = _hook_topics(items, min(40, max(16, top * 2)))
    if not topics:
        return []
    # 抓到的那批素材当「事实毛坯」递过去：模型改编时手上有细节，不至于全靠编
    seeds = [{"标题": s["title"], "正文": s["desc"][:400]} for s in (seeds or [])[:top]]
    wrote = []
    for start in range(0, top, _AI_BATCH):
        want = min(_AI_BATCH, top - len(wrote))
        if want <= 0:
            break
        user = [
            "今天各平台的热门话题（括号里是在几个平台上过榜）：",
            "\n".join(f"{i}. {t}（{n} 个平台）" for i, (t, n) in enumerate(topics, 1)),
        ]
        if seeds:
            user += ["", "今天从公众号 / 虎扑 / 贴吧抓到的原始素材（可以取细节，也可以完全不用）：",
                     json.dumps(seeds, ensure_ascii=False)]
        user += [
            "", f"写 {want} 条，每条独立成帖。要求：",
            "- 题材分散：婚嫁最多 2 条，其余分散到职场、钱、家庭、科技对比、生活观察。",
            "- 已经写过的题材不要重复：" + ("、".join(x["title"] for x in wrote) or "（这是第一批）"),
            "- title 是一到两句话的开场钩子（就像范文的第一句，直接抓住人）。",
            "- paras 是后面 3~5 段，每段 1~3 句，隔断式。",
            "", _AI_FEWSHOT,
            "", '只输出 JSON 数组：[{"title": "开场钩子", "paras": ["第一段", "第二段"]}]，'
                "不要任何解释，不要用 ``` 包裹。",
        ]
        got = _ai_parse(_ai_call(key, base, model, _AI_SYSTEM, "\n".join(user)))
        if not got:
            break
        wrote += got
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
    ap.add_argument("--story-top", type=int, default=30,
                    help="素材池放几条（都带正文，用户自己挑着一条条发）")
    ap.add_argument("--no-stories", action="store_true", help="不抓公众号故事，退回榜单标题")
    ap.add_argument("--no-ai", action="store_true", help="不做 AI 改编，直接发抓来的原贴")
    ap.add_argument("--ai-top", type=int, default=12, help="AI 改编几条（要 30 条得有人工挑）")
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
        assert _ai_parse('[{"title":"只有标题"}]') == []
        ai_st = {"title": "开场钩子", "desc": "第一段。\n\n第二段。", "ai": True, "src": "AI 改编", "url": ""}
        apool = render_pool([ai_st], top=1, now=datetime(2026, 9, 30, 8, 0))
        assert "\n\n第二段。" in apool, "AI 故事的分段被 _paragraphs 重排了"
        assert not any(h in apool for h in _HOOK_SELF), "AI 故事不该再套模板钩子"
        assert "网友：" not in apool
        # 钩子话题：只有标题里带钩子的才算，且按共振数排热度
        hot = [{"title": "男子讨薪偷老板6千元被抓", "platforms": ["微博", "知乎"], "rank": 1},
               {"title": "某公司发布新款服务器", "platforms": ["IT之家"], "rank": 1},
               {"title": "彩礼谈崩了", "platforms": ["微博"], "rank": 5}]
        tops = _hook_topics(hot, 5)
        assert [t for t, _ in tops] == ["男子讨薪偷老板6千元被抓", "彩礼谈崩了"], tops
        assert _ai_config()[1].startswith("http"), "AI base 没配出默认值"
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
    # collect_stories 里已经补过正文（换不到原文的已丢掉），这里不用再补一次。
    # 板块二优先走 AI 改编：抓来的原贴读起来断章取义（摘要 + 几个热评拼在一起），
    # 用户要的是「热搜话题 -> 有人有事的小故事」。改写失败就退回原贴，不开天窗。
    ai_n = 0 if args.no_ai else args.ai_top
    ai_stories = ai_write_stories(items, stories, ai_n) if ai_n else []
    posted = ai_stories or stories
    (out / "copy.txt").write_text(
        render_pool(posted, top=args.story_top) if posted
        else render_copy(items, args.site, top=args.copy_top), encoding="utf-8")
    (out / "sources.txt").write_text(render_sources(stories or ai_stories), encoding="utf-8")

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
    shown = (posted or sorted(items, key=_sort_key))[: args.story_top if posted else args.copy_top]
    for i, it in enumerate(shown, 1):
        print(f"  {i:>2}. {it['title'][:44]}")


if __name__ == "__main__":
    main()
