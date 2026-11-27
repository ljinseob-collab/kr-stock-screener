# 📈 국내·미국 주식 매물대 스크리너

코스피·코스닥 **보통주 2,500여 종목**과 미국 **S&P 500 · S&P 400 · NASDAQ 100 약 920종목**을
**1~5년 매물대**·평균단가·RSI·3년 실적·배당·주요 수익원·최근 공시·뉴스로 한 화면에서 거르는 정적 웹앱입니다.
상단 **🇰🇷 국내 / 🇺🇸 미국** 탭으로 시장을 바꿉니다. 서버가 필요 없고, GitHub Actions가 평일 장 마감 후
데이터를 자동 갱신해 GitHub Pages에 배포합니다.

## 화면에서 볼 수 있는 것

| 지표 | 내용 |
|---|---|
| 매물대 (1~5년) | 위쪽 **1년/2년/3년/4년/5년** 버튼으로 기준 기간을 바꿉니다. 선택 기간 일별 종가·거래량을 20개 가격대로 나눈 분포, **POC**(최대 매물대 가격), **🟢 바닥권/🟡 중간/🔴 상단** 뱃지(누적 백분위 30% 이하·30~70%·70% 초과), **최대 매물대 대비 괴리율** |
| 평균단가 (1~5년) | 선택 기간 일별 종가의 거래량 가중평균(VWAP)과 현재가 괴리율 |
| RSI(14) | 일별 종가 기준 와일더 RSI. 30 이하 과매도 / 70 이상 과매수 표시 |
| 3년 실적 | 최근 3개 사업연도 **영업이익·당기순이익**(확정치). **🟢 3년 흑자 / 🔵 흑자전환 / 🟡 최근 적자 / 🔴 계속 적자** 4색 뱃지(영업이익 메인, 순이익 보조) |
| 시가총액 | 시총, `영업이익×10 > 시가총액` 여부와 배수 |
| 배당 | 국내: 최근 확정 사업연도 주당배당금 ÷ 현재가 · 미국: **최근 1년 지급 배당금 합계** ÷ 현재가. 무배당은 `–` |
| 주요 수익원 (미국) | 목록 종목명 아래 **한 줄 수익원**(예: `AI GPU · 데이터센터 · 게임칩`), 상세 화면에 **2~3줄 사업 구조 설명 · 업종 ·기업 홈페이지 링크** |
| 최근 공시·뉴스 | 상세 화면에서 종목별 최근 공시(국내, 네이버 금융)와 **최근 뉴스 3건**(국내 네이버·미국 Yahoo Finance). 미국은 **SEC EDGAR 공시 링크**를 함께 둡니다 |

**프리셋**: `전체 종목` / `매물대 하단 + 과매도(RSI≤35)` / `내 기준 전체 통과` / `저평가 흑자주`(미국 탭)
(내 기준 = 영업이익 3년 연속 흑자 + 영업이익×10 > 시총 + 매물대 하단 30% 이하 + 현재가 ≤ 평균단가 + RSI ≤ 35,
저평가 흑자주 = 3년 연속 흑자 + 영업이익×10 > 시총)

필터: 시장(미국은 **지수: 전체 / S&P 500 / 나스닥 100**), 실적 추이, 가치 조건, 매물대 위치, 평균단가 이하, 배당주만, RSI 구간, 종목명·티커·수익원 검색, 정렬.
종목을 누르면 **선택 기간 매물대 막대 차트**, 3년 실적 표, 시총 vs 영업이익×10 비교, 주요 수익원(미국), 최근 공시·뉴스가 열립니다.

## 대상 종목

- **국내**: 코스피(KOSPI)·코스닥(KOSDAQ) 일반 주식회사 보통주. ETF·ETN·스팩·리츠·우선주·펀드 제외.
- **미국**: S&P 500(503) · S&P 400(400) · NASDAQ 100(101) 합집합에서 중복 제거한 **약 920종목**. ETF·ETN 등 상품은 포함되지 않습니다.

## 구조

```
.
├── .github/workflows/daily-update.yml   # 평일 17:00 KST 자동 수집·커밋·배포 (+ 수동 실행)
├── requirements.txt
├── scripts/update_data.py               # 국내 수집·계산·JSON 생성 (+ 끝에서 미국 스크립트 호출)
├── scripts/update_us_data.py            # 미국 수집·계산·JSON 생성
├── scripts/us_labels.py                 # 미국 업종·기업 ‘주요 수익원’ 한국어 사전
└── docs/                                # GitHub Pages 루트
    ├── index.html · style.css · app.js
    ├── data/stocks.json · failed.json   # 국내 (자동 커밋)
    ├── data/extras/{0..15}.json         # 국내 종목별 최근 공시·뉴스 (상세에서 지연 로드)
    ├── data/us_stocks.json · us_meta.json · us_failed.json   # 미국 목록·메타 (자동 커밋)
    └── data/us_extras/{0..15}.json      # 미국 상세: 뉴스·수익원 설명·홈페이지·업종 (지연 로드)
```

## 데이터 출처

| 데이터 | 출처 |
|---|---|
| **국내** 종목 목록·시가총액 | KRX 상장 목록 (FinanceDataReader `StockListing('KRX')`) |
| 국내 5년 일별 OHLCV | 네이버 금융 `api.finance.naver.com/siseJson.naver` (수정주가, 액면분할 반영) |
| 국내 연간 영업이익·당기순이익 | 네이버 금융 `m.stock.naver.com/api/stock/{code}/finance/annual` (확정 실적) |
| 국내 주당배당금(연간) | 같은 연간 실적 응답의 `주당배당금` 행 — 가장 최근 **확정** 사업연도 값 |
| 국내 최근 공시·뉴스 | 네이버 금융 disclosure · news API |
| **미국** 종목 목록 | S&P 500·S&P 400 — Wikipedia 구성종목 표 (+실패 시 GitHub datasets CSV), NASDAQ 100 — slickcharts |
| 미국 5년 일별 시세·배당 | Yahoo Finance chart API (`range=5y`, 수정주가 adjclose·거래량·배당 이벤트) |
| 미국 연간 영업이익·당기순이익 | Yahoo Finance fundamentals-timeseries (`annualOperatingIncome`·`annualNetIncome`) |
| 미국 시가총액·사업 요약·홈페이지 | Yahoo Finance quoteSummary (`price.marketCap`, `assetProfile`) |
| 미국 공시 링크 | SEC `company_tickers.json` 의 CIK → EDGAR 공시 목록 |
| 미국 최근 뉴스 | Yahoo Finance RSS (`feeds.finance.yahoo.com/rss/2.0/headline?s=…`) |
| 미국 주요 수익원 한국어 요약 | `scripts/us_labels.py` 업종·기업 사전 + Yahoo 사업 요약 자동 번역 |

- 공시·뉴스 API는 **CORS 를 허용하지 않아** 브라우저에서 직접 부를 수 없습니다. 그래서 수집 단계에서 종목별로 받아 `docs/data/extras/{n}.json`(16분할, 약 1.5MB)에 저장하고, 상세 화면을 열 때 해당 샤드 하나만 받습니다(첫 화면 로딩에는 영향 없음).
- GitHub Actions 에서는 워크플로우의 `git add` 목록에 `docs/data/extras` 가 없어도 커밋되도록, 스크립트가 CI(`GITHUB_ACTIONS=true`)일 때만 `git add docs/data/extras` 를 실행합니다.
- 배당수익률은 **현재가 기준**(주당배당금 ÷ 현재가)이며, 배당락·분할로 실제 통지서와 다를 수 있습니다.

- 매물대는 일별 **종가** 기준입니다(분봉·체결 단위 아님).
- 국내는 가격 원·실적/시총 억원, 미국은 가격·시총·실적·배당 모두 **달러(USD)** 로 저장합니다.
- 상장 1년 미만 종목은 매물대가 그 기간만 반영되므로 화면에 `1년 미만` 표시가 붙습니다.

## 처음 배포하기

1. 이 폴더를 GitHub 저장소 루트로 푸시합니다.
2. 저장소 **Settings → Pages → Build and deployment → Source**를 **Deploy from a branch**로,
   브랜치 **`main`**, 폴더 **`/docs`** 로 지정하고 **Save**를 누릅니다.
3. **Actions → 매물대 스크리너 데이터 갱신 → Run workflow**로 한 번 수동 실행합니다.
   - 실행이 `docs/data/`를 갱신·커밋하면 Pages가 그 커밋으로 사이트를 다시 빌드합니다.
   - 테스트로 적은 종목만 돌려보려면 `limit`에 `50`을 넣습니다.
4. 잠시 뒤 `https://<아이디>.github.io/<저장소>/` 로 열립니다.

이후 평일 17:00 KST(08:00 UTC)에 자동으로 갱신됩니다.

> 데이터를 매일 커밋하면 저장소가 하루 약 1MB씩 커집니다. 갱신은 하되 커밋은 남기지 않으려면
> 워크플로우의 "데이터 커밋" 스텝을 빼고, 배포용 아티팩트 업로드·배포 스텝을 추가하면 됩니다.

> `pip` 환경에 따라 `FinanceDataReader`(대문자) 이름은 인덱스에서 찾지 못합니다.
> `requirements.txt`는 소문자 `finance-datareader`로 고정해 두었습니다.

## 로컬에서 돌려보기

```bash
pip install -r requirements.txt

# 시총 상위 50개만 빠르게 (약 3초)
python scripts/update_data.py --limit 50 --workers 8

# 전체 (약 2,500종목, 3~4분, 끝에 미국 약 920종목도 함께 갱신)
python scripts/update_data.py --workers 10

# 미국만 따로 (약 1~2분)
python scripts/update_us_data.py --workers 8

# 화면 열기
python -m http.server 8000 --directory docs
# 브라우저에서 http://localhost:8000
```

주요 옵션

| 옵션 | 설명 |
|---|---|
| `--limit N` | 시총 상위 N개만 수집 |
| `--codes 005930,000660` | 특정 종목만 (국내) |
| `--workers N` | 동시 요청 수(기본 8) |
| `--resume` | 같은 날짜의 `.cache/` 수집 캐시로 이어서 실행 |
| `--no-finance` | 재무 수집 생략(속도 테스트) |
| `--no-extras` | 최근 공시·뉴스 수집 생략(속도 테스트) |
| `--no-us` | 끝에 이어지는 미국 데이터 갱신 생략 |
| `--out 경로` | 출력 JSON 경로 변경 |

`update_us_data.py` 는 미국장 최근 거래일이 이미 저장된 `us_meta.json` 기준일과 같으면 건너뜁니다(주말·휴장일).
`--force` 로 강제 갱신하고, `--no-news` 로 뉴스·수익원 상세 수집을 생략할 수 있습니다.

수집에 실패했거나 거래일이 20일 미만인 종목은 `docs/data/failed.json`에 사유와 함께 남습니다.

## 주의

공개 데이터를 정리한 **참고 자료**이며 투자 권유가 아닙니다. 네이버 금융 비공식 API를 쓰므로
제공처 사정에 따라 값이 달라지거나 일시적으로 수집이 실패할 수 있습니다.
투자 판단 전에는 증권사 HTS·공시로 다시 확인하세요.
