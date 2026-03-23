import os
import threading
import time
from typing import Any, Callable, Dict, Optional, Set

from fastapi import APIRouter, Depends

from bot import TradingBot
from services.live_data_cache import LiveDataCache
from services.trade_metrics import RealizedPnlCalculator


def _parse_env_rate(name: str, default: float = 0.0) -> float:
    try:
        v = float(os.getenv(name, str(default)).strip())
        return max(0.0, v)
    except Exception:
        return default


def _get_sell_cost_rates() -> Dict[str, float]:
    return {
        # 미설정 시 미국주식 수수료 기본값 0.25%
        "sell_fee_rate": _parse_env_rate("SELL_FEE_RATE", 0.0025),
        "sell_tax_rate": _parse_env_rate("SELL_TAX_RATE", 0.0),
    }


def create_status_router(
    auth_dependency: Callable[..., str],
    get_bot: Callable[[], Optional[TradingBot]],
    status_cache: Dict[str, Any],
    realized_pnl: RealizedPnlCalculator,
    live_data_cache: Optional[LiveDataCache] = None,
) -> APIRouter:
    router = APIRouter()
    slot_price_cache: Dict[str, Dict[str, float]] = {}
    slot_price_ttl_sec: float = 15.0
    snapshot_stale_sec_active: float = 3.5
    snapshot_stale_sec_idle: float = 10.0
    quote_rr_state: Dict[str, int] = {"idx": 0}
    quote_refresh_inflight: Set[str] = set()
    quote_refresh_last_ts: Dict[str, float] = {}
    quote_refresh_min_interval_sec: float = 1.5
    quote_refresh_lock = threading.Lock()

    def _get_cached_slot_price(symbol: str, now_ts: float) -> float:
        cached = slot_price_cache.get(symbol)
        if not cached:
            return 0.0
        if (now_ts - cached.get("ts", 0.0)) > slot_price_ttl_sec:
            return 0.0
        return float(cached.get("price", 0.0))

    def _get_cached_slot_ts(symbol: str, now_ts: float) -> float:
        cached = slot_price_cache.get(symbol)
        if not cached:
            return 0.0
        ts = float(cached.get("ts", 0.0) or 0.0)
        if (now_ts - ts) > slot_price_ttl_sec:
            return 0.0
        return ts

    def _set_cached_slot_price(symbol: str, price: float, now_ts: float) -> None:
        if price <= 0:
            return
        slot_price_cache[symbol] = {"price": float(price), "ts": now_ts}
        if len(slot_price_cache) > 64:
            oldest_symbol = min(slot_price_cache.keys(), key=lambda s: slot_price_cache[s].get("ts", 0.0))
            slot_price_cache.pop(oldest_symbol, None)

    def _get_prev_status_price(symbol: str) -> float:
        try:
            prev_data = status_cache.get("data") or {}
            prev_positions = prev_data.get("positions", []) or []
            sym = str(symbol or "").upper()
            for row in prev_positions:
                if str(row.get("symbol", "")).upper() != sym:
                    continue
                prev_price = float(row.get("current_price", 0.0) or 0.0)
                if prev_price > 0:
                    return prev_price
        except Exception:
            pass
        return 0.0

    def _pick_round_robin_symbol(candidates: list) -> str:
        if not candidates:
            return ""
        idx = int(quote_rr_state.get("idx", 0))
        picked = candidates[idx % len(candidates)]
        quote_rr_state["idx"] = idx + 1
        return str(picked)

    def _schedule_slot_price_refresh(bot: TradingBot, symbol: str, prefer_daytime: bool) -> bool:
        sym = str(symbol or "").upper().strip()
        if not sym:
            return False
        now_ts = time.time()
        with quote_refresh_lock:
            if sym in quote_refresh_inflight:
                return False
            last_ts = float(quote_refresh_last_ts.get(sym, 0.0) or 0.0)
            if (now_ts - last_ts) < quote_refresh_min_interval_sec:
                return False
            quote_refresh_inflight.add(sym)
            quote_refresh_last_ts[sym] = now_ts

        def _worker() -> None:
            try:
                price = float(bot.api.get_current_price(sym, prefer_daytime=prefer_daytime) or 0.0)
                if price > 0:
                    _set_cached_slot_price(sym, price, time.time())
            except Exception:
                pass
            finally:
                with quote_refresh_lock:
                    quote_refresh_inflight.discard(sym)

        threading.Thread(target=_worker, daemon=True).start()
        return True

    @router.get("/api/status")
    def get_status(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"error": "Bot is not initialized."}
        rates = _get_sell_cost_rates()
        sell_fee_rate = rates["sell_fee_rate"]
        sell_tax_rate = rates["sell_tax_rate"]

        now: float = time.time()
        if status_cache["data"] and (now - status_cache["ts"]) < 2:
            return status_cache["data"]

        try:
            started_at: float = time.perf_counter()
            bot_snapshot: Optional[Dict[str, Any]] = bot.get_live_snapshot(max_age_sec=8.0)
            if bot_snapshot:
                positions_list = list(bot_snapshot.get("positions", []))
                now_kst = bot.get_korean_time()
                now_et = bot.get_eastern_time()
                is_daytime_session: bool = bot.is_daytime_market_open(now_kst)
                is_us_session: bool = bot.is_active_trading_time(now_et)
                allow_quote_refresh: bool = is_us_session or is_daytime_session
                snapshot_quote_ts: float = float(bot_snapshot.get("ts", 0.0) or 0.0)
                snapshot_age_sec: float = (now - snapshot_quote_ts) if snapshot_quote_ts > 0 else 999.0
                snapshot_stale_sec: float = snapshot_stale_sec_active if is_daytime_session else snapshot_stale_sec_idle
                snapshot_is_stale: bool = snapshot_age_sec > snapshot_stale_sec
                active_slots = bot.slot_manager.get_active_slots()
                slot_map: Dict[str, Dict[str, Any]] = {slot.get("symbol"): slot for slot in active_slots}
                holding_slot_symbols = [
                    str(slot.get("symbol", "")).upper()
                    for slot in active_slots
                    if slot.get("symbol") and (not bool(slot.get("watch_only", False)))
                ]
                # 스냅샷이 비정상(평가액 존재 + 보유 수량 전부 0)일 때는 즉시 재동기화 1회 시도
                if holding_slot_symbols and float(bot_snapshot.get("tot_stck_evlu", 0.0) or 0.0) > 100:
                    qty_by_symbol: Dict[str, float] = {
                        str(p.get("symbol", "")).upper(): float(p.get("quantity", 0.0) or 0.0)
                        for p in positions_list
                        if p.get("symbol")
                    }
                    all_holdings_zero: bool = all(qty_by_symbol.get(sym, 0.0) <= 0.0 for sym in holding_slot_symbols)
                    if all_holdings_zero:
                        try:
                            bot.refresh_live_snapshot()
                            refreshed_snapshot = bot.get_live_snapshot(max_age_sec=8.0)
                            if refreshed_snapshot:
                                bot_snapshot = refreshed_snapshot
                                positions_list = list(bot_snapshot.get("positions", []))
                        except Exception:
                            pass
                existing_symbols = {str(p.get("symbol", "")).upper() for p in positions_list if p.get("symbol")}
                quote_refresh_budget: int = 6
                stale_price_candidates: Set[str] = set()

                for position in positions_list:
                    symbol = str(position.get("symbol", "")).upper()
                    if not symbol:
                        continue
                    slot_info = slot_map.get(symbol, {})
                    watch_only = bool(slot_info.get("watch_only", position.get("watch_only", False)))
                    anchor_price = float(slot_info.get("anchor_price", position.get("anchor_price", 0.0)) or 0.0)
                    peak_price = float(
                        slot_info.get(
                            "peak_price",
                            slot_info.get("anchor_price", position.get("peak_price", 0.0)),
                        ) or 0.0
                    )
                    all_time_high = float(
                        slot_info.get(
                            "all_time_high",
                            slot_info.get("peak_price", position.get("all_time_high", 0.0)),
                        ) or 0.0
                    )
                    position["watch_only"] = watch_only
                    position["anchor_price"] = anchor_price
                    position["peak_price"] = peak_price
                    position["all_time_high"] = all_time_high
                    position["ath_ready"] = bool(slot_info.get("ath_ready", position.get("ath_ready", True)))
                    position["anchor_at"] = str(slot_info.get("anchor_at", position.get("anchor_at", "")))
                    current_price = float(position.get("current_price", 0.0) or 0.0)
                    cached_price = _get_cached_slot_price(symbol, now)
                    cached_ts = _get_cached_slot_ts(symbol, now)
                    prev_status_price = _get_prev_status_price(symbol)
                    watch_anchor_like: bool = (
                        watch_only
                        and anchor_price > 0
                        and current_price > 0
                        and abs(current_price - anchor_price) <= max(0.01, anchor_price * 0.00005)
                    )
                    if current_price > 0:
                        # 오래된 스냅샷이 최신 캐시 가격을 덮어쓰지 않도록 보호
                        if cached_price > 0 and (snapshot_is_stale or (cached_ts > snapshot_quote_ts > 0)):
                            position["current_price"] = cached_price
                            if snapshot_is_stale and allow_quote_refresh:
                                stale_price_candidates.add(symbol)
                        else:
                            _set_cached_slot_price(symbol, current_price, now)
                            if allow_quote_refresh and (snapshot_is_stale or (watch_anchor_like and cached_price <= 0)):
                                stale_price_candidates.add(symbol)
                        continue
                    if cached_price > 0:
                        position["current_price"] = cached_price
                        if snapshot_is_stale and allow_quote_refresh:
                            stale_price_candidates.add(symbol)
                        continue
                    if prev_status_price > 0:
                        position["current_price"] = prev_status_price
                        if snapshot_is_stale and allow_quote_refresh:
                            stale_price_candidates.add(symbol)
                        continue
                    if allow_quote_refresh:
                        stale_price_candidates.add(symbol)

                for slot in active_slots:
                    symbol = str(slot.get("symbol", "")).upper()
                    if not symbol or symbol in existing_symbols:
                        continue
                    fallback_price: float = _get_cached_slot_price(symbol, now)
                    prev_status_price: float = _get_prev_status_price(symbol)
                    if fallback_price <= 0 and prev_status_price > 0:
                        fallback_price = prev_status_price
                    watch_only = bool(slot.get("watch_only", False))
                    anchor_price: float = float(slot.get("anchor_price", 0.0) or 0.0)
                    if watch_only and fallback_price <= 0 and anchor_price > 0:
                        fallback_price = anchor_price
                    peak_price = float(slot.get("peak_price", slot.get("anchor_price", 0.0)) or 0.0)
                    all_time_high = float(slot.get("all_time_high", peak_price) or peak_price)
                    should_refresh_missing_slot: bool = fallback_price <= 0 or (
                        watch_only
                        and anchor_price > 0
                        and abs(fallback_price - anchor_price) <= max(0.01, anchor_price * 0.00005)
                    )
                    if allow_quote_refresh and should_refresh_missing_slot and quote_refresh_budget > 0:
                        if _schedule_slot_price_refresh(bot, symbol, is_daytime_session):
                            quote_refresh_budget -= 1
                    positions_list.append(
                        {
                            "symbol": symbol,
                            "quantity": 0.0,
                            "avg_price": 0.0,
                            "current_price": fallback_price,
                            "return_rate": 0.0,
                            "is_leveraged": slot.get("is_leveraged", False),
                            "base_asset": slot.get("base_asset", symbol),
                            "watch_only": watch_only,
                            "anchor_price": float(slot.get("anchor_price", 0.0) or 0.0),
                            "peak_price": peak_price,
                            "all_time_high": all_time_high,
                            "ath_ready": bool(slot.get("ath_ready", True)),
                            "anchor_at": str(slot.get("anchor_at", "")),
                            "base_price": 0.0,
                        }
                    )

                if allow_quote_refresh and quote_refresh_budget > 0:
                    refresh_candidates = [
                        str(p.get("symbol", "")).upper()
                        for p in positions_list
                        if p.get("symbol")
                        and (
                            float(p.get("current_price", 0.0) or 0.0) <= 0.0
                            or str(p.get("symbol", "")).upper() in stale_price_candidates
                        )
                    ]
                    for _ in range(min(quote_refresh_budget, len(refresh_candidates))):
                        target_symbol = _pick_round_robin_symbol(refresh_candidates)
                        if not target_symbol:
                            break
                        _schedule_slot_price_refresh(bot, target_symbol, is_daytime_session)

                slot_order: Dict[str, int] = {str(slot.get("symbol", "")).upper(): idx for idx, slot in enumerate(active_slots)}
                positions_list.sort(key=lambda position: slot_order.get(str(position.get("symbol", "")).upper(), 999))

                daily_pnl_usd: float = 0.0
                for position in positions_list:
                    symbol = position.get("symbol", "")
                    quantity = float(position.get("quantity", 0.0) or 0.0)
                    current_price = float(position.get("current_price", 0.0) or 0.0)
                    previous_close = bot.prev_close.get(symbol, 0.0)
                    if quantity > 0 and previous_close > 0 and current_price > 0:
                        daily_pnl_usd += (current_price - previous_close) * quantity

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
                    "slots": active_slots,
                    "max_slots": bot.slot_manager.max_slots,
                    "market_open": bot.is_active_trading_time(now_et),
                    "daytime_open": is_daytime_session,
                    "is_dst": bool(now_et.dst()),
                    "et_time": now_et.strftime("%H:%M"),
                    "kst_time": now_kst.strftime("%H:%M"),
                    "sell_fee_rate": sell_fee_rate,
                    "sell_tax_rate": sell_tax_rate,
                    "source": "bot_snapshot",
                }
                status_cache["data"] = result
                status_cache["ts"] = now
                return result

            active_slots = bot.slot_manager.get_active_slots()
            all_slot_symbols: list = [str(slot.get("symbol", "")).upper() for slot in active_slots if slot.get("symbol")]
            trading_symbols: list = list(bot.symbols)
            seed_symbols: list = trading_symbols if trading_symbols else all_slot_symbols
            item_code: str = seed_symbols[0] if seed_symbols else "AAPL"
            data: Optional[Dict[str, Any]] = None
            if live_data_cache:
                data = live_data_cache.get_portfolio(ttl_sec=2.0)
            if not data:
                data = bot.api.get_balance_and_positions(item_cd=item_code, symbols=seed_symbols)
                if live_data_cache:
                    live_data_cache.set_portfolio(data)
            balance_done_at: float = time.perf_counter()
            now_kst = bot.get_korean_time()
            now_et = bot.get_eastern_time()
            is_daytime_session: bool = bot.is_daytime_market_open(now_kst)
            is_us_session: bool = bot.is_active_trading_time(now_et)
            allow_quote_refresh: bool = is_us_session or is_daytime_session
            current_symbols: list = list(all_slot_symbols)
            positions_list: list = []
            held_symbols: set = set()
            quote_refresh_budget: int = 6
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
                    else:
                        prev_status_price = _get_prev_status_price(symbol)
                        if prev_status_price > 0:
                            current_price = prev_status_price
                slot_info = slot_map.get(symbol, {})
                watch_only = bool(slot_info.get("watch_only", False))
                if watch_only and current_price <= 0:
                    current_price = float(slot_info.get("anchor_price", 0.0) or 0.0)
                peak_price = float(slot_info.get("peak_price", slot_info.get("anchor_price", 0.0)) or 0.0)
                all_time_high = float(slot_info.get("all_time_high", peak_price) or peak_price)
                ath_ready = bool(slot_info.get("ath_ready", True))
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
                        "watch_only": watch_only,
                        "anchor_price": float(slot_info.get("anchor_price", 0.0) or 0.0),
                        "peak_price": peak_price,
                        "all_time_high": all_time_high,
                        "ath_ready": ath_ready,
                        "anchor_at": str(slot_info.get("anchor_at", "")),
                        # 본주 차트/실시간 표시를 사용하지 않아 상태 조회에서 별도 본주 시세 조회를 생략
                        "base_price": 0.0,
                    }
                )

            for symbol in current_symbols:
                if symbol in held_symbols:
                    continue
                slot_info = slot_map.get(symbol, {})
                fallback_price: float = _get_cached_slot_price(symbol, now)
                prev_status_price: float = _get_prev_status_price(symbol)
                if fallback_price <= 0 and prev_status_price > 0:
                    fallback_price = prev_status_price
                watch_only = bool(slot_info.get("watch_only", False))
                anchor_price: float = float(slot_info.get("anchor_price", 0.0) or 0.0)
                if watch_only and fallback_price <= 0 and anchor_price > 0:
                    fallback_price = anchor_price
                peak_price = float(slot_info.get("peak_price", slot_info.get("anchor_price", 0.0)) or 0.0)
                all_time_high = float(slot_info.get("all_time_high", peak_price) or peak_price)
                ath_ready = bool(slot_info.get("ath_ready", True))
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
                        "watch_only": watch_only,
                        "anchor_price": float(slot_info.get("anchor_price", 0.0) or 0.0),
                        "peak_price": peak_price,
                        "all_time_high": all_time_high,
                        "ath_ready": ath_ready,
                        "anchor_at": str(slot_info.get("anchor_at", "")),
                        "base_price": 0.0,
                    }
                )

            if allow_quote_refresh and quote_refresh_budget > 0:
                refresh_candidates = [
                    str(p.get("symbol", "")).upper()
                    for p in positions_list
                    if float(p.get("current_price", 0.0) or 0.0) <= 0.0 and p.get("symbol")
                ]
                for _ in range(min(quote_refresh_budget, len(refresh_candidates))):
                    target_symbol = _pick_round_robin_symbol(refresh_candidates)
                    if not target_symbol:
                        break
                    _schedule_slot_price_refresh(bot, target_symbol, is_daytime_session)

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
                "sell_fee_rate": sell_fee_rate,
                "sell_tax_rate": sell_tax_rate,
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
                result = {
                    "error": f"상태 조회 실패: {error}",
                    "is_running": False,
                    "positions": [],
                    "sell_fee_rate": sell_fee_rate,
                    "sell_tax_rate": sell_tax_rate,
                }

        status_cache["data"] = result
        status_cache["ts"] = now
        return result

    return router
