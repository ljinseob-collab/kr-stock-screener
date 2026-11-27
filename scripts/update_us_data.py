#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""미국주식(S&P 500 · S&P 400 · NASDAQ 100 약 920종목) 5년 매물대 · VWAP · RSI(14)
· 3년 실적 · 배당 · 시총 · 주요 수익원 스크리너 데이터 생성기.

데이터 소스 (모두 무료 공개 데이터, API 키 불필요)
  1) 종목 목록   : S&P 500·S&P 400 — Wikipedia 구성종목 표 (+ 실패 시 GitHub datasets CSV)
                   NASDAQ 100 — slickcharts.com 구성종목 표
  2) 5년 일별    : Yahoo Finance chart API (range=5y, 수정주가 adjclose·거래량·배당 이벤트)
  3) 연간 실적   : Yahoo Finance fundamentals-timeseries (annualOperatingIncome·annualNetIncome)
  4) 시총·프로필 : Yahoo Finance quoteSummary (price.marketCap, assetProfile.website/요약)
  5) CIK(공시)   : SEC company_tickers.json → EDGAR 공시 링크
  6) 최근 뉴스   : Yahoo Finance RSS (종목별 헤드라인 3건)
  7) 주요 수익원 : us_labels.py 의 업종·기업 한국어 사전 + Yahoo 사업 요약 자동 번역

출력
  docs/data/us_stocks.json      : 목록용 데이터 (지표 + 매물대 20구간 + 한 줄 수익원 revShort)
  docs/data/us_meta.json        : 기준일·종목 수·출처 등 메타
  docs/data/us_extras/{0..15}.json : 상세용 (최근 뉴스·수익원 상세설명·홈페이지·업종)
  docs/data/us_failed.json      : 수집 실패 종목 로그

사용 예
  python scripts/update_us_data.py                  # 전체 갱신 (오늘 미국장 데이터가 이미 있으면 건너뜀)
  python scripts/update_us_data.py --force          # 최신 여부와 무관하게 갱신
  python scripts/update_us_data.py --limit 20       # 시총 상위 20개만 (테스트)
  python scripts/update_us_data.py --no-news        # 뉴스·수익원 상세 생략 (속도 테스트)
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import random
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

try:                                    # 같은 폴더의 한국어 라벨 사전
    from us_labels import (COMPANY_DETAIL, COMPANY_SHORT, INDUSTRY_DETAIL,
                           INDUSTRY_SHORT, SECTOR_KO, SECTOR_SHORT)
except ImportError:                     # 스크립트를 직접 실행할 때
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from us_labels import (COMPANY_DETAIL, COMPANY_SHORT, INDUSTRY_DETAIL,
                           INDUSTRY_SHORT, SECTOR_KO, SECTOR_SHORT)

# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------

YEARS = 5
PERIODS = (1, 2, 3, 4, 5)
BINS = 20
RSI_PERIOD = 14
MIN_TRADING_DAYS = 20
EXTRA_SHARDS = 16
NEWS_LIMIT = 3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "docs", "data")
CACHE_DIR = os.path.join(BASE_DIR, ".cache")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SEC_UA = "Lee Jinseob leejinseob8@gmail.com"

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5y&interval=1d&events=div"
TS_URL = ("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{sym}"
          "?symbol={sym}&type={types}&period1={p1}&period2={p2}")
QS_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym}?modules={mods}"
NEWS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
NAVER_BASIC = "https://api.stock.naver.com/stock/{code}/basic"   # 네이버 해외증권 종목코드 확인
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"

SP500_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP400_WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
NDX_SLICK = "https://www.slickcharts.com/nasdaq100"
SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"

FIN_TYPES = ("annualOperatingIncome,annualNetIncome,"
             "annualBasicAverageShares,annualTotalRevenue")

# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "application/json,text/html,*/*"})
        try:                                    # Yahoo 쿠키 + crumb (quoteSummary 용)
            s.get("https://fc.yahoo.com", timeout=15)
            s.crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).text.strip()
        except Exception:  # noqa: BLE001
            s.crumb = ""
        s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8))
        _local.session = s
    return s


class Retryable(Exception):
    pass


def with_retry(fn, tries: int = 3, base: float = 0.6):
    last: Optional[Exception] = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == tries - 1:
                break
            time.sleep(base * (2 ** attempt) + random.uniform(0, 0.3))
    raise last  # type: ignore[misc]


def yahoo_get(path: str, params: Optional[Dict[str, Any]] = None, mods: Optional[str] = None,
              sym: Optional[str] = None) -> Any:
    """Yahoo JSON 1건. crumb 오류(401)면 crumb를 새로 받아 한 번 더."""
    def one() -> Any:
        s = _session()
        url = path
        p = dict(params or {})
        if "{sym}" in url:
            url = url.replace("{sym}", sym or "")
        if mods is not None:
            url = url.replace("{mods}", mods)
        if s.crumb:
            p["crumb"] = s.crumb
        r = s.get(url, params=p or None, timeout=25)
        if r.status_code in (401, 403):
            s.get("https://fc.yahoo.com", timeout=10)
            s.crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10).text.strip()
            raise Retryable(f"HTTP {r.status_code}")
        if r.status_code == 429:
            time.sleep(2.0)
            raise Retryable("429")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise Retryable("JSON 아님")
    return with_retry(one, tries=4, base=0.7)


# ----------------------------------------------------------------------------
# 1) 종목 유니버스
# ----------------------------------------------------------------------------

def norm_sym(sym: Any) -> str:
    """BRK.B → BRK-B (Yahoo 표기)."""
    return str(sym).strip().upper().replace(".", "-").replace("/", "-")


def _wiki_tables(url: str):
    import io
    import pandas as pd
    r = _session().get(url, timeout=40)
    r.raise_for_status()
    return pd.read_html(io.StringIO(r.text))


def fetch_sp500(log) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        t = _wiki_tables(SP500_WIKI)[0]
        for _, row in t.iterrows():
            sym = norm_sym(row.get("Symbol"))
            if not sym:
                continue
            out[sym] = {"sym": sym, "name": str(row.get("Security") or sym).strip(),
                        "sp500": 1, "ndx": 0, "cik": _cik(row.get("CIK"))}
        log(f"  S&P 500 {len(out):,}종목 (Wikipedia)")
    except Exception as exc:  # noqa: BLE001
        log(f"  ! Wikipedia S&P 500 실패: {exc} → GitHub datasets CSV")
        r = _session().get(SP500_CSV, timeout=40)
        r.raise_for_status()
        import csv
        import io
        for row in csv.DictReader(io.StringIO(r.text)):
            sym = norm_sym(row.get("Symbol"))
            out[sym] = {"sym": sym, "name": str(row.get("Security") or sym).strip(),
                        "sp500": 1, "ndx": 0, "cik": _cik(row.get("CIK"))}
        log(f"  S&P 500 {len(out):,}종목 (datasets CSV)")
    return out


def _cik(v: Any) -> Optional[int]:
    s = re.sub(r"\D", "", str(v or ""))
    return int(s) if s else None


def fetch_sp400(log) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        t = _wiki_tables(SP400_WIKI)[0]
        for _, row in t.iterrows():
            sym = norm_sym(row.get("Symbol"))
            if not sym:
                continue
            out[sym] = {"sym": sym, "name": str(row.get("Security") or sym).strip(),
                        "sp500": 0, "ndx": 0, "cik": None}
        log(f"  S&P 400 {len(out):,}종목 (Wikipedia)")
    except Exception as exc:  # noqa: BLE001
        log(f"  ! S&P 400 실패: {exc} (S&P 500·NASDAQ 100만으로 진행)")
    return out


def fetch_ndx(log) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        t = _wiki_tables(NDX_SLICK)[0]
        for _, row in t.iterrows():
            sym = norm_sym(row.get("Symbol"))
            if not sym or not re.fullmatch(r"[A-Z][A-Z0-9\-]{0,6}", sym):
                continue
            out[sym] = {"sym": sym, "name": str(row.get("Company") or sym).strip(),
                        "sp500": 0, "ndx": 1, "cik": None}
        log(f"  NASDAQ 100 {len(out):,}종목 (slickcharts)")
    except Exception as exc:  # noqa: BLE001
        log(f"  ! NASDAQ 100 실패: {exc}")
    return out


def fetch_cik_map(log) -> Dict[str, int]:
    try:
        r = requests.get(SEC_TICKERS, headers={"User-Agent": SEC_UA}, timeout=40)
        r.raise_for_status()
        data = r.json()
        m = {}
        for v in data.values():
            t = norm_sym(v.get("ticker"))
            if t and t not in m:
                m[t] = int(v["cik_str"])
        log(f"  SEC CIK {len(m):,}건")
        return m
    except Exception as exc:  # noqa: BLE001
        log(f"  ! SEC CIK 목록 실패: {exc}")
        return {}


def load_universe(log) -> List[Dict[str, Any]]:
    log("[1/5] 종목 목록")
    merged: Dict[str, Dict[str, Any]] = {}
    for part in (fetch_sp500(log), fetch_sp400(log), fetch_ndx(log)):
        for sym, d in part.items():
            if sym in merged:
                merged[sym]["sp500"] = max(merged[sym]["sp500"], d["sp500"])
                merged[sym]["ndx"] = max(merged[sym]["ndx"], d["ndx"])
                merged[sym]["cik"] = merged[sym]["cik"] or d["cik"]
            else:
                merged[sym] = d
    cik = fetch_cik_map(log)
    for sym, d in merged.items():
        if not d.get("cik") and sym in cik:
            d["cik"] = cik[sym]
    rows = sorted(merged.values(), key=lambda d: d["sym"])
    log(f"  합계 {len(rows):,}종목 (S&P 500 {sum(d['sp500'] for d in rows):,} · "
        f"NASDAQ 100 {sum(d['ndx'] for d in rows):,})")
    return rows


# ----------------------------------------------------------------------------
# 2) 종목별 데이터
# ----------------------------------------------------------------------------

def fetch_chart(sym: str) -> Dict[str, Any]:
    d = yahoo_get(CHART_URL, sym=sym)
    res = (d.get("chart") or {}).get("result") or []
    if not res:
        raise RuntimeError("차트 없음")
    r = res[0]
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((r.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
    closes = q.get("close") or []
    vols = q.get("volume") or []
    rows: List[List[Any]] = []
    for i, t in enumerate(ts):
        c = (adj[i] if adj and i < len(adj) and adj[i] is not None else
             (closes[i] if i < len(closes) else None))
        v = vols[i] if i < len(vols) else None
        if c is None or c <= 0:
            continue
        dt = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y%m%d")
        rows.append([dt, float(c), float(v or 0)])
    if not rows:
        raise RuntimeError("일별 시세 없음")
    rows.sort(key=lambda x: x[0])
    divs: List[Tuple[str, float]] = []
    for k, ev in ((r.get("events") or {}).get("dividends") or {}).items():
        try:
            amt = float((ev or {}).get("amount") or 0)
            d0 = datetime.fromtimestamp(int((ev or {}).get("date") or 0), tz=timezone.utc).strftime("%Y%m%d")
            if amt > 0:
                divs.append((d0, amt))
        except (TypeError, ValueError):
            continue
    meta = r.get("meta") or {}
    return {"rows": rows, "divs": sorted(divs), "meta": meta}


def fetch_fundamentals(sym: str) -> Dict[str, List[Tuple[str, float]]]:
    now = int(time.time())
    p1 = now - (YEARS + 2) * 365 * 86400
    url = TS_URL.format(sym=sym, types=FIN_TYPES, p1=p1, p2=now)
    d = yahoo_get(url)
    out: Dict[str, List[Tuple[str, float]]] = {}
    for item in ((d.get("timeseries") or {}).get("result") or []):
        keys = [k for k in item if k not in ("meta", "timestamp")]
        if not keys:
            continue
        key = keys[0]
        vals = []
        for x in item.get(key) or []:
            if not x:
                continue
            v = (x.get("reportedValue") or {}).get("raw")
            asof = x.get("asOfDate")
            if v is None or not asof:
                continue
            vals.append((asof, float(v)))
        out[key] = vals
    return out


def fetch_profile(sym: str) -> Dict[str, Any]:
    d = yahoo_get(QS_URL, mods="assetProfile,price", sym=sym)
    res = (d.get("quoteSummary") or {}).get("result") or []
    if not res:
        raise RuntimeError("프로필 없음")
    r = res[0]
    ap = r.get("assetProfile") or {}
    pr = r.get("price") or {}
    return {
        "sector": ap.get("sector"),
        "industry": ap.get("industry"),
        "website": ap.get("website"),
        "summary": ap.get("longBusinessSummary") or "",
        "city": ap.get("city"),
        "longName": pr.get("longName") or "",
        "shortName": pr.get("shortName") or "",
        "quoteType": pr.get("quoteType"),
        "marketCap": (pr.get("marketCap") or {}).get("raw"),
    }


def fetch_news(sym: str, limit: int = NEWS_LIMIT) -> List[List[str]]:
    def one():
        s = _session()
        r = s.get(NEWS_URL.format(sym=sym), timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.text
    try:
        text = with_retry(one, tries=3, base=0.5)
    except Exception:  # noqa: BLE001 - 뉴스는 없어도 된다
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out: List[List[str]] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title or not link:
            continue
        dt = ""
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
            try:
                dt = datetime.strptime(pub, fmt).strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        if not dt:
            m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})", pub)
            if m:
                months = {m2: i for i, m2 in enumerate(
                    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
                dt = f"{m.group(3)}-{months.get(m.group(2), 1):02d}-{int(m.group(1)):02d}"
        out.append([dt, html.unescape(title), "Yahoo Finance", link])
        if len(out) >= limit:
            break
    return out


# ----------------------------------------------------------------------------
# 3) 주요 수익원 (한국어)
# ----------------------------------------------------------------------------

_rev_cache_lock = threading.Lock()
_rev_cache: Dict[str, str] = {}


def _cache_path(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def load_rev_cache() -> None:
    global _rev_cache
    try:
        with open(_cache_path("us_translations.json"), encoding="utf-8") as f:
            _rev_cache = json.load(f)
    except Exception:  # noqa: BLE001
        _rev_cache = {}


def save_rev_cache() -> None:
    p = _cache_path("us_translations.json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_rev_cache, f, ensure_ascii=False)
    os.replace(tmp, p)


def translate_ko(text: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return None
    key = hashlib.md5(text[:1200].encode("utf-8")).hexdigest()
    with _rev_cache_lock:
        hit = _rev_cache.get(key)
    if hit:
        return hit

    def one():
        r = _session().get(TRANSLATE_URL, params={
            "client": "gtx", "sl": "en", "tl": "ko", "dt": "t", "q": text[:1200]}, timeout=20)
        if r.status_code != 200:
            raise Retryable(f"HTTP {r.status_code}")
        d = r.json()
        return "".join(seg[0] for seg in d[0] if seg and seg[0])

    try:
        ko = with_retry(one, tries=3, base=0.8)
    except Exception:  # noqa: BLE001
        return None
    ko = re.sub(r"\s+", " ", ko or "").strip()
    if not ko:
        return None
    with _rev_cache_lock:
        _rev_cache[key] = ko
    return ko


def _first_sentences(text: str, limit: int = 2, max_len: int = 320) -> str:
    parts = re.split(r"(?<=[.!?。])\s+", (text or "").strip())
    out = ""
    for p in parts:
        if not p:
            continue
        if out and len(out) + len(p) > max_len:
            break
        out = (out + " " + p).strip() if out else p
        if out.count(".") + out.count("다.") >= limit and len(out) > 80:
            break
    if len(out) > max_len:
        out = out[:max_len].rsplit(" ", 1)[0] + "…"
    return out


def rev_short(sym: str, profile: Dict[str, Any]) -> str:
    if sym in COMPANY_SHORT:
        return COMPANY_SHORT[sym]
    ind = profile.get("industry")
    if ind and ind in INDUSTRY_SHORT:
        return INDUSTRY_SHORT[ind]
    sec = profile.get("sector")
    if sec and sec in SECTOR_SHORT:
        return SECTOR_SHORT[sec]
    return "사업 다각화"


def rev_detail(sym: str, profile: Dict[str, Any], short: str) -> str:
    if sym in COMPANY_DETAIL:
        return COMPANY_DETAIL[sym][1]
    ind = profile.get("industry")
    summary = profile.get("summary") or ""
    ko = translate_ko(summary) if summary else None
    if ko:
        body = _first_sentences(ko, 2, 300)
        if body and len(body) >= 20:
            if not body.endswith((".", "다", "요", "…")):
                body += "."
            tail = f" 주요 수익원은 {short}입니다."
            if short.replace(" ", "") not in body.replace(" ", "")[:40]:
                return (body + tail)[:460]
            return body[:400]
    base = INDUSTRY_DETAIL.get(ind or "")
    if not base:
        base = f"{INDUSTRY_SHORT.get(ind or '', '다양한 사업')}을 하는 회사입니다."
    return f"{base} 주요 수익원은 {short}입니다."


# ----------------------------------------------------------------------------
# 4) 지표 계산 (한국 스크리너와 같은 방식, 가격은 소수 2자리)
# ----------------------------------------------------------------------------

def rsi_wilder(closes: List[float], period: int = RSI_PERIOD) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def volume_profile(rows: List[List[Any]], bins: int = BINS) -> Optional[Dict[str, Any]]:
    closes = [r[1] for r in rows]
    vols = [r[2] for r in rows]
    total = sum(vols)
    if not closes or total <= 0:
        return None
    lo, hi = min(closes), max(closes)
    step = (hi - lo) / bins if hi > lo else 1.0
    if hi <= lo:
        hi = lo + step * bins
    acc = [0.0] * bins
    for c, v in zip(closes, vols):
        idx = int((c - lo) / step)
        idx = 0 if idx < 0 else (bins - 1 if idx >= bins else idx)
        acc[idx] += v
    shares = [round(v / total * 100.0, 1) for v in acc]
    poc_idx = max(range(bins), key=lambda i: acc[i])
    return {
        "poc": lo + (poc_idx + 0.5) * step,
        "vwap": sum(c * v for c, v in zip(closes, vols)) / total,
        "bin_lo": lo, "bin_hi": hi, "step": step,
        "shares": shares,
    }


def cum_below(price: float, vp: Dict[str, Any]) -> Optional[float]:
    lo, hi, step = vp["bin_lo"], vp["bin_hi"], vp.get("step") or 0
    shares = vp.get("shares") or []
    if not shares or step <= 0:
        return None
    if price < lo:
        return 0.0
    if price >= hi:
        return 100.0
    pos = (price - lo) / step
    idx = int(pos)
    frac = pos - idx
    idx = min(idx, len(shares) - 1)
    cum = sum(shares[:idx]) + shares[idx] * frac
    return round(min(100.0, max(0.0, cum)), 1)


def vp_periods(rows: List[List[Any]], price: float) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    as_of = datetime.strptime(rows[-1][0], "%Y%m%d").date()
    for y in PERIODS:
        if y >= YEARS:
            sub = rows
        else:
            try:
                cutoff = as_of.replace(year=as_of.year - y)
            except ValueError:
                cutoff = as_of.replace(year=as_of.year - y, day=28)
            cs = cutoff.strftime("%Y%m%d")
            sub = [r for r in rows if r[0] >= cs]
            if len(sub) < MIN_TRADING_DAYS:
                sub = rows
        vp = volume_profile(sub)
        if not vp:
            continue
        rec = {
            "poc": round(vp["poc"], 2),
            "vwap": round(vp["vwap"], 2),
            "bl": round(vp["bin_lo"], 2),
            "bh": round(vp["bin_hi"], 2),
            "bins": vp["shares"],
        }
        cum = cum_below(price, vp)
        if cum is not None:
            rec["cum"] = int(round(cum))
        out[str(y)] = rec
    return out


# ----------------------------------------------------------------------------
# 5) 종목 1개 조립
# ----------------------------------------------------------------------------

def build_stock(u: Dict[str, Any], want_news: bool = True) -> Dict[str, Any]:
    sym = u["sym"]
    ch = fetch_chart(sym)
    rows, divs, cmeta = ch["rows"], ch["divs"], ch["meta"]
    if len(rows) < MIN_TRADING_DAYS:
        raise RuntimeError(f"거래일 부족({len(rows)}일)")
    if str(cmeta.get("instrumentType") or "").upper() not in ("", "EQUITY"):
        raise RuntimeError(f"주식 아님({cmeta.get('instrumentType')})")

    price = rows[-1][1]
    closes = [r[1] for r in rows]
    chg = None
    if len(rows) >= 2 and rows[-2][1] > 0:
        chg = round((rows[-1][1] / rows[-2][1] - 1.0) * 100.0, 2)

    prof = fetch_profile(sym)
    if str(prof.get("quoteType") or "").upper() not in ("", "EQUITY"):
        raise RuntimeError(f"주식 아님({prof.get('quoteType')})")
    mc = prof.get("marketCap") or 0
    if not mc:
        mc = 0

    data: Dict[str, Any] = {
        "c": sym,
        "n": (prof.get("longName") or prof.get("shortName") or u.get("name") or sym).strip(),
        "x": _exchange(cmeta.get("fullExchangeName") or cmeta.get("exchangeName") or ""),
        "p": round(price, 2),
        "mc": int(mc),
        "nd": len(rows),
    }
    if chg is not None:
        data["chg"] = chg
    if u.get("sp500"):
        data["p500"] = 1
    if u.get("ndx"):
        data["ndx"] = 1
    if u.get("cik"):
        data["cik"] = int(u["cik"])
    nv = fetch_naver_code(sym, data["x"], data["p"])
    if nv:
        data["nv"] = nv
    data["_last"] = rows[-1][0]

    rsi = rsi_wilder(closes)
    if rsi is not None:
        data["rsi"] = round(rsi, 1)

    vps = vp_periods(rows, price)
    if vps:
        data["vp"] = vps
        base = vps.get(str(YEARS)) or vps[max(vps, key=int)]
        data["poc"], data["vwap"] = base["poc"], base["vwap"]
        if "cum" in base:
            data["cum"] = base["cum"]

    # 배당: 최근 1년(마지막 거래일 기준 365일) 지급된 배당 합계
    last = datetime.strptime(rows[-1][0], "%Y%m%d").date()
    cutoff = (last - timedelta(days=365)).strftime("%Y%m%d")
    dps = sum(a for (d, a) in divs if d >= cutoff)
    if dps > 0:
        data["dps"] = round(dps, 4)
        data["dy"] = round(dps / price * 100, 2)

    # 연간 실적 3개년
    f = fetch_fundamentals(sym)
    op = {a: v for a, v in f.get("annualOperatingIncome", [])}
    ni = {a: v for a, v in f.get("annualNetIncome", [])}
    dates = sorted(set(op) | set(ni))[-3:]
    fin: List[List[Any]] = []
    for a in dates:
        y = int(a[:4])
        o = op.get(a)
        n = ni.get(a)
        if o is None and n is None:
            continue
        fin.append([y, _round_money(o), _round_money(n)])
    if fin:
        data["fin"] = fin
        last_a = dates[-1] if dates else ""
        fm = int(last_a[5:7]) if len(last_a) >= 7 else 12
        if fm != 12:
            data["fm"] = fm
        ops = [x[1] for x in fin]
        nis = [x[2] for x in fin]
        if len(ops) == 3 and all(v is not None for v in ops):
            data["op3"] = 1 if all(v > 0 for v in ops) else 0
        if len(nis) == 3 and all(v is not None for v in nis):
            data["ni3"] = 1 if all(v > 0 for v in nis) else 0
        last_op = ops[-1] if ops else None
        if last_op and last_op > 0 and data["mc"] > 0:
            mult = data["mc"] / last_op
            data["mult"] = round(mult, 1)
            data["pass"] = 1 if mult < 10 else 0

    # 요약(목록)용 주요 수익원
    short = rev_short(sym, prof)
    data["revShort"] = short

    # 상세 데이터 (샤드로 분리 저장)
    extra: Dict[str, Any] = {"w": prof.get("website") or ""}
    if prof.get("sector"):
        extra["s"] = SECTOR_KO.get(prof["sector"], prof["sector"])
    if prof.get("industry"):
        extra["i"] = prof["industry"]
    if u.get("cik"):
        extra["cik"] = u["cik"]
    if want_news:
        extra["revDetail"] = rev_detail(sym, prof, short)
        try:
            news = fetch_news(sym)
            if news:
                extra["n"] = news
        except Exception:  # noqa: BLE001
            pass
    data["_x"] = extra
    return data


def _round_money(v: Optional[float]) -> Optional[int]:
    return None if v is None else int(round(v))


def _exchange(name: str) -> str:
    n = (name or "").upper()
    if "NASDAQ" in n:
        return "NASDAQ"
    if "NYSE" in n or "NEW YORK" in n:
        return "NYSE"
    if "AMEX" in n or "NYSE AMERICAN" in n:
        return "AMEX"
    return "US"


def fetch_naver_code(sym: str, exch: str, price: float) -> Optional[str]:
    """네이버 해외증권에서 실제로 열리는 종목 페이지 URL(예: .../worldstock/stock/JPM).

    네이버는 자체 종목 마스터를 쓰므로 티커 표기가 다를 수 있다(AAPL.O, JPM 등).
    여러 표기를 시도해 200 응답이 오고, 종목명·현재가가 우리 데이터와 맞는 것만
    채택한다. 없으면 None → 화면은 Yahoo Finance 를 대표 링크로 쓴다.
    """
    suf = "N" if exch == "NYSE" else ("A" if exch == "AMEX" else "O")
    cands: List[str] = []
    for c in (f"{sym}.{suf}", sym, f"{sym.replace('-', '.')}.{suf}", sym.replace("-", ".")):
        if c not in cands:
            cands.append(c)
    headers = {
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                       "Mobile/15E148 Safari/604.1"),
        "Referer": "https://m.stock.naver.com/",
    }
    for code in cands:
        try:
            r = _session().get(NAVER_BASIC.format(code=code), headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            d = r.json()
            if d.get("isEtf"):
                continue
            cp = d.get("closePriceRaw") or d.get("closePrice")
            try:
                cp = float(cp)
            except (TypeError, ValueError):
                cp = None
            if cp and price > 0 and abs(cp - price) / price > 0.15:
                continue                      # 다른 종목(동명/오매칭) 방지
            url = str(d.get("endUrl") or "")
            if url.startswith("https://") and "/worldstock/stock/" in url:
                return url
        except Exception:  # noqa: BLE001
            continue
    return None


def shard_of(sym: str, n: int = EXTRA_SHARDS) -> int:
    return sum(ord(c) for c in sym) % n


# ----------------------------------------------------------------------------
# 5) 최신 여부 확인
# ----------------------------------------------------------------------------

def last_us_session(log) -> Optional[str]:
    try:
        d = yahoo_get(CHART_URL.replace("range=5y", "range=5d"), sym="SPY")
        r = (d.get("chart") or {}).get("result") or []
        ts = (r[0].get("timestamp") or []) if r else []
        if not ts:
            return None
        return datetime.fromtimestamp(ts[-1], tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception as exc:  # noqa: BLE001
        log(f"  ! 미국장 최근 거래일 확인 실패: {exc}")
        return None


def existing_as_of() -> Optional[str]:
    try:
        with open(os.path.join(DATA_DIR, "us_meta.json"), encoding="utf-8") as f:
            return json.load(f).get("as_of")
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------------
# 6) 메인
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="미국주식 매물대 스크리너 데이터 생성")
    ap.add_argument("--limit", type=int, default=0, help="시총 상위 N개만 (테스트용)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-news", action="store_true", help="뉴스·수익원 상세 생략")
    ap.add_argument("--force", action="store_true", help="최신 여부와 무관하게 갱신")
    ap.add_argument("--resume", action="store_true", help="같은 기준일 수집 캐시 이어쓰기")
    ap.add_argument("--out-dir", type=str, default=DATA_DIR)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    lock = threading.Lock()

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    load_rev_cache()

    log("[0/5] 미국장 최근 거래일 확인")
    session_last = last_us_session(log)
    prev = existing_as_of()
    if session_last:
        log(f"  최근 미국장 거래일 {session_last} · 기존 데이터 기준일 {prev or '없음'}")
        if prev and prev >= session_last and not args.force and not args.limit:
            log("  이미 최신입니다. 갱신을 건너뜁니다 (--force 로 강제 갱신).")
            return 0

    universe = load_universe(log)
    if args.limit:
        # --limit 은 시총 순 정렬을 위해 프로필을 한 번 더 받아 쓴다
        log("  --limit: 시총 상위만 추리기 위해 프로필 조회")
        def cap(u):
            try:
                return u, (fetch_profile(u["sym"]).get("marketCap") or 0)
            except Exception:  # noqa: BLE001
                return u, 0
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            got = list(ex.map(cap, universe))
        got.sort(key=lambda x: -x[1])
        universe = [u for u, _ in got[: args.limit]]
    log(f"  대상 {len(universe):,}종목")

    today = date.today().strftime("%Y%m%d")
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"us_{today}.jsonl")
    results: Dict[str, Dict[str, Any]] = {}
    if args.resume and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    results[rec["c"]] = rec
                except Exception:  # noqa: BLE001
                    continue
        log(f"  캐시에서 {len(results):,}종목 로드")
        wanted = {u["sym"] for u in universe}
        results = {k: v for k, v in results.items() if k in wanted}

    pending = [u for u in universe if u["sym"] not in results]
    log(f"[2/5] 시세·실적·수익원 수집 (대상 {len(pending):,}개, workers={args.workers})")
    failed: List[Dict[str, str]] = []
    done = 0
    t1 = time.time()
    cache_f = open(cache_path, "a" if args.resume else "w", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(build_stock, u, not args.no_news): u for u in pending}
        for fut in as_completed(futs):
            u = futs[fut]
            done += 1
            try:
                data = fut.result()
                with lock:
                    results[data["c"]] = data
                    cache_f.write(json.dumps(data, ensure_ascii=False) + "\n")
                    cache_f.flush()
            except Exception as exc:  # noqa: BLE001
                with lock:
                    failed.append({"code": u["sym"], "name": u.get("name", ""),
                                   "reason": f"{type(exc).__name__}: {exc}"[:200]})
            if done % 100 == 0 or done == len(pending):
                el = time.time() - t1
                log(f"      {done:,}/{len(pending):,} 완료 · 성공 {len(results):,} · 실패 {len(failed):,} · "
                    f"{done/max(el,1):.1f}건/초 · 경과 {el/60:.1f}분")
    cache_f.close()
    save_rev_cache()

    log("[3/5] 결과 정리")
    extras: Dict[str, Dict[str, Any]] = {}
    for sym, d in results.items():
        x = d.pop("_x", None)
        if x:
            extras[sym] = x

    stocks = sorted(results.values(), key=lambda d: d.get("mc", 0), reverse=True)
    last_dates = [d.pop("_last", None) for d in stocks]
    as_of = session_last or today
    counts: Dict[str, int] = {}
    for d_ in last_dates:
        if d_:
            counts[d_] = counts.get(d_, 0) + 1
    if counts:
        as_of = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    as_of_dash = f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:8]}"
    for d, ld in zip(stocks, last_dates):
        if ld and ld != as_of:
            d["ld"] = f"{ld[:4]}-{ld[4:6]}-{ld[6:8]}"

    sp500_count = sum(1 for d in stocks if d.get("p500"))
    ndx_count = sum(1 for d in stocks if d.get("ndx"))
    meta = {
        "tab": "us",
        "as_of": as_of_dash,
        "generated_at": datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"),
        "count": len(stocks),
        "failed": len(failed),
        "years": YEARS,
        "periods": list(PERIODS),
        "bins": BINS,
        "rsi_period": RSI_PERIOD,
        "sp500_count": sp500_count,
        "ndx_count": ndx_count,
        "source": "Yahoo Finance(일별 수정주가·연간 실적·시총·배당·사업 요약·뉴스), "
                  "Wikipedia(slickcharts) 구성종목, SEC EDGAR(CIK)",
        "universe": "S&P 500 · S&P 400 · NASDAQ 100 (ETF·ETN 등 상품 제외, 약 920종목)",
        "units": {"price": "USD", "marcap": "USD", "fin": "USD", "dps": "USD", "dy": "%"},
        "currency": "USD",
        "extra": {"shards": EXTRA_SHARDS, "news": NEWS_LIMIT, "rev": 1},
        "notes": [
            "매물대는 최근 1~5년 일별 수정주가(adjclose)·거래량을 가격 20구간으로 나눠 누적한 분포입니다(화면에서 기간 전환).",
            "누적 백분위는 현재가 이하 가격대에 쌓인 거래량 비율(낮을수록 매물대 하단)입니다.",
            "평균단가는 선택 기간 일별 종가의 거래량 가중평균(VWAP)입니다.",
            "RSI(14)는 일별 종가 기준 와일더 방식입니다.",
            "영업이익·순이익은 Yahoo Finance 연간 확정 실적(최근 3개 사업연도)이며 단위는 달러(USD)입니다.",
            "배당수익률은 최근 1년간 지급된 주당배당금 합계를 현재가로 나눈 값입니다.",
            "시가총액은 Yahoo Finance 기준(USD)입니다.",
            "등락률은 최근 두 거래일 종가 비교입니다.",
            "주요 수익원 한 줄(revShort)은 목록에, 상세 설명(revDetail)·홈페이지(w)·업종(s·i)은 상세 화면에서 지연 로드합니다(data/us_extras/{n}.json).",
        ],
    }

    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    log("[4/5] 저장")
    for name, payload in (("us_meta.json", meta),
                          ("us_stocks.json", {"meta": {"as_of": as_of_dash, "count": len(stocks)},
                                              "stocks": stocks})):
        p = os.path.join(out_dir, name)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)

    failed_path = os.path.join(out_dir, "us_failed.json")
    with open(failed_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": meta["generated_at"], "count": len(failed), "failed": failed},
                  f, ensure_ascii=False, indent=1)

    extra_dir = os.path.join(out_dir, "us_extras")
    os.makedirs(extra_dir, exist_ok=True)
    shards: List[Dict[str, Dict[str, Any]]] = [{} for _ in range(EXTRA_SHARDS)]
    for sym, x in extras.items():
        shards[shard_of(sym)][sym] = x
    extra_bytes = 0
    for i, sh in enumerate(shards):
        p = os.path.join(extra_dir, f"{i}.json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sh, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
        extra_bytes += os.path.getsize(p)

    size_mb = os.path.getsize(os.path.join(out_dir, "us_stocks.json")) / 1024 / 1024
    log(f"  us_stocks.json {len(stocks):,}종목 {size_mb:.2f} MB · "
        f"us_extras {len(extras):,}종목 {extra_bytes/1024/1024:.2f} MB · 기준일 {as_of_dash}")
    log(f"  S&P500 {sp500_count:,} · NASDAQ100 {ndx_count:,} · 실패 {len(failed)}")
    log(f"  주요 수익원 있음 {sum(1 for d in stocks if d.get('revShort')):,} / "
        f"상세설명 있음 {sum(1 for x in extras.values() if x.get('revDetail')):,} · "
        f"홈페이지 있음 {sum(1 for x in extras.values() if x.get('w')):,}")

    # CI: 워크플로우의 '데이터 커밋' 단계가 인덱스를 그대로 커밋하므로 미리 스테이징
    if os.environ.get("GITHUB_ACTIONS") == "true":
        import subprocess
        rel = [os.path.relpath(os.path.join(out_dir, n), BASE_DIR)
               for n in ("us_stocks.json", "us_meta.json", "us_failed.json", "us_extras")]
        rel = [r for r in rel if not r.startswith("..")]
        try:
            subprocess.run(["git", "add", "--"] + rel, cwd=BASE_DIR, check=False, timeout=180)
            log(f"  CI: {', '.join(rel)} 스테이징")
        except Exception as exc:  # noqa: BLE001
            log(f"  CI: 스테이징 생략 ({exc})")

    elapsed = time.time() - t0
    log(f"[5/5] 완료 · 총 {elapsed/60:.1f}분" if elapsed > 90 else f"[5/5] 완료 · 총 {elapsed:.0f}초")
    if failed:
        log("실패 상위 사유:")
        reasons: Dict[str, int] = {}
        for f_ in failed:
            k = f_["reason"].split(":")[0]
            reasons[k] = reasons.get(k, 0) + 1
        for k, v in sorted(reasons.items(), key=lambda x: -x[1])[:6]:
            log(f"  - {k}: {v}건")
        for f_ in failed[:8]:
            log(f"  · {f_['code']} {f_['name'][:24]}: {f_['reason'][:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
