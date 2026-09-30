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

輸出
  data/movers_history.csv   所有日期的累積紀錄（UTF-8 BOM，Excel 可直接開）
  dashboard.html            單檔互動頁：選日期、切漲/跌、點欄位排序、搜尋代號看歷史紀錄
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
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
HIST_COLS = ["date", "group", "side", "rank", "ticker", "name", "sector", "mcap", "price", "ret", "rel",
             "spy", "vr", "r1m", "r3m", "r6m", "ytd", "r1y", "earn", "event", "src", "url"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


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
    if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < 7 * 86400:
        return pd.read_csv(cache)

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
        out = df[["ticker", "name", "sector", "shares", "mcap_now"]]
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
        out["shares"] = np.nan  # 沒有股數時無法估歷史市值，市值門檻會略過
        out["mcap_now"] = np.nan
    data_dir.mkdir(parents=True, exist_ok=True)
    out.to_csv(cache, index=False)
    return out


# ─────────────────────────── 股價 ───────────────────────────
def download_prices(tickers, start, end, chunk=150):
    adj, close, vol = [], [], []
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(part, start=start, end=end, auto_adjust=False, actions=False,
                                 progress=False, threads=True, group_by="column")
                break
            except Exception as e:
                log(f"  下載失敗重試 {attempt + 1}/3：{e}")
                time.sleep(5 * (attempt + 1))
        if df is None or df.empty:
            continue
        if not isinstance(df.columns, pd.MultiIndex):
            df.columns = pd.MultiIndex.from_product([df.columns, part])
        adj.append(df["Adj Close"]); close.append(df["Close"]); vol.append(df["Volume"])
        log(f"  股價 {min(i + chunk, len(tickers))}/{len(tickers)}")
    fix = lambda fs: (pd.concat(fs, axis=1).loc[:, lambda d: ~d.columns.duplicated()]
                      .sort_index().pipe(lambda d: d.set_axis(pd.to_datetime(d.index).tz_localize(None))))
    return fix(adj), fix(close), fix(vol)


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


# ─────────────────────────── 事件 ───────────────────────────
_earn_cache: dict[str, set] = {}


def earnings_dates(t):
    if t not in _earn_cache:
        s = set()
        try:
            ed = yf.Ticker(t).get_earnings_dates(limit=40)
            if ed is not None and len(ed):
                ix = pd.to_datetime(ed.index)
                if ix.tz is not None:
                    ix = ix.tz_convert("America/New_York").tz_localize(None)
                s = set(ix.normalize())
        except Exception:
            pass
        _earn_cache[t] = s
    return _earn_cache[t]


def google_news(ticker, name, d, n=3):
    after = (d - timedelta(days=1)).strftime("%Y-%m-%d")
    before = (d + timedelta(days=2)).strftime("%Y-%m-%d")
    q = f'("{short_name(name)}" OR "{ticker}") stock after:{after} before:{before}'
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    try:
        r = requests.get(url, headers=UA, timeout=20)
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


def _llm_prompt(row, earn, news):
    heads = "\n".join(f"- {h['title']} ({h['src']})" for h in news) or "（無）"
    move = "上漲" if row["ret"] > 0 else "下跌"
    return (
        f"美股 {row['name']}（代號 {row['ticker']}）在 {row['date']}（美東時間）單日{move} {abs(row['ret']) * 100:.2f}%，"
        f"同日 S&P 500 (SPY) {row['spy'] * 100:+.2f}%。{'這天或前一個交易日有公布財報。' if earn else ''}\n"
        f"Google News 找到的參考標題：\n{heads}\n\n"
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


def attach_event(row, d, prev_d, args):
    earn = False
    if not args.no_earnings:
        eds = earnings_dates(row["ticker"])
        earn = d in eds or (prev_d is not None and prev_d in eds)
    row["earn"] = earn
    news = [] if args.no_news else google_news(row["ticker"], row["name"], d)
    if not args.no_news:
        time.sleep(args.news_delay)

    res = None
    if args.llm != "none" and row["rank"] <= args.llm_top:
        prompt = _llm_prompt(row, earn, news)
        res = claude_event(prompt) if args.llm == "claude" else gemini_event(prompt)
        time.sleep(args.llm_delay)

    if res:
        event, src, url = res["event"], res.get("source", ""), res.get("url", "")
    elif news:
        event, src, url = news[0]["title"], news[0]["src"], news[0]["url"]
    else:
        event, src, url = ("查無明確個股消息" if not args.no_news else ""), "", ""
    if earn and not event.startswith("【財報】"):
        event = "【財報】" + event
    row["event"], row["src"], row["url"] = event, src or "", url or ""
    return row


# ─────────────────────────── 儲存 / 頁面 ───────────────────────────
def load_history(path: Path) -> pd.DataFrame:
    if path.exists():
        h = pd.read_csv(path, dtype={"date": str}, keep_default_na=False, na_values=[""])
        if "group" not in h.columns:
            h.insert(1, "group", "大型股")
        return h
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
    return {"updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "dates": dates,
            "groups": groups or list(group_order), "years": sorted({d[:4] for d in dates}, reverse=True)}


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
    ap.add_argument("--refresh-news", action="store_true", help="已有紀錄的日期也重抓新聞")
    ap.add_argument("--refresh-universe", action="store_true", help="強制更新股票池（預設快取 7 天）")
    ap.add_argument("--news-delay", type=float, default=0.6, help="每次新聞查詢間隔秒數")
    ap.add_argument("--out-dir", default=".", help="輸出資料夾（data/ 歷史與本機 dashboard.html）")
    ap.add_argument("--site-dir", default="site", help="網站版輸出資料夾（部署到 GitHub/GitLab Pages）")
    ap.add_argument("--rebuild", action="store_true", help="不抓資料，只用現有歷史重建網站與 dashboard")
    args = ap.parse_args()
    args.group_list = []
    for part in args.groups.split(","):
        name, rng = part.split(":")
        lo, hi = rng.split("-")
        args.group_list.append((name.strip(), float(lo), float(hi) if hi.strip() else float("inf")))
    min_mcap = min(lo for _, lo, _ in args.group_list)
    if args.rebuild:
        out_dir = Path(args.out_dir)
        hist = load_history(out_dir / "data" / "movers_history.csv")
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
    else:
        days = list(idx[(idx >= start) & (idx <= end)])
    if not days:
        sys.exit("指定期間沒有交易日資料")

    hist = load_history(hist_path)
    old_events = {}
    if len(hist):
        for _, r in hist.iterrows():
            old_events[(r["date"], r["ticker"])] = (r.get("earn"), r.get("event"), r.get("src"), r.get("url"))

    new_rows = []
    for d in days:
        rows = compute_day(d, adj, close, vol, univ, args) or []
        i = idx.get_loc(d); prev_d = idx[i - 1] if i > 0 else None
        for n, row in enumerate(rows, 1):
            key = (row["date"], row["ticker"])
            old = old_events.get(key)
            if old and not args.refresh_news and isinstance(old[1], str) and old[1]:
                row["earn"], row["event"], row["src"], row["url"] = old
            else:
                attach_event(row, d, prev_d, args)
            new_rows.append(row)
        cnt = "  ".join(f"{g} 漲{sum(r['group'] == g and r['side'] == 'up' for r in rows)}/跌"
                        f"{sum(r['group'] == g and r['side'] == 'down' for r in rows)}" for g, _, _ in args.group_list)
        log(f"{d:%Y-%m-%d}  {cnt}  SPY {rows[0]['spy'] * 100:+.2f}%" if rows else f"{d:%Y-%m-%d}  無資料")

    done_dates = {d.strftime("%Y-%m-%d") for d in days}
    hist = hist[~hist["date"].isin(done_dates)]
    hist = pd.concat([hist, pd.DataFrame(new_rows, columns=HIST_COLS)], ignore_index=True)
    hist = hist.sort_values(["date", "group", "side", "rank"], ascending=[False, False, False, True])[HIST_COLS]
    num = ["ret", "rel", "spy", "vr", "r1m", "r3m", "r6m", "ytd", "r1y"]
    hist[num] = hist[num].apply(pd.to_numeric, errors="coerce").round(6)
    hist[["mcap", "price"]] = hist[["mcap", "price"]].apply(pd.to_numeric, errors="coerce").round(2)
    hist.to_csv(hist_path, index=False, encoding="utf-8-sig")
    build_outputs(hist, out_dir, Path(args.site_dir), [g for g, _, _ in args.group_list])
    log(f"完成：{hist_path}（{hist['date'].nunique()} 個交易日）、{out_dir / 'dashboard.html'}、{args.site_dir}/")


if __name__ == "__main__":
    main()
