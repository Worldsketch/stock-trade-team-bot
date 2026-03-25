import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, Depends

from bot import TradingBot
from services.live_data_cache import LiveDataCache

SMART_SELL_INITIAL_DISCOUNT: float = 0.001  # -0.1%
SMART_SELL_REPRICE_STEPS: List[Tuple[int, float]] = [
    (8, 0.002),   # 8초: 현재가 기준 -0.2%로 재호가
    (20, 0.003),  # 20초: 현재가 기준 -0.3%
    (35, 0.0045), # 35초: 현재가 기준 -0.45%
    (55, 0.006),  # 55초: 현재가 기준 -0.6%
    (90, 0.008),  # 90초: 현재가 기준 -0.8%
]
SMART_SELL_MONITOR_SEC: int = 300


def create_trading_router(
    auth_dependency: Callable[..., str],
    get_bot: Callable[[], Optional[TradingBot]],
    invalidate_status_cache: Callable[[], None],
    live_data_cache: Optional[LiveDataCache] = None,
) -> APIRouter:
    router = APIRouter()

    def _order_window_error(bot: TradingBot) -> Optional[str]:
        now_et = bot.get_eastern_time()
        now_kst = bot.get_korean_time()
        if bot.is_active_trading_time(now_et) or bot.is_daytime_market_open(now_kst):
            return None
        return "거래 가능 시간에만 주문할 수 있습니다. (미국장 ET 04:00~20:00 / 데이장 KST 09:00~16:00)"

    def _pick_pending_sell_order(orders: List[Dict[str, Any]], symbol: str) -> Optional[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for order in orders:
            if order.get("symbol") != symbol:
                continue
            if order.get("side") != "매도":
                continue
            try:
                rem_qty = int(float(order.get("remaining_qty", 0)))
            except Exception:
                rem_qty = 0
            if rem_qty <= 0:
                continue
            candidates.append(order)
        if not candidates:
            return None
        candidates.sort(
            key=lambda o: (
                int(float(o.get("remaining_qty", 0) or 0)),
                str(o.get("order_time", "")),
                str(o.get("order_no", "")),
            ),
            reverse=True,
        )
        return candidates[0]

    def _start_smart_sell_manager(
        symbol: str,
        total_qty: int,
        initial_price: float,
        label: str,
        is_daytime: bool,
        position_qty_before: float = 0.0,
    ) -> None:
        def _worker() -> None:
            start_ts = time.time()
            last_price = initial_price
            last_remaining = total_qty

            def _load_pending_orders(bot: TradingBot) -> List[Dict[str, Any]]:
                if live_data_cache:
                    live_data_cache.invalidate_pending()
                return bot.api.get_pending_orders(symbols=bot.symbols)

            def _finalize_after_pending_cleared(bot: TradingBot) -> None:
                nonlocal last_remaining
                fallback_filled_qty: int = max(0, total_qty - last_remaining)
                filled_qty: int = bot.estimate_sell_filled_qty(
                    symbol=symbol,
                    ordered_qty=total_qty,
                    position_qty_before=position_qty_before,
                    fallback_filled_qty=fallback_filled_qty,
                )
                if filled_qty <= 0 and position_qty_before > 0:
                    time.sleep(0.6)
                    filled_qty = bot.estimate_sell_filled_qty(
                        symbol=symbol,
                        ordered_qty=total_qty,
                        position_qty_before=position_qty_before,
                        fallback_filled_qty=fallback_filled_qty,
                    )
                completed: bool = filled_qty >= total_qty
                filled_amount: float = filled_qty * last_price
                krw_filled: float = filled_amount * bot.exchange_rate
                bot.finalize_pending_sell_trade(
                    symbol=symbol,
                    ordered_qty=total_qty,
                    filled_qty=filled_qty,
                    fill_price=last_price,
                    completed=completed,
                )
                if filled_qty > 0:
                    bot.send_telegram_message(
                        f"✅ [수동 매도 체결 확인]\n종목: {symbol}\n수량: {filled_qty}주 ({label})\n"
                        f"최근 주문가: ${last_price:.2f}\n예상 체결 금액: ${filled_amount:,.2f} (약 {krw_filled:,.0f}원)"
                    )
                    bot.log(f"✅ [수동매도 체결확인] {symbol} {filled_qty}주 ({label})")
                else:
                    bot.send_telegram_message(
                        f"ℹ️ [수동 매도 체결 확인 필요]\n종목: {symbol}\n"
                        f"미체결 목록에서 주문은 사라졌지만 체결수량을 0주로 추정했습니다.\n"
                        f"최근 주문가: ${last_price:.2f}\n브로커 체결내역을 확인해주세요."
                    )
                    bot.log(f"ℹ️ [수동매도 체결확인 보류] {symbol} 체결수량 추정 0주 ({label})")

            try:
                # 단계형 재호가
                for after_sec, discount in SMART_SELL_REPRICE_STEPS:
                    sleep_sec = after_sec - (time.time() - start_ts)
                    if sleep_sec > 0:
                        time.sleep(sleep_sec)

                    bot = get_bot()
                    if not bot:
                        return

                    orders = _load_pending_orders(bot)
                    pending = _pick_pending_sell_order(orders, symbol)
                    if not pending:
                        _finalize_after_pending_cleared(bot)
                        return

                    try:
                        remaining_qty = int(float(pending.get("remaining_qty", 0)))
                    except Exception:
                        remaining_qty = 0
                    if remaining_qty <= 0:
                        continue
                    last_remaining = remaining_qty

                    order_no = str(pending.get("order_no", "")).strip()
                    if not order_no:
                        continue

                    canceled = bot.api.cancel_order(
                        order_no=order_no,
                        symbol=symbol,
                        remaining_qty=remaining_qty,
                        prefer_daytime=is_daytime,
                    )
                    if not canceled:
                        continue

                    # 취소 반영 직후 재호가
                    time.sleep(0.25)
                    current_price = float(
                        bot.api.get_current_price(symbol, prefer_daytime=is_daytime) or 0.0
                    )
                    if current_price <= 0:
                        current_price = last_price
                    new_price = round(current_price * (1.0 - discount), 2)
                    if new_price <= 0:
                        continue

                    reordered = bot.api.place_order(
                        symbol=symbol,
                        quantity=remaining_qty,
                        price=new_price,
                        is_buy=False,
                        prefer_daytime=is_daytime,
                    )
                    if reordered:
                        last_price = new_price
                        if live_data_cache:
                            live_data_cache.invalidate_pending()
                        bot.log(
                            f"🔁 [수동매도 재호가] {symbol} 잔량 {remaining_qty}주 @ ${new_price:.2f} "
                            f"(현재가 기준 -{discount*100:.1f}%)"
                        )
                    else:
                        bot.send_telegram_message(
                            f"⚠️ [수동매도 재호가 실패]\n종목: {symbol}\n잔량: {remaining_qty}주 ({label})\n"
                            f"재주문가: ${new_price:.2f}\n수동으로 미체결 주문을 확인해주세요."
                        )
                        bot.log(
                            f"⚠️ [수동매도 재호가 실패] {symbol} 잔량 {remaining_qty}주 @ ${new_price:.2f}"
                        )
                        return

                # 마지막 재호가 이후 5분까지 모니터링
                while (time.time() - start_ts) < SMART_SELL_MONITOR_SEC:
                    time.sleep(5)
                    bot = get_bot()
                    if not bot:
                        return
                    orders = _load_pending_orders(bot)
                    pending = _pick_pending_sell_order(orders, symbol)
                    if not pending:
                        _finalize_after_pending_cleared(bot)
                        return
                    try:
                        last_remaining = int(float(pending.get("remaining_qty", last_remaining)))
                    except Exception:
                        pass

                bot = get_bot()
                if bot:
                    partial_filled_qty: int = max(0, total_qty - last_remaining)
                    bot.finalize_pending_sell_trade(
                        symbol=symbol,
                        ordered_qty=total_qty,
                        filled_qty=partial_filled_qty,
                        fill_price=last_price,
                        completed=False,
                    )
                    bot.send_telegram_message(
                        f"⏰ [수동 매도 미체결]\n종목: {symbol}\n잔량: {last_remaining}주 ({label})\n"
                        f"최근 주문가: ${last_price:.2f}\n5분간 완전 체결되지 않았습니다."
                    )
                    bot.log(f"⏰ [수동매도 미체결] {symbol} 잔량 {last_remaining}주 ({label}) @ ${last_price:.2f}")
            except Exception as e:
                bot = get_bot()
                if bot:
                    bot.log(f"[수동매도 추격형 오류] {symbol}: {e}")

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    @router.post("/api/sell")
    def manual_sell(body: Optional[Dict[str, Any]] = Body(default=None), username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"success": False, "message": "봇이 초기화되지 않았습니다."}

        try:
            body = body or {}
            symbol: str = body.get("symbol", "")
            percent: int = int(body.get("percent", 0))
        except Exception:
            return {"success": False, "message": "잘못된 요청입니다."}

        if symbol not in bot.symbols:
            return {"success": False, "message": f"슬롯에 등록되지 않은 종목: {symbol}"}
        if percent not in (10, 25, 50, 100):
            return {"success": False, "message": f"잘못된 매도 비율: {percent}%"}
        order_window_error = _order_window_error(bot)
        if order_window_error:
            return {"success": False, "message": order_window_error}

        try:
            data: Optional[Dict[str, Any]] = None
            used_cached_portfolio: bool = False
            if live_data_cache:
                data = live_data_cache.get_portfolio(ttl_sec=3.0)
                used_cached_portfolio = data is not None
            if not data:
                bot_snapshot: Optional[Dict[str, Any]] = bot.get_live_snapshot(max_age_sec=12.0)
                if bot_snapshot:
                    data = {
                        "positions": list(bot_snapshot.get("positions", [])),
                        "usd_balance": float(bot_snapshot.get("usd_balance", 0.0) or 0.0),
                        "exchange_rate": float(bot_snapshot.get("exchange_rate", 0.0) or 0.0),
                    }
            if not data:
                data = bot.api.get_balance_and_positions(item_cd=symbol, symbols=bot.symbols)
                if live_data_cache:
                    live_data_cache.set_portfolio(data)
            position = None
            for pos in data["positions"]:
                if pos["symbol"] == symbol and pos.get("quantity", 0) > 0:
                    position = pos
                    break

            if not position and used_cached_portfolio:
                data = bot.api.get_balance_and_positions(item_cd=symbol, symbols=bot.symbols)
                if live_data_cache:
                    live_data_cache.set_portfolio(data)
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
                current_price = bot.api.get_current_price(symbol)
            if current_price <= 0:
                return {"success": False, "message": f"{symbol} 현재가를 가져올 수 없습니다."}

            now_et = bot.get_eastern_time()
            now_kst = bot.get_korean_time()
            is_regular: bool = bot.is_regular_market_open(now_et)
            is_daytime: bool = bot.is_daytime_market_open(now_kst)
            _ = is_regular  # 향후 세션별 세분화용
            sell_price: float = round(current_price * (1.0 - SMART_SELL_INITIAL_DISCOUNT), 2)
            max_discount: float = max([d for _, d in SMART_SELL_REPRICE_STEPS], default=SMART_SELL_INITIAL_DISCOUNT)
            order_desc: str = (
                f"추격형 지정가 ${sell_price:.2f} "
                f"(시작 -{SMART_SELL_INITIAL_DISCOUNT*100:.1f}%, 최대 -{max_discount*100:.1f}%)"
            )

            success: bool = bot.api.place_order(symbol, sell_qty, sell_price, is_buy=False, prefer_daytime=is_daytime)
            if success:
                bot.manual_sell_block[symbol] = time.time()
                invalidate_status_cache()
                if live_data_cache:
                    live_data_cache.invalidate_pending()
                    live_data_cache.invalidate_portfolio()
                est_amount: float = sell_qty * sell_price
                krw_est: float = est_amount * bot.exchange_rate
                label: str = f"{percent}%"
                msg: str = (
                    f"📤 [수동 매도 주문 접수]\n종목: {symbol}\n수량: {sell_qty}주 ({label})\n"
                    f"{order_desc}\n예상 금액: ${est_amount:,.2f} (약 {krw_est:,.0f}원)"
                )
                bot.send_telegram_message(msg)
                bot.log(f"📤 [수동매도 접수] {symbol} {sell_qty}주 {order_desc} ({label})")
                manual_avg: float = position.get("avg_price", 0.0)
                bot._log_trade(
                    symbol,
                    "매도",
                    sell_qty,
                    sell_price,
                    est_amount,
                    f"[{bot._get_mode_label()}] 수동 매도 ({label})",
                    avg_price=manual_avg,
                    status="pending",
                    ordered_qty=sell_qty,
                )
                # 체결 보장과 체결가 개선을 위해 추격형 단계 재호가를 백그라운드에서 실행
                _start_smart_sell_manager(
                    symbol=symbol,
                    total_qty=sell_qty,
                    initial_price=sell_price,
                    label=label,
                    is_daytime=is_daytime,
                    position_qty_before=float(total_qty),
                )
                return {
                    "success": True,
                    "message": f"{symbol} {sell_qty}주 매도 주문 완료",
                    "qty": sell_qty,
                    "price": sell_price,
                }
            return {"success": False, "message": f"{symbol} 매도 주문 실패"}
        except Exception as error:
            return {"success": False, "message": f"매도 처리 오류: {str(error)}"}

    @router.get("/api/pending-orders")
    def get_pending_orders(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"orders": []}
        try:
            orders = live_data_cache.get_pending(ttl_sec=3.0) if live_data_cache else None
            if orders is None:
                orders = bot.api.get_pending_orders(symbols=bot.symbols)
                if live_data_cache:
                    live_data_cache.set_pending(orders)
            return {"orders": orders}
        except Exception as error:
            return {"orders": [], "error": str(error)}

    @router.post("/api/cancel-order")
    def cancel_order(body: Optional[Dict[str, Any]] = Body(default=None), username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"success": False, "message": "봇이 초기화되지 않았습니다."}
        try:
            body = body or {}
            order_no: str = body.get("order_no", "")
            symbol: str = body.get("symbol", "")
            remaining_qty: int = int(body.get("remaining_qty", 0))
        except Exception:
            return {"success": False, "message": "잘못된 요청입니다."}

        if not order_no or not symbol:
            return {"success": False, "message": "주문번호와 종목이 필요합니다."}

        try:
            now_kst = bot.get_korean_time()
            success: bool = bot.api.cancel_order(
                order_no,
                symbol,
                remaining_qty,
                prefer_daytime=bot.is_daytime_market_open(now_kst),
            )
            if success:
                cancelled_sync: bool = bot.mark_trade_cancelled(symbol, remaining_qty, order_no=order_no)
                bot.log(f"🚫 [주문취소] {symbol} 주문번호 {order_no}")
                if live_data_cache:
                    live_data_cache.invalidate_pending()
                    live_data_cache.invalidate_portfolio()
                if cancelled_sync:
                    bot.log(f"🧾 [매매내역 정정] {symbol} 최근 매수 기록에 취소 반영", send_tg=False)
                return {"success": True, "message": f"{symbol} 주문 취소 완료"}
            return {"success": False, "message": f"{symbol} 주문 취소 실패"}
        except Exception as error:
            return {"success": False, "message": f"취소 처리 오류: {str(error)}"}

    return router
