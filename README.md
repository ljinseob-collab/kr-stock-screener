# 📈 국내주식 매물대 스크리너

코스피·코스닥 **보통주 2,500여 종목**을 5년 매물대·평균단가·RSI·3년 실적으로 한 화면에서 거르는 정적 웹앱입니다.
서버가 필요 없고, GitHub Actions가 평일 장 마감 후 데이터를 자동 갱신해 GitHub Pages에 배포합니다.

## 화면에서 볼 수 있는 것

| 지표 | 내용 |
|---|---|
| 5년 매물대 | 5년 일별 종가·거래량을 20개 가격대로 나눈 거래량 분포, **POC**(최대 매물대 가격), **현재가 vs POC 괴리율**, **매물대 누적 백분위**(현재가 이하 거래량 비율) |
| 5년 평균단가 | 5년 일별 종가의 거래량 가중평균(VWAP)과 현재가 괴리율 |
| RSI(14) | 일별 종가 기준 와일더 RSI. 30 이하 과매도 / 70 이상 과매수 표시 |
| 3년 실적 | 최근 3개 사업연도 **영업이익·당기순이익**(확정치), 3년 연속 흑자 여부 |
| 시가총액 | 시총, `영업이익×10 > 시가총액` 여부와 배수 |

**프리셋**: `전체 종목` / `매물대 하단 + 과매도(RSI≤35)` / `내 기준 전체 통과`
(내 기준 = 영업이익 3년 연속 흑자 + 영업이익×10 > 시총 + 매물대 하단 30% 이하 + 현재가 ≤ 5년 평균단가 + RSI ≤ 35)

필터: 시장, 3년 연속 흑자, 가치 조건, 매물대 위치, 5년 평균단가 이하, RSI 구간, 종목명·코드 검색, 정렬.
종목을 누르면 **5년 매물대 막대 차트**(현재가·POC·평균단가 표시), 3년 실적 표, 시총 vs 영업이익×10 비교가 열립니다.

## 대상 종목

- 포함: 코스피(KOSPI)·코스닥(KOSDAQ) **일반 주식회사 보통주**
- 제외: ETF, ETN, 스팩(SPAC), 리츠(REIT), 우선주, 펀드·선박투자회사, 코넥스·코스닥글로벌

## 구조

```
.
├── .github/workflows/daily-update.yml   # 평일 17:00 KST 자동 수집·커밋·배포 (+ 수동 실행)
├── requirements.txt
├── scripts/update_data.py               # 수집·계산·JSON 생성
└── docs/                                # GitHub Pages 루트
    ├── index.html · style.css · app.js
    └── data/stocks.json · failed.json   # 생성 결과 (자동 커밋)
```

## 데이터 출처

| 데이터 | 출처 |
|---|---|
| 종목 목록·시가총액 | KRX 상장 목록 (FinanceDataReader `StockListing('KRX')`) |
| 5년 일별 OHLCV | 네이버 금융 `api.finance.naver.com/siseJson.naver` (수정주가, 액면분할 반영) |
| 연간 영업이익·당기순이익 | 네이버 금융 `m.stock.naver.com/api/stock/{code}/finance/annual` (확정 실적) |

- 매물대는 일별 **종가** 기준입니다(분봉·체결 단위 아님).
- 실적 단위는 **억원**, 가격은 원, 시가총액은 억원으로 저장합니다.
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

# 전체 (약 2,500종목, 1~2분)
python scripts/update_data.py --workers 10

# 화면 열기
python -m http.server 8000 --directory docs
# 브라우저에서 http://localhost:8000
```

주요 옵션

| 옵션 | 설명 |
|---|---|
| `--limit N` | 시총 상위 N개만 수집 |
| `--codes 005930,000660` | 특정 종목만 |
| `--workers N` | 동시 요청 수(기본 8) |
| `--resume` | 같은 날짜의 `.cache/` 수집 캐시로 이어서 실행 |
| `--no-finance` | 재무 수집 생략(속도 테스트) |
| `--out 경로` | 출력 JSON 경로 변경 |

수집에 실패했거나 거래일이 20일 미만인 종목은 `docs/data/failed.json`에 사유와 함께 남습니다.

## 주의

공개 데이터를 정리한 **참고 자료**이며 투자 권유가 아닙니다. 네이버 금융 비공식 API를 쓰므로
제공처 사정에 따라 값이 달라지거나 일시적으로 수집이 실패할 수 있습니다.
투자 판단 전에는 증권사 HTS·공시로 다시 확인하세요.
