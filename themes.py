# -*- coding: utf-8 -*-
"""
產業分類：大分類 + 細分類（當紅主題）

判斷順序（前面的優先）
  1. data/themes_manual.csv   你自己指定的（ticker,大分類,細分類），永遠最優先
  2. THEME_TICKERS            下方手動整理的熱門主題名單
  3. KEYWORD_THEMES           用公司業務描述的關鍵字判斷主題（例如 optical → 光通訊）
  4. INDUSTRY_RULES           用 Yahoo 產業別翻成中文
  5. 都沒有 → 用 Nasdaq 的產業大類

想新增或修正主題，直接改下方 _T，或在 data/themes_manual.csv 加一行即可。
細分類可以同時標多個（例如 MRVL：客製化 ASIC、網通晶片、光通訊）。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

# ───────────── 1. 熱門主題名單 ─────────────
# 格式：(細分類, 大分類): "代號 代號 ..."
# 同一檔可以出現在多行 → 會同時標出多個細分類；大分類以第一次出現的為準。
_T = [
    (("記憶體", "半導體"), "MU SNDK"),
    (("硬碟/儲存", "硬體與網通"), "WDC STX"),
    (("光通訊", "硬體與網通"), "COHR LITE AAOI FN CIEN POET IPGP NOK"),
    (("高速傳輸/互連", "半導體"), "CRDO ALAB SITM"),
    (("AI 晶片/GPU", "半導體"), "NVDA AMD"),
    (("客製化 ASIC", "半導體"), "AVGO MRVL"),
    (("晶圓代工", "半導體"), "TSM GFS UMC"),
    (("CPU", "半導體"), "INTC AMD ARM"),
    (("晶圓代工", "半導體"), "INTC"),
    (("先進封裝", "半導體"), "TSM AMKR"),
    (("半導體設備", "半導體"), "AMAT LRCX KLAC ASML TER ONTO CAMT NVMI ACMR AEHR FORM COHU KLIC ACLS VECO UCTT ICHR ENTG MKSI"),
    (("類比/電源 IC", "半導體"), "TXN ADI MCHP ON NXPI MPWR STM"),
    (("RF 射頻", "半導體"), "QCOM SWKS QRVO"),
    (("碳化矽/化合物半導體", "半導體"), "WOLF"),
    (("矽智財/EDA", "半導體"), "ARM SNPS CDNS"),
    (("網通晶片", "半導體"), "AVGO MRVL"),
    (("光通訊", "半導體"), "MRVL CRDO AVGO"),
    (("AI 伺服器", "硬體與網通"), "SMCI DELL HPE CLS"),
    (("網通交換器", "硬體與網通"), "ANET CSCO CLS"),
    (("資料中心電力/散熱", "工業"), "VRT ETN GEV NVT MOD POWL"),
    (("核能", "能源"), "OKLO SMR NNE BWXT LEU CCJ UEC"),
    (("鈾礦", "能源"), "CCJ UEC LEU"),
    (("核能", "公用事業"), "CEG VST TLN"),
    (("電力", "公用事業"), "CEG VST TLN"),
    (("量子運算", "硬體與網通"), "IONQ RGTI QBTS QUBT"),
    (("資安", "軟體"), "CRWD PANW ZS FTNT OKTA S CYBR SAIL TENB RPD QLYS VRNS CHKP NET AKAM"),
    (("邊緣雲/CDN", "軟體"), "NET AKAM FSLY"),
    (("資料儲存/備份", "軟體"), "RBRK NTNX PSTG NTAP"),
    (("AI 軟體/數據分析", "軟體"), "PLTR AI SNOW ESTC"),
    (("國防科技", "軟體"), "PLTR"),
    (("可觀測性/資料庫", "軟體"), "DDOG MDB DT"),
    (("雲端 SaaS", "軟體"), "CRM NOW WDAY VEEV TEAM HUBS ADBE INTU ADSK"),
    (("AI 雲端/Neocloud", "軟體"), "CRWV NBIS"),
    (("AI 算力/資料中心", "金融"), "IREN CORZ CIFR WULF HUT APLD"),
    (("比特幣/加密", "金融"), "COIN MSTR MARA RIOT CLSK GLXY BLSH IREN CORZ CIFR WULF HUT HOOD"),
    (("網路券商", "金融"), "HOOD IBKR SCHW"),
    (("太空/衛星", "航太國防"), "RKLB ASTS LUNR PL IRDM FLY"),
    (("國防科技", "航太國防"), "RKLB KTOS AVAV"),
    (("無人機", "航太國防"), "KTOS AVAV"),
    (("電動車", "汽車"), "TSLA RIVN LCID"),
    (("機器人", "汽車"), "TSLA"),
    (("GLP-1/減重", "醫藥生技"), "LLY NVO VKTX"),
    (("手術機器人", "醫療器材與服務"), "ISRG"),
]
THEME_TICKERS: dict[str, tuple[str, list[str]]] = {}
for (_cat2, _cat1), _s in _T:
    for _t in _s.split():
        c1, lst = THEME_TICKERS.setdefault(_t, (_cat1, []))
        if _cat2 not in lst:
            lst.append(_cat2)

SEP = "、"          # 多個細分類之間的分隔符號
_SPLIT = re.compile(r"[、;；|｜,，]+")


# ───────────── 2. 業務描述關鍵字 → 主題（限定大分類才套用，避免誤判）─────────────
KEYWORD_THEMES = [
    ("記憶體", {"半導體", "硬體與網通"}, r"\bdram\b|\bnand\b|high[- ]bandwidth memory|\bhbm\b|flash memory"),
    ("光通訊", {"半導體", "硬體與網通"}, r"optical (network|communication|transceiver|interconnect|component)|photonic|transceiver"),
    ("量子運算", None, r"quantum comput"),
    ("核能", {"能源", "公用事業", "工業"}, r"nuclear|small modular reactor|uranium"),
    ("資安", {"軟體", "硬體與網通"}, r"cybersecurity|identity security|endpoint (protection|security)|zero[- ]trust"),
    ("比特幣/加密", {"金融", "軟體", "其他"}, r"bitcoin|cryptocurrenc|digital asset"),
    ("太空/衛星", {"航太國防", "工業", "硬體與網通", "電信"}, r"satellite|launch vehicle|spacecraft|space systems"),
    ("無人機/國防科技", {"航太國防", "工業"}, r"unmanned|drone"),
    ("GLP-1/減重", {"醫藥生技"}, r"obesity|glp-1"),
    ("機器人", {"工業", "醫療器材與服務", "軟體"}, r"robot"),
    ("儲能/電池", {"工業", "能源", "汽車"}, r"energy storage|lithium|battery"),
    ("太陽能", {"能源", "半導體", "工業"}, r"\bsolar\b"),
    ("資料中心", {"房地產", "工業", "軟體", "硬體與網通"}, r"data cent(er|re)"),
    ("AI", {"軟體", "硬體與網通", "半導體"}, r"artificial intelligence|\bai\b"),
]
GENERIC = {"資料中心", "AI"}   # 太籠統，只有沒有其他主題時才標
_KW = [(n, allow, re.compile(p, re.I)) for n, allow, p in KEYWORD_THEMES]

# ───────────── 3. Yahoo 產業別 → (大分類, 細分類) ─────────────
INDUSTRY_RULES = [
    # Nasdaq 產業別（Yahoo 抓不到時的備援）
    (r"prepackaged software|edp services|computer software", "軟體", "軟體服務"),
    (r"computer communications equipment|telecommunications equipment", "硬體與網通", "通訊設備"),
    (r"computer manufacturing|computer peripheral", "硬體與網通", "電腦硬體"),
    (r"major pharmaceuticals|pharmaceutical preparations", "醫藥生技", "製藥"),
    (r"medical/dental instruments|medical specialities|medical electronics", "醫療器材與服務", "醫療器材"),
    (r"major banks|savings institutions|commercial banks", "金融", "銀行"),
    (r"investment bankers|brokers", "金融", "券商/投資銀行"),
    (r"real estate investment trust", "房地產", "REIT"),
    (r"military/government", "航太國防", "航太國防"),
    (r"auto manufacturing|motor vehicles", "汽車", "汽車製造"),
    (r"electric utilities|power generation", "公用事業", "電力/公用事業"),
    (r"oil & gas production|integrated oil|oil refining", "能源", "石油天然氣"),
    (r"semiconductor equipment", "半導體", "半導體設備"),
    (r"semiconductor", "半導體", "半導體"),
    (r"software - infrastructure", "軟體", "基礎架構軟體"),
    (r"software - application", "軟體", "應用軟體"),
    (r"information technology services", "軟體", "IT 服務"),
    (r"computer hardware", "硬體與網通", "電腦硬體"),
    (r"communication equipment", "硬體與網通", "通訊設備"),
    (r"electronic components", "硬體與網通", "電子零組件"),
    (r"scientific & technical instruments", "硬體與網通", "精密儀器"),
    (r"consumer electronics", "硬體與網通", "消費電子"),
    (r"electronics & computer distribution", "硬體與網通", "電子通路"),
    (r"\bsolar\b", "能源", "太陽能"),
    (r"internet retail", "零售", "電商"),
    (r"internet content", "網路與媒體", "網路平台"),
    (r"electronic gaming", "網路與媒體", "遊戲"),
    (r"entertainment", "網路與媒體", "娛樂影音"),
    (r"advertising", "網路與媒體", "廣告行銷"),
    (r"publishing|broadcasting", "網路與媒體", "媒體"),
    (r"telecom", "電信", "電信服務"),
    (r"biotechnology", "醫藥生技", "生技"),
    (r"drug manufacturers", "醫藥生技", "製藥"),
    (r"pharmaceutical retailers", "醫藥生技", "藥局"),
    (r"medical (devices|instruments)", "醫療器材與服務", "醫療器材"),
    (r"diagnostics", "醫療器材與服務", "診斷/研究服務"),
    (r"healthcare plans", "醫療器材與服務", "健康保險"),
    (r"medical care|medical distribution", "醫療器材與服務", "醫療服務"),
    (r"health information", "醫療器材與服務", "醫療資訊"),
    (r"banks", "金融", "銀行"),
    (r"capital markets", "金融", "券商/投資銀行"),
    (r"asset management", "金融", "資產管理"),
    (r"insurance", "金融", "保險"),
    (r"credit services", "金融", "支付/信貸"),
    (r"financial data|stock exchanges", "金融", "交易所/金融數據"),
    (r"aerospace", "航太國防", "航太國防"),
    (r"auto manufacturers", "汽車", "汽車製造"),
    (r"auto parts|auto & truck", "汽車", "汽車零件/經銷"),
    (r"airlines|airports", "運輸", "航空"),
    (r"trucking|railroads|freight|shipping|marine", "運輸", "物流運輸"),
    (r"uranium", "能源", "鈾礦"),
    (r"oil & gas|thermal coal", "能源", "石油天然氣"),
    (r"utilities", "公用事業", "電力/公用事業"),
    (r"reit", "房地產", "REIT"),
    (r"real estate", "房地產", "不動產"),
    (r"residential construction", "房地產", "住宅營建"),
    (r"gold|silver|precious metals", "原物料", "貴金屬"),
    (r"copper|steel|aluminum|industrial metals|coking coal", "原物料", "工業金屬"),
    (r"chemicals|agricultural inputs", "原物料", "化工"),
    (r"building materials|building products", "原物料", "建材"),
    (r"engineering & construction|infrastructure operations", "工業", "工程營建"),
    (r"industrial machinery|farm & heavy", "工業", "機械設備"),
    (r"electrical equipment", "工業", "電機設備"),
    (r"waste management|pollution", "工業", "環保"),
    (r"security & protection", "工業", "安全防護"),
    (r"staffing|consulting|business services|rental", "工業", "商業服務"),
    (r"conglomerates|industrial distribution|tools", "工業", "綜合工業"),
    (r"restaurants", "消費", "餐飲"),
    (r"apparel|footwear|luxury", "消費", "服飾精品"),
    (r"travel|lodging|resorts|casinos|gambling|leisure", "消費", "旅遊休閒"),
    (r"beverages|packaged foods|confectioners|tobacco|household|personal products|farm products", "消費", "必需消費品"),
    (r"education", "消費", "教育"),
    (r"retail|discount stores|department stores|grocery", "零售", "零售通路"),
    (r"packaging|furnishings|recreational vehicles", "消費", "消費用品"),
]
_IR = [(re.compile(p, re.I), c1, c2) for p, c1, c2 in INDUSTRY_RULES]

SECTOR_ZH = {
    # Yahoo
    "technology": "科技", "communication services": "網路與媒體", "consumer cyclical": "消費",
    "consumer defensive": "消費", "energy": "能源", "financial services": "金融", "healthcare": "醫藥生技",
    "industrials": "工業", "basic materials": "原物料", "real estate": "房地產", "utilities": "公用事業",
    # Nasdaq
    "telecommunications": "電信", "health care": "醫藥生技", "finance": "金融",
    "consumer discretionary": "消費", "consumer staples": "消費", "miscellaneous": "其他",
}


def keyword_theme(summary: str, cat1: str, limit: int = 3) -> str:
    """從業務描述找主題（可多個）；只有在允許的大分類內才套用。"""
    if not isinstance(summary, str) or not summary:
        return ""
    hits = [name for name, allow, rx in _KW if (allow is None or cat1 in allow) and rx.search(summary)]
    specific = [h for h in hits if h not in GENERIC]
    return SEP.join((specific or hits)[:limit])


def from_industry(industry: str, sector: str) -> tuple[str, str]:
    industry = industry if isinstance(industry, str) else ""
    sector = sector if isinstance(sector, str) else ""
    for rx, c1, c2 in _IR:
        if industry and rx.search(industry):
            return c1, c2
    c1 = SECTOR_ZH.get(sector.strip().lower(), "")
    return c1, industry


_DATE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
SRC_RANK = {"manual": 5, "curated": 4, "keyword": 3, "industry": 2, "nasdaq": 1, "": 0}


def load_manual_rules(path: Path) -> dict[str, list[tuple[str, str, str, str]]]:
    """themes_manual.csv：ticker,大分類,細分類,起日,迄日
    - 細分類可寫多個，用「、」或「;」分隔
    - 起日、迄日可省略；省略代表不限。同一檔可以寫多行，代表不同時期的分類：
        IREN,金融,比特幣/加密,,2025-12-31
        IREN,軟體,AI 算力/資料中心、比特幣/加密,2026-01-01,
    回傳 {ticker: [(起日, 迄日, 大分類, 細分類), ...]}"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ticker,大分類,細分類,起日,迄日\n", encoding="utf-8")
        return {}
    out: dict[str, list] = {}
    with path.open(encoding="utf-8-sig") as f:
        for cells in csv.reader(f):
            cells = [c.strip() for c in cells]
            if len(cells) < 2 or not cells[0] or cells[0].lower() == "ticker":
                continue
            themes, dates = [], []
            for i, c in enumerate(cells[2:], start=2):
                if i >= 3 and (c == "" or _DATE.match(c)):
                    dates.append(c.replace("/", "-"))
                else:
                    themes += [x.strip() for x in _SPLIT.split(c) if x.strip()]
            start = dates[0] if len(dates) > 0 and dates[0] else "0000-00-00"
            end = dates[1] if len(dates) > 1 and dates[1] else "9999-99-99"
            # 日期補零：2026-1-5 → 2026-01-05
            norm = lambda d: "-".join(x.zfill(2) for x in d.split("-")) if d[0] != "0" and d[0] != "9" else d
            out.setdefault(cells[0].upper(), []).append(
                (norm(start), norm(end), cells[1], SEP.join(dict.fromkeys(themes))))
    return out


def manual_at(rules: dict, ticker: str, date: str):
    """回傳該日期適用的手動分類 (大分類, 細分類)；多行都符合時以最後一行為準。"""
    hit = None
    for start, end, c1, c2 in rules.get(ticker.upper(), []):
        if start <= date <= end and (c1 or c2):
            hit = (c1, c2)
    return hit


def base_classify(ticker: str, industry: str = "", sector: str = "", kw_theme: str = "",
                  nasdaq_sector: str = "", from_nasdaq: bool = False) -> tuple[str, str, str]:
    """不含手動指定的自動分類，回傳 (大分類, 細分類, 來源)。"""
    t = ticker.upper()
    if t in THEME_TICKERS:
        c1, lst = THEME_TICKERS[t]
        return c1, SEP.join(lst), "curated"
    c1, c2 = from_industry(industry, sector or nasdaq_sector)
    src = "nasdaq" if from_nasdaq else ("industry" if industry else "")
    if kw_theme and isinstance(kw_theme, str):
        c2, src = kw_theme, "keyword"
    return c1 or "其他", c2, src


def load_manual_themes(path: Path) -> dict[str, tuple[str, str]]:
    """（相容舊版）不分時期的手動分類。"""
    rules = load_manual_rules(path)
    return {t: (r[-1][2], r[-1][3]) for t, r in rules.items() if r}


def classify(ticker: str, industry: str = "", sector: str = "", kw_theme: str = "",
             nasdaq_sector: str = "", manual: dict | None = None) -> tuple[str, str]:
    """（相容舊版）回傳 (大分類, 細分類)。"""
    t = ticker.upper()
    if manual and t in manual and any(manual[t]):
        c1, c2 = manual[t]
        return c1 or base_classify(t, industry, sector, kw_theme, nasdaq_sector)[0], c2
    c1, c2, _ = base_classify(t, industry, sector, kw_theme, nasdaq_sector)
    return c1, c2


# ───────────── 細分類的別名（網站「找細分類」搜尋框用）─────────────
# 你習慣的叫法都可以加進來，例如 光模塊 → 光通訊。英文、簡體也可以。
ALIASES: dict[str, list[str]] = {
    "光通訊": ["光模塊", "光模組", "光收發模組", "光收發器", "矽光子", "CPO", "共同封裝光學", "光纖", "光元件",
              "800G", "1.6T", "optical", "transceiver", "photonics", "silicon photonics"],
    "記憶體": ["存儲", "內存", "DRAM", "NAND", "HBM", "快閃記憶體", "記憶體晶片", "memory", "flash"],
    "硬碟/儲存": ["硬碟", "HDD", "儲存設備", "存儲設備", "storage"],
    "高速傳輸/互連": ["互連", "AEC", "銅纜", "SerDes", "Retimer", "PCIe", "CXL", "高速傳輸", "interconnect"],
    "AI 晶片/GPU": ["GPU", "顯卡", "AI晶片", "加速器", "AI加速器", "accelerator"],
    "客製化 ASIC": ["ASIC", "客製化晶片", "自研晶片", "TPU", "XPU", "custom silicon"],
    "網通晶片": ["交換器晶片", "網通IC", "乙太網路晶片", "switch chip", "ethernet"],
    "晶圓代工": ["代工", "foundry", "晶圓廠"],
    "CPU": ["處理器", "x86", "processor"],
    "先進封裝": ["CoWoS", "封裝", "封測", "3D封裝", "advanced packaging"],
    "半導體設備": ["設備", "蝕刻", "曝光", "微影", "量測", "檢測", "沉積", "semicap", "equipment"],
    "類比/電源 IC": ["類比", "電源管理", "PMIC", "功率半導體", "analog", "power IC"],
    "RF 射頻": ["射頻", "RF", "手機晶片", "基頻"],
    "碳化矽/化合物半導體": ["SiC", "GaN", "氮化鎵", "第三代半導體"],
    "矽智財/EDA": ["IP", "EDA", "矽智財", "晶片設計軟體"],
    "AI 伺服器": ["伺服器", "server", "機櫃", "整機櫃", "GB200", "GB300", "AI server"],
    "網通交換器": ["交換器", "交換機", "switch", "網通", "網路設備", "networking"],
    "資料中心電力/散熱": ["散熱", "液冷", "水冷", "冷卻", "電源", "電力設備", "UPS", "變壓器", "cooling", "liquid cooling"],
    "核能": ["核電", "小型核反應爐", "SMR", "核反應爐", "nuclear"],
    "鈾礦": ["鈾", "uranium"],
    "電力": ["電廠", "發電", "電網", "獨立電力", "power", "utility"],
    "量子運算": ["量子", "量子電腦", "量子計算", "quantum"],
    "資安": ["網路安全", "資訊安全", "網安", "身分安全", "cybersecurity", "security"],
    "邊緣雲/CDN": ["CDN", "邊緣運算", "edge"],
    "資料儲存/備份": ["備份", "資料管理", "資料保護", "backup"],
    "AI 軟體/數據分析": ["AI軟體", "數據分析", "大數據", "AI應用", "analytics"],
    "國防科技": ["國防", "軍工", "軍事", "defense"],
    "可觀測性/資料庫": ["資料庫", "監控", "observability", "database"],
    "雲端 SaaS": ["SaaS", "雲端軟體", "企業軟體", "軟體服務", "cloud software"],
    "AI 雲端/Neocloud": ["算力雲", "GPU雲", "雲端算力", "neocloud", "AI cloud"],
    "AI 算力/資料中心": ["算力", "資料中心", "數據中心", "IDC", "data center", "HPC"],
    "比特幣/加密": ["比特幣", "加密貨幣", "虛擬貨幣", "幣圈", "礦商", "挖礦", "crypto", "bitcoin", "BTC"],
    "網路券商": ["券商", "交易平台", "broker", "brokerage"],
    "太空/衛星": ["太空", "衛星", "火箭", "低軌衛星", "space", "satellite"],
    "無人機": ["drone", "無人載具", "UAV"],
    "電動車": ["EV", "電車", "electric vehicle"],
    "機器人": ["人形機器人", "robot", "robotics", "機械人"],
    "手術機器人": ["達文西", "surgical robot"],
    "GLP-1/減重": ["減肥藥", "減重藥", "瘦瘦針", "肥胖", "obesity", "GLP1"],
    "儲能/電池": ["電池", "儲能", "鋰電", "battery", "energy storage"],
    "太陽能": ["光電", "solar", "PV"],
}
