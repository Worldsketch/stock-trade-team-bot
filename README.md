# Stock Trade Bot v2

한국투자증권 Open API 기반 미국 주식 자동매매 봇 (v2 - 동적 슬롯 시스템)

## v1 대비 변경점

| 항목 | v1 | v2 |
|------|----|----|
| 종목 관리 | NVDL, TSLL, TQQQ 하드코딩 | 최대 6슬롯 동적 관리 |
| 종목 추가 | 코드 수정 필요 | 대시보드에서 + 버튼으로 추가 |
| 첫 매수 | 조건 충족 시 DCA | 슬롯 추가 시 시장가 1주 즉시 매수 |
| 종목 제거 | 불가 | 대시보드에서 제거 (전량 매도 or 감시 중단) |

## 핵심 구조: 슬롯 시스템

- 6개 슬롯이 비어 있는 상태로 시작
- 사용자가 대시보드에서 종목을 선택하면 슬롯에 등록 + 1주 시장가 매수
- 등록된 종목에 대해 v1과 동일한 전략(SMA200 + DCA + 트레일링 스탑) 자동 적용
- 슬롯 상태는 `slots.json`에 영속화

## 매매 전략 (v1 계승)

- **SMA200 필터**: 기초자산이 200일 이동평균선 위에 있을 때만 매수
- **DCA**: 전일종가 대비 -2%, -4%, -8% 하락 시 추가 매수
- **트레일링 스탑**: HWM 대비 -40% 하락 시 50% 부분 매도
- **자동 모드 전환**: 예수금 비중 ≤35% 또는 SMA200 하회 2종목 이상 → 방어 모드

## 기술 스택

- Python 3.11+
- FastAPI (대시보드 + API 서버)
- 한국투자증권 Open API
- yfinance (시장 데이터 + 티커 검증)
- Tailwind CSS (대시보드 UI)
- PM2 (서버 프로세스 관리)

## 파일 구조

```
├── api.py              # 한국투자증권 API 래퍼
├── bot.py              # 매매 로직 (슬롯 동적 관리)
├── app.py              # FastAPI 웹 서버 + 슬롯 관리 API
├── deploy.py           # 서버 배포 스크립트
├── slots.json          # 슬롯 상태 영속화 (자동 생성)
├── requirements.txt
├── .env.example
└── static/
    └── index.html      # 대시보드 UI (슬롯 기반)
```

## 설치 및 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # API 키 설정
python app.py
```
