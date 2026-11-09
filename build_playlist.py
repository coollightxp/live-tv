#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
直播源自动构建器
================
流程：多源拉取 → 解析 → 并发体检 → 分片深检 → 按频道择优 → 生成 M3U → 输出报告

用法：
    python build_playlist.py                  # 默认（国内环境，严格测速，含深检）
    python build_playlist.py --region overseas # 海外 CI 环境，403 保留为备用
    python build_playlist.py --no-deep         # 跳过分片深检（更快，但可能混入假活源）

输出：
    live.m3u      订阅播放列表（可直接给 jsDelivr 分发）
    sources.json  源池（每频道多条线路，供 APP 故障切换）
    report.csv    体检明细
    stats.json    统计信息
"""

import argparse
import concurrent.futures as cf
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin

import requests

# ---------------------------------------------------------------- 配置区

# 每个源提供 3 个镜像：依次尝试，任一成功即止
# mode: m3u = 标准 M3U；txt = "频道名,url" 纯文本 + "#genre#"
SOURCES = [
    {
        "name": "vbskycn/iptv",
        "mode": "txt",
        "mirrors": [
            "https://cdn.jsdelivr.net/gh/vbskycn/iptv@master/tv/iptv4.txt",
            "https://ghproxy.net/https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.txt",
            "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.txt",
        ],
    },
    {
        "name": "iptv-org/cn",
        "mode": "m3u",
        "mirrors": [
            "https://iptv-org.github.io/iptv/countries/cn.m3u",
            "https://cdn.jsdelivr.net/gh/iptv-org/iptv@master/streams/cn.m3u",
        ],
    },
    {
        "name": "fanmingming/live-ipv6",
        "mode": "m3u",
        "only_ipv6": True,          # 纯 IPv6 源，默认不并入主列表
        "enabled": False,
        "mirrors": [
            "https://cdn.jsdelivr.net/gh/fanmingming/live@main/tv/m3u/ipv6.m3u",
            "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u",
        ],
    },
]

# 频道白名单关键字：命中任一即收录（想收全量就改成 None）
CHANNEL_KEYWORDS = [
    # 央视
    "CCTV", "CGTN", "CETV", "中国教育",
    # 卫视
    "卫视", "凤凰", "东方", "南方", "大湾区", "大湾区卫视",
    # 省级/城市台（按需增删）
    "北京", "上海", "天津", "重庆", "广东", "江苏", "浙江", "湖南", "湖北",
    "河南", "河北", "山东", "山西", "陕西", "安徽", "福建", "江西", "四川",
    "云南", "贵州", "广西", "海南", "辽宁", "吉林", "黑龙江", "内蒙古",
    "甘肃", "宁夏", "青海", "新疆", "西藏", "深圳", "厦门", "苏州", "成都",
]

# 频道名归一化：同名不同写法合并为一组
NAME_ALIASES = {
    "CCTV1": "CCTV-1 综合", "CCTV-1": "CCTV-1 综合", "CCTV1综合": "CCTV-1 综合",
    "CCTV2": "CCTV-2 财经", "CCTV-2": "CCTV-2 财经", "CCTV2财经": "CCTV-2 财经",
    "CCTV3": "CCTV-3 综艺", "CCTV-3": "CCTV-3 综艺", "CCTV3综艺": "CCTV-3 综艺",
    "CCTV4": "CCTV-4 中文国际", "CCTV-4": "CCTV-4 中文国际",
    "CCTV5": "CCTV-5 体育", "CCTV-5": "CCTV-5 体育",
    "CCTV5+": "CCTV-5+ 体育赛事", "CCTV-5+": "CCTV-5+ 体育赛事",
    "CCTV6": "CCTV-6 电影", "CCTV-6": "CCTV-6 电影",
    "CCTV7": "CCTV-7 国防军事", "CCTV-7": "CCTV-7 国防军事",
    "CCTV8": "CCTV-8 电视剧", "CCTV-8": "CCTV-8 电视剧",
    "CCTV9": "CCTV-9 纪录", "CCTV-9": "CCTV-9 纪录",
    "CCTV10": "CCTV-10 科教", "CCTV-10": "CCTV-10 科教",
    "CCTV11": "CCTV-11 戏曲", "CCTV-11": "CCTV-11 戏曲",
    "CCTV12": "CCTV-12 社会与法", "CCTV-12": "CCTV-12 社会与法",
    "CCTV13": "CCTV-13 新闻", "CCTV-13": "CCTV-13 新闻",
    "CCTV14": "CCTV-14 少儿", "CCTV-14": "CCTV-14 少儿",
    "CCTV15": "CCTV-15 音乐", "CCTV-15": "CCTV-15 音乐",
    "CCTV16": "CCTV-16 奥林匹克", "CCTV-16": "CCTV-16 奥林匹克",
    "CCTV17": "CCTV-17 农业农村", "CCTV-17": "CCTV-17 农业农村",
}

UA = ("Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")

HEADERS = {"User-Agent": UA, "Accept": "*/*"}

EPG_URL = "https://live.fanmingming.cn/e.xml"

# ---------------------------------------------------------------- 数据结构


@dataclass
class Candidate:
    name: str
    url: str
    source: str
    latency: int = -1
    status: str = "untested"      # ok / geo / dead
    detail: str = ""
    is_hls: bool = False          # 首包是否为 HLS 播放列表


@dataclass
class Stats:
    fetched: int = 0
    accepted: int = 0
    ok: int = 0
    geo: int = 0
    dead: int = 0
    deep_removed: int = 0         # 深检后被剔除的假活源
    deep_checked: int = 0
    channels: int = 0
    sources_up: list = field(default_factory=list)
    sources_down: list = field(default_factory=list)


# ---------------------------------------------------------------- 抓取


def fetch(url: str, timeout: int = 25) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code != 200 or not r.text:
            return None
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        print(f"    ! 抓取失败 {url[:60]}... ({type(e).__name__})")
        return None


def parse_m3u(text: str, source: str) -> list[Candidate]:
    out, name = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r",([^,]*)$", line)
            name = m.group(1).strip() if m else ""
        elif not line.startswith("#") and name:
            if line.startswith(("http://", "https://", "rtp://", "udp://")):
                out.append(Candidate(name=name, url=line, source=source))
            name = None
    return out


def parse_txt(text: str, source: str) -> list[Candidate]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith(",#genre#"):
            continue
        if "," not in line:
            continue
        name, url = line.split(",", 1)
        name, url = name.strip(), url.strip()
        if url.startswith(("http://", "https://", "rtp://", "udp://")):
            out.append(Candidate(name=name, url=url, source=source))
    return out


def collect(stats: Stats) -> list[Candidate]:
    all_c: list[Candidate] = []
    for src in SOURCES:
        if not src.get("enabled", True):
            print(f"  跳过（未启用）: {src['name']}")
            continue
        print(f"  拉取: {src['name']}")
        text = None
        for mirror in src["mirrors"]:
            text = fetch(mirror)
            if text:
                print(f"    ✓ 成功: {mirror[:70]}")
                break
        if not text:
            stats.sources_down.append(src["name"])
            print(f"    ✗ 全部镜像失败")
            continue
        stats.sources_up.append(src["name"])
        parser = parse_m3u if src["mode"] == "m3u" else parse_txt
        got = parser(text, src["name"])

        if src.get("only_ipv6"):
            got = [c for c in got if "://[" in c.url]

        stats.fetched += len(got)
        print(f"    → 解析出 {len(got)} 条")
        all_c.extend(got)
    return all_c


# ---------------------------------------------------------------- 过滤


QUALITY_RE = re.compile(
    r"(超高清|高清|超清|蓝光|标清|原画|流畅|H265|HEVC|UHD|FHD|HD|SD|4K|8K|2K|\d{3,4}\s*[PI])",
    re.I,
)


def strip_quality(s: str) -> str:
    """去掉任意位置的画质标记：600P/720P/1080P/4K/高清/HD..."""
    s = re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", " ", s)      # 括号内容
    s = QUALITY_RE.sub(" ", s)
    s = re.sub(r"[·•\-_\s]+", " ", s).strip()
    return s


def normalize(name: str) -> str:
    """频道名归一：同一频道不论清晰度/写法，统一成一个名字"""
    s = strip_quality(name.replace("\u3000", " ").strip())

    # 央视：CCTV 1 / CCTV-01 / CCTV1综合 → CCTV-N 标准名
    m = re.search(r"CCTV\s*[-_]?\s*(\d{1,2})\s*(\+)?", s, re.I)
    if m:
        num, plus = int(m.group(1)), ("+" if m.group(2) else "")
        key = f"CCTV{num}{plus}"
        if key in NAME_ALIASES:
            return NAME_ALIASES[key]
        return f"CCTV-{num}{plus}"

    return s if s else name.strip()


def is_wanted(name: str) -> bool:
    if CHANNEL_KEYWORDS is None:
        return True
    return any(k.upper() in name.upper() for k in CHANNEL_KEYWORDS)


def dedupe(cands: Iterable[Candidate]) -> list[Candidate]:
    seen, out = set(), []
    for c in cands:
        if c.url in seen:
            continue
        seen.add(c.url)
        out.append(c)
    return out


# ---------------------------------------------------------------- 体检


def probe(c: Candidate, region: str) -> Candidate:
    """探测单条源：拉首包判断是否为合法流"""
    t0 = time.time()
    try:
        r = requests.get(c.url, headers=HEADERS, timeout=8, stream=True, allow_redirects=True)
        head = b""
        try:
            head = r.raw.read(512)
        except Exception:
            pass
        code = r.status_code
        r.close()
        c.latency = int((time.time() - t0) * 1000)

        if code in (200, 206):
            body_ok = (b"#EXTM3U" in head or b"#EXTINF" in head
                       or head[:1] == b"\x47" or b"FLV" in head[:16])
            if body_ok:
                c.status, c.detail = "ok", f"HTTP {code}"
                c.is_hls = b"#EXTM3U" in head
            else:
                c.status, c.detail = "geo", f"HTTP {code} 内容异常"
        elif code in (401, 403):
            # 海外 CI 常见：源存在但按地域/Referer 限制
            c.status, c.detail = ("geo" if region == "overseas" else "dead"), f"HTTP {code}"
        elif code == 404:
            c.status, c.detail = "dead", "HTTP 404"
        else:
            c.status, c.detail = "geo", f"HTTP {code}"
    except Exception as e:
        c.latency, c.status, c.detail = -1, "dead", type(e).__name__
    return c


def health_check(cands: list[Candidate], region: str) -> list[Candidate]:
    print(f"  并发体检 {len(cands)} 条（region={region}）...")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=40) as ex:
        futures = [ex.submit(probe, c, region) for c in cands]
        for f in cf.as_completed(futures):
            done += 1
            if done % 50 == 0:
                print(f"    {done}/{len(cands)}")
            f.result()
    return cands


def _first_uri(text: str) -> str | None:
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            return ln
    return None


def deep_verify(c: Candidate) -> Candidate:
    """深度校验：顺着 m3u8 追到真实分片并试拉 1KB。

    很多人踩的坑是「播放列表能打开、播放器一播就转圈」——
    典型原因就是主列表 200、子列表或分片 404。只有这一步能抓到。
    """
    if c.status != "ok" or not c.is_hls:
        return c
    try:
        r = requests.get(c.url, headers=HEADERS, timeout=8)
        body, base = (r.text or ""), r.url
        if r.status_code != 200 or "#EXTM3U" not in body:
            return c

        # 多码率主列表 → 取第一条子列表
        if "#EXT-X-STREAM-INF" in body:
            sub = _first_uri(body.split("#EXT-X-STREAM-INF", 1)[1])
            if not sub:
                return c
            sub_url = urljoin(base, sub)
            r2 = requests.get(sub_url, headers=HEADERS, timeout=8)
            body, base = (r2.text or ""), r2.url
            if r2.status_code != 200 or "#EXTINF" not in body:
                if r2.status_code in (401, 403):
                    c.status, c.detail = "geo", f"子列表 HTTP {r2.status_code}"
                elif r2.status_code == 404:
                    c.status, c.detail = "dead", "子列表 404"
                return c

        # media 列表 → 第一个分片
        seg = None
        lines = body.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("#EXTINF"):
                for nxt in lines[i + 1:]:
                    nxt = nxt.strip()
                    if nxt and not nxt.startswith("#"):
                        seg = nxt
                        break
                break
        if not seg:
            return c          # 解析不出分片，宁可放过不误杀

        seg_url = urljoin(base, seg)
        r3 = requests.get(seg_url, headers={**HEADERS, "Range": "bytes=0-1023"},
                          timeout=8, stream=True)
        got = 0
        try:
            got = len(r3.raw.read(1024))
        except Exception:
            pass
        code = r3.status_code
        r3.close()

        if code in (200, 206) and got > 0:
            c.detail = f"{c.detail} 分片OK"
        elif code in (401, 403):
            c.status, c.detail = "geo", f"分片 HTTP {code}"
        elif code == 404:
            c.status, c.detail = "dead", "分片 404"
        # 其余异常（超时/断连）不动，避免网络抖动误杀
    except Exception:
        return c
    return c


def deep_check(cands: list[Candidate], stats: Stats) -> None:
    targets = [c for c in cands if c.status == "ok" and c.is_hls]
    stats.deep_checked = len(targets)
    print(f"  深度校验 {len(targets)} 条 HLS 源（追到分片）...")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=32) as ex:
        futures = [ex.submit(deep_verify, c) for c in targets]
        for f in cf.as_completed(futures):
            done += 1
            if done % 50 == 0:
                print(f"    {done}/{len(targets)}")
            f.result()
    stats.deep_removed = sum(1 for c in targets if c.status != "ok")


# ---------------------------------------------------------------- 择优


def category(name: str) -> str:
    u = name.upper()
    if "CCTV" in u or "CGTN" in u or "CETV" in u or "中国教育" in name:
        return "央视"
    if "卫视" in name or "凤凰" in name or "东方" in name or "南方" in name:
        return "卫视"
    return "地方台"


def group_by_channel(cands: list[Candidate]) -> dict[str, list[Candidate]]:
    """按频道归并所有可用源（不分清晰度/来源），组内按延迟排序"""
    groups: dict[str, list[Candidate]] = {}
    for c in cands:
        if c.status == "dead":
            continue
        groups.setdefault(c.name, []).append(c)
    for name in groups:
        groups[name].sort(key=lambda x: (x.status != "ok", x.latency if x.latency > 0 else 99999))
    return groups


def pick_best(groups: dict[str, list[Candidate]]) -> list[Candidate]:
    """每个频道只出一条：优先 ok 源中延迟最低的；全 geo 则取 geo 第一条"""
    chosen = []
    for name, items in groups.items():
        oks = [c for c in items if c.status == "ok"]
        chosen.append(oks[0] if oks else items[0])
    return chosen


def write_pool(groups: dict[str, list[Candidate]], path: str) -> None:
    """完整源池：一个频道下挂多条线路，供 APP 做故障切换"""
    pool = []
    for name, items in sorted(groups.items()):
        pool.append({
            "name": name,
            "category": category(name),
            "urls": [
                {
                    "url": c.url,
                    "status": c.status,
                    "latency_ms": c.latency,
                    "source": c.source,
                }
                for c in items
            ],
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 输出


def write_m3u(items: list[Candidate], path: str) -> None:
    """一个频道只写一条（清晰度不区分），多源池见 sources.json"""
    order = {"央视": 0, "卫视": 1, "地方台": 2}
    items = sorted(items, key=lambda c: (order.get(category(c.name), 9), c.name))

    lines = [f'#EXTM3U x-tvg-url="{EPG_URL}"']
    for c in items:
        lines.append(
            f'#EXTINF:-1 tvg-name="{c.name}" group-title="{category(c.name)}",{c.name}'
        )
        lines.append(c.url)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_report(items: list[Candidate], all_c: list[Candidate], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["分组", "频道", "状态", "延迟ms", "来源", "详情", "地址"])
        for c in sorted(items, key=lambda x: (category(x.name), x.name)):
            w.writerow([category(c.name), c.name, c.status, c.latency,
                        c.source, c.detail, c.url])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", choices=["cn", "overseas"], default="cn",
                    help="cn=严格测速；overseas=海外CI，403保留为备用")
    ap.add_argument("--out", default="live.m3u")
    ap.add_argument("--no-deep", action="store_true",
                    help="跳过分片深度校验（更快，但可能混入假活源）")
    args = ap.parse_args()

    print("=" * 60)
    print("直播源构建开始")
    print("=" * 60)

    stats = Stats()

    print("\n[1/5] 拉取订阅源")
    candidates = collect(stats)

    print("\n[2/5] 过滤频道")
    candidates = dedupe([c for c in candidates if is_wanted(c.name)])
    for c in candidates:
        c.name = normalize(c.name)
    candidates = dedupe(candidates)
    stats.accepted = len(candidates)
    print(f"  候选: {len(candidates)} 条")

    if not candidates:
        print("!! 无候选源，终止（保留旧文件）")
        return 1

    print("\n[3/5] 并发体检")
    health_check(candidates, args.region)

    if not args.no_deep:
        print("\n[3.5/5] 深度校验（追到分片，剔除假活源）")
        deep_check(candidates, stats)
        print(f"  剔除假活源 {stats.deep_removed} 条"
              f"（播放列表能开、分片打不开的）")

    for c in candidates:
        if c.status == "ok":
            stats.ok += 1
        elif c.status == "geo":
            stats.geo += 1
        else:
            stats.dead += 1
    print(f"  可用 {stats.ok} / 地域限制 {stats.geo} / 失效 {stats.dead}")

    print("\n[4/5] 按频道归并")
    groups = group_by_channel(candidates)
    best = pick_best(groups)
    stats.channels = len(groups)
    total_lines = sum(len(v) for v in groups.values())
    print(f"  收录 {stats.channels} 个频道，源池共 {total_lines} 条线路（M3U 每台一条）")

    if stats.channels < 10:
        print("!! 可用频道过少，判定源站异常，保留旧文件")
        return 2

    print("\n[5/5] 输出")
    write_m3u(best, args.out)
    write_pool(groups, "sources.json")
    write_report(best, candidates, "report.csv")

    from collections import Counter
    cat_cnt = Counter(category(c.name) for c in best)
    stats_json = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "region": args.region,
        "sources_up": stats.sources_up,
        "sources_down": stats.sources_down,
        "candidates": stats.accepted,
        "alive": stats.ok,
        "geo_limited": stats.geo,
        "dead": stats.dead,
        "deep_checked": stats.deep_checked,
        "deep_removed": stats.deep_removed,
        "channels": stats.channels,
        "lines": total_lines,
        "by_category": dict(cat_cnt),
    }
    with open("stats.json", "w", encoding="utf-8") as f:
        json.dump(stats_json, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成：{args.out}（{stats.channels} 频道 / {len(best)} 线路）")
    print(f"   {dict(cat_cnt)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
