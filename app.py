import os
import json
import time
import secrets
import threading
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, List
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn
from dotenv import load_dotenv

from api import KoreaInvestmentAPI
from bot import TradingBot
from routes.ai import create_ai_router
from routes.chart import create_chart_router
from routes.trading import create_trading_router
from routes.slots_strategy import create_slots_strategy_router
from routes.status import create_status_router
from services.live_data_cache import LiveDataCache
from services.trade_metrics import RealizedPnlCalculator, migrate_trade_pnl

bot_instance: Optional[TradingBot] = None
bot_thread: Optional[threading.Thread] = None
_status_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_strategy_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_realized_pnl: RealizedPnlCalculator = RealizedPnlCalculator(cache_ttl_seconds=180.0, trade_file="trade_log.json")
_live_data_cache: LiveDataCache = LiveDataCache()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_instance, bot_thread
    load_dotenv()
    
    app_key: str = os.getenv("KIS_APP_KEY", "")
    app_secret: str = os.getenv("KIS_APP_SECRET", "")
    account_no: str = os.getenv("KIS_ACCOUNT_NUMBER", "")
    account_code: str = os.getenv("KIS_ACCOUNT_CODE", "01")
    
    api: KoreaInvestmentAPI = KoreaInvestmentAPI(app_key, app_secret, account_no, account_code, is_mock=False)
    bot_instance = TradingBot(api)

    bot_thread = threading.Thread(target=bot_instance.run_loop, daemon=True)
    bot_thread.start()
    print("[자동 시작] 봇 매매 루프가 자동으로 시작되었습니다.")

    ai_thread = threading.Thread(target=_auto_generate_report, daemon=True)
    ai_thread.start()

    migrate_trade_pnl("trade_log.json")

    yield
    
    print("\n[웹 서버 종료] 봇 루프를 중단합니다. (포지션은 유지됩니다)")
    if bot_instance:
        bot_instance.stop_loop()
    if bot_thread and bot_thread.is_alive():
        bot_thread.join(timeout=5)

app = FastAPI(title="한국투자증권 자동매매 봇", lifespan=lifespan)
security = HTTPBasic()

def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    admin_user: str = os.getenv("ADMIN_USERNAME", "")
    admin_pass: str = os.getenv("ADMIN_PASSWORD", "")
    if not admin_user or not admin_pass:
        raise HTTPException(status_code=500, detail="ADMIN_USERNAME/ADMIN_PASSWORD 환경변수가 설정되지 않았습니다.")
    correct_username = secrets.compare_digest(credentials.username, admin_user)
    correct_password = secrets.compare_digest(credentials.password, admin_pass)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_index() -> str:
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

def _monitor_sell_fill(symbol: str, qty: int, price: float, label: str) -> None:
    for i in range(60):
        time.sleep(5)
        if not bot_instance:
            return
        try:
            orders = _live_data_cache.get_pending(ttl_sec=2.0)
            if orders is None:
                orders = bot_instance.api.get_pending_orders(symbols=bot_instance.symbols)
                _live_data_cache.set_pending(orders)
            still_pending: bool = any(
                o.get("symbol") == symbol and int(o.get("remaining_qty", 0)) > 0
                for o in orders
            )
            if not still_pending:
                filled_amount: float = qty * price
                krw_filled: float = filled_amount * bot_instance.exchange_rate
                msg: str = f"✅ [수동 매도 체결 완료]\n종목: {symbol}\n수량: {qty}주 ({label})\n체결가: ${price:.2f}\n체결 금액: ${filled_amount:,.2f} (약 {krw_filled:,.0f}원)"
                bot_instance.send_telegram_message(msg)
                bot_instance.log(f"✅ [수동매도 체결] {symbol} {qty}주 @ ${price:.2f} ({label})")
                return
        except Exception:
            continue
    msg = f"⏰ [수동 매도 미체결]\n종목: {symbol}\n수량: {qty}주 ({label})\n지정가: ${price:.2f}\n5분간 체결되지 않았습니다. 미체결 주문을 확인하세요."
    bot_instance.send_telegram_message(msg)
    bot_instance.log(f"⏰ [수동매도 미체결] {symbol} {qty}주 @ ${price:.2f} ({label})")

def _get_bot_instance() -> Optional[TradingBot]:
    return bot_instance

def _invalidate_status_cache() -> None:
    _status_cache["data"] = None
    _status_cache["ts"] = 0.0
    _live_data_cache.invalidate_portfolio()

app.include_router(
    create_status_router(
        auth_dependency=get_current_username,
        get_bot=_get_bot_instance,
        status_cache=_status_cache,
        realized_pnl=_realized_pnl,
        live_data_cache=_live_data_cache,
    )
)
app.include_router(
    create_trading_router(
        auth_dependency=get_current_username,
        get_bot=_get_bot_instance,
        invalidate_status_cache=_invalidate_status_cache,
        monitor_sell_fill=_monitor_sell_fill,
        live_data_cache=_live_data_cache,
    )
)
app.include_router(
    create_slots_strategy_router(
        auth_dependency=get_current_username,
        get_bot=_get_bot_instance,
        invalidate_status_cache=_invalidate_status_cache,
    )
)
app.include_router(
    create_chart_router(
        auth_dependency=get_current_username,
        get_bot=_get_bot_instance,
        live_data_cache=_live_data_cache,
    )
)
app.include_router(
    create_ai_router(
        auth_dependency=get_current_username,
        generate_ai_report=lambda: _generate_ai_report(),
        ai_report_file="ai_report.json",
    )
)


@app.get("/api/strategy-params")
async def get_strategy_params(
    snapshot: int = Query(default=0),
    refresh: int = Query(default=0),
    username: str = Depends(get_current_username),
) -> Dict[str, Any]:
    global _strategy_cache
    if not bot_instance:
        return {"error": "Bot not initialized"}

    now: float = time.time()
    use_snapshot: bool = snapshot == 1
    force_refresh: bool = refresh == 1
    cached_market = _strategy_cache.get("market")
    cache_ttl: float = 30.0 if bot_instance.is_active_trading_time(bot_instance.get_eastern_time()) else 300.0
    if use_snapshot:
        prev_close = dict(bot_instance.prev_close)
        base_price = dict(cached_market.get("base_price", {})) if cached_market else {}
        base_sma200 = dict(cached_market.get("base_sma200", {})) if cached_market else {}
        etf_current_price = dict(cached_market.get("etf_current_price", {})) if cached_market else {}

        bot_snapshot: Optional[Dict[str, Any]] = bot_instance.get_live_snapshot(max_age_sec=20.0)
        if bot_snapshot:
            for pos in bot_snapshot.get("positions", []) or []:
                symbol = str(pos.get("symbol", "")).upper()
                if not symbol:
                    continue
                try:
                    current_price = float(pos.get("current_price", 0.0) or 0.0)
                except Exception:
                    current_price = 0.0
                if current_price > 0:
                    etf_current_price[symbol] = current_price
    elif (not force_refresh) and cached_market and (now - _strategy_cache["ts"]) < cache_ttl:
        base_price, base_sma200, etf_current_price = cached_market["base_price"], cached_market["base_sma200"], cached_market["etf_current_price"]
        prev_close = cached_market["prev_close"]
    else:
        prev_close = dict(bot_instance.prev_close)
        base_price = {}
        base_sma200 = {}
        etf_current_price = {}
        for etf_sym, base_sym in bot_instance.base_assets.items():
            try:
                snapshot: Optional[Dict[str, float]] = bot_instance._get_trend_snapshot_from_kis(base_sym, force_refresh=False)
                if snapshot:
                    base_sma200[etf_sym] = float(snapshot["sma_200"])
                    base_price[etf_sym] = float(snapshot["current_price"])
            except Exception:
                pass

            try:
                realtime_etf: float = bot_instance.api.get_current_price(etf_sym)
                if realtime_etf > 0:
                    etf_current_price[etf_sym] = realtime_etf
                else:
                    closes = bot_instance._get_kis_daily_closes(etf_sym, min_points=1, force_refresh=False)
                    if closes:
                        etf_current_price[etf_sym] = float(closes[-1])
            except Exception:
                pass

            if prev_close.get(etf_sym, 0.0) <= 0:
                try:
                    bot_instance._update_prev_close_from_kis(etf_sym, force_refresh=False)
                    if bot_instance.prev_close.get(etf_sym, 0.0) > 0:
                        prev_close[etf_sym] = float(bot_instance.prev_close[etf_sym])
                except Exception:
                    pass
        _strategy_cache = {"market": {"base_price": base_price, "base_sma200": base_sma200, "etf_current_price": etf_current_price, "prev_close": prev_close}, "ts": now}

    return {
        "symbols": bot_instance.symbols,
        "base_assets": bot_instance.base_assets,
        "base_buy_ratio": bot_instance.base_buy_ratio,
        "w2_ratio": bot_instance.w2_ratio,
        "w4_ratio": bot_instance.w4_ratio,
        "w8_ratio": bot_instance.w8_ratio,
        "dca_2_threshold": bot_instance.dca_2_threshold,
        "dca_4_threshold": bot_instance.dca_4_threshold,
        "dca_8_threshold": bot_instance.dca_8_threshold,
        "trailing_stop_threshold": bot_instance.trailing_stop_threshold,
        "trailing_sell_pct": bot_instance.trailing_sell_pct,
        "is_uptrend": bot_instance.is_uptrend,
        "is_rsi_oversold": bot_instance.is_rsi_oversold,
        "prev_close": prev_close,
        "hwm": bot_instance.hwm,
        "strategy_mode": bot_instance.strategy_mode,
        "base_price": base_price,
        "base_sma200": base_sma200,
        "etf_current_price": etf_current_price,
        "source": "snapshot" if use_snapshot else ("live_refresh" if force_refresh else "cache_or_live"),
        "server_time": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S"),
    }


AI_REPORT_FILE: str = "ai_report.json"
_ai_report_lock = threading.Lock()


def _generate_ai_report() -> Dict[str, Any]:
    import yfinance as yf
    import requests as req
    import pandas as pd

    now_check = datetime.now(ZoneInfo("Asia/Seoul"))
    if now_check.weekday() >= 5:
        return {"error": "주말에는 리포트를 생성하지 않습니다."}

    gemini_key: str = os.getenv("GEMINI_API_KEY", "")
    if not gemini_key:
        return {"error": "GEMINI_API_KEY not set"}

    analyze_symbols: List[str] = []
    slot_to_base: Dict[str, str] = {}
    if bot_instance:
        for sym in bot_instance.symbols:
            base: str = bot_instance.base_assets.get(sym, sym)
            if base != sym:
                slot_to_base[sym] = base
            target: str = base if base != sym else sym
            if target not in analyze_symbols:
                analyze_symbols.append(target)
    if not analyze_symbols:
        analyze_symbols = ["NVDA", "TSLA", "QQQ"]
    symbols: Dict[str, str] = {}
    for sym in analyze_symbols:
        try:
            import yfinance as _yf
            _info = _yf.Ticker(sym).info
            symbols[sym] = _info.get('shortName', _info.get('longName', sym))
        except Exception:
            symbols[sym] = sym
    market_data: List[str] = []

    for sym, name in symbols.items():
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period="1y")
            if len(hist) < 200:
                continue
            close: pd.Series = hist['Close']
            volume: pd.Series = hist['Volume']
            prev: float = float(close.iloc[-1])

            realtime_price: float = 0.0
            price_source: str = "전일종가"
            if bot_instance:
                try:
                    realtime_price = bot_instance.api.get_current_price(sym)
                except Exception:
                    pass
            if realtime_price <= 0:
                try:
                    info_price = ticker.info.get('regularMarketPrice', 0) or 0
                    if info_price > 0:
                        realtime_price = float(info_price)
                except Exception:
                    pass
            if realtime_price > 0:
                cur = realtime_price
                price_source = "실시간"
            else:
                cur = prev
                prev = float(close.iloc[-2])
            sma_20: float = float(close.tail(20).mean())
            sma_50: float = float(close.tail(50).mean())
            sma_200: float = float(close.tail(200).mean())
            high_52w: float = float(close.max())
            low_52w: float = float(close.min())
            vol_avg_20: float = float(volume.tail(20).mean())
            vol_today: float = float(volume.iloc[-1])
            day_chg: float = (cur - prev) / prev * 100
            week_chg: float = (cur - float(close.iloc[-5])) / float(close.iloc[-5]) * 100 if len(close) >= 5 else 0
            month_chg: float = (cur - float(close.iloc[-21])) / float(close.iloc[-21]) * 100 if len(close) >= 21 else 0
            from_high: float = (cur - high_52w) / high_52w * 100

            delta: pd.Series = close.diff()
            gain: pd.Series = delta.where(delta > 0, 0.0).ewm(alpha=1 / 14, adjust=False).mean()
            loss_raw: pd.Series = (-delta.where(delta < 0, 0.0)).ewm(alpha=1 / 14, adjust=False).mean()
            loss_safe: pd.Series = loss_raw.replace(0.0, 1e-10)
            rs: pd.Series = gain / loss_safe
            rsi: float = float((100 - (100 / (1 + rs))).iloc[-1])
            if pd.isna(rsi):
                rsi = 50.0

            golden_cross: bool = sma_50 > sma_200 and float(close.tail(50).iloc[0]) <= float(close.tail(200).iloc[0])
            death_cross: bool = sma_50 < sma_200 and float(close.tail(50).iloc[0]) >= float(close.tail(200).iloc[0])
            sma_signal: str = "골든크로스 진행" if golden_cross else ("데드크로스 진행" if death_cross else ("SMA50>SMA200 강세배열" if sma_50 > sma_200 else "SMA50<SMA200 약세배열"))

            vol_ratio: float = vol_today / vol_avg_20 if vol_avg_20 > 0 else 1.0

            recent_high: float = float(close.tail(20).max())
            recent_low: float = float(close.tail(20).min())

            analyst_str: str = ""
            try:
                info: Dict[str, Any] = ticker.info
                rec_key: str = info.get("recommendationKey", "")
                rec_mean: float = info.get("recommendationMean", 0)
                target_mean: float = info.get("targetMeanPrice", 0)
                target_high: float = info.get("targetHighPrice", 0)
                target_low: float = info.get("targetLowPrice", 0)
                num_analysts: int = info.get("numberOfAnalystOpinions", 0)
                if rec_key and num_analysts > 0:
                    rec_kr_map: Dict[str, str] = {"strong_buy": "적극매수", "buy": "매수", "hold": "중립", "sell": "매도", "strong_sell": "적극매도"}
                    rec_kr: str = rec_kr_map.get(rec_key, rec_key)
                    upside: float = (target_mean - cur) / cur * 100 if target_mean > 0 else 0
                    analyst_str = (
                        f"\n월가 컨센서스: {rec_kr} ({rec_mean:.1f}/5.0) | 애널리스트 {num_analysts}명"
                        f"\n목표가: 평균 ${target_mean:.2f} (현재가 대비 {upside:+.1f}%) | 최고 ${target_high:.2f} | 최저 ${target_low:.2f}"
                    )
            except Exception:
                pass

            data_str: str = (
                f"[{sym} ({name})]\n"
                f"현재가: ${cur:.2f} ({price_source}) | 전일종가: ${prev:.2f} | 전일대비: {day_chg:+.2f}%\n"
                f"주간 수익률: {week_chg:+.2f}% | 월간 수익률: {month_chg:+.2f}%\n"
                f"SMA20: ${sma_20:.2f} | SMA50: ${sma_50:.2f} | SMA200: ${sma_200:.2f}\n"
                f"이평선 신호: {sma_signal}\n"
                f"RSI(14): {rsi:.1f}\n"
                f"52주 최고: ${high_52w:.2f} (대비 {from_high:.1f}%) | 52주 최저: ${low_52w:.2f}\n"
                f"20일 최고: ${recent_high:.2f} | 20일 최저: ${recent_low:.2f}\n"
                f"거래량 비율(금일/20일평균): {vol_ratio:.2f}x | 금일: {vol_today:,.0f} | 20일평균: {vol_avg_20:,.0f}"
                f"{analyst_str}"
            )
            market_data.append(data_str)
        except Exception as e:
            market_data.append(f"[{sym}] 데이터 조회 실패: {e}")

    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))

    slot_info: str = ""
    if slot_to_base:
        pairs: List[str] = [f"{s} → 기초자산 {b}" for s, b in slot_to_base.items()]
        slot_info = f"\n보유 레버리지 ETF 매핑: {', '.join(pairs)}\n"

    prompt: str = f"""당신은 월가 출신의 시니어 주식 시장 기술적 분석 전문가입니다.
사용자가 현재 보유 중인 종목의 기술적 지표 데이터를 기반으로 맞춤 분석 리포트를 한국어로 작성하세요.
현재가가 '실시간(프리장)'인 종목은 미국 본장 개장 전 프리마켓 가격이며, 본장 시작 시 변동 가능성이 있습니다.
현재 시각: {now_kst.strftime('%Y년 %m월 %d일 %H:%M')} (한국시간)
{slot_info}
{chr(10).join(market_data)}

각 종목별로 다음을 분석하세요:

1. 추세 판단
   - 현재가와 SMA20/50/200의 위치 관계로 단기/중기/장기 추세 판단
   - 이평선 배열 상태 (정배열/역배열/수렴)
   - 골든크로스/데드크로스 여부와 신뢰도

2. 모멘텀 분석
   - RSI 수준과 과매수/과매도/중립 판단
   - 거래량 흐름 (평균 대비 증가/감소, 의미 해석)
   - 주간/월간 수익률로 본 모멘텀 방향

3. 핵심 가격대
   - 단기 지지선과 저항선 (20일 최고/최저, SMA 기반)
   - 52주 고점 대비 하락률로 본 위치

4. 월가 애널리스트 동향 (데이터가 있는 경우)
   - 컨센서스 방향 (매수/중립/매도)과 신뢰도
   - 목표가 대비 현재가 괴리율 해석
   - 기술적 분석과 월가 의견의 일치/괴리 여부

5. 보유 종목별 전략 제안
   - 레버리지 ETF 보유자 관점에서 기초자산 흐름이 레버리지 ETF에 미치는 영향
   - 추가 매수/관망/비중 축소 등 구체적 액션 제안
   - 주의할 변수와 리스크

마지막에 종합 의견:
- 보유 종목 간 상관관계와 포트폴리오 리스크 분석
- 기술적 분석과 월가 컨센서스를 종합한 현 시점 핵심 포인트

작성 규칙:
- 각 종목명 앞에 🟢(강세) 🟡(중립) 🔴(약세) 이모지 표시
- 구분선(---), 마크다운 기호(#, **, ```) 사용 금지
- 간결하지만 핵심이 담긴 문장으로 작성
- 숫자와 근거를 반드시 포함
- 본 분석은 기술적 지표 기반 참고자료임을 마지막에 한 줄로 명시"""

    try:
        url: str = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent?key={gemini_key}"
        payload: Dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 8192}
        }
        resp = req.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        result = resp.json()
        candidates = result.get("candidates", [])
        if not candidates:
            return {"error": "Gemini API 응답에 candidates가 없습니다."}
        candidate = candidates[0]
        finish_reason: str = candidate.get("finishReason", "")
        parts = candidate.get("content", {}).get("parts", [])
        if not parts or "text" not in parts[0]:
            return {"error": "Gemini API 응답에서 텍스트를 추출할 수 없습니다."}
        text: str = parts[0]["text"]
        if finish_reason == "MAX_TOKENS":
            text += "\n\n(분석이 길어 일부 생략되었습니다)"
        import re
        text = re.sub(r'-{3,}', '', text)
        text = re.sub(r'#{1,6}\s?', '', text)
        text = text.replace("```", "").replace("**", "")
        text = re.sub(r'\n{3,}', '\n\n', text).strip()

        report: Dict[str, Any] = {
            "report": text,
            "generated_at": now_kst.strftime("%Y-%m-%d %H:%M KST"),
            "model": "gemini-3.1-pro-preview"
        }
        with _ai_report_lock:
            with open(AI_REPORT_FILE, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)

        if bot_instance:
            preview: str = text[:500] + ("..." if len(text) > 500 else "")
            tg_msg: str = f"📊 [AI 시장 분석 발행]\n⏰ {report['generated_at']}\n\n{preview}"
            bot_instance.send_telegram_message(tg_msg)

        return report
    except Exception as e:
        return {"error": f"Gemini API 호출 실패: {e}"}


def _is_us_dst() -> bool:
    """미국 동부시간 썸머타임 여부 (ET offset이 -4이면 DST)"""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    return now_et.utcoffset().total_seconds() == -4 * 3600


def _auto_generate_report() -> None:
    """하루 2회 자동 리포트 (KST 10:00, 본장 시작 시점 - 썸머타임 자동 반영)"""
    import time as _time
    reported_sessions: set = set()
    while True:
        try:
            now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
            today_str: str = now_kst.strftime("%Y-%m-%d")
            hour: int = now_kst.hour
            minute: int = now_kst.minute

            morning_key: str = f"{today_str}-morning"
            market_key: str = f"{today_str}-market"

            market_hour: int = 22 if _is_us_dst() else 23
            market_min: int = 0

            if now_kst.weekday() < 5:
                if hour == 10 and minute < 15 and morning_key not in reported_sessions:
                    _generate_ai_report()
                    reported_sessions.add(morning_key)
                elif hour == market_hour and market_min <= minute < market_min + 15 and market_key not in reported_sessions:
                    _generate_ai_report()
                    reported_sessions.add(market_key)

            old_keys = [k for k in reported_sessions if not k.startswith(today_str)]
            for k in old_keys:
                reported_sessions.discard(k)
        except Exception:
            pass
        _time.sleep(60)


@app.post("/api/start")
async def start_bot(username: str = Depends(get_current_username)) -> Dict[str, str]:
    global bot_thread, bot_instance
    if bot_instance and not bot_instance.is_running:
        bot_thread = threading.Thread(target=bot_instance.run_loop, daemon=True)
        bot_thread.start()
        return {"status": "started", "message": "봇이 시작되었습니다."}
    return {"status": "already_running", "message": "이미 실행 중입니다."}

@app.post("/api/stop")
async def stop_bot(username: str = Depends(get_current_username)) -> Dict[str, str]:
    global bot_instance
    if bot_instance and bot_instance.is_running:
        bot_instance.stop_loop()
        return {"status": "stopped", "message": "봇이 중지되었습니다."}
    return {"status": "already_stopped", "message": "이미 중지되어 있습니다."}

if __name__ == "__main__":
    print("\n🚀 대시보드 주소: http://localhost:8000")
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
