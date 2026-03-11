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

- 최대 6개 슬롯, 빈 상태로 시작
- **슬롯 추가**: 정규장 시간에만 가능 → 티커 검색/검증 → 비율 선택(총자산 대비 1~10%) → 매수 주문 → 슬롯 활성화
- **자동 등록**: 서버 시작 시 `slots.json`이 비어있으면 한투 API에서 보유 종목을 감지하여 자동 등록
- **슬롯 제거**: 전량 매도 후 제거 or 매도 없이 감시 중단
- 슬롯 상태는 `slots.json`에 영속화 (서버 재시작 시 복원)

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

## 안전장치

- **하드코딩 제로**: 종목·거래소·슬롯 수 등 모든 값이 동적 (API 호출 시 보유 종목 기반 자동 결정)
- **API 이상 감지**: 예수금/평가액이 $0 반환 시 기존값 유지 (거짓 알림 방지)
- **예수금 비중 알림**: 40% 이하 주의, 30% 이하 위험 (하루 1회)
- **에러 쓰로틀링**: 동일 에러 10분 간격 제한
- **헬스체크**: 6시간 간격 텔레그램 상태 리포트
- **Graceful Shutdown**: Ctrl+C 시 미체결 주문 취소 및 포지션 정리

## 기술 스택

- Python 3.11+
- FastAPI (대시보드 + API 서버)
- 한국투자증권 Open API (잔고, 주문, 시세)
- yfinance (시장 데이터, 티커 검증, SMA/RSI 계산)
- Gemini API (AI 시장 분석)
- Tailwind CSS + LightweightCharts (대시보드 UI)
- PM2 (서버 프로세스 관리)
- Telegram Bot API (알림)

## 파일 구조

```
├── api.py                # 한국투자증권 API 래퍼 (동적 거래소 탐색)
├── bot.py                # 매매 로직 (슬롯 동적 관리 + 전략 엔진)
├── app.py                # FastAPI 웹 서버 + 슬롯/AI 리포트 API
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

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/api/status` | 봇 상태 + 포지션 + 슬롯 정보 |
| GET | `/api/slots` | 현재 슬롯 상태 조회 |
| POST | `/api/slots/add` | 슬롯에 종목 추가 (비율 매수) |
| POST | `/api/slots/remove` | 슬롯에서 종목 제거 |
| GET | `/api/search-ticker` | 티커 검색/검증 |
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
