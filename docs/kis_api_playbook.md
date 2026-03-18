# KIS Open API 플레이북

## 목적
- 한국투자증권 Open API 연동 시, 프로젝트에서 일관되게 사용할 규칙을 정리한다.
- 세션 메모리 대신 저장 문서로 유지하여 이후 개발/점검 시 기준으로 사용한다.

## 기준 자료
- 원본 엑셀: `/Users/bobby/Downloads/한국투자증권_오픈API_전체문서_20260316_030000.xlsx`
- 반영일: 2026-03-16

## 핵심 API 매핑
- `TTTS3012R` / `/uapi/overseas-stock/v1/trading/inquire-balance`
  - 해외주식 잔고 조회
  - 연속조회(`tr_cont`, `ctx_area_fk200/nk200`) 지원
- `TTTS3007R` / `/uapi/overseas-stock/v1/trading/inquire-psamount`
  - 해외주식 매수가능금액 조회
  - `ovrs_ord_psbl_amt` 단일 의존 금지
- `TTTS3018R` / `/uapi/overseas-stock/v1/trading/inquire-nccs`
  - 해외주식 미체결 조회
  - 연속조회(`tr_cont`, `ctx_area_fk200/nk200`) 지원
- `TTTC2101R` / `/uapi/overseas-stock/v1/trading/foreign-margin`
  - 해외증거금 통화별조회
  - USD 예수금 fallback 소스로 사용

## 프로젝트 적용 규칙
1. 미국 조회 거래소 정규화
- 실전 조회 계열은 `NYSE/AMEX/NAS`를 `NASD`로 정규화해 미국 전체 조회로 통일한다.

2. USD 예수금 계산 우선순위
- `TTTS3007R.output`에서 아래 필드 최대값을 사용한다.
  - `ovrs_ord_psbl_amt`
  - `ord_psbl_frcr_amt`
  - `frcr_ord_psbl_amt1`
- 값이 0이면 `TTTC2101R` USD 행 fallback:
  - `frcr_gnrl_ord_psbl_amt`
  - `frcr_dncl_amt1`
  - `frcr_ord_psbl_amt1`

3. `TTTS3007R` 입력값 보정
- `ITEM_CD`는 거래소별 대표 종목으로 선택(예: `NASD=AAPL`, `NYSE=BA`, `AMEX=SPY`)
- `OVRS_ORD_UNPR`는 가능한 실시간가 기반으로 입력한다.

4. 연속조회 기본 적용
- 잔고(`TTTS3012R`)와 미체결(`TTTS3018R`)은 페이지를 모두 순회한다.
- 다음 페이지 조건:
  - 응답 헤더 `tr_cont`가 `F` 또는 `M`
  - 응답 본문 `ctx_area_fk200`, `ctx_area_nk200` 존재

5. 실패 코드 추적
- `rt_cd != 0` 시 `msg_cd`, `msg1`를 마스킹 후 기록한다.
- 집계 파일: `api_fail_stats.json`
  - `totals`: 누적 실패 코드 카운트
  - `recent`: 최근 실패 이벤트
- 5분마다 콘솔 요약 로그를 출력한다.

6. 짧은 캐시 사용
- `foreign-margin` 결과는 초단기 캐시(기본 2초)로 재호출을 줄인다.
- API 오류 시 최근 캐시값으로 안전 폴백한다.

7. `/api/status` 현재가 동기화 규칙
- 슬롯 현재가는 `bot_snapshot` + `slot_price_cache`를 함께 사용한다.
- `bot_snapshot.portfolio_ts`가 오래된 경우(stale)에는 최신 캐시 가격을 우선 사용한다.
- stale/미수신 슬롯은 백그라운드 시세 갱신으로 복구하고, 요청 처리 스레드에서 동기 시세조회로 블로킹하지 않는다.
- 종목별 중복 조회 방지를 위해 inflight/최소 간격(현재 1.5초) 제어를 유지한다.

## 운영 체크리스트
- `api_fail_stats.json`가 생성/갱신되는지 확인
- `USD 예수금 0` 발생 시 같은 시각의 `msg_cd` 패턴 확인
- 잔고/미체결 누락 의심 시 연속조회 키(`ctx_area_*`) 진행 여부 확인
- 호출량 급증 시 조회 거래소 정규화 및 캐시 동작 확인
- 슬롯 현재가가 간헐 정지로 보이면 `portfolio_ts` 지연(stale)과 `slot_price_cache` 갱신 로그를 함께 확인

## 최근 반영 메모 (2026-03-18)
- `GGLL -> GOOG` 레버리지 매핑을 표준 맵에 추가
- 기존 `slots.json` 로드 시 레버리지 슬롯의 `base_asset/is_leveraged` 자동 보정
- 상태 조회에서 stale snapshot guard를 추가해 오래된 가격이 최신 값을 덮어쓰는 문제를 차단

## 확장 후보
- 실패 코드별 재시도 정책 분기(즉시중단/재시도)
- 장애 리포트 자동 생성(일별 실패 코드 TOP)
- 웹소켓 도입 시 이 문서에 세션/구독 정책 추가
