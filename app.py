import os
import sys
import json
import time
import secrets
import threading
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, List
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn
from dotenv import load_dotenv

from api import KoreaInvestmentAPI
from bot import TradingBot

bot_instance: Optional[TradingBot] = None
bot_thread: Optional[threading.Thread] = None
_status_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_strategy_cache: Dict[str, Any] = {"data": None, "ts": 0.0}
_realized_pnl_cache: Dict[str, Any] = {"data": None, "ts": 0.0}


def _calc_realized_pnl() -> Dict[str, Any]:
    """trade_log.json에서 평균단가 기반 누적 실현 손익을 계산합니다."""
    now: float = time.time()
    if _realized_pnl_cache["data"] and (now - _realized_pnl_cache["ts"]) < 60:
        return _realized_pnl_cache["data"]

    trade_file: str = "trade_log.json"
    if not os.path.exists(trade_file):
        return {"total": 0.0, "count": 0, "wins": 0, "losses": 0}

    try:
        with open(trade_file, "r", encoding="utf-8") as f:
            trades: List[Dict[str, Any]] = json.load(f)
    except Exception:
        return {"total": 0.0, "count": 0, "wins": 0, "losses": 0}

    holdings: Dict[str, Dict[str, float]] = {}
    total_pnl: float = 0.0
    sell_count: int = 0
    win_count: int = 0
    loss_count: int = 0

    for t in trades:
        sym: str = t.get("symbol", "")
        side: str = t.get("side", "")
        qty: float = float(t.get("qty", 0))
        price: float = float(t.get("price", 0))
        if qty <= 0 or price <= 0:
            continue

        if sym not in holdings:
            holdings[sym] = {"qty": 0.0, "avg_cost": 0.0}

        h = holdings[sym]
        if side == "매수":
            total_cost: float = h["qty"] * h["avg_cost"] + qty * price
            h["qty"] += qty
            h["avg_cost"] = total_cost / h["qty"] if h["qty"] > 0 else 0.0
        elif side == "매도":
            if h["qty"] > 0 and h["avg_cost"] > 0:
                pnl: float = qty * (price - h["avg_cost"])
                total_pnl += pnl
                sell_count += 1
                if pnl >= 0:
                    win_count += 1
                else:
                    loss_count += 1
                h["qty"] = max(0.0, h["qty"] - qty)

    result: Dict[str, Any] = {
        "total": round(total_pnl, 2),
        "count": sell_count,
        "wins": win_count,
        "losses": loss_count,
    }
    _realized_pnl_cache["data"] = result
    _realized_pnl_cache["ts"] = now
    return result

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

    yield
    
    print("\n[웹 서버 종료] 봇 루프를 중단합니다. (포지션은 유지됩니다)")
    if bot_instance:
        bot_instance.stop_loop()
    if bot_thread and bot_thread.is_alive():
        bot_thread.join(timeout=5)

app = FastAPI(title="한국투자증권 자동매매 봇", lifespan=lifespan)
security = HTTPBasic()

def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, os.getenv("ADMIN_USERNAME", "admin"))
    correct_password = secrets.compare_digest(credentials.password, os.getenv("ADMIN_PASSWORD", "admin123!"))
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

@app.get("/api/status")
async def get_status(username: str = Depends(get_current_username)) -> Dict[str, Any]:
    global _status_cache
    if not bot_instance:
        return {"error": "Bot is not initialized."}

    now: float = time.time()
    if _status_cache["data"] and (now - _status_cache["ts"]) < 5:
        return _status_cache["data"]

    try:
        _item: str = bot_instance.symbols[0] if bot_instance.symbols else "AAPL"
        data: Dict[str, Any] = bot_instance.api.get_balance_and_positions(item_cd=_item)
        current_symbols: list = bot_instance.symbols
        positions_list: list = []
        held_symbols: set = set()

        for pos in data["positions"]:
            sym: str = pos["symbol"]
            if sym not in current_symbols:
                continue
            held_symbols.add(sym)
            cur_price: float = pos.get("current_price", 0.0)
            if cur_price <= 0:
                try:
                    cur_price = bot_instance.api.get_current_price(sym)
                except Exception:
                    pass
            slot_info = next((s for s in bot_instance.slot_manager.get_active_slots() if s['symbol'] == sym), {})
            positions_list.append({
                "symbol": sym,
                "quantity": pos.get("quantity", 0.0),
                "avg_price": pos.get("avg_price", 0.0),
                "current_price": cur_price,
                "evlu_amt": pos.get("evlu_amt", 0.0),
                "evlu_pfls": pos.get("evlu_pfls", 0.0),
                "return_rate": pos.get("return_rate", 0.0),
                "pchs_amt": pos.get("pchs_amt", 0.0),
                "is_leveraged": slot_info.get("is_leveraged", False),
                "base_asset": slot_info.get("base_asset", sym),
            })

        for sym in current_symbols:
            if sym not in held_symbols:
                slot_info = next((s for s in bot_instance.slot_manager.get_active_slots() if s['symbol'] == sym), {})
                positions_list.append({
                    "symbol": sym, "quantity": 0.0, "avg_price": 0.0,
                    "current_price": 0.0, "return_rate": 0.0,
                    "is_leveraged": slot_info.get("is_leveraged", False),
                    "base_asset": slot_info.get("base_asset", sym),
                })

        slot_order: Dict[str, int] = {s['symbol']: i for i, s in enumerate(bot_instance.slot_manager.get_active_slots())}
        positions_list.sort(key=lambda p: slot_order.get(p["symbol"], 999))

        api_exrt: float = data.get("exchange_rate", 0.0)
        if api_exrt > 0:
            bot_instance.exchange_rate = api_exrt
        elif not bot_instance.is_running:
            bot_instance.update_exchange_rate()

        daily_pnl_usd: float = 0.0
        for pos in positions_list:
            sym = pos["symbol"]
            qty = pos.get("quantity", 0.0)
            cur = pos.get("current_price", 0.0)
            prev = bot_instance.prev_close.get(sym, 0.0)
            if qty > 0 and prev > 0 and cur > 0:
                daily_pnl_usd += (cur - prev) * qty

        result: Dict[str, Any] = {
            "is_running": bot_instance.is_running,
            "usd_balance": data["usd_balance"],
            "krw_balance": data.get("krw_balance", 0.0),
            "krw_cash": data.get("krw_cash", 0.0),
            "exchange_rate": bot_instance.exchange_rate,
            "positions": positions_list,
            "logs": bot_instance.logs,
            "tot_evlu_pfls": data.get("tot_evlu_pfls", 0.0),
            "tot_pchs_amt": data.get("tot_pchs_amt", 0.0),
            "tot_stck_evlu": data.get("tot_stck_evlu", 0.0),
            "total_eval": data["usd_balance"] + data.get("tot_stck_evlu", 0.0),
            "daily_pnl_usd": round(daily_pnl_usd, 2),
            "strategy_mode": bot_instance.strategy_mode,
            "auto_active": bot_instance.auto_active_mode,
            "realized_pnl": _calc_realized_pnl(),
            "slots": bot_instance.slot_manager.get_active_slots(),
            "max_slots": bot_instance.slot_manager.max_slots,
            "market_open": bot_instance.is_active_trading_time(bot_instance.get_eastern_time()),
            "is_dst": bool(bot_instance.get_eastern_time().dst()),
            "et_time": bot_instance.get_eastern_time().strftime("%H:%M"),
        }
    except Exception as e:
        try:
            result = bot_instance.get_status()
        except Exception:
            result = {"error": f"상태 조회 실패: {e}", "is_running": False, "positions": []}

    _status_cache = {"data": result, "ts": now}
    return result

def _monitor_sell_fill(symbol: str, qty: int, price: float, label: str) -> None:
    for i in range(60):
        time.sleep(5)
        if not bot_instance:
            return
        try:
            orders = bot_instance.api.get_pending_orders()
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


@app.post("/api/sell")
async def manual_sell(request: Request, username: str = Depends(get_current_username)) -> Dict[str, Any]:
    global _status_cache
    if not bot_instance:
        return {"success": False, "message": "봇이 초기화되지 않았습니다."}

    try:
        body: Dict[str, Any] = await request.json()
        symbol: str = body.get("symbol", "")
        percent: int = int(body.get("percent", 0))
    except Exception:
        return {"success": False, "message": "잘못된 요청입니다."}

    if symbol not in bot_instance.symbols:
        return {"success": False, "message": f"슬롯에 등록되지 않은 종목: {symbol}"}
    if percent not in (10, 25, 50, 100):
        return {"success": False, "message": f"잘못된 매도 비율: {percent}%"}

    try:
        data: Dict[str, Any] = bot_instance.api.get_balance_and_positions(item_cd=symbol)
        position = None
        for pos in data["positions"]:
            if pos["symbol"] == symbol and pos.get("quantity", 0) > 0:
                position = pos
                break

        if not position:
            return {"success": False, "message": f"{symbol} 보유 포지션이 없습니다."}

        total_qty: int = int(position["quantity"])
        sell_qty: int = max(1, int(total_qty * percent / 100))
        if percent == 100:
            sell_qty = total_qty

        current_price: float = position.get("current_price", 0.0)
        if current_price <= 0:
            current_price = bot_instance.api.get_current_price(symbol)
        if current_price <= 0:
            return {"success": False, "message": f"{symbol} 현재가를 가져올 수 없습니다."}

        now_et = datetime.now(ZoneInfo("America/New_York"))
        is_regular: bool = (9 <= now_et.hour < 16) and (now_et.weekday() < 5)

        if is_regular:
            sell_price: float = round(current_price * 0.98, 2)
            order_desc: str = f"시장가 (하한 ${sell_price:.2f})"
        else:
            sell_price = round(current_price * 0.995, 2)
            order_desc = f"지정가 ${sell_price:.2f} (현재가 -0.5%)"

        success: bool = bot_instance.api.place_order(symbol, sell_qty, sell_price, is_buy=False)

        if success:
            bot_instance.manual_sell_block[symbol] = time.time()
            _status_cache = {"data": None, "ts": 0.0}
            est_amount: float = sell_qty * current_price
            krw_est: float = est_amount * bot_instance.exchange_rate
            label: str = f"{percent}%"
            msg: str = f"📤 [수동 매도 주문 접수]\n종목: {symbol}\n수량: {sell_qty}주 ({label})\n{order_desc}\n예상 금액: ${est_amount:,.2f} (약 {krw_est:,.0f}원)"
            bot_instance.send_telegram_message(msg)
            bot_instance.log(f"📤 [수동매도 접수] {symbol} {sell_qty}주 {order_desc} ({label})")
            bot_instance._log_trade(symbol, "매도", sell_qty, sell_price, sell_qty * current_price, f"[{bot_instance._get_mode_label()}] 수동 매도 ({label})")
            t = threading.Thread(target=_monitor_sell_fill, args=(symbol, sell_qty, current_price, label), daemon=True)
            t.start()
            return {"success": True, "message": f"{symbol} {sell_qty}주 매도 주문 완료", "qty": sell_qty, "price": sell_price}
        else:
            return {"success": False, "message": f"{symbol} 매도 주문 실패"}
    except Exception as e:
        return {"success": False, "message": f"매도 처리 오류: {str(e)}"}


@app.get("/api/pending-orders")
async def get_pending_orders(username: str = Depends(get_current_username)) -> Dict[str, Any]:
    if not bot_instance:
        return {"orders": []}
    try:
        orders = bot_instance.api.get_pending_orders()
        return {"orders": orders}
    except Exception as e:
        return {"orders": [], "error": str(e)}


@app.post("/api/cancel-order")
async def cancel_order(request: Request, username: str = Depends(get_current_username)) -> Dict[str, Any]:
    if not bot_instance:
        return {"success": False, "message": "봇이 초기화되지 않았습니다."}
    try:
        body: Dict[str, Any] = await request.json()
        order_no: str = body.get("order_no", "")
        symbol: str = body.get("symbol", "")
        remaining_qty: int = int(body.get("remaining_qty", 0))
    except Exception:
        return {"success": False, "message": "잘못된 요청입니다."}

    if not order_no or not symbol:
        return {"success": False, "message": "주문번호와 종목이 필요합니다."}

    try:
        success: bool = bot_instance.api.cancel_order(order_no, symbol, remaining_qty)
        if success:
            bot_instance.log(f"🚫 [주문취소] {symbol} 주문번호 {order_no}")
            return {"success": True, "message": f"{symbol} 주문 취소 완료"}
        else:
            return {"success": False, "message": f"{symbol} 주문 취소 실패"}
    except Exception as e:
        return {"success": False, "message": f"취소 처리 오류: {str(e)}"}


_chart_cache: Dict[str, Any] = {}

@app.get("/api/chart-data")
async def get_chart_data(symbol: str = "", period: str = "5d", interval: str = "5m", username: str = Depends(get_current_username)) -> Dict[str, Any]:
    import yfinance as yf
    import pandas as pd

    if not symbol and bot_instance and bot_instance.symbols:
        symbol = bot_instance.symbols[0]
    if not symbol:
        return {"candles": [], "symbol": "", "trades": []}

    cache_key: str = f"{symbol}_{period}_{interval}"
    now: float = time.time()
    ttl: float = 30.0 if interval in ("1m", "5m") else 300.0
    cached = _chart_cache.get(cache_key)
    if cached and (now - cached["ts"]) < ttl:
        return cached["data"]

    try:
        ticker = yf.Ticker(symbol)
        use_prepost: bool = interval not in ("1d", "1wk", "1mo")
        hist = ticker.history(period=period, interval=interval, prepost=use_prepost)
        if hist.empty:
            return {"candles": [], "symbol": symbol}

        timestamps: pd.Series = hist.index.astype('int64') // 10**9
        candles: list = pd.DataFrame({
            "time": timestamps,
            "open": hist["Open"].round(2),
            "high": hist["High"].round(2),
            "low": hist["Low"].round(2),
            "close": hist["Close"].round(2),
            "volume": hist["Volume"].astype(int),
        }).to_dict("records")

        trades: list = []
        trade_file: str = "trade_log.json"
        if os.path.exists(trade_file):
            with open(trade_file, "r", encoding="utf-8") as f:
                all_trades = json.load(f)
            for t in all_trades:
                if t.get("symbol") == symbol:
                    trades.append({"time": t.get("timestamp", ""), "side": t.get("side", ""), "price": t.get("price", 0), "qty": t.get("qty", 0)})

        result: Dict[str, Any] = {"candles": candles, "symbol": symbol, "trades": trades}
        _chart_cache[cache_key] = {"data": result, "ts": now}
        return result
    except Exception as e:
        return {"candles": [], "symbol": symbol, "error": str(e)}


@app.get("/api/equity-history")
async def get_equity_history(username: str = Depends(get_current_username)) -> Dict[str, Any]:
    try:
        equity_file: str = "equity_log.json"
        if os.path.exists(equity_file):
            with open(equity_file, 'r', encoding='utf-8') as f:
                import json as _json
                history = _json.load(f)
            return {"history": history}
        return {"history": []}
    except Exception as e:
        return {"history": [], "error": str(e)}


@app.get("/api/trade-history")
async def get_trade_history(username: str = Depends(get_current_username)) -> Dict[str, Any]:
    try:
        trade_file: str = "trade_log.json"
        if os.path.exists(trade_file):
            import json as _json
            with open(trade_file, 'r', encoding='utf-8') as f:
                trades = _json.load(f)
            trades.reverse()
            return {"trades": trades}
        return {"trades": []}
    except Exception as e:
        return {"trades": [], "error": str(e)}


@app.get("/api/strategy-params")
async def get_strategy_params(username: str = Depends(get_current_username)) -> Dict[str, Any]:
    global _strategy_cache
    if not bot_instance:
        return {"error": "Bot not initialized"}

    now: float = time.time()
    cached_market = _strategy_cache.get("market")
    cache_ttl: float = 30.0 if bot_instance.is_active_trading_time(bot_instance.get_eastern_time()) else 300.0
    if cached_market and (now - _strategy_cache["ts"]) < cache_ttl:
        base_price, base_sma200, etf_current_price = cached_market["base_price"], cached_market["base_sma200"], cached_market["etf_current_price"]
        prev_close = cached_market["prev_close"]
    else:
        prev_close = dict(bot_instance.prev_close)
        base_price = {}
        base_sma200 = {}
        etf_current_price = {}
        try:
            import yfinance as yf
            for etf_sym, base_sym in bot_instance.base_assets.items():
                ticker = yf.Ticker(base_sym)
                hist = ticker.history(period="1y")
                if len(hist) >= 200:
                    base_sma200[etf_sym] = float(hist['Close'].tail(200).mean())

                    realtime_base: float = bot_instance.api.get_current_price(base_sym)
                    base_price[etf_sym] = realtime_base if realtime_base > 0 else float(hist['Close'].iloc[-1])

                realtime_etf: float = bot_instance.api.get_current_price(etf_sym)
                if realtime_etf > 0:
                    etf_current_price[etf_sym] = realtime_etf
                else:
                    etf_ticker = yf.Ticker(etf_sym)
                    etf_hist = etf_ticker.history(period="5d")
                    if len(etf_hist) >= 1:
                        etf_current_price[etf_sym] = float(etf_hist['Close'].iloc[-1])

                if all(v == 0.0 for v in prev_close.values()):
                    etf_ticker = yf.Ticker(etf_sym)
                    etf_hist_pc = etf_ticker.history(period="5d")
                    if len(etf_hist_pc) >= 2:
                        prev_close[etf_sym] = float(etf_hist_pc['Close'].iloc[-2])
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


@app.get("/api/ai-report")
async def get_ai_report(username: str = Depends(get_current_username)) -> Dict[str, Any]:
    try:
        if os.path.exists(AI_REPORT_FILE):
            with open(AI_REPORT_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"report": None, "message": "아직 생성된 리포트가 없습니다."}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/ai-report/refresh")
async def refresh_ai_report(username: str = Depends(get_current_username)) -> Dict[str, Any]:
    try:
        result: Dict[str, Any] = _generate_ai_report()
        if result.get("error"):
            return {"success": False, "message": result["error"]}
        return {"success": True, "report": result.get("report", ""), "generated_at": result.get("generated_at", "")}
    except Exception as e:
        return {"success": False, "message": f"리포트 생성 실패: {e}"}


@app.get("/api/strategy-mode")
async def get_strategy_mode(username: str = Depends(get_current_username)) -> Dict[str, Any]:
    if not bot_instance:
        return {"error": "봇이 초기화되지 않았습니다."}
    mode: str = bot_instance.strategy_mode
    auto_active: str = bot_instance.auto_active_mode
    active: str = auto_active if mode == "auto" else mode
    return {
        "mode": mode,
        "auto_active": auto_active,
        "active": active,
    }


@app.post("/api/strategy-mode")
async def set_strategy_mode(request: Request, username: str = Depends(get_current_username)) -> Dict[str, Any]:
    if not bot_instance:
        return {"error": "봇이 초기화되지 않았습니다."}
    body: Dict[str, Any] = await request.json()
    mode: str = body.get("mode", "")
    if bot_instance.set_strategy_mode(mode):
        return {"status": "ok", "mode": mode, "auto_active": bot_instance.auto_active_mode}
    return {"error": f"유효하지 않은 모드: {mode}"}


@app.get("/api/slots")
async def get_slots(username: str = Depends(get_current_username)) -> Dict[str, Any]:
    if not bot_instance:
        return {"slots": [], "max_slots": 6}
    return {
        "slots": bot_instance.slot_manager.get_active_slots(),
        "max_slots": bot_instance.slot_manager.max_slots,
        "current_count": len(bot_instance.symbols),
    }


@app.post("/api/slots/add")
async def add_slot(request: Request, username: str = Depends(get_current_username)) -> Dict[str, Any]:
    global _status_cache
    if not bot_instance:
        return {"success": False, "message": "봇이 초기화되지 않았습니다."}
    try:
        body: Dict[str, Any] = await request.json()
        symbol: str = body.get("symbol", "").strip().upper()
    except Exception:
        return {"success": False, "message": "잘못된 요청입니다."}
    if not symbol:
        return {"success": False, "message": "종목 코드를 입력해주세요."}
    buy_percent: float = float(body.get("buy_percent", 0))
    result: Dict[str, Any] = bot_instance.add_symbol(symbol, buy_percent=buy_percent)
    if result.get("success"):
        _status_cache = {"data": None, "ts": 0.0}
    return result


@app.post("/api/slots/remove")
async def remove_slot(request: Request, username: str = Depends(get_current_username)) -> Dict[str, Any]:
    global _status_cache
    if not bot_instance:
        return {"success": False, "message": "봇이 초기화되지 않았습니다."}
    try:
        body: Dict[str, Any] = await request.json()
        symbol: str = body.get("symbol", "").strip().upper()
        sell_all: bool = bool(body.get("sell_all", True))
    except Exception:
        return {"success": False, "message": "잘못된 요청입니다."}
    if not symbol:
        return {"success": False, "message": "종목 코드를 입력해주세요."}
    result: Dict[str, Any] = bot_instance.remove_symbol(symbol, sell_all=sell_all)
    if result.get("success"):
        _status_cache = {"data": None, "ts": 0.0}
    return result


@app.get("/api/search-ticker")
async def search_ticker(symbol: str = "", username: str = Depends(get_current_username)) -> Dict[str, Any]:
    if not bot_instance:
        return {"found": False, "message": "봇이 초기화되지 않았습니다."}
    if not symbol.strip():
        return {"found": False, "message": "종목 코드를 입력해주세요."}
    return bot_instance.search_ticker(symbol)


_autocomplete_cache: Dict[str, Any] = {}

@app.get("/api/autocomplete")
async def autocomplete_ticker(q: str = "", username: str = Depends(get_current_username)) -> Dict[str, Any]:
    import yfinance as yf
    query: str = q.strip().upper()
    if len(query) < 1:
        return {"results": []}

    now: float = time.time()
    cached = _autocomplete_cache.get(query)
    if cached and (now - cached["ts"]) < 600:
        return cached["data"]

    try:
        search = yf.Search(query, max_results=8)
        results: list = []
        for item in search.quotes:
            exchange: str = item.get("exchange", "")
            quote_type: str = item.get("quoteType", "")
            if quote_type not in ("EQUITY", "ETF"):
                continue
            if any(x in exchange for x in ("PNK", "OTC")):
                continue
            results.append({
                "symbol": item.get("symbol", ""),
                "name": item.get("shortname", item.get("longname", "")),
                "exchange": exchange,
                "type": quote_type,
            })
        data: Dict[str, Any] = {"results": results[:8]}
        _autocomplete_cache[query] = {"data": data, "ts": now}
        return data
    except Exception:
        return {"results": []}


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