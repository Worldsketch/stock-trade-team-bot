import os
import json
import time
import secrets
import threading
import shutil
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, List
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn
from dotenv import load_dotenv

# 라우터/설정 모듈 import 전에 .env를 먼저 로드해 환경변수 의존 상수 초기화를 안전하게 맞춘다.
load_dotenv()

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
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR: str = os.path.join(BASE_DIR, "runtime_data")
LEGACY_AI_REPORT_FILE: str = os.path.join(BASE_DIR, "ai_report.json")


def _get_ai_report_file_path() -> str:
    configured: str = os.getenv("AI_REPORT_FILE", "").strip()
    if configured:
        return configured
    return os.path.join(RUNTIME_DIR, "ai_report.json")


AI_REPORT_FILE: str = _get_ai_report_file_path()


def _is_ai_report_enabled() -> bool:
    return bool(os.getenv("GEMINI_API_KEY", "").strip())


def _ensure_ai_report_storage() -> None:
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    if AI_REPORT_FILE == LEGACY_AI_REPORT_FILE:
        return
    if os.path.exists(AI_REPORT_FILE):
        return
    if os.path.exists(LEGACY_AI_REPORT_FILE):
        target_dir: str = os.path.dirname(AI_REPORT_FILE)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)
        shutil.copy2(LEGACY_AI_REPORT_FILE, AI_REPORT_FILE)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_instance, bot_thread, _bot_starting
    load_dotenv()
    _ensure_ai_report_storage()
    
    app_key: str = os.getenv("KIS_APP_KEY", "")
    app_secret: str = os.getenv("KIS_APP_SECRET", "")
    account_no: str = os.getenv("KIS_ACCOUNT_NUMBER", "")
    account_code: str = os.getenv("KIS_ACCOUNT_CODE", "01")
    
    api: KoreaInvestmentAPI = KoreaInvestmentAPI(app_key, app_secret, account_no, account_code, is_mock=False)
    bot_instance = TradingBot(api)

    with _bot_control_lock:
        _bot_starting = True
    bot_thread = threading.Thread(target=_run_bot_loop_wrapper, daemon=True)
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
_auth_fail_lock = threading.Lock()
_auth_fail_state: Dict[str, Dict[str, float]] = {}
_AUTH_FAIL_WINDOW_SEC: float = 300.0
_AUTH_FAIL_MAX_COUNT: int = 8
_AUTH_BLOCK_SEC: float = 900.0
_bot_control_lock = threading.Lock()
_bot_starting: bool = False


def _env_flag(name: str, default: str = "false") -> bool:
    value: str = str(os.getenv(name, default)).strip().lower()
    return value in ("1", "true", "yes", "on")


_TRUST_PROXY_HEADERS: bool = _env_flag("TRUST_PROXY_HEADERS", "false")
_TRUSTED_PROXY_IPS: set = {
    ip.strip() for ip in str(os.getenv("TRUSTED_PROXY_IPS", "")).split(",") if ip.strip()
}


def _run_bot_loop_wrapper() -> None:
    global _bot_starting, bot_instance
    with _bot_control_lock:
        _bot_starting = False
    try:
        if bot_instance:
            bot_instance.run_loop()
    finally:
        with _bot_control_lock:
            _bot_starting = False


def _get_client_ip(request: Request) -> str:
    remote_ip: str = ""
    if request.client and request.client.host:
        remote_ip = str(request.client.host).strip()
    if _TRUST_PROXY_HEADERS:
        # TRUSTED_PROXY_IPS가 비어있으면 모든 프록시를 신뢰, 채워져 있으면 해당 프록시만 신뢰
        if (not _TRUSTED_PROXY_IPS) or (remote_ip in _TRUSTED_PROXY_IPS):
            xff: str = str(request.headers.get("x-forwarded-for", "")).strip()
            if xff:
                forwarded: str = xff.split(",")[0].strip()
                if forwarded:
                    return forwarded
    if remote_ip:
        return remote_ip
    return "unknown"


def _load_auth_users() -> Dict[str, str]:
    # 1) ADMIN_USERS_JSON='{"user":"pass"}' 형식 우선
    users_json: str = str(os.getenv("ADMIN_USERS_JSON", "")).strip()
    if users_json:
        try:
            parsed = json.loads(users_json)
            if isinstance(parsed, dict):
                users: Dict[str, str] = {}
                for raw_u, raw_p in parsed.items():
                    u: str = str(raw_u).strip()
                    p: str = str(raw_p)
                    if u and p:
                        users[u] = p
                if users:
                    return users
        except Exception:
            pass

    # 2) ADMIN_USERS='user1:pass1,user2:pass2' 형식
    users_csv: str = str(os.getenv("ADMIN_USERS", "")).strip()
    if users_csv:
        users: Dict[str, str] = {}
        for pair in users_csv.split(","):
            item: str = pair.strip()
            if not item or ":" not in item:
                continue
            u, p = item.split(":", 1)
            username: str = u.strip()
            password: str = p.strip()
            if username and password:
                users[username] = password
        if users:
            return users

    # 3) 하위호환: 단일 관리자 계정
    admin_user: str = str(os.getenv("ADMIN_USERNAME", "")).strip()
    admin_pass: str = str(os.getenv("ADMIN_PASSWORD", "")).strip()
    if admin_user and admin_pass:
        return {admin_user: admin_pass}
    return {}


def get_current_username(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
):
    auth_users: Dict[str, str] = _load_auth_users()
    if not auth_users:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_USERS(또는 ADMIN_USERS_JSON) / ADMIN_USERNAME+ADMIN_PASSWORD 환경변수가 설정되지 않았습니다.",
        )

    ip: str = _get_client_ip(request)
    now_ts: float = time.time()
    with _auth_fail_lock:
        state: Dict[str, float] = dict(_auth_fail_state.get(ip, {}))
        first_ts: float = float(state.get("first_ts", now_ts))
        if (now_ts - first_ts) > _AUTH_FAIL_WINDOW_SEC:
            state = {"count": 0.0, "first_ts": now_ts, "blocked_until": 0.0}
        blocked_until: float = float(state.get("blocked_until", 0.0))
        _auth_fail_state[ip] = state
    if blocked_until > now_ts:
        remain: int = int(blocked_until - now_ts)
        raise HTTPException(status_code=429, detail=f"로그인 시도 제한 중입니다. {remain}초 후 다시 시도하세요.")

    input_username: str = str(credentials.username or "").strip()
    input_password: str = str(credentials.password or "")
    expected_password: Optional[str] = auth_users.get(input_username)
    is_valid: bool = False
    if expected_password is not None:
        is_valid = secrets.compare_digest(input_password, expected_password)
    else:
        # username 미존재 시에도 짧은 비교를 수행해 응답 시간 편차를 줄인다.
        fallback_pw: str = next(iter(auth_users.values()))
        secrets.compare_digest(input_password, fallback_pw)

    if not is_valid:
        time.sleep(0.15)
        with _auth_fail_lock:
            state = dict(_auth_fail_state.get(ip, {"count": 0.0, "first_ts": now_ts, "blocked_until": 0.0}))
            first_ts = float(state.get("first_ts", now_ts))
            if (now_ts - first_ts) > _AUTH_FAIL_WINDOW_SEC:
                first_ts = now_ts
                state["count"] = 0.0
            state["first_ts"] = first_ts
            state["count"] = float(state.get("count", 0.0)) + 1.0
            if int(state["count"]) >= _AUTH_FAIL_MAX_COUNT:
                state["blocked_until"] = now_ts + _AUTH_BLOCK_SEC
                state["count"] = 0.0
                state["first_ts"] = now_ts
            _auth_fail_state[ip] = state
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    with _auth_fail_lock:
        _auth_fail_state.pop(ip, None)
    return input_username

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_index() -> str:
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

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
        ai_report_file=AI_REPORT_FILE,
        is_ai_enabled=lambda: _is_ai_report_enabled(),
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

    total_equity: float = bot_instance._calc_total_equity()
    effective_buy_cap_ratio, cash_ratio, cap_reason = bot_instance._resolve_daily_buy_cap_ratio(total_equity)
    target_weights: Dict[str, float] = bot_instance._resolve_symbol_target_weights(bot_instance.symbols)

    return {
        "symbols": bot_instance.symbols,
        "base_assets": bot_instance.base_assets,
        "base_buy_ratio": bot_instance.base_buy_ratio,
        "daily_fixed_buy_qty": int(getattr(bot_instance, "daily_fixed_buy_qty", 1) or 1),
        "w2_ratio": bot_instance.w2_ratio,
        "w4_ratio": bot_instance.w4_ratio,
        "w8_ratio": bot_instance.w8_ratio,
        "daily_buy_cap_ratio": effective_buy_cap_ratio,
        "daily_buy_cap_ratio_effective": effective_buy_cap_ratio,
        "daily_buy_cap_ratio_legacy": bot_instance.daily_buy_cap_ratio,
        "daily_buy_used_usd": float(bot_instance.daily_state.get("daily_buy_used_usd", 0.0) or 0.0),
        "daily_buy_cap_reason": str(bot_instance.daily_state.get("daily_buy_cap_reason", cap_reason) or cap_reason),
        "cash_ratio": float(bot_instance.daily_state.get("cash_ratio", cash_ratio) or cash_ratio),
        "cash_floor_hard_ratio": bot_instance.cash_floor_hard_ratio,
        "cash_floor_soft_ratio": bot_instance.cash_floor_soft_ratio,
        "buy_cap_high_cash_trigger": bot_instance.buy_cap_high_cash_trigger,
        "buy_cap_ratio_high_cash": bot_instance.buy_cap_ratio_high_cash,
        "buy_cap_ratio_mid_cash": bot_instance.buy_cap_ratio_mid_cash,
        "buy_cap_ratio_low_cash": bot_instance.buy_cap_ratio_low_cash,
        "target_weights": target_weights,
        "dca_2_threshold": bot_instance.dca_2_threshold,
        "dca_4_threshold": bot_instance.dca_4_threshold,
        "dca_8_threshold": bot_instance.dca_8_threshold,
        "trailing_stop_threshold": bot_instance.trailing_stop_threshold,
        "trailing_sell_pct": bot_instance.trailing_sell_pct,
        "auto_take_profit_enabled": bot_instance.auto_take_profit_enabled,
        "take_profit_rules": [{"level": lv, "sell_pct": pct} for lv, pct in bot_instance._take_profit_rules],
        "is_uptrend": bot_instance.is_uptrend,
        "is_rsi_oversold": bot_instance.is_rsi_oversold,
        "prev_close": prev_close,
        "hwm": bot_instance.hwm,
        "strategy_mode": bot_instance.strategy_mode,
        "auto_active_mode": bot_instance.auto_active_mode,
        "auto_defensive_cash_ratio": bot_instance.auto_defensive_cash_ratio,
        "auto_defensive_sma_count": bot_instance.auto_defensive_sma_count,
        "strategy_modes": bot_instance.strategy_modes,
        "base_price": base_price,
        "base_sma200": base_sma200,
        "etf_current_price": etf_current_price,
        "source": "snapshot" if use_snapshot else ("live_refresh" if force_refresh else "cache_or_live"),
        "server_time": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%H:%M:%S"),
    }


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
        return {
            "error": "AI 시장 분석 기능이 비활성화되어 있습니다.",
            "disabled": True,
            "reason": "GEMINI_API_KEY not set",
        }

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
        # 슬롯이 비어있을 때는 특정 종목이 아닌 대표 시장 지수 ETF로 중립 분석
        analyze_symbols = ["SPY", "QQQ", "IWM"]
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
        headers: Dict[str, str] = {
            "x-goog-api-key": gemini_key,
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 8192}
        }
        preferred_model: str = str(os.getenv("GEMINI_MODEL", "") or "").strip()
        fallback_models_raw: str = str(
            os.getenv(
                "GEMINI_MODEL_FALLBACKS",
                "gemini-2.5-pro,gemini-2.5-flash,gemini-1.5-pro",
            ) or ""
        ).strip()
        model_candidates: List[str] = []
        if preferred_model:
            model_candidates.append(preferred_model)
        for model_name in fallback_models_raw.split(","):
            m: str = str(model_name or "").strip()
            if m and m not in model_candidates:
                model_candidates.append(m)
        if not model_candidates:
            model_candidates = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-1.5-pro"]

        api_versions: List[str] = ["v1beta", "v1"]
        chosen_model: str = ""
        chosen_api_ver: str = ""
        finish_reason: str = ""
        text: str = ""
        last_http_status: int = 0
        last_http_error: str = ""

        for api_ver in api_versions:
            if text:
                break
            for model in model_candidates:
                url: str = f"https://generativelanguage.googleapis.com/{api_ver}/models/{model}:generateContent"
                try:
                    resp = req.post(url, headers=headers, json=payload, timeout=120)
                    if resp.status_code >= 400:
                        last_http_status = int(resp.status_code)
                        try:
                            err_payload: Dict[str, Any] = resp.json()
                            err_msg: str = str(err_payload.get("error", {}).get("message", "") or "")
                        except Exception:
                            err_msg = str(resp.text[:220] or "")
                        last_http_error = err_msg
                        # 모델/버전 불일치 시 다음 후보로 자동 재시도
                        if last_http_status in (400, 404):
                            if bot_instance:
                                bot_instance.log(
                                    f"[Gemini API 재시도] status={last_http_status} model={model} api={api_ver}",
                                    send_tg=False,
                                )
                            continue
                        resp.raise_for_status()

                    result = resp.json()
                    candidates = result.get("candidates", [])
                    if not candidates:
                        continue
                    candidate = candidates[0]
                    finish_reason = str(candidate.get("finishReason", "") or "")
                    parts = candidate.get("content", {}).get("parts", [])
                    texts: List[str] = [
                        str(part.get("text", "") or "")
                        for part in parts
                        if isinstance(part, dict) and str(part.get("text", "") or "").strip()
                    ]
                    if not texts:
                        continue
                    text = "\n".join(texts).strip()
                    chosen_model = model
                    chosen_api_ver = api_ver
                    break
                except req.HTTPError as http_err:
                    res = getattr(http_err, "response", None)
                    if res is not None:
                        last_http_status = int(getattr(res, "status_code", 0) or 0)
                    last_http_error = str(http_err)
                    continue

        if not text:
            if bot_instance:
                bot_instance.log(
                    f"[Gemini API 실패 상세] status={last_http_status} err={last_http_error[:160]}",
                    send_tg=False,
                )
            err_lower: str = str(last_http_error or "").lower()
            if ("api key not valid" in err_lower) or ("api_key_invalid" in err_lower):
                return {"error": "GEMINI_API_KEY가 유효하지 않습니다. 서버 env의 키를 새로 교체해주세요."}
            return {"error": "Gemini API 호출 실패 (모델/버전 또는 API 키 확인 필요)"}

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
            "model": chosen_model or preferred_model or "gemini",
            "api_version": chosen_api_ver or "unknown",
        }
        with _ai_report_lock:
            report_dir: str = os.path.dirname(AI_REPORT_FILE)
            if report_dir:
                os.makedirs(report_dir, exist_ok=True)
            with open(AI_REPORT_FILE, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)

        if bot_instance:
            preview: str = text[:500] + ("..." if len(text) > 500 else "")
            tg_msg: str = f"📊 [AI 시장 분석 발행]\n⏰ {report['generated_at']}\n\n{preview}"
            bot_instance.send_telegram_message(tg_msg)

        return report
    except Exception as e:
        # 예외 원문(URL 포함 가능)을 사용자 응답에 직접 노출하지 않음
        if bot_instance:
            bot_instance.log(f"[Gemini API 호출 실패] {type(e).__name__}", send_tg=False)
        return {"error": "Gemini API 호출 실패 (관리자 로그 확인 필요)"}


def _auto_generate_report() -> None:
    """하루 2회 자동 리포트 (ET 프리장 04:00, 본장 09:30 기준)"""
    import time as _time
    reported_sessions: set = set()
    while True:
        try:
            if not _is_ai_report_enabled():
                _time.sleep(300)
                continue
            now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
            now_et = now_kst.astimezone(ZoneInfo("America/New_York"))
            et_date: str = now_et.strftime("%Y-%m-%d")
            hour: int = now_et.hour
            minute: int = now_et.minute

            premarket_key: str = f"{et_date}-premarket-open"
            regular_key: str = f"{et_date}-regular-open"

            if now_et.weekday() < 5:
                if hour == 4 and minute < 15 and premarket_key not in reported_sessions:
                    _generate_ai_report()
                    reported_sessions.add(premarket_key)
                elif hour == 9 and 30 <= minute < 45 and regular_key not in reported_sessions:
                    _generate_ai_report()
                    reported_sessions.add(regular_key)

            old_keys = [k for k in reported_sessions if not k.startswith(et_date)]
            for k in old_keys:
                reported_sessions.discard(k)
        except Exception:
            pass
        _time.sleep(60)


@app.post("/api/start")
async def start_bot(username: str = Depends(get_current_username)) -> Dict[str, str]:
    global bot_thread, bot_instance, _bot_starting
    with _bot_control_lock:
        if not bot_instance:
            return {"status": "error", "message": "봇이 초기화되지 않았습니다."}
        if _bot_starting:
            return {"status": "starting", "message": "봇이 시작 중입니다."}
        if bot_instance.is_running:
            return {"status": "already_running", "message": "이미 실행 중입니다."}
        if bot_thread and bot_thread.is_alive():
            return {"status": "already_running", "message": "이미 실행 중입니다."}
        _bot_starting = True
        try:
            bot_thread = threading.Thread(target=_run_bot_loop_wrapper, daemon=True)
            bot_thread.start()
            return {"status": "started", "message": "봇이 시작되었습니다."}
        except Exception:
            _bot_starting = False
            raise

@app.post("/api/stop")
async def stop_bot(username: str = Depends(get_current_username)) -> Dict[str, str]:
    global bot_instance, _bot_starting
    with _bot_control_lock:
        if bot_instance and (bot_instance.is_running or _bot_starting):
            bot_instance.stop_loop()
            _bot_starting = False
            return {"status": "stopped", "message": "봇이 중지되었습니다."}
        return {"status": "already_stopped", "message": "이미 중지되어 있습니다."}

if __name__ == "__main__":
    app_host: str = os.getenv("APP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        app_port: int = int(os.getenv("APP_PORT", "8000").strip())
    except Exception:
        app_port = 8000
    print(f"\n🚀 대시보드 로컬 주소: http://{app_host}:{app_port}")
    uvicorn.run("app:app", host=app_host, port=app_port, reload=False)
