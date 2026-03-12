# Stock Trade Bot v2

한국투자증권 Open API 기반 미국 주식 자동매매 봇 (v2 - 동적 슬롯 시스템)

## v1 대비 변경점

| 항목 | v1 | v2 |
|------|----|----|
| 종목 관리 | NVDL, TSLL, TQQQ 하드코딩 | 최대 6슬롯 동적 관리 |
| 종목 추가 | 코드 수정 필요 | 대시보드에서 비율 매수(1%/3%/5%/10%) |
| 종목 제거 | 불가 | 대시보드에서 제거 (전량 매도 or 감시 중단) |
| 거래소 | NASDAQ 고정 | 자동 탐색 (NAS/NYS/AMS) |
| AI 분석 | 없음 | Gemini 기반 하루 2회 자동 리포트 |
| 기존 보유 | 수동 설정 | 서버 시작 시 자동 슬롯 등록 |

## 슬롯 시스템

- 최대 6개 슬롯, 빈 상태로 시작 (슬롯별 고유 컬러 + 글로우 효과 자동 배정)
- **슬롯 추가**: 프리마켓~애프터마켓(ET 04:00~20:00)에 가능 → 티커 자동완성 검색 → 비율 선택(총자산 대비 1~10%) → 매수 주문 → 슬롯 활성화
- **티커 검색**: yfinance 기반 실시간 자동완성 (종목명/티커 입력 시 드롭다운), Magnificent 7 + 레버리지 ETF 인기종목 바로가기
- **자동 등록**: 서버 시작 시 `slots.json`이 비어있으면 한투 API에서 보유 종목을 감지하여 자동 등록
- **슬롯 제거**: 전량 매도 후 제거 or 매도 없이 감시 중단
- **자동 정리**: 보유 수량 0주인 슬롯은 10분 경과 후 자동 제거 (텔레그램 알림)
- 슬롯 상태는 `slots.json`에 영속화 (서버 재시작 시 복원)
- 포지션 정렬: 슬롯 등록 시간 기준 오래된 순서

## 매매 전략 (Strategy E)

### SMA200 필터
- 기초자산(레버리지 ETF) 또는 자기 자신(일반 주식)의 200일 이동평균선 위에 있을 때만 매수
- 장중 5분 간격으로 SMA200 재확인, 본장 30분 전(ET 09:00) 실시간 재검수

### DCA (357 전략)
전일종가 대비 당일 하락률 기준 추가 매수:

| 하락률 | 공격 모드 | 방어 모드 |
|--------|----------|----------|
| 매일 기본적립 | 총자산 0.1% | 총자산 0.1% |
| -3% | 총자산 2.5% | 총자산 1.2% |
| -5% | 총자산 4.5% | 총자산 2.2% |
| -7% | 총자산 6.5% | 총자산 3.2% |

### RSI 과매도 보너스
- RSI(14) < 30 감지 시 하루 1회 추가 매수 (SMA200 필터 무관)

### 트레일링 스탑
- HWM(최고점) 대비 -40% 하락 시 보유량의 50% 부분 매도
- HWM은 `hwm_data.json`에 영속화 (배포 시 리셋 방지)

### 자동 모드 전환
- 예수금 비중 ≤ 35% 또는 SMA200 하회 2종목 이상 → 방어 모드
- 대시보드에서 수동 전환 가능 (공격/방어/자동)

## AI 시장 분석

- **Gemini 3.1 Pro** 기반 기술적 분석 리포트
- 하루 2회 자동 생성 (KST 10:00 / 본장 시작 시점)
- 보유 레버리지 ETF의 기초자산만 분석 (NVDL→NVDA, TSLL→TSLA 등)
- 수동 재발행 API 지원 (`POST /api/ai-report/refresh`)
- 분석 항목: 추세 판단, 모멘텀, 핵심 가격대, 월가 컨센서스, 종목별 전략 제안

## 거래 시간 및 휴장

- **거래 가능 시간**: 프리마켓~애프터마켓 (ET 04:00~20:00)
- **DST 자동 처리**: `ZoneInfo("America/New_York")` 기반, 서머타임 전환 자동 반영
- **휴장일 동적 관리**: `exchange_calendars` (NYSE 캘린더) 기반, 수동 업데이트 불필요
- **조기 폐장**: NYSE 캘린더에서 자동 감지 (반일 거래일 등)

## 안전장치

- **하드코딩 제로**: 종목·거래소·슬롯 수 등 모든 값이 동적 (API 호출 시 보유 종목 기반 자동 결정)
- **빈 슬롯 자동 정리**: 보유 0주 상태가 10분 이상 지속되면 슬롯 자동 제거 + 텔레그램 알림
- **API 이상 감지**: 예수금/평가액이 $0 반환 시 기존값 유지 (거짓 알림 방지)
- **예수금 비중 알림**: 40% 이하 주의, 30% 이하 위험 (하루 1회)
- **에러 쓰로틀링**: 동일 에러 10분 간격 제한
- **헬스체크**: 6시간 간격 텔레그램 상태 리포트
- **Graceful Shutdown**: Ctrl+C 시 미체결 주문 취소 및 포지션 정리

## 기술 스택

- Python 3.11+
- FastAPI (대시보드 + API 서버)
- 한국투자증권 Open API (잔고, 주문, 시세)
- yfinance (시장 데이터, 티커 검증/자동완성, SMA/RSI 계산)
- exchange_calendars (NYSE 휴장일/조기폐장 동적 관리)
- Gemini API (AI 시장 분석)
- Tailwind CSS + LightweightCharts (대시보드 UI)
- PM2 (서버 프로세스 관리)
- Telegram Bot API (알림)

## 파일 구조

```
├── api.py                # 한국투자증권 API 래퍼 (동적 거래소 탐색)
├── bot.py                # 매매 로직 (슬롯 동적 관리 + 전략 엔진)
├── app.py                # FastAPI 웹 서버 + 슬롯/AI 리포트 API (엔드포인트 중심)
├── routes/
│   ├── trading.py        # 주문/미체결/취소 API 라우터
│   └── slots_strategy.py # 슬롯/전략모드/티커검색 API 라우터
├── services/
│   ├── trade_metrics.py  # 실현손익 계산 + 기존 trade_log pnl 마이그레이션
│   └── price_cache.py    # 기초자산 현재가 캐시
├── deploy.py             # 서버 배포 스크립트
├── slots.json            # 슬롯 상태 영속화 (자동 생성)
├── hwm_data.json         # 최고점(HWM) 추적 데이터 (자동 생성)
├── daily_state.json      # 일일 매수 상태 (중복 매수 방지)
├── strategy_mode.json    # 전략 모드 저장 (auto/aggressive/defensive)
├── trade_log.json        # 매매 내역 기록
├── equity_log.json       # 일별 자산 스냅샷
├── ai_report.json        # AI 분석 리포트 캐시
├── requirements.txt
├── .env.example
└── static/
    ├── index.html        # 대시보드 UI (슬롯 기반, PWA 지원)
    ├── manifest.json     # PWA 매니페스트
    └── sw.js             # Service Worker
```

## v3 리팩터링 포인트

- `app.py`의 공통 계산/캐시 로직을 `services/`로 분리해 유지보수성과 재사용성을 개선
- 주문/슬롯/전략 관련 엔드포인트를 `routes/`로 분리해 API 책임을 모듈 단위로 정리
- 기존 동작/엔드포인트는 유지하고, 책임 분리만 우선 적용 (안전한 1차 리팩터링)

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/api/status` | 봇 상태 + 포지션 + 슬롯 정보 |
| GET | `/api/slots` | 현재 슬롯 상태 조회 |
| POST | `/api/slots/add` | 슬롯에 종목 추가 (비율 매수) |
| POST | `/api/slots/remove` | 슬롯에서 종목 제거 |
| GET | `/api/search-ticker` | 티커 검색/검증 |
| GET | `/api/autocomplete` | 티커 자동완성 (yfinance Search) |
| POST | `/api/sell` | 수동 매도 (비율 지정) |
| GET | `/api/ai-report` | AI 분석 리포트 조회 |
| POST | `/api/ai-report/refresh` | AI 리포트 수동 재발행 |
| GET | `/api/strategy-params` | 전략 파라미터 + 시장 상태 |
| POST | `/api/strategy-mode` | 전략 모드 변경 |
| GET | `/api/chart-data` | 차트 데이터 (캔들 + 매매 마커) |
| GET | `/api/equity-history` | 자산 추이 데이터 |
| GET | `/api/trade-history` | 매매 내역 |
| GET | `/api/pending-orders` | 미체결 주문 조회 |
| POST | `/api/cancel-order` | 미체결 주문 취소 |
| POST | `/api/start` | 봇 시작 |
| POST | `/api/stop` | 봇 정지 |

## 설치 및 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # API 키 설정
python app.py
```

## 배포

```bash
python deploy.py   # .env의 DEPLOY_HOST로 scp + pm2 restart
```
