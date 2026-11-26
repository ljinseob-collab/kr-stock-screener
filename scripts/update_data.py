#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
국내주식(코스피·코스닥 보통주) 5년 매물대 · 5년 VWAP · RSI(14) · 3년 실적 · 배당 · 시가총액 스크리너
데이터 생성기.

데이터 소스 (모두 무료 공개 데이터, API 키 불필요)
  1) 종목 목록 + 시가총액  : FinanceDataReader  StockListing('KRX')   (네이버 금융/ KRX 상장 목록)
  2) 5년 일별 OHLCV        : 네이버 금융 api.finance.naver.com/siseJson.naver (수정주가·액면분할 반영)
  3) 연간 영업이익/순이익  : 네이버 금융 m.stock.naver.com/api/stock/{code}/finance/annual
  4) 주당배당금(연간)      : 같은 연간 실적 응답의 '주당배당금' 행 (최근 확정 사업연도)
  5) 최근 공시             : 네이버 금융 m.stock.naver.com/api/stock/{code}/disclosure
  6) 최근 뉴스             : 네이버 금융 m.stock.naver.com/api/news/stock/{code}
     (CORS 미허용 → 브라우저 직접 호출 불가, 여기서 수집해 정적 JSON 으로 배포)

출력
  docs/data/stocks.json   : 스크리너용 데이터 (종목별 지표 + 매물대 20구간 + 배당)
  docs/data/extras/{n}.json : 종목별 최근 공시·뉴스 (상세 화면에서 지연 로드, 16개 분할)
  docs/data/failed.json   : 수집 실패/제외 종목 로그

사용 예
  python scripts/update_data.py                      # 전체 수집
  python scripts/update_data.py --limit 50            # 시총 상위 50개만 (테스트)
  python scripts/update_data.py --workers 10          # 동시 요청 수
  python scripts/update_data.py --codes 005930,000660 # 특정 종목만
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------

YEARS = 5                 # 최대 수집/산정 기간 (년)
PERIODS = (1, 2, 3, 4, 5)  # 화면에서 전환할 매물대 기간 (년)
BINS = 20                 # 매물대 가격 구간 수
RSI_PERIOD = 14
MIN_TRADING_DAYS = 20     # 지표 산출 최소 거래일 수

UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")

DAILY_URL = ("https://api.finance.naver.com/siseJson.naver"
             "?symbol={code}&requestType=1&startTime={start}&endTime={end}&timeframe=day")
FIN_URL = "https://m.stock.naver.com/api/stock/{code}/finance/annual"
# 코스피200 구성종목 (네이버 금융 지수 구성종목 API, 20종목/페이지)
K200_URL = "https://m.stock.naver.com/api/index/KPI200/enrollStocks?page={page}"
K200_MAX_PAGES = 15
DISC_URL = "https://m.stock.naver.com/api/stock/{code}/disclosure"
NEWS_URL = "https://m.stock.naver.com/api/news/stock/{code}?pageSize=20&page=1"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(BASE_DIR, "docs", "data", "stocks.json")

FIN_LABELS = ("영업이익", "당기순이익")

EXTRA_SHARDS = 16         # 상세 화면용 공시·뉴스 JSON 분할 수
DISC_LIMIT = 4            # 종목당 최근 공시 건수
NEWS_LIMIT = 3            # 종목당 최근 뉴스 건수

# 상장 목록에서 제외할 종목 판별 규칙 (ETF/ETN/스팩/리츠/우선주/펀드 등)
RE_SPAC_DEPT = re.compile(r"SPAC", re.I)
RE_REIT = re.compile(r"(^|[\s(])리츠|[리]츠\s*\d*\s*호?$|코크렙|위탁관리부동산")
RE_PREF_NAME = re.compile(r"우[A-Z]?$|우선주$")
RE_ETC_FUND = re.compile(r"ETF|ETN|인버스|레버리지|선박투자|투자회사$")
# ETF/ETN 발행 브랜드 (혹시 상장 목록에 섞여 들어오는 경우 대비)
RE_ETF_BRAND = re.compile(
    r"^(KODEX|TIGER|ARIRANG|KBSTAR|HANARO|KOSEF|SOL|ACE|PLUS|RISE|TIMEFOLIO|"
    r"WOORI|마이다스|히어로즈|파워|TREX|FOCUS|BNK|삼성KODEX)")

# 이름만으로는 회사와 구분되지 않는 공모 펀드(인프라투자회사 등)
FUND_NAMES = {"맥쿼리인프라", "KB발해인프라"}


# ----------------------------------------------------------------------------
# HTTP 유틸
# ----------------------------------------------------------------------------

_local = threading.local()


def _session() -> requests.Session:
    """스레드별 requests 세션 (커넥션 재사용)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=8)
        s.mount("https://", adapter)
        _local.session = s
    return s


class HttpError(Exception):
    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


def http_get(url: str, referer: str = "https://finance.naver.com/",
             timeout: int = 25) -> requests.Response:
    headers = {
        "User-Agent": UA_PC if "api.finance.naver.com" in url else UA_MOBILE,
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }
    return _session().get(url, headers=headers, timeout=timeout)


def with_retry(fn, tries: int = 4, base_delay: float = 0.7, what: str = ""):
    """지수 백오프 재시도. 마지막 예외는 그대로 올린다."""
    last: Optional[Exception] = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 네트워크/파싱 오류 모두 재시도 대상
            last = exc
            if isinstance(exc, HttpError) and not exc.retryable:
                raise
            if attempt == tries - 1:
                break
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.4)
            if isinstance(exc, HttpError) and "429" in str(exc):
                delay += 3.0
            time.sleep(delay)
    raise last  # type: ignore[misc]


# ----------------------------------------------------------------------------
# 1) 상장 종목 + 시가총액
# ----------------------------------------------------------------------------

def fetch_kospi200(log) -> Dict[str, str]:
    """코스피200 구성종목 {종목코드: 종목명}.

    네이버 금융 지수 구성종목 API(m.stock.naver.com/api/index/KPI200/enrollStocks)는
    한 페이지에 20종목씩 돌려준다. 페이지를 비울 때까지 이어 붙인다.
    """
    def one_page(page: int) -> List[Dict[str, Any]]:
        def one():
            r = http_get(K200_URL.format(page=page))
            if r.status_code == 429:
                raise HttpError("429 rate limited")
            if r.status_code != 200:
                raise HttpError(f"HTTP {r.status_code}")
            arr = r.json()
            if not isinstance(arr, list):
                raise HttpError("코스피200 응답 형식 오류")
            return arr

        return with_retry(one, tries=3, base_delay=0.8)

    def collect() -> Dict[str, str]:
        got: Dict[str, str] = {}
        for page in range(1, K200_MAX_PAGES + 1):
            try:
                arr = one_page(page)
            except Exception as exc:  # noqa: BLE001 - 실패 페이지는 건너뛴다
                log(f"  ! 코스피200 {page}페이지 실패: {exc}")
                continue
            if not arr:              # 빈 페이지 = 끝
                break
            for it in arr:
                code = str(it.get("itemCode") or "").strip()
                name = str(it.get("stockName") or "").strip()
                # 신형 종목코드(0126Z0 등 영문 포함 6자리)도 포함한다
                if re.fullmatch(r"[0-9A-Z]{6}", code):
                    got.setdefault(code, name)
        return got

    # 페이지가 일시적으로 짧게 오는 경우가 있어(199개) 200개 미만이면 최대 3번 더
    # 훑어 합칩(nums). 정상 응답은 201개(20×10 + 1)다.
    out: Dict[str, str] = {}
    for attempt in range(3):
        got = collect()
        merged = dict(out)
        merged.update(got)
        if len(merged) > len(out):
            out = merged
        if len(out) >= 200:
            break
        if attempt < 2:
            log(f"  코스피200 {len(out)}개로 짧아 다시 확인")
    log(f"코스피200 구성종목 {len(out)}개 수신")
    return out


def load_universe(log, k200: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    import FinanceDataReader as fdr

    k200 = k200 or {}

    def fetch():
        df = fdr.StockListing("KRX")
        if df is None or len(df) == 0:
            raise HttpError("StockListing('KRX') 결과가 비었습니다")
        return df

    df = with_retry(fetch, tries=4, base_delay=2.0)
    log(f"상장 목록 원본 {len(df):,}건 수신")

    rows: List[Dict[str, Any]] = []
    excluded = {"market": 0, "preferred": 0, "spac": 0, "reit": 0, "fund": 0, "no_price": 0}

    for rec in df.to_dict("records"):
        code = str(rec.get("Code") or "").strip()
        name = str(rec.get("Name") or "").strip()
        market = str(rec.get("Market") or "").strip()
        dept = str(rec.get("Dept") or "")

        if market not in ("KOSPI", "KOSDAQ"):
            excluded["market"] += 1
            continue
        if not re.fullmatch(r"\d{6}", code):
            # 0197V0 같은 신형 코드는 보통주도 존재 → 6자리 아니면 접미사 검사로 대체
            if not code or len(code) != 6:
                excluded["market"] += 1
                continue
        if not code.endswith("0") or RE_PREF_NAME.search(name):
            excluded["preferred"] += 1
            continue
        if RE_SPAC_DEPT.search(dept) or "스팩" in name:
            excluded["spac"] += 1
            continue
        if RE_REIT.search(name):
            excluded["reit"] += 1
            continue
        # 코스피200 구성종목은 실제 상장 기업의 보통주이므로 이름 기반 ETF/펀드
        # 제외 규칙에서 빼준다 (예: 'BNK금융지주'가 ETF 브랜드 'BNK'에 걸리던 문제).
        if code not in k200 and (RE_ETC_FUND.search(name) or RE_ETF_BRAND.search(name)
                                 or name in FUND_NAMES):
            excluded["fund"] += 1
            continue

        try:
            marcap = float(rec.get("Marcap") or 0)
        except (TypeError, ValueError):
            marcap = 0.0
        close = rec.get("Close")
        if not marcap or marcap <= 0:
            excluded["no_price"] += 1
            continue

        rows.append({
            "code": code,
            "name": name,
            "market": "KOSPI" if market == "KOSPI" else "KOSDAQ",
            "marcap_eok": int(round(marcap / 1e8)),          # 원 → 억원
            "listing_close": int(close) if close and close == close else None,
            "k200": 1 if code in k200 else 0,
        })

    log("제외 내역: " + ", ".join(f"{k} {v:,}" for k, v in excluded.items()))
    if k200:
        matched = sum(1 for r in rows if r["k200"])
        log(f"코스피200 매칭 {matched:,}개 / 수신 {len(k200):,}개")
        if not 190 <= matched <= 210:
            log(f"  ! 코스피200 매칭 수가 예상(약 200)과 다릅니다: {matched}")
    rows.sort(key=lambda r: r["marcap_eok"], reverse=True)
    return rows


# ----------------------------------------------------------------------------
# 2) 일별 시세 파싱
# ----------------------------------------------------------------------------

def parse_sise_json(text: str) -> List[List[Any]]:
    """네이버 siseJson 응답 파싱.

    응답은 헤더 줄(['날짜', ...])과 빈 줄/탭이 섞인 비표준 JSON 이라
    '["' 로 시작하는 줄만 골라 한 줄씩 파싱한다.
    """
    out: List[List[Any]] = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith('["'):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, list) or len(row) < 6:
            continue
        if not isinstance(row[0], str) or not re.fullmatch(r"\d{8}", row[0]):
            continue                    # 헤더 줄
        try:
            close = float(row[4])
            volume = float(row[5])
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        out.append([row[0], close, volume])
    return out


def fetch_daily(code: str, start: str, end: str) -> List[List[Any]]:
    url = DAILY_URL.format(code=code, start=start, end=end)

    def one():
        r = http_get(url)
        if r.status_code == 429:
            raise HttpError("429 rate limited")
        if r.status_code != 200:
            raise HttpError(f"HTTP {r.status_code}")
        rows = parse_sise_json(r.text)
        if not rows:
            raise HttpError("일별 시세 없음", retryable=False)
        return rows

    return with_retry(one, tries=4, base_delay=0.8)


# ----------------------------------------------------------------------------
# 3) 재무 (연간 영업이익/당기순이익)
# ----------------------------------------------------------------------------

def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-", "N/A", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_finance(code: str) -> Dict[str, Any]:
    """최근 연간(확정치) 영업이익/당기순이익 최대 3개년. 단위: 억원.

    같은 응답의 '주당배당금' 행에서 최근 확정 사업연도 주당배당금(dps, 원)의
    사업연도(dvy)도 함께 뽑는다. 무배당('-')이면 값이 없다.
    """
    url = FIN_URL.format(code=code)

    def one():
        r = http_get(url)
        if r.status_code == 429:
            raise HttpError("429 rate limited")
        if r.status_code == 404:
            return {"fy": [], "fm": 12}
        if r.status_code != 200:
            raise HttpError(f"HTTP {r.status_code}")
        data = r.json()
        info = data.get("financeInfo") or {}
        titles = info.get("trTitleList") or []
        rows = info.get("rowList") or []
        if not titles or not rows:
            return {"fy": [], "fm": 12}

        # 확정 실적만 (isConsensus == 'N'), 연도 오름차순
        actual = [t for t in titles if str(t.get("isConsensus", "N")).upper() != "Y"]
        actual.sort(key=lambda t: str(t.get("key", "")))
        keys = [str(t["key"]) for t in actual]

        by_title = {str(row.get("title", "")).strip(): row for row in rows}

        def values(title: str) -> Dict[str, Optional[float]]:
            row = by_title.get(title) or {}
            cols = row.get("columns") or {}
            return {k: _num((cols.get(k) or {}).get("value")) for k in keys}

        op = values("영업이익")
        ni = values("당기순이익")

        fy: List[List[Any]] = []
        for k in keys:
            if len(k) < 6:
                continue
            y, m = int(k[:4]), int(k[4:6])
            if op.get(k) is None and ni.get(k) is None:
                continue
            fy.append([y, m, op.get(k), ni.get(k)])

        fy = fy[-3:]
        fm = fy[-1][1] if fy else 12
        out: Dict[str, Any] = {"fy": fy, "fm": fm}

        # 주당배당금: 가장 최근 확정 사업연도의 값 (무배당 '-' 면 값이 없다)
        dps = values("주당배당금")
        if keys:
            k = keys[-1]
            v = dps.get(k)
            if v is not None and v > 0:
                out["dps"] = int(round(v))
                out["dvy"] = int(k[:4])
        return out

    return with_retry(one, tries=4, base_delay=0.8)


# ----------------------------------------------------------------------------
# 3-b) 최근 공시 · 뉴스
# ----------------------------------------------------------------------------

RE_NAME_PREFIX = re.compile(r"^(?:\(주\)|㈜|주식회사)\s*")


def _clean_text(t: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(t or ""))).strip()


def strip_company(title: str, name: str) -> str:
    """공시 제목 앞의 회사명 표기('삼성전자(주) ', '(주)피엔풍년 ' 등)를 떼어낸다.

    종목 상세 화면 안에서 보여주므로 회사명은 중복이다. 떼고 나면 남는 게
    없으면 원문을 그대로 둔다. 종목명과 공시 표기가 달라도('한화생명' ↔
    '한화생명보험(주)', 'GS글로벌' ↔ '(주)지에스글로벌') 두세 번 돌려 정리한다.
    """
    n_pat = re.compile(r"^" + r"\s*".join(map(re.escape, name.replace(" ", ""))) +
                       r"\s*(?:\(주\)|㈜|주식회사)?\s*[·ㆍ,.\-:]?\s*")
    p_mark = re.compile(r"^(?:\(주\)|㈜|주식회사)\s*[^\s]{2,20}\s*[·ㆍ,.\-:]?\s*")
    p_paren = re.compile(r"^[^()\s]{1,20}\(주\)\s*[·ㆍ,.\-:]?\s*")

    t = _clean_text(title)
    for _ in range(3):
        prev = t
        for pat in (n_pat, p_mark, p_paren):
            m = pat.match(t)
            if m and len(t) - m.end() >= 3:
                t = t[m.end():].strip()
        if t == prev:
            break
    return t if len(t) >= 3 else _clean_text(title)


def fetch_disclosure(code: str, name: str, limit: int = DISC_LIMIT) -> List[List[str]]:
    """최근 공시 [[날짜 YYYY-MM-DD, 제목], ...] (최신순)."""
    url = DISC_URL.format(code=code)

    def one():
        r = http_get(url)
        if r.status_code == 404:
            return []
        if r.status_code == 429:
            raise HttpError("429 rate limited")
        if r.status_code != 200:
            raise HttpError(f"HTTP {r.status_code}")
        try:
            arr = r.json() or []
        except ValueError:
            return []
        out: List[List[str]] = []
        for it in arr:
            if len(out) >= limit:
                break
            dt = str(it.get("datetime") or "")[:10]
            title = strip_company(_clean_text(it.get("title")), name)
            if not title or not dt:
                continue
            out.append([dt, title])
        return out

    return with_retry(one, tries=3, base_delay=0.6)


def fetch_news(code: str, name: str, limit: int = NEWS_LIMIT) -> List[List[str]]:
    """최근 뉴스 [[날짜, 제목, 언론사, 'officeId/articleId'], ...] (최신순).

    응답은 비슷한 기사 묶음(list of groups)이라 평평하게 펴서 중복을 없앤다.
    종목과 무관한 기사(관련 기업 묶음)가 섞여 오므로 종목명이 제목·요약에
    나오는 기사를 먼저 채우고, 모자라면 최신순으로 채운다.
    """
    url = NEWS_URL.format(code=code)

    def one():
        r = http_get(url)
        if r.status_code == 404:
            return []
        if r.status_code == 429:
            raise HttpError("429 rate limited")
        if r.status_code != 200:
            raise HttpError(f"HTTP {r.status_code}")
        try:
            groups = r.json() or []
        except ValueError:
            return []

        seen = set()
        items: List[Dict[str, Any]] = []
        for g in groups:
            for it in (g.get("items") or []):
                iid = str(it.get("id") or (str(it.get("officeId")) + str(it.get("articleId"))))
                if iid in seen:
                    continue
                seen.add(iid)
                items.append(it)
        items.sort(key=lambda x: str(x.get("datetime") or ""), reverse=True)

        def related(it: Dict[str, Any]) -> bool:
            return name in str(it.get("title") or "") or name in str(it.get("body") or "")

        picked = [it for it in items if related(it)][:limit]
        if len(picked) < limit:
            picked += [it for it in items if not related(it)][: limit - len(picked)]

        out: List[List[str]] = []
        for it in picked:
            dt = re.sub(r"\D", "", str(it.get("datetime") or ""))[:8]
            title = _clean_text(it.get("titleFull") or it.get("title"))
            if not title or len(dt) < 8:
                continue
            press = _clean_text(it.get("officeName"))
            link = f"{it.get('officeId') or ''}/{it.get('articleId') or ''}".strip("/")
            out.append([f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}", title, press, link])
        return out

    return with_retry(one, tries=3, base_delay=0.6)


def shard_of(code: str, n: int = EXTRA_SHARDS) -> int:
    return sum(int(ch) for ch in code if ch.isdigit()) % n


# ----------------------------------------------------------------------------
# 4) 지표 계산
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


def volume_profile(rows: List[List[Any]], bins: int = BINS) -> Dict[str, Any]:
    """일별 종가·거래량 기반 매물대.

    가격축: 5년 종가 최저~최고를 bins 등분. 각 일자의 거래량을 그날 종가가 속한
    구간에 누적. POC는 최대 거래량 구간의 중심 가격.
    누적 백분위는 현재가 이하 구간의 거래량 비율(현재가가 속한 구간은 선형 보간).
    """
    closes = [r[1] for r in rows]
    vols = [r[2] for r in rows]
    total_vol = sum(vols)
    lo, hi = min(closes), max(closes)
    if total_vol <= 0:
        return {"poc": None, "vwap": None, "cum": None,
                "bin_lo": lo, "bin_hi": hi, "shares": []}

    if hi <= lo:
        step = 1.0
        hi = lo + step * bins
    else:
        step = (hi - lo) / bins

    acc = [0.0] * bins
    for c, v in zip(closes, vols):
        idx = int((c - lo) / step)
        if idx >= bins:
            idx = bins - 1
        if idx < 0:
            idx = 0
        acc[idx] += v

    shares = [round(v / total_vol * 100.0, 2) for v in acc]
    poc_idx = max(range(bins), key=lambda i: acc[i])
    poc = lo + (poc_idx + 0.5) * step
    vwap = sum(c * v for c, v in zip(closes, vols)) / total_vol
    return {
        "poc": poc,
        "vwap": vwap,
        "bin_lo": lo,
        "bin_hi": hi,
        "step": step,
        "shares": shares,
        "poc_idx": poc_idx,
    }


def vp_periods(rows: List[List[Any]], price: int,
               max_years: int = YEARS) -> Dict[str, Dict[str, Any]]:
    """1~max_years년 구간별 매물대(POC·VWAP·누적·가격범위·20구간 비중).

    구간은 최근 거래일에서 N년 전 같은 날 이후의 거래일로 자른다(달력 기준).
    상장 기간이 N년보다 짧으면 가진 거래일을 그대로 쓴다.
    """
    out: Dict[str, Dict[str, Any]] = {}
    as_of = datetime.strptime(rows[-1][0], "%Y%m%d").date()
    for y in range(1, max_years + 1):
        if y >= max_years:
            sub = rows
        else:
            try:
                cutoff = as_of.replace(year=as_of.year - y)
            except ValueError:                      # 2/29 기준일 보정
                cutoff = as_of.replace(year=as_of.year - y, day=28)
            cs = cutoff.strftime("%Y%m%d")
            sub = [r for r in rows if r[0] >= cs]
            if len(sub) < MIN_TRADING_DAYS:          # 상장 직후: 가진 만큼
                sub = rows
        vp = volume_profile(sub)
        if vp["poc"] is None:
            continue
        rec: Dict[str, Any] = {
            "poc": int(round(vp["poc"])),
            "vwap": int(round(vp["vwap"])),
            "bl": int(round(vp["bin_lo"])),
            "bh": int(round(vp["bin_hi"])),
            "bins": [round(x, 1) for x in vp["shares"]],
        }
        cum = cum_below(price, vp)
        if cum is not None:
            rec["cum"] = int(round(cum))
        out[str(y)] = rec
    return out


def cum_below(price: Optional[float], vp: Dict[str, Any]) -> Optional[float]:
    """현재가 이하 가격대에 쌓인 거래량 비율(%)."""
    if price is None or not vp["shares"]:
        return None
    lo, hi, step = vp["bin_lo"], vp["bin_hi"], vp.get("step") or 0
    if step <= 0:
        return None
    if price < lo:
        return 0.0
    if price >= hi:
        return 100.0
    pos = (price - lo) / step
    idx = int(pos)
    frac = pos - idx
    cum = sum(vp["shares"][:idx]) + vp["shares"][idx] * frac
    return round(min(100.0, max(0.0, cum)), 1)


# ----------------------------------------------------------------------------
# 5) 종목 단위 수집
# ----------------------------------------------------------------------------

def build_stock(row: Dict[str, Any], start: str, end: str,
                want_finance: bool = True, want_extras: bool = True) -> Dict[str, Any]:
    """종목 1개 수집·계산. 결과 딕셔너리에 k200(코스피200 여부)·chg(전일 대비 %)를 담는다."""
    rows = fetch_daily(row["code"], start, end)
    if len(rows) < MIN_TRADING_DAYS:
        raise HttpError(f"거래일 부족({len(rows)}일)", retryable=False)

    rows.sort(key=lambda r: r[0])
    price = int(round(rows[-1][1]))
    vps = vp_periods(rows, price)
    closes = [r[1] for r in rows]

    # 전일(직전 거래일) 종가 대비 등락률(%) — 소수점 2자리
    chg: Optional[float] = None
    if len(rows) >= 2 and rows[-2][1] > 0:
        chg = round((rows[-1][1] / rows[-2][1] - 1.0) * 100.0, 2)

    data: Dict[str, Any] = {
        "c": row["code"],
        "n": row["name"],
        "m": "K" if row["market"] == "KOSPI" else "Q",
        "p": price,
        "mc": row["marcap_eok"],
        "nd": len(rows),
    }
    if chg is not None:
        data["chg"] = chg
    if row.get("k200"):
        data["k200"] = 1
    data["_last"] = rows[-1][0][:8]          # 최근 거래일 (기준일 산정 후 정리)

    rsi = rsi_wilder(closes)
    if rsi is not None:
        data["rsi"] = round(rsi, 1)

    if vps:
        data["vp"] = vps                      # {"1": {...}, ..., "5": {...}}
        base = vps.get(str(YEARS)) or vps[max(vps, key=int)]
        data["poc"], data["vwap"] = base["poc"], base["vwap"]   # 기본(최대 기간) 값 하위 호환
        if "cum" in base:
            data["cum"] = base["cum"]

    if want_extras:
        extras: Dict[str, Any] = {}
        try:
            d = fetch_disclosure(row["code"], row["name"])
            if d:
                extras["d"] = d
        except Exception as exc:  # noqa: BLE001 - 공시 실패는 종목 수집을 막지 않는다
            print(f"  ! 공시 실패 {row['code']} {row['name']}: {exc}", flush=True)
        try:
            n = fetch_news(row["code"], row["name"])
            if n:
                extras["n"] = n
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 뉴스 실패 {row['code']} {row['name']}: {exc}", flush=True)
        if extras:
            data["_x"] = extras

    if want_finance:
        fin = fetch_finance(row["code"])
        fy = fin["fy"]
        # 배당: 최근 확정 사업연도 주당배당금 + 현재가 기준 배당수익률
        if fin.get("dps"):
            data["dps"] = int(fin["dps"])
            data["dvy"] = int(fin["dvy"])
            if price > 0:
                data["dy"] = round(fin["dps"] / price * 100, 2)
        if fy:
            # [연도, 영업이익, 당기순이익]  (억원)
            data["fin"] = [[y, _r(op), _r(ni)] for (y, _m, op, ni) in fy]
            if fin["fm"] != 12:
                data["fm"] = fin["fm"]
            ops = [x[1] for x in data["fin"]]
            nis = [x[2] for x in data["fin"]]
            op_all = len(ops) == 3 and all(v is not None for v in ops)
            ni_all = len(nis) == 3 and all(v is not None for v in nis)
            if op_all:
                data["op3"] = 1 if all(v > 0 for v in ops) else 0
            if ni_all:
                data["ni3"] = 1 if all(v > 0 for v in nis) else 0
            last_op = ops[-1] if ops else None
            if last_op and last_op > 0 and row["marcap_eok"] > 0:
                mult = row["marcap_eok"] / last_op
                data["mult"] = round(mult, 1)
                data["pass"] = 1 if mult < 10 else 0
    return data


def _r(v: Optional[float]) -> Optional[int]:
    return None if v is None else int(round(v))


# ----------------------------------------------------------------------------
# 6) 메인
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="국내주식 매물대 스크리너 데이터 생성")
    ap.add_argument("--limit", type=int, default=0, help="시총 상위 N개만 (테스트용)")
    ap.add_argument("--codes", type=str, default="", help="쉼표로 구분한 종목코드만 수집")
    ap.add_argument("--workers", type=int, default=8, help="동시 요청 스레드 수 (기본 8)")
    ap.add_argument("--years", type=int, default=YEARS, help="매물대 기간(년), 기본 5")
    ap.add_argument("--out", type=str, default=DEFAULT_OUT, help="출력 JSON 경로")
    ap.add_argument("--no-finance", action="store_true", help="재무 수집 생략(속도 테스트)")
    ap.add_argument("--no-extras", action="store_true", help="최근 공시·뉴스 수집 생략(속도 테스트)")
    ap.add_argument("--end", type=str, default="", help="기준일 YYYYMMDD (기본: 오늘)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--resume", action="store_true",
                    help="같은 기준일의 수집 캐시(.cache/)가 있으면 이어서 수집")
    args = ap.parse_args()

    t_start = time.time()
    lock = threading.Lock()

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    today = date.today()
    end = args.end or today.strftime("%Y%m%d")
    start = (datetime.strptime(end, "%Y%m%d").date()
             - timedelta(days=365 * args.years)).strftime("%Y%m%d")

    log(f"[1/4] 코스피200 구성종목 수집")
    k200: Dict[str, str] = {}
    try:
        k200 = fetch_kospi200(log)
    except Exception as exc:  # noqa: BLE001 - 지수 목록 실패는 전체 수집을 막지 않는다
        log(f"  ! 코스피200 수집 실패: {exc} (k200 플래그 없이 진행)")

    log(f"[2/4] 상장 종목 수집 (기간 {start}~{end})")
    universe = load_universe(log, k200)

    if args.codes:
        wanted = {c.strip() for c in args.codes.split(",") if c.strip()}
        universe = [u for u in universe if u["code"] in wanted]
    if args.limit and args.limit > 0:
        universe = universe[: args.limit]
    log(f"      대상 종목 {len(universe):,}개 (코스피 {sum(1 for u in universe if u['market']=='KOSPI'):,} / "
        f"코스닥 {sum(1 for u in universe if u['market']=='KOSDAQ'):,})")
    if not universe:
        log("대상 종목이 없습니다.")
        return 1

    results: Dict[str, Dict[str, Any]] = {}
    failed: List[Dict[str, str]] = []
    done = 0
    t0 = time.time()

    # --- 수집 캐시 (중단 후 이어서 실행 / 반복 실행 시 재사용) -----------------
    cache_dir = os.path.join(BASE_DIR, ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{end}.jsonl")
    if args.resume and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    results[rec["c"]] = rec
                except Exception:  # noqa: BLE001
                    continue
        log(f"      캐시에서 {len(results):,}종목 로드")
        universe_codes = {u["code"] for u in universe}
        results = {k: v for k, v in results.items()
                   if k in universe_codes and (args.no_extras or "_x" in v)}
    cache_f = open(cache_path, "a" if args.resume else "w", encoding="utf-8")

    pending = [u for u in universe if u["code"] not in results]
    log(f"[3/4] 일별 시세·재무·공시·뉴스 수집 시작 (대상 {len(pending):,}개, workers={args.workers})")
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {
            ex.submit(build_stock, u, start, end, not args.no_finance, not args.no_extras): u
            for u in pending
        }
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
                    failed.append({"code": u["code"], "name": u["name"],
                                   "reason": f"{type(exc).__name__}: {exc}"[:200]})
            if done % 100 == 0 or done == len(pending):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(pending) - done) / rate if rate > 0 else 0
                log(f"      {done:,}/{len(pending):,} 완료 · 성공 {len(results):,} · "
                    f"실패 {len(failed):,} · {rate:.1f}건/초 · 경과 {elapsed/60:.1f}분 · "
                    f"남은 예상 {eta/60:.1f}분")
    cache_f.close()

    log(f"[4/4] 결과 저장")

    # 상세 화면용 공시·뉴스는 종목별로 떼어내 16개 파일로 나눠 저장 (지연 로드)
    extras: Dict[str, Dict[str, Any]] = {}
    for code, d in results.items():
        x = d.pop("_x", None)
        if x:
            extras[code] = x

    stocks = sorted(results.values(), key=lambda d: d.get("mc", 0), reverse=True)

    # 기준일 = 가장 많은 종목의 최근 거래일 (휴장일·주말 보정)
    last_dates = [d.pop("_last", end) for d in stocks]
    as_of = end
    if last_dates:
        counts: Dict[str, int] = {}
        for d_ in last_dates:
            counts[d_] = counts.get(d_, 0) + 1
        as_of = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    for d, ld in zip(stocks, last_dates):
        if ld and ld != as_of:
            d["ld"] = ld          # 거래정지 등 기준일과 다른 최근 거래일

    # 코스피200 플래그는 이번 실행에서 받은 지수 구성종목 목록으로 다시 맞춘다
    # (--resume 으로 캐시를 재사용해도 최신 목록이 반영되도록).
    if k200:
        for d in stocks:
            if d["c"] in k200:
                d["k200"] = 1
            else:
                d.pop("k200", None)
    k200_count = sum(1 for d in stocks if d.get("k200"))
    log(f"코스피200 표시 종목 {k200_count:,}개 · 등락률 있는 종목 "
        f"{sum(1 for d in stocks if d.get('chg') is not None):,}개")

    payload = {
        "meta": {
            "as_of": f"{as_of[:4]}-{as_of[4:6]}-{as_of[6:8]}",
            "generated_at": datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"),
            "count": len(stocks),
            "failed": len(failed),
            "years": args.years,
            "periods": [p for p in PERIODS if p <= args.years],
            "bins": BINS,
            "rsi_period": RSI_PERIOD,
            "k200_count": k200_count,
            "source": "네이버 금융(일별 시세·연간 실적·배당·공시·뉴스·코스피200 구성종목)·KRX 상장 목록(FinanceDataReader)",
            "universe": "코스피·코스닥 보통주 (ETF·ETN·스팩·리츠·우선주 제외)",
            "units": {"price": "원", "marcap": "억원", "fin": "억원", "dps": "원", "dy": "%"},
            "extra": {"shards": EXTRA_SHARDS, "disc": DISC_LIMIT, "news": NEWS_LIMIT},
            "notes": [
                "매물대는 최근 1~5년 일별 종가·거래량을 가격 20구간으로 나눠 누적한 분포입니다(화면에서 기간 전환).",
                "누적 백분위는 현재가 이하 가격대에 쌓인 거래량 비율(낮을수록 매물대 하단)입니다.",
                "평균단가는 선택한 기간 일별 종가의 거래량 가중평균(VWAP)입니다.",
                "RSI(14)는 일별 종가 기준 와일더 방식입니다.",
                "영업이익·순이익은 네이버 금융 연간 확정 실적(최근 3개 사업연도)입니다.",
                "배당은 최근 확정 사업연도 주당배당금(dps)과 그 배당금을 현재가로 나눈 배당수익률(dy)입니다.",
                "등락률(chg)은 일별 종가의 최근 두 거래일(전일 종가 대비) 기준이며, 코스피200(k200)은 네이버 금융 코스피200 구성종목 기준입니다.",
                "최근 공시·뉴스는 상세 화면에서만 지연 로드하며(data/extras/{n}.json), 네이버 금융 제공 기준입니다.",
            ],
        },
        "stocks": stocks,
    }

    out_path = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out_path)

    failed_path = os.path.join(os.path.dirname(os.path.abspath(out_path)), "failed.json")
    with open(failed_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": payload["meta"]["generated_at"],
                   "count": len(failed), "failed": failed},
                  f, ensure_ascii=False, indent=1)

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    elapsed = time.time() - t_start
    log(f"저장 완료: {out_path} ({len(stocks):,}종목, {size_mb:.2f} MB)")

    # 상세용 공시·뉴스: 16개 샤드 파일 (빈 샤드도 써서 404를 만들지 않는다)
    extra_dir = os.path.join(os.path.dirname(os.path.abspath(out_path)), "extras")
    os.makedirs(extra_dir, exist_ok=True)
    shards: List[Dict[str, Dict[str, Any]]] = [{} for _ in range(EXTRA_SHARDS)]
    for code, x in extras.items():
        shards[shard_of(code)][code] = x
    extra_bytes = 0
    for i, sh in enumerate(shards):
        p = os.path.join(extra_dir, f"{i}.json")
        tmp_p = p + ".tmp"
        with open(tmp_p, "w", encoding="utf-8") as f:
            json.dump(sh, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_p, p)
        extra_bytes += os.path.getsize(p)
    with_disc = sum(1 for x in extras.values() if x.get("d"))
    with_news = sum(1 for x in extras.values() if x.get("n"))
    log(f"상세 데이터: {extra_dir} ({len(extras):,}종목, {extra_bytes/1024/1024:.2f} MB, "
        f"공시 있음 {with_disc:,} · 뉴스 있음 {with_news:,})")

    # GitHub Actions: 이어지는 '데이터 커밋' 단계는 `git add docs/data/stocks.json
    # docs/data/failed.json` 만 하고 인덱스 전체를 커밋한다. 워크플로우 파일을 고치지
    # 않고도 상세 데이터가 함께 커밋되도록, CI 에서만 extras 를 미리 스테이징한다.
    if extras and os.environ.get("GITHUB_ACTIONS") == "true":
        rel = os.path.relpath(extra_dir, BASE_DIR)
        if not rel.startswith(".."):
            try:
                import subprocess
                subprocess.run(["git", "add", "--", rel], cwd=BASE_DIR,
                               check=False, timeout=120)
                log(f"CI: {rel} 스테이징 완료")
            except Exception as exc:  # noqa: BLE001 - 커밋 단계는 워크플로우가 처리
                log(f"CI: {rel} 스테이징 생략 ({exc})")
    log(f"실패 로그: {failed_path} ({len(failed)}건)")
    log(f"총 소요 {elapsed/60:.1f}분")

    if failed:
        log("실패 사유 상위:")
        reasons: Dict[str, int] = {}
        for f_ in failed:
            key = f_["reason"].split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
        for k, v in sorted(reasons.items(), key=lambda x: -x[1])[:6]:
            log(f"  - {k}: {v}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
