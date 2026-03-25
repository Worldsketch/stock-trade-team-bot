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
- 보유 슬롯: 슬롯별 고유 컬러 + 글로우 효과 자동 배정
- 관찰(Watch) 슬롯: 회색 테마(글로우 없음)로 표시해 보유 슬롯과 시각적으로 분리
- **슬롯 추가**: 미국장(ET 04:00~20:00) 또는 데이장(KST 09:00~16:00)에 가능 → 티커 자동완성 검색 → 비율 선택(예수금 대비 1~10%) → 매수 주문 → 슬롯 활성화
- **티커 검색**: KIS 해외 종목마스터 기반 자동완성 (종목명/티커 입력 시 드롭다운), Magnificent 7 + 레버리지 ETF 인기종목 바로가기
- 입력 UX: 슬롯 추가 티커 입력창 우측 `X` 버튼으로 즉시 입력값 초기화
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
- 하루 2회 자동 생성 (ET 프리장 시작 04:00 / 본장 시작 09:30)
- 보유 레버리지 ETF의 기초자산만 분석 (NVDL→NVDA, TSLL→TSLA 등)
- 수동 재발행 API 지원 (`POST /api/ai-report/refresh`)
- 분석 항목: 추세 판단, 모멘텀, 핵심 가격대, 월가 컨센서스, 종목별 전략 제안

## 거래 시간 및 휴장

- **봇 루프 구동 시간**: 프리마켓~애프터마켓 (ET 04:00~20:00)
- **자동 전략 주문 시간**: 정규장(ET 09:30~16:00)에서만 자동 매수/DCA/트레일링 매도 실행
- **수동 주문 지원 시간**: 미국장(ET 04:00~20:00) + 데이장(KST 09:00~16:00, 주간거래 주문 API 사용)
- **슬롯 매수/제거(매도 포함) 시간**: 수동 주문과 동일한 시간 가드 적용
- **주말/미국 휴장일**: 거래 시간 외에는 포트폴리오/시세 API 호출을 최소화하고 스냅샷 유지 중심으로 동작
- **DST 자동 처리**: `ZoneInfo("America/New_York")` 기반, 서머타임 전환 자동 반영
- **휴장일 동적 관리**: `exchange_calendars` (NYSE 캘린더) 기반, 수동 업데이트 불필요
- **조기 폐장**: NYSE 캘린더에서 자동 감지 (반일 거래일 등)

## 안전장치

- **하드코딩 제로**: 종목·거래소·슬롯 수 등 모든 값이 동적 (API 호출 시 보유 종목 기반 자동 결정)
- **빈 슬롯 자동 정리**: 보유 0주 상태가 10분 이상 지속되면 슬롯 자동 제거 + 텔레그램 알림
- **API 이상 감지**: 예수금/평가액이 $0 반환 시 기존값 유지 (거짓 알림 방지)
- **현재가 동기화 보호**: 오래된 스냅샷 가격이 최신 캐시를 덮어쓰지 않도록 보호하고, stale 슬롯은 백그라운드 시세 갱신으로 복구
- **예수금 비중 알림**: 40% 이하 주의, 30% 이하 위험 (하루 1회)
- **에러 쓰로틀링**: 동일 에러 10분 간격 제한
- **시작/중지 경쟁조건 방지**: `/api/start` 중복 호출 시 단일 루프만 기동되도록 락/starting 상태로 보호
- **KIS 회로차단기(Circuit Breaker)**: 같은 API 키에서 연속 실패 시 짧은 cool-down으로 재시도 폭주 완화
- **프록시 헤더 신뢰 제어**: `TRUST_PROXY_HEADERS`, `TRUSTED_PROXY_IPS`로 `X-Forwarded-For` 신뢰 범위 제한
- **런타임 파일 git 제외**: `slots.json`, `hwm_data.json`, `runtime_data/` 등 실거래 상태 파일은 커밋 방지
- **헬스체크**: 6시간 간격 텔레그램 상태 리포트
- **Graceful Shutdown**: Ctrl+C 시 미체결 주문 취소 및 포지션 정리

## 기술 스택

- Python 3.11+
- FastAPI (대시보드 + API 서버)
- 한국투자증권 Open API (잔고, 주문, 시세)
- yfinance (전략/AI 보조 시장 데이터)
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
│   ├── status.py         # 상태 조회 API 라우터
│   ├── chart.py          # 차트/히스토리 API 라우터
│   ├── ai.py             # AI 리포트 API 라우터
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
├── runtime_data/
│   └── ai_report.json    # AI 분석 리포트 캐시(서버 저장, 브라우저와 무관하게 유지)
├── us_symbol_master.json # KIS 해외 종목마스터 캐시 (자동 생성)
├── requirements.txt
├── .env.example
└── static/
    ├── index.html        # 대시보드 UI (슬롯 기반, PWA 지원)
    ├── manifest.json     # PWA 매니페스트
    └── sw.js             # Service Worker
```

## v3 리팩터링 포인트

- `app.py`의 공통 계산/캐시 로직을 `services/`로 분리해 유지보수성과 재사용성을 개선
- 상태/차트/AI/주문/슬롯/전략 엔드포인트를 `routes/`로 분리해 API 책임을 모듈 단위로 정리
- 기존 동작/엔드포인트는 유지하고, 책임 분리만 우선 적용 (안전한 1차 리팩터링)

## API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/api/status` | 봇 상태 + 포지션 + 슬롯 정보 |
| GET | `/api/slots` | 현재 슬롯 상태 조회 |
| POST | `/api/slots/add` | 슬롯에 종목 추가 (비율 매수 / `watch_only` 추가) |
| POST | `/api/slots/buy` | Watch 슬롯 매수 전환 |
| POST | `/api/slots/remove` | 슬롯에서 종목 제거 |
| GET | `/api/search-ticker` | 티커 검색/검증 |
| GET | `/api/autocomplete` | 티커 자동완성 (KIS 종목마스터 기반) |
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

## 주요 환경변수 (운영)

- `KIS_ENABLE_DAYTIME_TRADING=true` : KST 09:00~16:00 데이장 주문 허용
- `SELL_FEE_RATE=0.0025` : 매도 수수료율(예: 0.25%)
- `SELL_TAX_RATE=0` : 매도 제비용/세금율
- `TRUST_PROXY_HEADERS=false` : 프록시 헤더(IP) 신뢰 여부 (기본 비활성)
- `TRUSTED_PROXY_IPS=127.0.0.1,10.0.0.2` : 신뢰 프록시 IP allowlist
- `KIS_CIRCUIT_FAIL_THRESHOLD=4` : 같은 API 실패 누적 시 회로 오픈 임계치
- `KIS_CIRCUIT_COOLDOWN_SEC=8` : 회로 오픈 후 cool-down 초

## 최근 반영 (2026-03-18)

- 슬롯/카드
  - Watch 슬롯 추가/삭제/매수전환 플로우 정리
  - Watch 슬롯 가격 0 깜빡임 완화(마지막 유효 가격 유지)
  - 슬롯 컬러를 심볼 기준 고정(순서 변경 시 색상 유지)
  - 슬롯 드래그 정렬 + 서버 저장(`/api/slots/reorder`)
  - 슬롯 추가 입력창 우측 `X` 버튼 추가
- 최고점(ATH) 기능
  - Watch 슬롯에 `all_time_high` 기반 최고점 추적 추가
  - legacy Watch 슬롯 ATH 백필(장외 포함) + 분할 왜곡값 보정
  - 최고점 계산 기준을 `5y -> 3y -> 2y -> 1y` 우선으로 정규화
  - 카드 표시 개선: Watch/보유 슬롯 모두 `최고점 대비` 수익률 표시
  - Watch 카드 보조라인에 `최고점 대비`/`추가가 대비` 동시 표시(+초록, -빨강)
- 시세/상태 성능
  - 상태 조회를 bot snapshot 우선 경로로 전환해 요청 시 KIS 직접조회 축소
  - stale snapshot guard + slot price cache + round-robin refresh로 슬롯 가격 동기화 안정화
  - `status/pending/chart/quote` 적응형 폴링(정상/위험/백그라운드) 적용
- 거래/정산
  - 수동 매도 로그/예상 금액 일관성 보정(주문가 기준)
  - 수동 매도/트레일링 매도 추격형 재호가 단계 정교화
  - 수수료/세금(`SELL_FEE_RATE`, `SELL_TAX_RATE`) 반영 정산 및 UI 표기 개선
  - 슬롯 매수 비율 기준을 총자산이 아닌 예수금 기준으로 보정
- 보안/운영
  - 기본 인증 하드닝(기본 계정 의존 제거)
  - 에러/민감정보 마스킹 강화
  - 예약 배포(`deploy.py --schedule-restart`) 및 기존 예약 교체 동작 정리
  - AI 리포트 서버 영속 저장(`runtime_data/ai_report.json`) 및 노출 안정화

## 최근 반영 (2026-03-23)

- 안정성/보안
  - `/api/start`/`/api/stop` 동시 호출 경쟁조건 방지 락 추가 (중복 루프 기동 차단)
  - `TRUST_PROXY_HEADERS`/`TRUSTED_PROXY_IPS` 기반 클라이언트 IP 신뢰 정책 강화
  - KIS API 연속 실패 시 endpoint 단위 circuit breaker(cool-down) 적용
- API 처리
  - `routes/trading.py`, `routes/slots_strategy.py`의 JSON 파싱 경로를 동기 핸들러+`Body`로 정리해 이벤트루프 블로킹 리스크 완화
  - 수동 매도/슬롯 제거 전량매도 경로에 서버측 거래시간 가드 일원화
- 운영
  - 런타임 상태 파일(`slots.json`, `hwm_data.json`, `runtime_data/` 등) `.gitignore` 반영

## 설치 및 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # API 키 설정
python app.py
```

## 배포

```bash
python deploy.py                                  # 업로드 + 즉시 재시작
python deploy.py --upload-only                   # 파일만 선업로드
python deploy.py --restart-only                  # 업로드 없이 즉시 재시작
python deploy.py --upload-only --schedule-restart "21:50"   # 파일 업로드 후 지정시간 재시작(Asia/Seoul)
python deploy.py --schedule-restart "2026-03-17 21:50"      # 절대시간 재시작 예약
```

예약 배포 규칙:
- `--schedule-restart`는 기본 `Asia/Seoul` 기준으로 해석됩니다. (`--timezone`으로 변경 가능)
- 새 예약을 걸면 기존 예약은 자동 취소되고 새 예약만 유지됩니다. (legacy sleep 예약도 정리)
- 프로젝트 분리 운영 시 `.env`의 `DEPLOY_PATH`, `DEPLOY_PM2_NAME`, `APP_PORT`를 각 프로젝트별로 고유하게 설정하세요.
- 서버 확인 파일:
  - 로그: `<DEPLOY_PATH>/deploy_scheduled_restart.log`
  - PID: `<DEPLOY_PATH>/deploy_scheduled_restart.pid`

보안 권장:
- 프로덕션은 Nginx/Caddy HTTPS reverse proxy 뒤에서 운영
- 앱은 `.env`에서 `APP_HOST=127.0.0.1` 로컬 바인딩 유지
