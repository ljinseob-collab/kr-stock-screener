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

# 주요 수익원(기업개요·주요제품 매출구성·홈페이지) — WISEfn 기업모니터
WISE_MAIN_URL = "https://comp.wisereport.co.kr/company/c1010001.aspx?cmp_cd={code}"
WISE_PROD_URL = "https://comp.wisereport.co.kr/company/c1020001.aspx?cmp_cd={code}"
WISE_REFERER = "https://comp.wisereport.co.kr/"
# 업종 분류 — 네이버 금융 업종 목록(79개 업종, 100종목/페이지)
NAVER_IND_LIST_URL = "https://m.stock.naver.com/api/stocks/industry?page={page}&pageSize=100"
NAVER_IND_STOCKS_URL = "https://m.stock.naver.com/api/stocks/industry/{no}?page={page}&pageSize=100"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(BASE_DIR, "docs", "data", "stocks.json")
# 기업개요 캐시: 한 번 받은 기업개요·주요제품·업종을 저장해 두고 신규/미보유 종목만 다시 받는다.
PROFILE_CACHE_PATH = os.path.join(BASE_DIR, "scripts", "kr_profile_cache.json")
PROFILE_VER = 3           # 파생 규칙이 바뀌면 올린다(캐시 전체 재수집)

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
             timeout: int = 25, ua: Optional[str] = None) -> requests.Response:
    headers = {
        "User-Agent": ua or (UA_PC if "api.finance.naver.com" in url else UA_MOBILE),
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
        try:
            shares = float(rec.get("Stocks") or 0)
        except (TypeError, ValueError):
            shares = 0.0
        if math.isnan(shares):
            shares = 0.0
        try:                                  # 오늘처럼 Close 가 '-' 로 오는 날이 있다
            close_v = float(str(close).strip()) if close is not None else 0.0
            if math.isnan(close_v) or close_v <= 0:
                close_v = 0.0
        except (TypeError, ValueError):
            close_v = 0.0
        # Marcap 이 비어 오는 날(데이터 소스 일시 장애)이 있다. 상장주식수가 있으면
        # 통과시켜 두고, 일별 종가를 받은 뒤 시총을 계산한다(build_stock).
        if (not marcap or marcap <= 0 or math.isnan(marcap)) and shares <= 0:
            excluded["no_price"] += 1
            continue

        marcap_eok = int(round(marcap / 1e8)) if (marcap and not math.isnan(marcap) and marcap > 0) else 0
        rows.append({
            "code": code,
            "name": name,
            "market": "KOSPI" if market == "KOSPI" else "KOSDAQ",
            "marcap_eok": marcap_eok,                        # 원 → 억원 (0이면 build_stock에서 계산)
            "shares": shares,
            "listing_close": int(close_v) if close_v else None,
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
# 3-c) 주요 수익원 · 사업 구조 (기업개요·주요제품 매출구성·업종·홈페이지)
# ----------------------------------------------------------------------------
#
# 국내 종목은 미국 탭처럼 증권사 요약문이 따로 없어서, WISEfn 기업모니터의
# '기업현황'(기업개요 서술 + 홈페이지)과 '기업개요'(주요제품 매출구성)를 합쳐
# 한 줄 수익원(revShort)과 상세 설명(revDetail)을 만든다. 2,500종목을 매일
# 처음부터 받으면 시간이 걸리므로 scripts/kr_profile_cache.json 에 캐시하고
# 신규/미보유 종목만 받는다. 업종은 네이버 금융 업종 분류를 쓴다.

# 지역 구분도 수익원이 아니다
REGION_WORDS = {
    "내수", "수출", "국내", "해외", "북중미", "유럽", "아시아", "중국", "미국", "일본",
    "국내외", "기타지역", "아메리카", "중남미", "중동", "아프리카", "오세아니아",
}


def _norm_label(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s or "").lower()


# 수익원 문구로 쓰기 어려운 포괄 상품명
GENERIC_PRODUCT = {
    "제품", "상품", "용역", "서비스", "기타", "매출", "매출액", "상품매출", "제품매출",
    "용역매출", "영업수익", "임대", "투자", "로열티", "수수료", "연결조정", "내부거래제거",
    "기타매출", "기타수익", "사업", "사업부문", "기타사업", "기타부문", "운영", "판매", "제조",
    "제품판매", "상품판매", "용역제공", "배당수익", "배당금수익", "로열티수익", "임대수익",
    "브랜드수익", "상표권수익", "corporate account", "기타수익",
    "도소매", "제품 및 상품매출", "제품및상품매출", "상품 및 제품매출",
    # 금융업 매출구성은 사업이 아니라 계정 과목이라 수익원으로 쓰지 않는다
    "이자수익", "수수료수익", "유가증권평가및처분이익", "유가증권평가및매도이익",
    "외환거래이익", "보험료수익", "신탁업무운용수익", "창업투자및기업구조조정수익",
    "배당금수익", "기타영업수익", "이자비용", "보험금수익", "투자수익", "평가이익",
}
GENERIC_NORM = {_norm_label(x) for x in GENERIC_PRODUCT}
# 상품명 축약
PRODUCT_SYNONYM = {
    "차량": "자동차", "전지": "2차전지", "이차전지": "2차전지",
    "A·S": "AS", "A/S": "AS",
}
# 업종명이 너무 길거나 어색한 것만 짧게 (나머지는 '와/과/및' → '·' 로 다듬는다)
INDUSTRY_SHORT = {
    "반도체와반도체장비": "반도체",
    "디스플레이장비및부품": "디스플레이 부품",
    "디스플레이패널": "디스플레이 패널",
    "전자장비와기기": "전자부품",
    "전자제품": "전자제품",
    "IT서비스": "IT서비스",
    "컴퓨터와주변기기": "컴퓨터·주변기기",
    "통신장비": "통신장비",
    "무선통신서비스": "통신서비스",
    "다각화된통신서비스": "통신서비스",
    "생명과학도구및서비스": "바이오 장비·서비스",
    "건강관리장비와용품": "의료기기",
    "건강관리업체및서비스": "의료서비스",
    "건강관리기술": "디지털 헬스",
    "생명보험": "생명보험", "손해보험": "손해보험",
    "복합기업": "지주회사",
    "판매업체": "유통",
    "무역회사와판매업체": "무역·유통",
    "상업서비스와공급품": "상업서비스",
    "다각화된소비자서비스": "소비자서비스",
    "인터넷과카탈로그소매": "온라인 유통",
    "식품과기본식료품소매": "식품 유통",
    "백화점과일반상점": "백화점·유통",
    "전문소매": "전문소매",
    "항공화물운송과물류": "물류",
    "도로와철도운송": "운송",
    "운송인프라": "운송인프라",
    "방송과엔터테인먼트": "방송·엔터",
    "양방향미디어와서비스": "인터넷 서비스",
    "게임엔터테인먼트": "게임",
    "호텔,레스토랑,레저": "호텔·레저",
    "섬유,의류,신발,호화품": "의류·패션",
    "가정용기기와용품": "생활가전",
    "가정용품": "생활용품",
    "에너지장비및서비스": "에너지 장비",
    "석유와가스": "석유·가스",
    "전기유틸리티": "전력",
    "가스유틸리티": "가스",
    "복합유틸리티": "에너지·유틸리티",
    "우주항공과국방": "항공우주·국방",
    "레저용장비와제품": "레저용품",
    "사무용전자제품": "사무기기",
    "포장재": "포장재",
    "종이와목재": "제지·목재",
    "문구류": "문구",
    "건축제품": "건축자재",
    "건축자재": "건축자재",
    "출판": "출판",
    "교육서비스": "교육",
    "광고": "광고",
    "화장품": "화장품",
    "담배": "담배",
    "음료": "음료", "식품": "식품",
    "제약": "제약",
    "생물공학": "바이오",
    "은행": "은행", "증권": "증권", "카드": "카드", "창업투자": "벤처투자",
    "기타금융": "금융",
    "부동산": "부동산",
    "해운사": "해운",
    "항공사": "항공",
    "조선": "조선",
    "건설": "건설",
    "철강": "철강", "비철금속": "비철금속",
    "화학": "화학",
    "기계": "기계",
    "전기장비": "전기장비", "전기제품": "전기제품",
    "자동차": "자동차", "자동차부품": "자동차부품",
    "소프트웨어": "소프트웨어",
    "핸드셋": "스마트폰·단말기",
    "가구": "가구",
    "기타": "",
}


# 대형·눈에 띄는 종목은 한 줄 수익원을 직접 다듬는다(자동 추출이 어색한 경우만).
# {종목코드: (한 줄 수익원, 상세 설명 또는 None)} — 상세를 비우면 자동 문구를 쓴다.
COMPANY_REV: Dict[str, Tuple[str, Optional[str]]] = {
    "373220": ("2차전지(EV·ESS)", None),          # LG에너지솔루션
    "009150": ("MLCC · 카메라모듈 · 기판", None),   # 삼성전기
    "034730": ("에너지·통신 지주", None),          # SK
    "034020": ("발전설비 · 건설기계", None),         # 두산에너빌리티
    "066570": ("가전 · TV · 전장부품", None),       # LG전자
    "010120": ("전력기기 · 자동화", None),          # LS ELECTRIC
    "096770": ("정유 · 화학 · 배터리", None),       # SK이노베이션
    "003670": ("양극재·음극재 · 내화물", None),     # 포스코퓨처엠
    "028050": ("플랜트 건설(EPC)", None),         # 삼성E&A
    "180640": ("지주(대한항공·호텔)", None),       # 한진칼
    "000150": ("지주(건설기계·발전·전자소재)", None),  # 두산
    "454910": ("협동로봇", None),                  # 두산로보틱스
    "064350": ("철도차량 · 방산", None),           # 현대로템
    "000880": ("방산 · 태양광 · 금융", None),       # 한화
    "012450": ("조선 · 방산 · 항공엔진", None),     # 한화에어로스페이스
    "108860": ("AI 음성인식 · 의료 AI", None),     # 셀바스AI
}


def _clean_html(s: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))).strip()


def _industry_short(name: str) -> str:
    """업종명 → 한 줄 수익원 대체 문구. 사전에 없으면 '와/과/및'만 다듬는다."""
    name = (name or "").strip()
    if not name:
        return ""
    if name in INDUSTRY_SHORT:
        return INDUSTRY_SHORT[name]
    return re.sub(r"\s*(?:와|과|및)\s*", "·", name)


def _product_label(raw: str) -> str:
    """'DS(반도체 등)' → '반도체', '의류, 잡화(상품)' → '의류·잡화' 처럼 수익원 키워드로 다듬는다."""
    s = _clean_html(raw).replace("·", ",")
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*(.*)$", s)
    if m:
        outer = re.sub(r"\s*등$", "", m.group(1).strip()).strip()
        inner_core = re.sub(r"\s*등$", "", m.group(2).strip()).strip()
        if outer in REGION_WORDS:
            return ""            # '북중미(KUS)' 같은 지역 구분은 통째로 뺀다
        # 바깥이 포괄 상품명이거나 DS·DX 같은 짧은 영문 약어이면 괄호 안을 쓴다
        outer_is_code = (not re.search("[가-힣]", outer)
                         and bool(re.fullmatch(r"[A-Za-z0-9&.\- ]{1,8}", outer)))
        if outer_is_code or outer in GENERIC_PRODUCT or _norm_label(outer) in GENERIC_NORM:
            s = inner_core
        elif inner_core in GENERIC_PRODUCT or inner_core == "등":
            s = outer
        else:
            s = outer
    s = re.sub(r"\s*등$", "", s)
    s = re.sub(r"\s*(?:사업)?부문$", "", s).strip()
    s = re.sub(r"\(주\)|㈜", "", s)
    s = re.sub(r"\s*[,/]\s*", "·", s)
    s = re.sub(r"\s+", " ", s).strip(" ·-")
    s = re.sub("^(" + "|".join(REGION_WORDS) + r")\s+", "", s)   # '국내 판재' → '판재'
    s = s.replace("A/S", "AS").replace("A·S", "AS")
    s = PRODUCT_SYNONYM.get(s, s)
    s = re.sub(r"\s*(?:外|외)$", "", s).strip()
    if re.search(r"(제품|상품)\s*(?:및|·)?\s*(?:상품|제품)?\s*매출", s):
        return ""
    if s in REGION_WORDS or s in GENERIC_PRODUCT or _norm_label(s) in GENERIC_NORM:
        return ""
    if len(s) > 22:          # 너무 긴 상품 설명은 한 줄 수익원으로 쓰기 어렵다
        head = re.split(r"\s*[&,]\s*", s)[0].strip()
        s = head if 2 <= len(head) <= 22 else ""
    if len(s) < 2:
        return ""
    return s


def _pick_products(prods: List[List[Any]], company: str = "", max_items: int = 3,
                   max_len: int = 28, min_share: float = 4.0
                   ) -> List[Tuple[str, float, float]]:
    """주요제품 매출구성에서 한 줄 수익원으로 쓸 항목을 고른다.

    같은 이름('의약품(제품)'·'의약품(상품)')은 합산하고, 합계가 100%를 넘으면
    가장 큰 단일 항목 비중을 쓴다(내부거래가 섞여 100%를 넘는 경우가 있다).
    반환: [(상품명, 표시 비중, 합산 비중)]
    """
    merged: List[Tuple[str, float, float]] = []
    idx: Dict[str, int] = {}
    for name, share in prods or []:
        try:
            share = float(share)
        except (TypeError, ValueError):
            continue
        if share <= 0:
            continue
        label = _product_label(str(name))
        if not label:
            continue
        if label in idx:
            i = idx[label]
            merged[i] = (label, merged[i][1] + share, max(merged[i][2], share))
        else:
            idx[label] = len(merged)
            merged.append((label, share, share))
    merged.sort(key=lambda x: -x[1])
    # 상품명이 회사 이름 그대로인 경우(예: 'LS ELECTRIC')는 수익원이 아니므로 뺀다
    norm_company = re.sub(r"[^0-9A-Za-z가-힣]", "", company or "").lower()
    if norm_company:
        merged = [m for m in merged
                  if re.sub(r"[^0-9A-Za-z가-힣]", "", m[0]).lower() != norm_company]
    if not merged or sum(s for _, s, _ in merged[:3]) < 25:
        # 대부분의 매출이 '제품·상품매출' 같은 포괄 항목이라 수익원을 특정할 수 없다
        return []
    picked: List[Tuple[str, float, float]] = []
    total = 0
    for label, share, single in merged:
        if picked and share < min_share:
            break
        add = len(label) if not picked else len(label) + 3
        if picked and total + add > max_len:
            break
        picked.append((label, share, single))
        total += add
        if len(picked) >= max_items:
            break
    return picked


def parse_wise_profile(main_html: str, prod_html: str) -> Dict[str, Any]:
    """WISEfn 기업현황(기업개요·홈페이지) + 기업개요(주요제품 매출구성) 파싱."""
    out: Dict[str, Any] = {}
    # 홈페이지: 기업개요 페이지(c1020001)의 표 행, 없으면 기업현황(c1010001) 머리말 아이콘
    for page_html in (prod_html, main_html):
        if not page_html or out.get("w"):
            continue
        m = re.search(r"<th[^>]*>\s*홈페이지\s*</th>(.*?)</tr>", page_html, re.S)
        if not m:
            m = re.search(r'title="새창열기:\[홈페이지\][^"]*"[^>]*href="([^"]+)"', page_html)
            if m:
                w = m.group(1).strip()
                if w.startswith("http"):
                    out["w"] = w
            continue
        a = re.search(r'href="([^"]+)"', m.group(1))
        if a and a.group(1).strip().startswith("http"):
            out["w"] = a.group(1).strip()
    if main_html:
        m = re.search(r'<div class="cmp_comment">(.*?)</div>', main_html, re.S)
        if m:
            items = re.findall(r'<li[^>]*class="dot_cmp"[^>]*>(.*?)</li>', m.group(1), re.S)
            bullets = [_clean_html(x) for x in items]
            bullets = [b for b in bullets if len(b) >= 10][:3]
            if bullets:
                out["b"] = bullets
    if prod_html:
        m = re.search(r'<table id="cTB203".*?</table>', prod_html, re.S)
        if m:
            prods: List[List[Any]] = []
            for row in re.findall(r"<tr>(.*?)</tr>", m.group(0), re.S):
                th = re.search(r"<th[^>]*>(.*?)</th>", row, re.S)
                td = re.search(r"<td[^>]*>(.*?)</td>", row, re.S)
                if not th or not td:
                    continue
                name = _clean_html(th.group(1))
                try:
                    share = float(_clean_html(td.group(1)))
                except ValueError:
                    continue
                if name and share > 0:
                    prods.append([name, round(share, 2)])
            if prods:
                out["p"] = prods[:6]
    return out


def fetch_wise_profile(code: str) -> Dict[str, Any]:
    """종목 1개의 기업개요·주요제품·홈페이지. 페이지가 없으면 {'none': 1}."""
    def one() -> Dict[str, Any]:
        r1 = http_get(WISE_MAIN_URL.format(code=code), referer=WISE_REFERER,
                      timeout=30, ua=UA_PC)
        if r1.status_code == 429:
            raise HttpError("WISE 429 rate limited")
        if r1.status_code == 404:
            return {"none": 1}
        if r1.status_code != 200:
            raise HttpError(f"WISE 기업현황 HTTP {r1.status_code}")
        r2 = http_get(WISE_PROD_URL.format(code=code), referer=WISE_REFERER,
                      timeout=30, ua=UA_PC)
        if r2.status_code == 429:
            raise HttpError("WISE 429 rate limited")
        prod_html = r2.text if r2.status_code == 200 else ""
        return parse_wise_profile(r1.text, prod_html)

    try:
        data = with_retry(one, tries=3, base_delay=1.0)
    except Exception as exc:  # noqa: BLE001 - 개별 종목 실패는 전체를 막지 않는다
        return {"err": f"{type(exc).__name__}: {exc}"[:120]}
    return data


def fetch_industry_map(log) -> Dict[str, str]:
    """네이버 금융 업종 분류 {종목코드: 업종명}. 79개 업종을 페이지 단위로 훑는다."""
    groups: List[Dict[str, Any]] = []
    page = 1
    while page <= 3:
        def one(p=page):
            r = http_get(NAVER_IND_LIST_URL.format(page=p), timeout=20)
            if r.status_code != 200:
                raise HttpError(f"HTTP {r.status_code}")
            d = r.json()
            return d.get("groups") or [], int(d.get("totalCount") or 0)
        try:
            got, total = with_retry(one, tries=3, base_delay=0.7)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! 업종 목록 {page}페이지 실패: {exc}")
            break
        groups.extend(got)
        if not got or len(groups) >= total:
            break
        page += 1

    out: Dict[str, str] = {}
    for g in groups:
        no, name = g.get("no"), str(g.get("name") or "")
        if not no or not name:
            continue
        p = 1
        while p <= 5:
            def one(page_no=no, page_p=p):
                r = http_get(NAVER_IND_STOCKS_URL.format(no=page_no, page=page_p), timeout=20)
                if r.status_code != 200:
                    raise HttpError(f"HTTP {r.status_code}")
                return r.json()
            try:
                d = with_retry(one, tries=2, base_delay=0.6)
            except Exception:  # noqa: BLE001 - 업종 하나 실패는 넘어간다
                break
            rows = d.get("stocks") or []
            for s in rows:
                code = str(s.get("itemCode") or "").strip()
                if code:
                    out.setdefault(code, name)
            total = int(d.get("totalCount") or 0)
            if not rows or p * 100 >= total:
                break
            p += 1
    log(f"업종 분류 {len(out):,}종목 수신")
    return out


def _short_sentence(text: str, limit: int = 150) -> str:
    """문장 하나를 limit 자 안으로 자른다(문장 경계 우선)."""
    t = re.sub(r"\s+", " ", text or "").strip()
    t = t.replace("동사는", "이 회사는").replace("당사는", "이 회사는")
    t = t.replace("동사", "이 회사").replace("당사", "이 회사")
    if len(t) <= limit:
        return t
    parts = re.split(r"(?<=[.!?])\s+", t)
    out = ""
    for part in parts:
        if out and len(out) + len(part) + 1 > limit:
            break
        out = (out + " " + part).strip()
    if not out or len(out) < 30:
        out = t[:limit].rsplit(" ", 1)[0]
        return out.rstrip(" ,·") + "…"
    return out


def rev_text(entry: Dict[str, Any], industry: str, code: str = "",
             company: str = "") -> Tuple[str, str]:
    """기업개요 캐시 1건 → (한 줄 수익원, 상세 설명)."""
    prods = entry.get("p") or []
    picked = _pick_products(prods, company)
    ind_short = _industry_short(industry)
    short = " · ".join(label for label, _, _ in picked) if picked else ind_short

    sents: List[str] = []
    for b in (entry.get("b") or [])[:2]:
        s = _short_sentence(b)
        if s:
            sents.append(s.rstrip(".") + ".")
    detail = " ".join(sents)

    if picked:
        def shown(share: float, single: float) -> float:
            return share if share <= 100.0 else single
        mix = "매출 비중은 " + " · ".join(
            f"{label} {shown(share, single):.1f}%" for label, share, single in picked) + "입니다."
        detail = (detail + " " + mix).strip() if detail else mix

    if not detail:
        if ind_short:
            detail = f"{ind_short} 업종으로, 주요 수익원은 {short or '사업 다각화'}입니다."
        elif short:
            detail = f"주요 수익원은 {short}입니다."
    if len(detail) > 420:
        cut = detail[:420]
        for sep in (". ", "다. "):
            i = cut.rfind(sep)
            if i > 200:
                cut = cut[: i + 1]
                break
        detail = cut.rstrip()
    if not short:
        short = "사업 다각화"
    ov = COMPANY_REV.get(code)
    if ov:
        short = ov[0]
        if ov[1]:
            detail = ov[1]
    return short, detail


def load_profile_cache() -> Dict[str, Dict[str, Any]]:
    try:
        with open(PROFILE_CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        d.pop("_v", None)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 - 캐시가 없거나 깨지면 새로 받는다
        return {}


def save_profile_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    payload: Dict[str, Any] = {"_v": PROFILE_VER}
    for k in sorted(cache):
        payload[k] = cache[k]
    os.makedirs(os.path.dirname(PROFILE_CACHE_PATH), exist_ok=True)
    tmp = PROFILE_CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, PROFILE_CACHE_PATH)


def apply_profiles(results: Dict[str, Dict[str, Any]], log,
                   workers: int = 6) -> None:
    """수집 결과에 주요 수익원(revShort)·상세(revDetail)·홈페이지(w)·업종(s)을 붙인다."""
    codes = [c for c, d in results.items()]
    if not codes:
        return
    cache = load_profile_cache()
    missing = [c for c in codes if cache.get(c, {}).get("v", 0) < PROFILE_VER]
    log(f"      기업개요 캐시 {len(cache):,}건 · 새로 받을 종목 {len(missing):,}개")

    industry: Dict[str, str] = {}
    if missing:
        try:
            industry = fetch_industry_map(log)
        except Exception as exc:  # noqa: BLE001 - 업종 없이도 수익원 문구는 만든다
            log(f"  ! 업종 분류 수집 실패: {exc}")

    if missing:
        t0 = time.time()
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = {ex.submit(fetch_wise_profile, c): c for c in missing}
            for fut in as_completed(futs):
                code = futs[fut]
                done += 1
                try:
                    entry = fut.result()
                except Exception as exc:  # noqa: BLE001
                    entry = {"err": f"{type(exc).__name__}: {exc}"[:120]}
                if entry.get("err"):
                    continue                      # 실패는 캐시에 남기지 않고 다음 실행에서 재시도
                entry["v"] = PROFILE_VER
                if industry.get(code):
                    entry["ind"] = industry[code]
                cache[code] = entry
                if done % 200 == 0:
                    save_profile_cache(cache)
                    log(f"      기업개요 {done:,}/{len(missing):,} · {time.time()-t0:.0f}s")
        save_profile_cache(cache)
        log(f"      기업개요 수집 완료 {len(missing):,}개 · {time.time()-t0:.01f}s")

    attached = 0
    for code in codes:
        entry = cache.get(code) or {}
        ind = entry.get("ind") or industry.get(code) or ""
        d = results[code]
        if entry.get("none"):
            # WISEfn에 기업개요가 없는 종목(신규 상장 등)은 업종만으로 채운다
            short = _industry_short(ind)
            if short:
                d["revShort"] = short
                x = d.get("_x")
                if x is None:
                    x = d["_x"] = {}
                x["revDetail"] = f"{ind} 업종으로, 주요 수익원은 {short}입니다."
                x["s"] = ind
                attached += 1
            continue
        if not entry:
            continue
        short, detail = rev_text(entry, ind, code, d.get("n") or "")
        d["revShort"] = short
        x = d.get("_x")
        if x is None:
            x = d["_x"] = {}
        x["revDetail"] = detail
        if entry.get("w"):
            x["w"] = entry["w"]
        if ind:
            x["s"] = ind
        attached += 1
    log(f"      주요 수익원 붙인 종목 {attached:,}개")


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

    marcap_eok = row.get("marcap_eok") or 0
    if marcap_eok <= 0 and row.get("shares"):
        marcap_eok = int(round(price * row["shares"] / 1e8))   # 원 → 억원
    data: Dict[str, Any] = {
        "c": row["code"],
        "n": row["name"],
        "m": "K" if row["market"] == "KOSPI" else "Q",
        "p": price,
        "mc": marcap_eok,
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
            if last_op and last_op > 0 and data["mc"] > 0:
                mult = data["mc"] / last_op
                data["mult"] = round(mult, 1)
                data["pass"] = 1 if mult < 10 else 0
    return data


def _r(v: Optional[float]) -> Optional[int]:
    return None if v is None else int(round(v))


# ----------------------------------------------------------------------------
# 6) 메인
# ----------------------------------------------------------------------------

def run_us_update(args, log) -> None:
    """미국주식 데이터 갱신(scripts/update_us_data.py)을 이어서 실행한다.

    GitHub Actions 워크플로우는 update_data.py 만 호출하므로, 여기서 미국 스크립트를
    이어 돌려 한국·미국 데이터가 함께 자동 갱신되게 한다. 실패해도 국내 데이터
    저장·커밋은 그대로 진행되도록 예외를 삼킨다.
    """
    if getattr(args, "no_us", False):
        return
    import subprocess
    us_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "update_us_data.py")
    if not os.path.exists(us_script):
        return
    log("[미국] 미국주식 매물대 데이터 갱신 (update_us_data.py)")
    cmd = [sys.executable, us_script, "--workers", str(getattr(args, "workers", 8))]
    if getattr(args, "quiet", False):
        cmd.append("--quiet")
    try:
        r = subprocess.run(cmd, cwd=BASE_DIR, timeout=2400)
        if r.returncode != 0:
            log(f"  ! 미국 데이터 갱신 실패 (exit {r.returncode}) — 이전 미국 데이터가 유지됩니다")
    except Exception as exc:  # noqa: BLE001
        log(f"  ! 미국 데이터 갱신 예외: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="국내주식 매물대 스크리너 데이터 생성")
    ap.add_argument("--limit", type=int, default=0, help="시총 상위 N개만 (테스트용)")
    ap.add_argument("--codes", type=str, default="", help="쉼표로 구분한 종목코드만 수집")
    ap.add_argument("--workers", type=int, default=8, help="동시 요청 스레드 수 (기본 8)")
    ap.add_argument("--years", type=int, default=YEARS, help="매물대 기간(년), 기본 5")
    ap.add_argument("--out", type=str, default=DEFAULT_OUT, help="출력 JSON 경로")
    ap.add_argument("--no-finance", action="store_true", help="재무 수집 생략(속도 테스트)")
    ap.add_argument("--no-extras", action="store_true", help="최근 공시·뉴스 수집 생략(속도 테스트)")
    ap.add_argument("--no-profiles", action="store_true", help="주요 수익원(기업개요) 수집 생략(속도 테스트)")
    ap.add_argument("--profile-workers", type=int, default=6,
                    help="기업개요(주요 수익원) 동시 요청 수 (기본 6)")
    ap.add_argument("--end", type=str, default="", help="기준일 YYYYMMDD (기본: 오늘)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-us", action="store_true",
                    help="끝에 이어서 도는 미국주식 데이터 갱신(update_us_data.py)을 생략")
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
        run_us_update(args, log)
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

    # 주요 수익원(기업개요·주요제품·업종·홈페이지) 보완 — extras 안으로 들어간다
    if not args.no_extras and not args.no_profiles:
        log("[3.5/4] 주요 수익원(기업개요) 보완")
        try:
            apply_profiles(results, log, workers=max(1, args.profile_workers))
        except Exception as exc:  # noqa: BLE001 - 수익원 실패는 수집·배포를 막지 않는다
            log(f"  ! 주요 수익원 보완 실패: {exc}")

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
            "source": "네이버 금융(일별 시세·연간 실적·배당·공시·뉴스·코스피200 구성종목·업종 분류)·KRX 상장 목록(FinanceDataReader)·WISEfn 기업모니터(기업개요·주요제품 매출구성·홈페이지)",
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
                "주요 수익원(revShort)·사업 구조(revDetail)·홈페이지(w)는 WISEfn 기업모니터 기업개요와 주요제품 매출구성, 네이버 금융 업종 분류를 정리한 것으로, 상세 화면에서 지연 로드합니다.",
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
        # 기업개요 캐시도 함께 커밋해 다음 실행에서 2,500종목을 다시 받지 않게 한다
        # (내용이 그대로면 git add 해도 변경이 없어 커밋되지 않는다).
        if not args.no_profiles and os.path.exists(PROFILE_CACHE_PATH):
            rel_cache = os.path.relpath(PROFILE_CACHE_PATH, BASE_DIR)
            try:
                import subprocess
                subprocess.run(["git", "add", "--", rel_cache], cwd=BASE_DIR,
                               check=False, timeout=120)
                log(f"CI: {rel_cache} 스테이징 완료")
            except Exception as exc:  # noqa: BLE001
                log(f"CI: {rel_cache} 스테이징 생략 ({exc})")
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

    # ------------------------------------------------------------------
    # 미국주식 데이터도 같은 실행에서 갱신한다.
    # GitHub Actions 워크플로우(.github/workflows/daily-update.yml)는 이 스크립트만
    # 호출하므로, 여기서 update_us_data.py 를 이어 실행해 한국·미국 데이터가 함께
    # 자동 갱신되게 한다. 실패해도 국내 데이터 커밋은 그대로 진행한다.
    # ------------------------------------------------------------------
    run_us_update(args, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
