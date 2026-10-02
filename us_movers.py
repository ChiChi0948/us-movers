#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
us_movers.py — 美股每日重大個股漲跌幅捕捉器（全部使用免費公開資料源）

資料來源
  股票池 / 股數   Nasdaq.com 公開篩選器（失敗時改用 Wikipedia 的 S&P 500 + Nasdaq-100 名單）
  股價 / 成交量   Yahoo Finance（yfinance）
  財報日          Yahoo Finance（yfinance）
  新聞事件        Google News RSS（支援日期區間，所以能回溯歷史）
  AI 事件說明(選用) --llm gemini（Google 搜尋接地，有免費額度）或 --llm claude（web search，付費）

用法
  python us_movers.py                                   最近一個已收盤交易日
  python us_movers.py --date 2026-09-01                 指定某一天
  python us_movers.py --start 2026-01-01 --end 2026-09-01   回溯一段區間（可中斷後重跑，已抓過的新聞會沿用）
  python us_movers.py --top 30                          每組漲跌各取前 30 名
  python us_movers.py --groups "大型股:10000-,中型股:2000-10000,小型股:300-2000"   自訂市值分組（百萬美元）
  python us_movers.py --no-news                         只算數字、不抓新聞（回溯很長區間時較快）
  python us_movers.py --llm gemini --llm-delay 6        用 Gemini 上網查新聞並寫中文事件說明
  python us_movers.py --llm claude --llm-top 10         用 Claude，只查每榜前 10 名
  python us_movers.py --refresh-news --start 2026-09-01  用新的新聞來源重抓事件
  python us_movers.py --rebuild                         套用 data/events_manual.csv 後重建網站（免 API：
                                                        在網站按「複製給 AI」→ 貼到 Claude/Gemini 對話 → 把回覆的 CSV 貼進該檔）

輸出
  data/movers_history.csv   所有日期的累積紀錄（UTF-8 BOM，Excel 可直接開）
  dashboard.html            單檔互動頁：選日期、切漲/跌、點欄位排序、搜尋代號看歷史紀錄
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:  # 讓 --help 在沒裝 yfinance 時也能用
    yf = None

BENCH = "SPY"
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
HIST_COLS = ["date", "group", "side", "rank", "ticker", "name", "cat1", "cat2", "sector", "mcap", "price",
             "ret", "rel", "spy", "vr", "r1m", "r3m", "r6m", "ytd", "r1y", "earn", "tags", "event", "src", "url",
             "cat_src", "cur1", "cur2"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


import logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # Yahoo 拒絕時不要洗版


class Breaker:
    """連續失敗太多次就暫停這個資料來源（例如 Yahoo 對雲端主機回 401），避免白白耗時。"""
    def __init__(self, name, limit=12):
        self.name, self.limit, self.fail, self.off = name, limit, 0, False

    def ok(self):
        self.fail = 0

    def bad(self):
        self.fail += 1
        if self.fail >= self.limit and not self.off:
            self.off = True
            log(f"  ⚠ {self.name} 連續 {self.fail} 次失敗（Yahoo 可能暫時拒絕雲端主機），本次執行先停用此來源")


BR_EARN, BR_INFO, BR_NEWS = Breaker("Yahoo 財報日"), Breaker("Yahoo 公司資料"), Breaker("Yahoo 個股新聞")


# ─────────────────────────── 股票池 ───────────────────────────
_NAME_CUT = re.compile(
    r"\s+(Class [A-C] )?(Common Stock|Ordinary Shares|Common Shares|American Depositary|"
    r"Depositary Shares|Sponsored ADR|ADS|Subordinate Voting|New York Registry).*$", re.I)


def clean_name(n: str) -> str:
    return _NAME_CUT.sub("", str(n)).strip()


def short_name(n: str) -> str:
    """給新聞搜尋用：'Okta, Inc.' -> 'Okta'"""
    s = re.sub(r"[,.]?\s+(Inc|Corp|Corporation|Company|Co|Holdings?|Group|plc|Ltd|N\.V|S\.A|SE|AG|"
               r"Limited|Incorporated|Technologies|Systems|Platforms|Class [A-C]).*$", "", n, flags=re.I)
    return s.strip(" ,.") or n


def load_universe(data_dir: Path, refresh: bool) -> pd.DataFrame:
    cache = data_dir / "universe.csv"
    if cache.exists() and not refresh:
        u = pd.read_csv(cache)
        # 用檔案內記錄的日期判斷新舊（GitHub/GitLab 每次 checkout 都會重設檔案時間）
        asof = pd.to_datetime(u["asof"].iloc[0], errors="coerce") if "asof" in u.columns and len(u) else pd.NaT
        if pd.notna(asof) and (pd.Timestamp.now("UTC").tz_localize(None) - asof).days < 7 and u["shares"].notna().any():
            return u

    try:
        r = requests.get("https://api.nasdaq.com/api/screener/stocks",
                         params={"tableonly": "true", "limit": "25000", "download": "true"},
                         headers=UA, timeout=60)
        r.raise_for_status()
        df = pd.DataFrame(r.json()["data"]["rows"])
        df = df[df["symbol"].str.fullmatch(r"[A-Z]{1,5}(/[A-Z])?", na=False)].copy()
        df["ticker"] = df["symbol"].str.replace("/", "-", regex=False)
        mcap = pd.to_numeric(df["marketCap"], errors="coerce")
        price = pd.to_numeric(df["lastsale"].astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce")
        df["shares"] = mcap / price
        df["mcap_now"] = mcap / 1e6
        df["name"] = df["name"].map(clean_name)
        out = df[["ticker", "name", "sector", "industry", "shares", "mcap_now"]]
        log(f"股票池：Nasdaq 篩選器 {len(out)} 檔")
    except Exception as e:
        log(f"Nasdaq 篩選器失敗（{e}），改用 Wikipedia S&P 500 + Nasdaq-100")
        rows = []
        for url, col in [("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol"),
                         ("https://en.wikipedia.org/wiki/Nasdaq-100", "Ticker")]:
            html = requests.get(url, headers=UA, timeout=60).text
            for t in pd.read_html(StringIO(html)):
                if col in t.columns:
                    name_col = "Security" if "Security" in t.columns else "Company"
                    sec_col = next((c for c in t.columns if "Sector" in str(c)), None)
                    for _, x in t.iterrows():
                        rows.append({"ticker": str(x[col]).replace(".", "-"), "name": x[name_col],
                                     "sector": x[sec_col] if sec_col else ""})
                    break
        out = pd.DataFrame(rows).drop_duplicates("ticker")
        out["industry"] = ""
        out["shares"] = np.nan  # 沒有股數時無法估歷史市值，市值門檻會略過
        out["mcap_now"] = np.nan
    data_dir.mkdir(parents=True, exist_ok=True)
    out = out.copy()
    out["asof"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out.to_csv(cache, index=False)
    return out


# ─────────────────────────── 股價 ───────────────────────────
def _yf_download(part, start, end):
    df = yf.download(part, start=start, end=end, auto_adjust=False, actions=False,
                     progress=False, threads=True, group_by="column")
    if df is None or df.empty:
        return None
    if not isinstance(df.columns, pd.MultiIndex):
        df.columns = pd.MultiIndex.from_product([df.columns, part])
    return df


def download_prices(tickers, start, end, chunk=100):
    """分批下載；Yahoo 限流時會有整批或部分股票抓不到，所以會檢查缺漏並分輪補抓。"""
    store = {}  # ticker -> (adj, close, vol)

    def absorb(df, part):
        for t in part:
            try:
                a, c, v = df[("Adj Close", t)], df[("Close", t)], df[("Volume", t)]
            except KeyError:
                continue
            idx = pd.to_datetime(a.index)
            if idx.tz is not None:
                idx = idx.tz_localize(None)
            a, c, v = a.set_axis(idx), c.set_axis(idx), v.set_axis(idx)
            n = int(a.notna().sum())
            if n and (t not in store or n >= int(store[t][0].notna().sum())):
                store[t] = (a, c, v)

    def grab(lst, size, pause, label):
        for i in range(0, len(lst), size):
            part = lst[i:i + size]
            df = None
            for attempt in range(3):
                try:
                    df = _yf_download(part, start, end)
                    if df is not None:
                        break
                except Exception as e:
                    log(f"  下載失敗重試 {attempt + 1}/3：{str(e)[:120]}")
                time.sleep(8 * (attempt + 1))
            if df is not None:
                absorb(df, part)
            log(f"  {label} {min(i + size, len(lst))}/{len(lst)}（目前有效 {len(store)} 檔）")
            time.sleep(pause)

    grab(list(tickers), chunk, 1.5, "股價")

    def last_day():
        """以「大多數股票都有資料」的最新一天為準（避免個別股票多出一筆奇怪日期，害全部被判定缺資料）。"""
        from collections import Counter
        cnt = Counter()
        for a, _, _ in store.values():
            cnt.update(a.dropna().index[-5:])
        if not cnt:
            return None
        top = max(cnt.values())
        return max(d for d, c in cnt.items() if c >= 0.5 * top)

    for rnd in range(1, 4):
        ld = last_day()
        missing = [t for t in tickers if t not in store
                   or ld is None or pd.isna(store[t][0].reindex([ld]).iloc[0])]
        if not missing:
            break
        log(f"第 {rnd} 輪補抓 {len(missing)} 檔（缺資料或缺 {ld:%Y-%m-%d}）" if ld is not None else f"第 {rnd} 輪補抓 {len(missing)} 檔")
        time.sleep(20 * rnd)
        size = 100 if len(missing) > 300 else (25 if rnd == 1 else 10)
        grab(missing, size, 3 * rnd, f"補抓{rnd}")

    # 最新一天只有少數股票有資料時（Yahoo 尚未完整發布），改用「近 5 日、不指定結束日」再抓一次
    from collections import Counter
    cnt = Counter()
    for a, _, _ in store.values():
        cnt.update(a.dropna().index[-3:])
    if cnt:
        newest, top = max(cnt), max(cnt.values())
        if cnt[newest] < 0.5 * top:
            log(f"最新交易日 {newest:%Y-%m-%d} 只有 {cnt[newest]} 檔有資料，改用近 5 日模式重抓一次")
            names = list(store)
            for i in range(0, len(names), 200):
                part = names[i:i + 200]
                try:
                    df = yf.download(part, period="5d", auto_adjust=False, actions=False,
                                     progress=False, threads=True, group_by="column")
                except Exception:
                    df = None
                if df is None or df.empty:
                    continue
                if not isinstance(df.columns, pd.MultiIndex):
                    df.columns = pd.MultiIndex.from_product([df.columns, part])
                for t in part:
                    try:
                        a, c, v = df[("Adj Close", t)], df[("Close", t)], df[("Volume", t)]
                    except KeyError:
                        continue
                    idx = pd.to_datetime(a.index)
                    if idx.tz is not None:
                        idx = idx.tz_localize(None)
                    a, c, v = a.set_axis(idx), c.set_axis(idx), v.set_axis(idx)
                    oa, oc, ov = store[t]
                    store[t] = (oa.combine_first(a), oc.combine_first(c), ov.combine_first(v))
                time.sleep(1.5)
            n2 = sum(1 for a, _, _ in store.values() if pd.notna(a.reindex([newest]).iloc[0]))
            log(f"  重抓後 {newest:%Y-%m-%d} 有資料：{n2} 檔")

    if not store:
        sys.exit("Yahoo 股價完全抓不到（可能被限流），請稍後重跑")
    build = lambda k: (pd.DataFrame({t: v[k] for t, v in store.items()}).sort_index()
                       .pipe(lambda d: d.set_axis(pd.to_datetime(d.index).tz_localize(None))))
    adj, close, vol = build(0), build(1), build(2)
    # 刪掉只有少數股票有資料的日期（例如個別股票多出的奇怪日期、尚未完整的當日資料）
    cnt = adj.notna().sum(axis=1)
    keep = cnt >= 0.5 * cnt.tail(30).median()
    if (~keep).any():
        log(f"略過資料不完整的日期：{', '.join(f'{d:%Y-%m-%d}({c})' for d, c in cnt[~keep].tail(5).items())}")
    return adj[keep], close[keep], vol[keep]


# ─────────────────────────── 計算 ───────────────────────────
def compute_day(d, adj, close, vol, univ, args):
    idx = adj.index
    i = idx.get_loc(d)
    if i < 1:
        return None
    base = i if args.include_day else i - 1

    def past(n):
        j = base - n
        return adj.iloc[base] / adj.iloc[j] - 1 if j >= 0 else pd.Series(np.nan, index=adj.columns)

    ret = adj.iloc[i] / adj.iloc[i - 1] - 1
    spy = float(ret.get(BENCH, np.nan))
    prior_year = adj.loc[:pd.Timestamp(d.year - 1, 12, 31)]
    ytd = (adj.iloc[base] / prior_year.iloc[-1] - 1) if len(prior_year) else pd.Series(np.nan, index=adj.columns)
    vol60 = vol.iloc[max(0, i - 60):i].mean()

    u = univ.set_index("ticker")
    df = pd.DataFrame({
        "ret": ret, "rel": ret - spy, "vr": vol.iloc[i] / vol60, "price": close.iloc[i],
        "r1m": past(21), "r3m": past(63), "r6m": past(126), "ytd": ytd, "r1y": past(252),
    })
    df = df.drop(index=[BENCH], errors="ignore").join(u[["name", "sector", "shares"]], how="inner")
    df["mcap"] = df["shares"] * df["price"] / 1e6  # 以現在股數 × 當日收盤估算
    df = df.dropna(subset=["ret"])
    df = df[(df["price"] >= args.min_price) & (df["ret"].abs() < 5)]
    df = df[df["ret"].abs() >= args.min_move / 100]

    out = []
    for gi, (gname, lo, hi) in enumerate(args.group_list):
        if df["mcap"].notna().any():
            g = df[(df["mcap"] >= lo) & (df["mcap"] < hi)]
        else:  # 備援股票池沒有股數，全部歸第一組
            g = df if gi == 0 else df.iloc[0:0]
        for side, sub in (("up", g[g.ret > 0].nlargest(args.top, "ret")),
                          ("down", g[g.ret < 0].nsmallest(args.top, "ret"))):
          for rank, (t, x) in enumerate(sub.iterrows(), 1):
            out.append({"date": d.strftime("%Y-%m-%d"), "group": gname, "side": side, "rank": rank, "ticker": t,
                        "name": x["name"], "sector": x["sector"] if pd.notna(x["sector"]) else "",
                        "mcap": x["mcap"], "price": x["price"], "ret": x["ret"], "rel": x["rel"],
                        "spy": spy, "vr": x["vr"], "r1m": x["r1m"], "r3m": x["r3m"], "r6m": x["r6m"],
                        "ytd": x["ytd"], "r1y": x["r1y"]})
    return out


def diagnose(tks, d, adj, close, univ, args, rows):
    """說明指定股票在 d 這天為什麼有／沒有上榜。"""
    u = univ.set_index("ticker")
    i = adj.index.get_loc(d)
    for t in tks:
        msg = [f"[檢查 {t} {d:%Y-%m-%d}]"]
        if t not in u.index:
            msg.append("不在股票池（Nasdaq 清單沒有，或代號格式不同）")
        elif t not in adj.columns:
            msg.append("在股票池，但 Yahoo 股價沒抓到（可能被限流或是現在市值低於門檻 1/3 被粗篩掉）")
        else:
            p0, p1 = adj[t].iloc[i - 1], adj[t].iloc[i]
            ret = p1 / p0 - 1 if pd.notna(p0) and pd.notna(p1) else np.nan
            sh = u.loc[t, "shares"]
            mc = sh * close[t].iloc[i] / 1e6 if pd.notna(sh) else np.nan
            grp = next((g for g, lo, hi in args.group_list if pd.notna(mc) and lo <= mc < hi), "不在任何組（市值不符）")
            hit = [r for r in rows if r["ticker"] == t]
            msg.append(f"前收 {p0:.2f} → 收 {p1:.2f}，日報酬 {ret * 100:+.2f}%，估算市值 {mc:,.0f} 百萬美元 → {grp}")
            msg.append(f"上榜：{hit[0]['group']} {'漲' if hit[0]['side'] == 'up' else '跌'}第 {hit[0]['rank']} 名" if hit
                       else "沒上榜（同組漲跌幅不在前列，或股價 < 最低股價門檻）")
        log("  ".join(msg))


# ─────────────────────────── 事件 ───────────────────────────
_earn_cache: dict[str, set] = {}


def earnings_dates(t):
    if t not in _earn_cache:
        s = set()
        if BR_EARN.off:
            return s
        try:
            ed = yf.Ticker(t).get_earnings_dates(limit=40)
            if ed is not None and len(ed):
                ix = pd.to_datetime(ed.index)
                if ix.tz is not None:
                    ix = ix.tz_convert("America/New_York").tz_localize(None)
                s = set(ix.normalize())
                BR_EARN.ok()
            else:
                BR_EARN.bad()
        except Exception:
            BR_EARN.bad()
        _earn_cache[t] = s
    return _earn_cache[t]


# ─────────────────────────── 新聞來源（依優先順序）───────────────────────────
# 1. CNBC「Stocks making the biggest moves」盤前/盤中/盤後專欄（逐檔寫漲跌原因）
# 2. Motley Fool / Benzinga「Why X stock ...」單檔原因文章
# 3. Yahoo Finance 個股新聞（只有最新資料，僅用於最近幾天）
# 4. Google News 財經媒體一般新聞
# 另外：SEC 8-K 公告 → 事件標籤（財報、併購、高層異動…）
try:
    from googlenewsdecoder import gnewsdecoder  # Google News 轉址連結 → 原始文章網址
except Exception:  # 沒裝也能跑，只是無法讀 CNBC/Fool 內文
    gnewsdecoder = None

import html as _html
from themes import (classify, keyword_theme, from_industry, load_manual_themes, ALIASES,
                    load_manual_rules, manual_at, base_classify, SRC_RANK)

NEWS_DELAY = 0.6
FIN_SITES = ["reuters.com", "cnbc.com", "benzinga.com", "fool.com", "marketwatch.com", "barrons.com",
             "finance.yahoo.com", "investopedia.com", "investors.com", "wsj.com", "bloomberg.com", "thefly.com"]
MOVE_RX = re.compile(
    r"\b(why|soar|surg|jump|rall|pop|climb|ris|gain|spik|rocket|skyrocket|plung|tumbl|sink|sank|slid|drop|fall|fell|"
    r"crater|slump|tank|dive|beat|miss|guidance|outlook|forecast|upgrad|downgrad|price target|acqui|merger|deal|"
    r"buyout|fda|approv|trial|offering|earnings|results|revenue|shares)", re.I)
SEC_ITEMS = {"1.01": "重大合約", "1.02": "終止合約", "1.03": "破產", "2.01": "併購/處分完成", "2.02": "財報",
             "2.03": "舉債", "2.05": "重組裁員", "2.06": "資產減損", "3.01": "下市/轉板通知", "3.02": "私募增資",
             "4.02": "財報重編", "5.01": "控制權變動", "5.02": "高層異動", "8.01": "重大事件"}
NY_TZ = "America/New_York"


def _get(url, headers=None, timeout=20, **kw):
    try:
        r = requests.get(url, headers=headers or UA, timeout=timeout, **kw)
        return r if r.status_code == 200 else None
    except Exception:
        return None


def gnews(query, after, before, n=10):
    """Google News RSS 搜尋；after/before 為日期（YYYY-MM-DD）。"""
    q = f"{query} after:{after:%Y-%m-%d} before:{before:%Y-%m-%d}"
    r = _get("https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}))
    time.sleep(NEWS_DELAY)
    if r is None:
        return []
    try:
        root = ET.fromstring(r.content)
    except Exception:
        return []
    items = []
    for it in root.iter("item"):
        src = it.find("source")
        src_name = src.text if src is not None else ""
        title = (it.findtext("title") or "").strip()
        if src_name and title.endswith(" - " + src_name):
            title = title[: -len(src_name) - 3]
        items.append({"title": title, "src": src_name, "url": it.findtext("link") or ""})
        if len(items) >= n:
            break
    return items


_resolved: dict[str, str] = {}


def resolve(url):
    """把 news.google.com 轉址連結還原成原始文章網址。"""
    if "news.google.com" not in url:
        return url
    if url in _resolved:
        return _resolved[url]
    real = ""
    if gnewsdecoder is not None:
        try:
            res = gnewsdecoder(url, interval=1)
            if res.get("status"):
                real = res.get("decoded_url", "")
        except Exception:
            pass
    _resolved[url] = real
    return real


def page_summary(url):
    """讀文章的 og:description（出版者自己寫的摘要）。"""
    r = _get(url)
    if r is None:
        return ""
    m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]*content=["\']([^"\']+)',
                  r.text, re.I) or re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\'](?:og:description|description)', r.text, re.I)
    return _html.unescape(m.group(1)).strip() if m else ""


def _mentions(text, ticker, name):
    t = text.lower()
    return ticker.lower() in re.findall(r"[a-z.\-]+", t) or short_name(name).lower() in t


def _trim(text, limit=300):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    p = max(cut.rfind(". "), cut.rfind("。"))
    return cut[: p + 1] if p > 80 else cut.rstrip() + "…"


# ---- 1. CNBC biggest moves ----
_cnbc: dict = {}


def _cnbc_articles(d, prev_d):
    """找出 d 當天盤前、盤中，以及前一交易日盤後的 CNBC movers 文章網址。"""
    want = {d.strftime("%Y/%m/%d"): ("midday", "premarket", "before-the-bell", "morning"),
            prev_d.strftime("%Y/%m/%d"): ("after-hours", "after-the-bell", "extended")}
    urls = set()
    r = _get("https://www.cnbc.com/id/20409666/device/rss/rss.html")  # Market Insider RSS（只有最近的）
    if r is not None:
        urls.update(re.findall(r"https://www\.cnbc\.com/\d{4}/\d{2}/\d{2}/[^<\"\s]+\.html", r.text))
    for it in gnews('site:cnbc.com "biggest moves"', prev_d - timedelta(days=1), d + timedelta(days=1), n=12):
        if "biggest moves" in it["title"].lower():
            u = resolve(it["url"])
            if u:
                urls.add(u)
    picked = set()
    for u in urls:
        m = re.search(r"cnbc\.com/(\d{4}/\d{2}/\d{2})/([^/]+)\.html", u)
        if not m or "biggest-moves" not in m.group(2):
            continue
        keys = want.get(m.group(1))
        if keys and any(k in m.group(2) for k in keys):
            picked.add(u)
    # 優先順序：當天盤中 > 當天盤前 > 前一天盤後
    today = d.strftime("%Y/%m/%d")
    return sorted(picked, key=lambda u: 0 if "midday" in u else 1 if today in u else 2)


def cnbc_lookup(ticker, name, d, prev_d):
    key = d.strftime("%Y-%m-%d")
    if key not in _cnbc:
        by_ticker, paras = {}, []
        for u in _cnbc_articles(d, prev_d):
            r = _get(u)
            if r is None:
                continue
            try:
                import lxml.html
                doc = lxml.html.fromstring(r.text)
            except Exception:
                continue
            for p in doc.iter("p"):
                text = p.text_content().strip()
                if len(text) < 30:
                    continue
                paras.append((text, u))
                for href in p.xpath(".//a/@href"):
                    m = re.search(r"/quotes/([A-Z][A-Z.\-]{0,6})", href)
                    if m:
                        by_ticker.setdefault(m.group(1).replace(".", "-"), (text, u))
                m = re.search(r"\(([A-Z]{1,5})\)", text[:80])
                if m:
                    by_ticker.setdefault(m.group(1), (text, u))
        _cnbc[key] = (by_ticker, paras)
        log(f"  CNBC movers {key}：{len(by_ticker)} 檔有說明")
    by_ticker, paras = _cnbc[key]
    hit = by_ticker.get(ticker)
    if not hit:
        sn = short_name(name).lower()
        hit = next(((t, u) for t, u in paras if t.lower().startswith(sn)), None)
    if hit:
        return {"event": _trim(hit[0]), "src": "CNBC", "url": hit[1]}
    return None


# ---- 2. Motley Fool / Benzinga ----
def why_article(ticker, name, d, prev_d):
    q = f'("{short_name(name)}" OR "{ticker}") (site:fool.com OR site:benzinga.com)'
    for it in gnews(q, prev_d - timedelta(days=1), d + timedelta(days=1), n=8):
        if not (_mentions(it["title"], ticker, name) and MOVE_RX.search(it["title"])):
            continue
        real = resolve(it["url"])
        desc = page_summary(real) if real else ""
        event = f"{it['title']}：{desc}" if desc and len(desc) > 40 else it["title"]
        return {"event": _trim(event), "src": it["src"] or ("Motley Fool" if "fool" in real else "Benzinga"),
                "url": real or it["url"]}
    return None


# ---- 3. Yahoo Finance 個股新聞（僅限最近）----
def yahoo_news(ticker, name, win_start, win_end):
    if BR_NEWS.off:
        return None
    try:
        items = yf.Ticker(ticker).news or []
        BR_NEWS.ok() if items else BR_NEWS.bad()
    except Exception:
        BR_NEWS.bad()
        return None
    best = None
    for it in items:
        c = it.get("content", it)
        title = c.get("title") or ""
        pub = c.get("pubDate") or c.get("displayTime") or it.get("providerPublishTime")
        try:
            ts = pd.Timestamp(pub, unit="s", tz="UTC") if isinstance(pub, (int, float)) else pd.Timestamp(pub)
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
        except Exception:
            continue
        if not (win_start <= ts <= win_end) or not title:
            continue
        url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url")
               or it.get("link") or "")
        src = (c.get("provider") or {}).get("displayName") or it.get("publisher") or "Yahoo Finance"
        cand = {"event": _trim(title), "src": src, "url": url}
        if _mentions(title, ticker, name) and MOVE_RX.search(title):
            return cand
        best = best or cand
    return best


# ---- 4. Google News 財經媒體 ----
def general_news(ticker, name, d, prev_d):
    base = f'("{short_name(name)}" OR "{ticker}") stock'
    sites = " OR ".join(f"site:{s}" for s in FIN_SITES)
    for q in (f"{base} ({sites})", base):
        items = gnews(q, prev_d - timedelta(days=1), d + timedelta(days=1), n=10)
        scored = sorted(items, key=lambda it: -(2 * _mentions(it["title"], ticker, name)
                                                 + bool(MOVE_RX.search(it["title"]))))
        if scored and _mentions(scored[0]["title"], ticker, name):
            it = scored[0]
            return {"event": _trim(it["title"]), "src": it["src"], "url": resolve(it["url"]) or it["url"]}
    return None


# ---- SEC 8-K ----
SEC_UA = {"User-Agent": os.environ.get("SEC_USER_AGENT") or "us-movers research bot contact@example.com",
          "Accept-Encoding": "gzip, deflate"}
_sec_cik: dict | None = None
_sec_subs: dict = {}


def sec_tags(ticker, win_start, win_end):
    global _sec_cik
    if _sec_cik is None:
        r = _get("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA, timeout=30)
        try:
            _sec_cik = {v["ticker"].upper().replace(".", "-"): int(v["cik_str"]) for v in r.json().values()} if r else {}
        except Exception:
            _sec_cik = {}
    cik = _sec_cik.get(ticker.upper())
    if not cik:
        return []
    if cik not in _sec_subs:
        r = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers=SEC_UA, timeout=30)
        time.sleep(0.15)  # SEC 限每秒 10 次
        try:
            _sec_subs[cik] = r.json()["filings"]["recent"] if r else {}
        except Exception:
            _sec_subs[cik] = {}
    rec = _sec_subs[cik]
    tags = []
    for form, acc, items in zip(rec.get("form", []), rec.get("acceptanceDateTime", []), rec.get("items", [])):
        if form not in ("8-K", "8-K/A"):
            continue
        try:
            ts = pd.Timestamp(acc)
            ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
        except Exception:
            continue
        if win_start <= ts <= win_end:
            for code in str(items).split(","):
                z = SEC_ITEMS.get(code.strip())
                if z and z not in tags:
                    tags.append(z)
    return tags


def event_window(d, prev_d):
    """前一交易日收盤（16:00 ET）到當天收盤後（16:30 ET）。"""
    start = pd.Timestamp(prev_d.strftime("%Y-%m-%d") + " 16:00", tz=NY_TZ).tz_convert("UTC")
    end = pd.Timestamp(d.strftime("%Y-%m-%d") + " 16:30", tz=NY_TZ).tz_convert("UTC")
    return start, end


# ---- 公司簡介（產業分類用）----
PROFILE_COLS = ["ticker", "yf_sector", "yf_industry", "kw_theme", "fetched"]


def load_profiles(path: Path) -> pd.DataFrame:
    if path.exists():
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    return pd.DataFrame(columns=PROFILE_COLS)


def ensure_profiles(tickers, path: Path, limit: int) -> pd.DataFrame:
    prof = load_profiles(path)
    have = set(prof["ticker"])
    todo = [t for t in tickers if t not in have][:limit]
    if todo and yf is not None:
        log(f"抓取公司產業資料 {len(todo)} 檔（已快取 {len(have)} 檔）")
        rows = []
        for i, t in enumerate(todo, 1):
            if BR_INFO.off:
                log(f"  其餘 {len(todo) - i + 1} 檔先用 Nasdaq 產業別分類，下次執行再補抓")
                break
            try:
                info = yf.Ticker(t).info or {}
            except Exception:
                info = {}
            if not (info.get("industry") or info.get("sector")):
                BR_INFO.bad()
                continue  # 沒抓到就不寫入快取，下次會再試
            BR_INFO.ok()
            sec, ind = info.get("sector", "") or "", info.get("industry", "") or ""
            c1, _ = from_industry(ind, sec)
            summ = info.get("longBusinessSummary", "") or ""
            rows.append({"ticker": t, "yf_sector": sec, "yf_industry": ind,
                         "kw_theme": keyword_theme(summ, c1), "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
            if i % 50 == 0:
                log(f"  產業資料 {i}/{len(todo)}")
            time.sleep(0.3)
        if rows:
            prof = pd.concat([prof, pd.DataFrame(rows, columns=PROFILE_COLS)], ignore_index=True)
        # 清掉以前存下的空白紀錄，讓它們下次重抓
        prof = prof[(prof["yf_industry"].astype(str) != "") | (prof["yf_sector"].astype(str) != "")]
        prof.to_csv(path, index=False, encoding="utf-8-sig")
    return prof


def apply_categories(hist: pd.DataFrame, data_dir: Path, univ: pd.DataFrame | None = None) -> pd.DataFrame:
    """產業分類（保留當時分類）：
    - cat1/cat2：上榜「當時」的分類。一旦用可靠的來源（熱門名單、Yahoo 產業別、業務描述）分好，就不再自動改動。
    - 只有當時分類不可靠（空白，或只是 Nasdaq 粗略產業別的備援）時，才會用更好的資料升級。
    - themes_manual.csv 手動指定的永遠優先，而且可以用起日、迄日指定適用期間。
    - cur1/cur2：現在的分類，網站上若和當時不同會另外標示。"""
    if hist.empty:
        return hist
    prof = load_profiles(data_dir / "profiles.csv").set_index("ticker")
    rules = load_manual_rules(data_dir / "themes_manual.csv")
    nas = {}
    if univ is not None and "industry" in univ.columns:
        nas = univ.set_index("ticker")[["sector", "industry"]].to_dict("index")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    base = {}
    for t in hist["ticker"].unique():
        p = prof.loc[t] if t in prof.index else None
        if isinstance(p, pd.DataFrame):
            p = p.iloc[-1]
        ind = p["yf_industry"] if p is not None else ""
        ysec = p["yf_sector"] if p is not None else ""
        kw = p["kw_theme"] if p is not None else ""
        from_nas = False
        if not ind and t in nas:
            v = nas[t].get("industry")
            ind = v if isinstance(v, str) else ""
            from_nas = bool(ind)
        sec = hist.loc[hist["ticker"] == t, "sector"].iloc[0]
        base[t] = base_classify(t, ind, ysec, kw, sec if isinstance(sec, str) else "", from_nas)

    hist = hist.copy()
    for c in ("cat1", "cat2", "cat_src", "cur1", "cur2"):
        if c not in hist.columns:
            hist[c] = ""
        hist[c] = hist[c].fillna("").astype(str)
    out = {"cat1": [], "cat2": [], "cat_src": [], "cur1": [], "cur2": []}
    for t, d, c1, c2, src in zip(hist["ticker"], hist["date"], hist["cat1"], hist["cat2"], hist["cat_src"]):
        b1, b2, bsrc = base[t]
        m = manual_at(rules, t, d)
        if m:                                   # 手動指定（含期間）最優先
            n1, n2, nsrc = (m[0] or b1), m[1], "manual"
        elif src not in ("", "manual", "nasdaq") and SRC_RANK.get(src, 0) >= 2 and (c1 or c2):
            n1, n2, nsrc = c1, c2, src           # 當時已有可靠分類 → 保留
        else:                                   # 沒有分類、只有備援、或手動規則已移除 → 用目前最好的
            n1, n2, nsrc = b1, b2, bsrc
        cm = manual_at(rules, t, today)
        out["cat1"].append(n1); out["cat2"].append(n2); out["cat_src"].append(nsrc)
        out["cur1"].append((cm[0] or b1) if cm else b1); out["cur2"].append(cm[1] if cm else b2)
    for k, v in out.items():
        hist[k] = v
    return hist


def _llm_prompt(row, earn, news, tags=()):
    heads = "\n".join(f"- {h['title']} ({h['src']})" for h in news) or "（無）"
    move = "上漲" if row["ret"] > 0 else "下跌"
    return (
        f"美股 {row['name']}（代號 {row['ticker']}）在 {row['date']}（美東時間）單日{move} {abs(row['ret']) * 100:.2f}%，"
        f"同日 S&P 500 (SPY) {row['spy'] * 100:+.2f}%。{'這天或前一個交易日有公布財報。' if earn else ''}{('SEC 公告類型：' + '、'.join(tags) + '。') if tags else ''}\n"
        f"已找到的參考新聞：\n{heads}\n\n"
        "請上網搜尋這一天前後（前一日盤後到當日收盤）的新聞，找出這檔股票當天大漲或大跌的主要原因。\n"
        "要求：\n"
        "1. 用繁體中文一段話寫出原因，80～120 字，保留關鍵數字（例如 EPS、營收與市場預期、財測、目標價調整、併購金額）。\n"
        "2. 只寫有來源支持的內容，不要推測；若找不到與這次股價變動相關的個股消息，"
        "event 寫「查無明確個股消息」，可再補一句當天可能的產業或大盤因素。\n"
        "3. 只輸出一個 JSON 物件，不要其他文字：\n"
        '{"event": "原因說明", "source": "主要來源媒體名稱", "url": "該新聞網址"}'
    )


def _parse_json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) and obj.get("event") else None
    except Exception:
        return None


def _post(url, headers, body, tries=3):
    for i in range(tries):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=120)
            if r.status_code in (429, 500, 502, 503, 529):
                time.sleep(15 * (i + 1)); continue
            if r.status_code != 200:
                log(f"  LLM 錯誤 {r.status_code}：{r.text[:200]}")
                return None
            return r.json()
        except Exception as e:
            log(f"  LLM 連線失敗：{e}")
            time.sleep(5)
    return None


def claude_event(prompt):
    """Claude + 內建 web search 工具（需在 Anthropic Console 啟用網路搜尋，按次計費）"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("--llm claude 需要設定環境變數 ANTHROPIC_API_KEY")
    j = _post("https://api.anthropic.com/v1/messages",
              {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
              {"model": os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001"), "max_tokens": 1500,
               "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
               "messages": [{"role": "user", "content": prompt}]})
    if not j:
        return None
    text, cites = "", []
    for blk in j.get("content", []):
        if blk.get("type") == "text":
            text += blk.get("text", "")
            cites += [c for c in blk.get("citations") or [] if c.get("url")]
    obj = _parse_json(text)
    if obj and not obj.get("url") and cites:
        obj["url"] = cites[0]["url"]
    return obj


def gemini_event(prompt):
    """Gemini + Google 搜尋接地（Google AI Studio 有免費額度，超過才計費）"""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("--llm gemini 需要設定環境變數 GEMINI_API_KEY（到 aistudio.google.com 申請）")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    j = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
              {"x-goog-api-key": key, "content-type": "application/json"},
              {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]})
    if not j or not j.get("candidates"):
        return None
    cand = j["candidates"][0]
    text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
    obj = _parse_json(text)
    chunks = [c.get("web", {}) for c in cand.get("groundingMetadata", {}).get("groundingChunks", [])]
    if obj and chunks:
        # 模型自己寫的網址可能是編的，改用搜尋接地實際引用的連結（Google 轉址連結）
        obj["url"] = chunks[0].get("uri") or obj.get("url", "")
        obj["source"] = obj.get("source") or chunks[0].get("title", "")
    return obj


def attach_event(row, d, prev_d, args, latest):
    t, name = row["ticker"], row["name"]
    win_start, win_end = event_window(d, prev_d)
    tags = []
    earn = False
    if not args.no_earnings:
        eds = earnings_dates(t)
        earn = d in eds or (prev_d is not None and prev_d in eds)
        if earn:
            tags.append("財報")
    if not args.no_sec:
        for z in sec_tags(t, win_start, win_end):
            if z not in tags:
                tags.append(z)
    row["earn"], row["tags"] = earn, ";".join(tags)

    found = None
    if not args.no_news:
        found = (cnbc_lookup(t, name, d, prev_d)
                 or why_article(t, name, d, prev_d)
                 or (yahoo_news(t, name, win_start, win_end) if latest else None)
                 or general_news(t, name, d, prev_d))

    res = None
    if args.llm != "none" and row["rank"] <= args.llm_top:
        hints = [{"title": found["event"], "src": found["src"]}] if found else []
        res = claude_event(_llm_prompt(row, earn, hints, tags)) if args.llm == "claude" \
            else gemini_event(_llm_prompt(row, earn, hints, tags))
        time.sleep(args.llm_delay)

    if res:
        event, src, url = res["event"], res.get("source", ""), res.get("url", "")
    elif found:
        event, src, url = found["event"], found["src"], found["url"]
    else:
        event, src, url = ("查無明確個股消息" if not args.no_news else ""), "", ""
    row["event"], row["src"], row["url"] = event, src or "", url or ""
    return row


# ─────────────────────────── 儲存 / 頁面 ───────────────────────────
MANUAL_HEADER = "date,ticker,event,src,url\n"


def load_manual_events(path: Path) -> dict:
    """讀 data/events_manual.csv（你用 Claude / Gemini 對話整理後貼進來的事件）。
    容錯：可重複貼上標題列、可夾雜 ``` 程式碼框、後面的同日同代號會覆蓋前面的。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(MANUAL_HEADER, encoding="utf-8")
        return {}
    out = {}
    text = path.read_text(encoding="utf-8-sig")
    for cells in csv.reader(l for l in text.splitlines() if l.strip() and not l.strip().startswith("```")):
        cells = [c.strip() for c in cells]
        if len(cells) < 3 or cells[0].lower() == "date":
            continue
        d = cells[0].replace("/", "-")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) or not cells[2]:
            continue
        out[(d, cells[1].upper())] = (cells[2], cells[3] if len(cells) > 3 else "",
                                      cells[4] if len(cells) > 4 else "")
    return out


def apply_manual_events(hist: pd.DataFrame, path: Path) -> pd.DataFrame:
    manual = load_manual_events(path)
    if not manual or hist.empty:
        return hist
    hist = hist.copy()
    n = 0
    for i, r in hist.iterrows():
        m = manual.get((r["date"], str(r["ticker"]).upper()))
        if not m:
            continue
        ev = m[0]
        hist.at[i, "event"], hist.at[i, "src"], hist.at[i, "url"] = ev, m[1], m[2]
        n += 1
    log(f"已套用手動事件 {n} 筆（data/events_manual.csv）")
    return hist


def load_history(path: Path) -> pd.DataFrame:
    if path.exists():
        h = pd.read_csv(path, dtype={"date": str}, keep_default_na=False, na_values=[""])
        if "group" not in h.columns:
            h.insert(1, "group", "大型股")
        for c in HIST_COLS:
            if c not in h.columns:
                h[c] = ""
        h["event"] = h["event"].fillna("").astype(str).str.replace("^【財報】", "", regex=True)
        return h[HIST_COLS]
    return pd.DataFrame(columns=HIST_COLS)


NUM_COLS = ["mcap", "price", "ret", "rel", "spy", "vr", "r1m", "r3m", "r6m", "ytd", "r1y"]


def pack(df: pd.DataFrame) -> dict:
    """欄列式 JSON（比逐筆物件小很多，手機載入較快）"""
    d = df[HIST_COLS].copy()
    d["earn"] = d["earn"].astype(str).str.lower().isin(["true", "1"])
    for c in NUM_COLS:
        d[c] = pd.to_numeric(d[c], errors="coerce").round(2 if c in ("mcap", "price") else 5)
    d["rank"] = pd.to_numeric(d["rank"], errors="coerce").astype("Int64")
    d = d.astype(object).where(d.notna(), None)
    return {"cols": HIST_COLS, "rows": d.values.tolist()}


def manifest(hist: pd.DataFrame, group_order) -> dict:
    groups = [g for g in group_order if g in set(hist["group"])]
    groups += sorted(set(hist["group"]) - set(groups))
    dates = sorted(hist["date"].unique(), reverse=True)
    # 所有細分類與大分類的上榜次數（給網站「找細分類」提示用）
    themes, cats = {}, {}
    for c1, c2 in zip(hist.get("cat1", []), hist.get("cat2", [])):
        for t in str(c2 if isinstance(c2, str) else "").split("、"):
            if t:
                themes[t] = themes.get(t, 0) + 1
        if isinstance(c1, str) and c1:
            cats[c1] = cats.get(c1, 0) + 1
    for c2 in hist.get("cur2", []):           # 現在才有的分類也列進提示
        for t in str(c2 if isinstance(c2, str) else "").split("、"):
            if t:
                themes.setdefault(t, 0)
    return {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "dates": dates,
            "groups": groups or list(group_order), "years": sorted({d[:4] for d in dates}, reverse=True),
            "themes": dict(sorted(themes.items(), key=lambda x: -x[1])),
            "cats": dict(sorted(cats.items(), key=lambda x: -x[1])), "aliases": ALIASES}


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def build_outputs(hist: pd.DataFrame, out_dir: Path, site_dir: Path, group_order):
    tpl = Path(__file__).with_name("dashboard_template.html").read_text(encoding="utf-8")
    man = manifest(hist, group_order)

    # 1) 本機單檔版：資料直接嵌入，雙擊就能開
    inline = _dump({"manifest": man, "pack": pack(hist)}).replace("</", "<\\/")
    (out_dir / "dashboard.html").write_text(tpl.replace("/*__DATA__*/null", inline), encoding="utf-8")

    # 2) 網站版：index.html + 依年份切分的 JSON，手機只載入需要的年份
    data = site_dir / "data"
    data.mkdir(parents=True, exist_ok=True)
    for f in data.glob("*.json"):
        f.unlink()
    for y, g in hist.groupby(hist["date"].str[:4]):
        (data / f"{y}.json").write_text(_dump(pack(g)), encoding="utf-8")
    (data / "manifest.json").write_text(_dump(man), encoding="utf-8")
    hist.to_csv(data / "movers_history.csv", index=False, encoding="utf-8-sig")
    (site_dir / "index.html").write_text(tpl, encoding="utf-8")
    (site_dir / ".nojekyll").write_text("", encoding="utf-8")


def merge_from(args):
    """遠端在這次執行期間有新提交時使用：以遠端資料為底，只換上這次重算的那些日期。"""
    src = Path(args.merge_from)
    out_dir = Path(args.out_dir); data_dir = out_dir / "data"
    hist_path = data_dir / "movers_history.csv"
    dates = json.loads((src / "last_run.json").read_text(encoding="utf-8"))["dates"]
    ours = load_history(src / "movers_history.csv")
    remote = load_history(hist_path)
    hist = pd.concat([remote[~remote["date"].isin(dates)], ours[ours["date"].isin(dates)]], ignore_index=True)
    hist = hist.sort_values(["date", "group", "side", "rank"], ascending=[False, False, False, True])[HIST_COLS]
    # 公司產業資料：兩邊聯集
    if (src / "profiles.csv").exists():
        prof = pd.concat([load_profiles(data_dir / "profiles.csv"), load_profiles(src / "profiles.csv")])
        prof.drop_duplicates("ticker", keep="first").to_csv(data_dir / "profiles.csv", index=False, encoding="utf-8-sig")
    hist = apply_manual_events(hist, data_dir / "events_manual.csv")
    uc = data_dir / "universe.csv"
    hist = apply_categories(hist, data_dir, pd.read_csv(uc) if uc.exists() else None)
    hist.to_csv(hist_path, index=False, encoding="utf-8-sig")
    (data_dir / "last_run.json").write_text(json.dumps({"dates": dates}), encoding="utf-8")
    build_outputs(hist, out_dir, Path(args.site_dir), [g for g, _, _ in args.group_list])
    log(f"已合併：遠端最新資料 + 這次重算的 {len(dates)} 個交易日")


def main():
    ap = argparse.ArgumentParser(description="美股每日重大個股漲跌幅捕捉器")
    ap.add_argument("--date", help="單一日期 YYYY-MM-DD")
    ap.add_argument("--start", help="回溯起日 YYYY-MM-DD")
    ap.add_argument("--end", help="回溯迄日 YYYY-MM-DD（預設今天）")
    ap.add_argument("--top", type=int, default=25, help="每組漲、跌各取前幾名（預設 25）")
    ap.add_argument("--groups", default="大型股:10000-,中型股:2000-10000",
                    help="市值分組，百萬美元，格式 名稱:下限-上限（上限留空=無上限）。預設 >100億 與 20~100億")
    ap.add_argument("--min-price", type=float, default=5, help="最低股價（預設 5 美元）")
    ap.add_argument("--min-move", type=float, default=0, help="最低漲跌幅 %%，例如 3 代表只收 ±3%% 以上")
    ap.add_argument("--include-day", action="store_true",
                    help="1M/3M/6M/YTD/1Y 報酬包含當天（預設算到前一天收盤，方便看事件前的走勢）")
    ap.add_argument("--no-news", action="store_true", help="不抓 Google News 標題")
    ap.add_argument("--llm", choices=["none", "claude", "gemini"], default="none",
                    help="用 AI 上網搜尋並撰寫中文事件說明（claude 需 ANTHROPIC_API_KEY，gemini 需 GEMINI_API_KEY）")
    ap.add_argument("--llm-top", type=int, default=25, help="每組每榜只有前幾名用 AI 查（省錢/省額度），其餘用新聞標題")
    ap.add_argument("--llm-delay", type=float, default=1.0, help="每次 AI 查詢間隔秒數（Gemini 免費版建議 6 以上）")
    ap.add_argument("--no-earnings", action="store_true", help="不查財報日")
    ap.add_argument("--no-sec", action="store_true", help="不查 SEC 8-K 公告標籤")
    ap.add_argument("--profile-limit", type=int, default=400, help="每次最多抓幾檔公司產業資料（有快取，只抓新出現的）")
    ap.add_argument("--refresh-news", action="store_true", help="已有紀錄的日期也重抓新聞")
    ap.add_argument("--refresh-universe", action="store_true", help="強制更新股票池（預設快取 7 天）")
    ap.add_argument("--news-delay", type=float, default=0.6, help="每次新聞查詢間隔秒數")
    ap.add_argument("--min-coverage", type=float, default=0.9, help="最新一天至少要有多少比例的股票有報價（預設 0.9）")
    ap.add_argument("--debug", default="", help="逗號分隔的代號，印出它們為什麼有/沒有上榜，例如 --debug FORM,SPY")
    ap.add_argument("--out-dir", default=".", help="輸出資料夾（data/ 歷史與本機 dashboard.html）")
    ap.add_argument("--site-dir", default="site", help="網站版輸出資料夾（部署到 GitHub/GitLab Pages）")
    ap.add_argument("--merge-from", default="", help="（CI 用）把這次執行的結果合併進遠端最新的歷史資料，避免推送衝突")
    ap.add_argument("--rebuild", action="store_true", help="不抓資料，只用現有歷史重建網站與 dashboard")
    args = ap.parse_args()
    args.group_list = []
    for part in args.groups.split(","):
        name, rng = part.split(":")
        lo, hi = rng.split("-")
        args.group_list.append((name.strip(), float(lo), float(hi) if hi.strip() else float("inf")))
    min_mcap = min(lo for _, lo, _ in args.group_list)
    global NEWS_DELAY
    NEWS_DELAY = args.news_delay
    if args.merge_from:
        return merge_from(args)
    if args.rebuild:
        out_dir = Path(args.out_dir)
        hist_path = out_dir / "data" / "movers_history.csv"
        hist = apply_manual_events(load_history(hist_path), out_dir / "data" / "events_manual.csv")
        uc = out_dir / "data" / "universe.csv"
        hist = apply_categories(hist, out_dir / "data", pd.read_csv(uc) if uc.exists() else None)
        hist.to_csv(hist_path, index=False, encoding="utf-8-sig")
        build_outputs(hist, out_dir, Path(args.site_dir), [g for g, _, _ in args.group_list])
        return log(f"已重建 dashboard.html 與 {args.site_dir}/")
    if yf is None:
        sys.exit("請先安裝：pip install yfinance pandas requests lxml")

    out_dir = Path(args.out_dir); data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    hist_path = data_dir / "movers_history.csv"

    now_ny = pd.Timestamp.now(tz="America/New_York")
    today = now_ny.normalize().tz_localize(None)
    if args.start:
        start, end = pd.Timestamp(args.start), pd.Timestamp(args.end) if args.end else today
    elif args.date:
        start = end = pd.Timestamp(args.date)
    else:
        start = end = None

    univ = load_universe(data_dir, args.refresh_universe)
    cand = univ
    if univ["mcap_now"].notna().any():  # 先粗篩，保留現在市值在門檻 1/3 以上者（回溯時當年可能更大）
        cand = univ[univ["mcap_now"] >= min_mcap / 3]
    tickers = sorted(set(cand["ticker"])) + [BENCH]
    dl_start = (start if start is not None else today) - timedelta(days=420)
    dl_end = (end if end is not None else today) + timedelta(days=1)
    log(f"下載 {len(tickers)} 檔股價 {dl_start:%Y-%m-%d} ~ {dl_end:%Y-%m-%d}")
    adj, close, vol = download_prices(tickers, dl_start, dl_end)
    idx = adj.index

    if start is None:
        last = idx[-1]
        if last == today and now_ny.hour * 60 + now_ny.minute < 16 * 60 + 30:
            last = idx[-2]  # 今天還沒收盤，用前一個交易日
        days = [last]
        if last < today and today.weekday() < 5 and now_ny.hour >= 18:
            log(f"⚠ 最新完整的交易日是 {last:%Y-%m-%d}，Yahoo 可能還沒發布 {today:%Y-%m-%d} 的完整資料；下次執行會自動補上")
        # 自動補漏：如果之前有排程沒跑到，把歷史最後一天之後漏掉的交易日一起補上（最多 10 天）
        if hist_path.exists():
            try:
                done = pd.read_csv(hist_path, usecols=["date"], dtype=str)["date"]
                last_done = pd.Timestamp(done.max()) if len(done) else None
            except Exception:
                last_done = None
            if last_done is not None:
                gap = [d for d in idx if last_done < d < last][-10:]
                if gap:
                    log(f"發現漏掉的交易日 {len(gap)} 天：{', '.join(f'{d:%m/%d}' for d in gap)}，一起補上")
                days = gap + days
    else:
        days = list(idx[(idx >= start) & (idx <= end)])
    if not days:
        sys.exit("指定期間沒有交易日資料")

    # 資料完整度檢查：最新一天有報價的比例太低就不更新，避免發布錯誤的排行
    chk = days[-1]
    have = int(adj.loc[chk].notna().sum())
    ratio = have / max(1, len(tickers))
    log(f"{chk:%Y-%m-%d} 有報價 {have}/{len(tickers)} 檔（{ratio:.0%}），SPY "
        f"{'有' if BENCH in adj.columns and pd.notna(adj.loc[chk].get(BENCH)) else '缺'}")
    if BENCH not in adj.columns or pd.isna(adj.loc[chk].get(BENCH)):
        sys.exit("SPY 報價缺漏，無法計算相對報酬；這次不更新網站，請稍後重跑")
    if ratio < args.min_coverage:
        sys.exit(f"報價完整度只有 {ratio:.0%}（低於 {args.min_coverage:.0%}），可能被 Yahoo 限流；這次不更新網站，請稍後重跑")

    hist = load_history(hist_path)
    old_events, old_cats = {}, {}
    if len(hist):
        for _, r in hist.iterrows():
            old_events[(r["date"], r["ticker"])] = (r.get("earn"), r.get("tags"), r.get("event"), r.get("src"), r.get("url"))
            old_cats[(r["date"], r["ticker"])] = (r.get("cat1"), r.get("cat2"), r.get("cat_src"))

    new_rows = []
    for d in days:
        rows = compute_day(d, adj, close, vol, univ, args) or []
        if args.debug:
            diagnose([x.strip().upper() for x in args.debug.split(",") if x.strip()], d, adj, close, univ, args, rows)
        for g, _, _ in args.group_list:
            for side in ("up", "down"):
                top = [f"{r['ticker']} {r['ret'] * 100:+.1f}%" for r in rows if r["group"] == g and r["side"] == side][:5]
                log(f"  {g} {'漲' if side == 'up' else '跌'}前5：{', '.join(top)}")
        i = idx.get_loc(d); prev_d = idx[i - 1] if i > 0 else None
        latest = (today - d).days <= 4
        for n, row in enumerate(rows, 1):
            key = (row["date"], row["ticker"])
            old = old_events.get(key)
            ev = old[2] if old else ""
            if old and not args.refresh_news and isinstance(ev, str) and ev and ev != "查無明確個股消息":
                row["earn"], row["tags"], row["event"], row["src"], row["url"] = old
            else:
                attach_event(row, d, prev_d, args, latest)
            if key in old_cats:   # 重算同一天時，保留當時的產業分類
                row["cat1"], row["cat2"], row["cat_src"] = old_cats[key]
            new_rows.append(row)
        cnt = "  ".join(f"{g} 漲{sum(r['group'] == g and r['side'] == 'up' for r in rows)}/跌"
                        f"{sum(r['group'] == g and r['side'] == 'down' for r in rows)}" for g, _, _ in args.group_list)
        log(f"{d:%Y-%m-%d}  {cnt}  SPY {rows[0]['spy'] * 100:+.2f}%" if rows else f"{d:%Y-%m-%d}  無資料")

    done_dates = {d.strftime("%Y-%m-%d") for d in days}
    (data_dir / "last_run.json").write_text(json.dumps({"dates": sorted(done_dates)}), encoding="utf-8")
    hist = hist[~hist["date"].isin(done_dates)]
    hist = pd.concat([hist, pd.DataFrame(new_rows, columns=HIST_COLS)], ignore_index=True)
    hist = hist.sort_values(["date", "group", "side", "rank"], ascending=[False, False, False, True])[HIST_COLS]
    num = ["ret", "rel", "spy", "vr", "r1m", "r3m", "r6m", "ytd", "r1y"]
    hist[num] = hist[num].apply(pd.to_numeric, errors="coerce").round(6)
    hist[["mcap", "price"]] = hist[["mcap", "price"]].apply(pd.to_numeric, errors="coerce").round(2)
    hist = apply_manual_events(hist, data_dir / "events_manual.csv")
    ensure_profiles(sorted(set(hist["ticker"])), data_dir / "profiles.csv", args.profile_limit)
    hist = apply_categories(hist, data_dir, univ)
    hist.to_csv(hist_path, index=False, encoding="utf-8-sig")
    build_outputs(hist, out_dir, Path(args.site_dir), [g for g, _, _ in args.group_list])
    log(f"完成：{hist_path}（{hist['date'].nunique()} 個交易日）、{out_dir / 'dashboard.html'}、{args.site_dir}/")


if __name__ == "__main__":
    main()
