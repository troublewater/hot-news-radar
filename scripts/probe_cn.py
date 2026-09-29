"""侦察：GitHub runner 能不能直连中文站点。

本地（国内 IP）能抓不代表 CI 能抓——runner 在美国机房，
中文站点经常按 IP 段拦。这个脚本只报告状态，不写入任何产物，
可以放心挂在 workflow 里跑一次看结论。

用法：python3 scripts/probe_cn.py
"""
import re
import sys
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

BLOCK_PAT = re.compile(r"验证码|antispider|安全验证|访问过于频繁|请开启JavaScript")


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "ignore")


def probe(name, url, marker, hint):
    try:
        status, text = fetch(url)
    except Exception as exc:  # noqa: BLE001 - 侦察脚本，任何异常都只报告
        print(f"❌ {name}: 请求失败 {type(exc).__name__}: {exc}")
        return
    n = len(re.findall(marker, text))
    blocked = "是" if BLOCK_PAT.search(text) else "否"
    flag = "✅" if n else "❌"
    print(f"{flag} {name}: HTTP {status} {len(text)} 字节，{hint} {n} 个，疑似拦截={blocked}")
    if not n and blocked == "是":
        print(f"     （返回的是拦截页，不是结果页）")


def main():
    print("=== 中文站点直连侦察（跑在 GitHub runner 上）===")
    probe(
        "搜狗微信（公众号文章）",
        "https://weixin.sogou.com/weixin?type=2&query=" + urllib.parse.quote("彩礼 涨价"),
        r'class="txt-box"',
        "公众号结果块",
    )
    probe("虎扑步行街", "https://bbs.hupu.com/bxj", r"/\d+\.html", "帖子链接")
    probe("百度贴吧热议", "https://tieba.baidu.com/hottopic/browse/topicList", r"topic_name|topicName", "话题")
    probe("知乎热榜网页版", "https://www.zhihu.com/hot", r"question", "问题链接")
    print("=== 侦察结束 ===")


if __name__ == "__main__":
    main()
