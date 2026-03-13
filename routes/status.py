import time
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends

from bot import TradingBot
from services.live_data_cache import LiveDataCache
from services.trade_metrics import RealizedPnlCalculator


def create_status_router(
    auth_dependency: Callable[..., str],
    get_bot: Callable[[], Optional[TradingBot]],
    status_cache: Dict[str, Any],
    realized_pnl: RealizedPnlCalculator,
    live_data_cache: Optional[LiveDataCache] = None,
) -> APIRouter:
    router = APIRouter()
    slot_price_cache: Dict[str, Dict[str, float]] = {}
    slot_price_ttl_sec: float = 5.0

    def _get_cached_slot_price(symbol: str, now_ts: float) -> float:
        cached = slot_price_cache.get(symbol)
        if not cached:
            return 0.0
        if (now_ts - cached.get("ts", 0.0)) > slot_price_ttl_sec:
            return 0.0
        return float(cached.get("price", 0.0))

    def _set_cached_slot_price(symbol: str, price: float, now_ts: float) -> None:
        if price <= 0:
            return
        slot_price_cache[symbol] = {"price": float(price), "ts": now_ts}
        if len(slot_price_cache) > 64:
            oldest_symbol = min(slot_price_cache.keys(), key=lambda s: slot_price_cache[s].get("ts", 0.0))
            slot_price_cache.pop(oldest_symbol, None)

    @router.get("/api/status")
    def get_status(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"error": "Bot is not initialized."}

        now: float = time.time()
        if status_cache["data"] and (now - status_cache["ts"]) < 2:
            return status_cache["data"]

        try:
            started_at: float = time.perf_counter()
            bot_snapshot: Optional[Dict[str, Any]] = bot.get_live_snapshot(max_age_sec=6.0)
            if bot_snapshot:
                positions_list = list(bot_snapshot.get("positions", []))

                daily_pnl_usd: float = 0.0
                for position in positions_list:
                    symbol = position.get("symbol", "")
                    quantity = float(position.get("quantity", 0.0) or 0.0)
                    current_price = float(position.get("current_price", 0.0) or 0.0)
                    previous_close = bot.prev_close.get(symbol, 0.0)
                    if quantity > 0 and previous_close > 0 and current_price > 0:
                        daily_pnl_usd += (current_price - previous_close) * quantity

                now_kst = bot.get_korean_time()
                now_et = bot.get_eastern_time()
                is_daytime_session: bool = bot.is_daytime_market_open(now_kst)
                usd_balance = float(bot_snapshot.get("usd_balance", 0.0) or 0.0)
                tot_stck_evlu = float(bot_snapshot.get("tot_stck_evlu", 0.0) or 0.0)
                result = {
                    "is_running": bot.is_running,
                    "usd_balance": usd_balance,
                    "krw_balance": float(bot_snapshot.get("krw_balance", 0.0) or 0.0),
                    "krw_cash": float(bot_snapshot.get("krw_cash", 0.0) or 0.0),
                    "exchange_rate": float(bot_snapshot.get("exchange_rate", bot.exchange_rate) or bot.exchange_rate),
                    "display_exchange_rate": float(bot.get_display_exchange_rate() or bot.exchange_rate),
                    "positions": positions_list,
                    "logs": bot.logs,
                    "tot_evlu_pfls": float(bot_snapshot.get("tot_evlu_pfls", 0.0) or 0.0),
                    "tot_pchs_amt": float(bot_snapshot.get("tot_pchs_amt", 0.0) or 0.0),
                    "tot_stck_evlu": tot_stck_evlu,
                    "total_eval": usd_balance + tot_stck_evlu,
                    "daily_pnl_usd": round(daily_pnl_usd, 2),
                    "strategy_mode": bot.strategy_mode,
                    "auto_active": bot.auto_active_mode,
                    "realized_pnl": realized_pnl.calculate(),
                    "slots": bot.slot_manager.get_active_slots(),
                    "max_slots": bot.slot_manager.max_slots,
                    "market_open": bot.is_active_trading_time(now_et),
                    "daytime_open": is_daytime_session,
                    "is_dst": bool(now_et.dst()),
                    "et_time": now_et.strftime("%H:%M"),
                    "kst_time": now_kst.strftime("%H:%M"),
                    "source": "bot_snapshot",
                }
                status_cache["data"] = result
                status_cache["ts"] = now
                return result

            item_code: str = bot.symbols[0] if bot.symbols else "AAPL"
            data: Optional[Dict[str, Any]] = None
            if live_data_cache:
                data = live_data_cache.get_portfolio(ttl_sec=2.0)
            if not data:
                data = bot.api.get_balance_and_positions(item_cd=item_code, symbols=bot.symbols)
                if live_data_cache:
                    live_data_cache.set_portfolio(data)
            balance_done_at: float = time.perf_counter()
            now_kst = bot.get_korean_time()
            now_et = bot.get_eastern_time()
            is_daytime_session: bool = bot.is_daytime_market_open(now_kst)
            current_symbols: list = list(bot.symbols)
            positions_list: list = []
            held_symbols: set = set()
            quote_refresh_budget: int = 1

            active_slots = bot.slot_manager.get_active_slots()
            slot_map: Dict[str, Dict[str, Any]] = {slot.get("symbol"): slot for slot in active_slots}
            sorted_positions = sorted(
                data.get("positions", []),
                key=lambda p: (
                    float(p.get("quantity", 0.0)),
                    float(p.get("evlu_amt", 0.0)),
                    float(p.get("current_price", 0.0)),
                ),
                reverse=True,
            )
            for pos in sorted_positions:
                symbol: str = pos["symbol"]
                if symbol not in current_symbols:
                    continue
                if symbol in held_symbols:
                    continue
                held_symbols.add(symbol)
                current_price: float = float(pos.get("current_price", 0.0) or 0.0)
                if current_price > 0:
                    _set_cached_slot_price(symbol, current_price, now)
                else:
                    cached_price = _get_cached_slot_price(symbol, now)
                    if cached_price > 0:
                        current_price = cached_price
                    elif quote_refresh_budget > 0:
                        quote_refresh_budget -= 1
                        try:
                            current_price = float(bot.api.get_current_price(symbol, prefer_daytime=is_daytime_session) or 0.0)
                            _set_cached_slot_price(symbol, current_price, now)
                        except Exception:
                            pass
                slot_info = slot_map.get(symbol, {})
                base_symbol: str = slot_info.get("base_asset", symbol)
                positions_list.append(
                    {
                        "symbol": symbol,
                        "quantity": pos.get("quantity", 0.0),
                        "avg_price": pos.get("avg_price", 0.0),
                        "current_price": current_price,
                        "evlu_amt": pos.get("evlu_amt", 0.0),
                        "evlu_pfls": pos.get("evlu_pfls", 0.0),
                        "return_rate": pos.get("return_rate", 0.0),
                        "pchs_amt": pos.get("pchs_amt", 0.0),
                        "is_leveraged": slot_info.get("is_leveraged", False),
                        "base_asset": base_symbol,
                        # 본주 차트/실시간 표시를 사용하지 않아 상태 조회에서 별도 본주 시세 조회를 생략
                        "base_price": 0.0,
                    }
                )

            for symbol in current_symbols:
                if symbol in held_symbols:
                    continue
                slot_info = slot_map.get(symbol, {})
                fallback_price: float = _get_cached_slot_price(symbol, now)
                if fallback_price <= 0 and quote_refresh_budget > 0:
                    quote_refresh_budget -= 1
                    try:
                        fallback_price = float(bot.api.get_current_price(symbol, prefer_daytime=is_daytime_session) or 0.0)
                        _set_cached_slot_price(symbol, fallback_price, now)
                    except Exception:
                        pass
                base_symbol = slot_info.get("base_asset", symbol)
                positions_list.append(
                    {
                        "symbol": symbol,
                        "quantity": 0.0,
                        "avg_price": 0.0,
                        "current_price": fallback_price,
                        "return_rate": 0.0,
                        "is_leveraged": slot_info.get("is_leveraged", False),
                        "base_asset": base_symbol,
                        "base_price": 0.0,
                    }
                )

            slot_order: Dict[str, int] = {slot["symbol"]: idx for idx, slot in enumerate(active_slots)}
            positions_list.sort(key=lambda position: slot_order.get(position["symbol"], 999))

            api_exchange_rate: float = data.get("exchange_rate", 0.0)
            if api_exchange_rate > 0:
                bot.exchange_rate = api_exchange_rate
            elif not bot.is_running:
                bot.update_exchange_rate()

            daily_pnl_usd: float = 0.0
            for position in positions_list:
                symbol = position["symbol"]
                quantity = position.get("quantity", 0.0)
                current_price = position.get("current_price", 0.0)
                previous_close = bot.prev_close.get(symbol, 0.0)
                if quantity > 0 and previous_close > 0 and current_price > 0:
                    daily_pnl_usd += (current_price - previous_close) * quantity

            result: Dict[str, Any] = {
                "is_running": bot.is_running,
                "usd_balance": data["usd_balance"],
                "krw_balance": data.get("krw_balance", 0.0),
                "krw_cash": data.get("krw_cash", 0.0),
                "exchange_rate": bot.exchange_rate,
                "display_exchange_rate": float(bot.get_display_exchange_rate() or bot.exchange_rate),
                "positions": positions_list,
                "logs": bot.logs,
                "tot_evlu_pfls": data.get("tot_evlu_pfls", 0.0),
                "tot_pchs_amt": data.get("tot_pchs_amt", 0.0),
                "tot_stck_evlu": data.get("tot_stck_evlu", 0.0),
                "total_eval": data["usd_balance"] + data.get("tot_stck_evlu", 0.0),
                "daily_pnl_usd": round(daily_pnl_usd, 2),
                "strategy_mode": bot.strategy_mode,
                "auto_active": bot.auto_active_mode,
                "realized_pnl": realized_pnl.calculate(),
                "slots": active_slots,
                "max_slots": bot.slot_manager.max_slots,
                "market_open": bot.is_active_trading_time(now_et),
                "daytime_open": is_daytime_session,
                "is_dst": bool(now_et.dst()),
                "et_time": now_et.strftime("%H:%M"),
                "kst_time": now_kst.strftime("%H:%M"),
            }
            total_ms: float = (time.perf_counter() - started_at) * 1000.0
            if total_ms >= 1200:
                balance_ms: float = (balance_done_at - started_at) * 1000.0
                compose_ms: float = total_ms - balance_ms
                print(
                    f"[status 느림] total={total_ms:.0f}ms "
                    f"(balance={balance_ms:.0f}ms, compose={compose_ms:.0f}ms) "
                    f"slots={len(current_symbols)} held={len(held_symbols)}"
                )
        except Exception as error:
            try:
                result = bot.get_status()
            except Exception:
                result = {"error": f"상태 조회 실패: {error}", "is_running": False, "positions": []}

        status_cache["data"] = result
        status_cache["ts"] = now
        return result

    return router
