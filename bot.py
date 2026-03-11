import os
import sys
import signal
import time
import threading
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional, Set
import yfinance as yf
import json
import pandas as pd

from api import KoreaInvestmentAPI

LEVERAGED_ETF_MAP: Dict[str, str] = {
    'NVDL': 'NVDA', 'TSLL': 'TSLA', 'TQQQ': 'QQQ',
    'SOXL': 'SOXX', 'UPRO': 'SPY', 'SPXL': 'SPY',
    'TECL': 'XLK', 'FAS': 'XLF', 'LABU': 'XBI',
    'TNA': 'IWM', 'UDOW': 'DIA', 'CURE': 'XLV',
    'NAIL': 'ITB', 'DFEN': 'ITA', 'FNGU': 'NYFANG',
    'AAPU': 'AAPL', 'MSFU': 'MSFT', 'AMZU': 'AMZN',
    'GOOU': 'GOOG', 'METU': 'META', 'CONL': 'COIN',
    'SQQQ': 'QQQ', 'SOXS': 'SOXX', 'SPXU': 'SPY',
    'TECS': 'XLK', 'FAZ': 'XLF', 'TZA': 'IWM',
    'WEBL': 'DJUSTC', 'BITX': 'BTC',
}


class SlotManager:
    """최대 6개 슬롯의 동적 종목 관리를 담당합니다."""

    def __init__(self, slots_file: str = "slots.json", max_slots: int = 6) -> None:
        self.slots_file: str = slots_file
        self.max_slots: int = max_slots
        self.slots: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self.slots_file):
                with open(self.slots_file, 'r', encoding='utf-8') as f:
                    data: Dict[str, Any] = json.load(f)
                    self.slots = data.get('slots', [])
                    self.max_slots = data.get('max_slots', 6)
        except Exception as e:
            print(f"[슬롯 로드 오류] {e}")

    def _save(self) -> None:
        try:
            with open(self.slots_file, 'w', encoding='utf-8') as f:
                json.dump({'slots': self.slots, 'max_slots': self.max_slots}, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[슬롯 저장 오류] {e}")

    def get_active_slots(self) -> List[Dict[str, Any]]:
        return [s for s in self.slots if s.get('active', True)]

    def get_symbols(self) -> List[str]:
        return [s['symbol'] for s in self.get_active_slots()]

    def get_base_assets(self) -> Dict[str, str]:
        return {s['symbol']: s.get('base_asset', s['symbol']) for s in self.get_active_slots()}

    def is_full(self) -> bool:
        return len(self.get_active_slots()) >= self.max_slots

    def has_symbol(self, symbol: str) -> bool:
        return symbol.upper() in self.get_symbols()

    def add_slot(self, symbol: str, base_asset: Optional[str], is_leveraged: bool) -> bool:
        if self.is_full() or self.has_symbol(symbol):
            return False
        self.slots.append({
            'symbol': symbol.upper(),
            'base_asset': base_asset or symbol.upper(),
            'added_at': datetime.now().isoformat(),
            'is_leveraged': is_leveraged,
            'active': True,
        })
        self._save()
        return True

    def remove_slot(self, symbol: str) -> bool:
        upper_sym: str = symbol.upper()
        self.slots = [s for s in self.slots if s['symbol'] != upper_sym]
        self._save()
        return True


class TradingBot:
    def __init__(self, api: KoreaInvestmentAPI) -> None:
        self.api: KoreaInvestmentAPI = api
        
        # 슬롯 매니저 (동적 종목 관리)
        self.slot_manager: SlotManager = SlotManager()
        
        # Strategy E: SMA200만 필터 (RSI>=50 이중 필터 제거)
        self.is_uptrend: Dict[str, bool] = {}
        self.is_rsi_oversold: Dict[str, bool] = {}
        
        # 최고점(High Water Mark) 추적용 (Trailing Stop 용도)
        self.hwm_file = "hwm_data.json"
        self.hwm: Dict[str, float] = self._load_hwm()
        self.trailing_stop_threshold: float = -0.40
        self.trailing_sell_pct: float = 0.50
        
        # Strategy E: 전일종가 대비 당일 하락률 기준 DCA
        self.dca_2_threshold: float = -0.03
        self.dca_4_threshold: float = -0.05
        self.dca_8_threshold: float = -0.07
        self.prev_close: Dict[str, float] = {}
        
        # Strategy E: 총 자산 대비 비율 매수
        self.strategy_mode_file: str = "strategy_mode.json"
        self.strategy_modes: Dict[str, Dict[str, float]] = {
            "aggressive": {"base": 0.001, "w2": 0.025, "w4": 0.045, "w8": 0.065},
            "defensive":  {"base": 0.001, "w2": 0.012, "w4": 0.022, "w8": 0.032},
        }
        self.auto_active_mode: str = "aggressive"
        self.strategy_mode: str = self._load_strategy_mode()
        self._apply_strategy_mode()
        
        # 포지션 DataFrame 초기화 (슬롯 기반 동적 구성)
        self.positions: pd.DataFrame = pd.DataFrame(
            columns=['symbol', 'avg_price', 'quantity', 'current_price', 'return_rate']
        )
        self._rebuild_positions_df()
        
        # 상태 변수
        self.is_running: bool = False
        self.last_usd_balance: float = 0.0
        self.last_krw_balance: float = 0.0
        self.exchange_rate: float = 1400.0
        self.tot_evlu_pfls: float = 0.0
        self.tot_pchs_amt: float = 0.0
        self.tot_stck_evlu: float = 0.0
        self.logs: List[str] = []
        
        # 일일 매수 상태 추적 (중복 매수 방지) - 파일 영속화
        self.daily_state_file: str = "daily_state.json"
        self.daily_state: Dict[str, Any] = self._load_daily_state()
        self._sync_logged: bool = False

        # 장중 SMA200 재체크 간격 (초)
        self._sma_recheck_interval: float = 300.0
        self._last_sma_recheck: float = 0.0
        
        # 수동 매도 후 매수 차단 (타임스탬프)
        self.manual_sell_block: Dict[str, float] = {}
        self.manual_sell_block_seconds: float = 10.0

        # 헬스체크 (6시간 간격)
        self._start_time: float = time.time()
        self.last_heartbeat: float = time.time()
        self.heartbeat_interval: float = 21600.0

        # 에러 알림 쓰로틀링 (10분)
        self._error_throttle: Dict[str, float] = {}
        self._error_throttle_seconds: float = 600.0

        # 일별 자산 추적
        self.equity_log_file: str = "equity_log.json"

        # 매매 내역 기록
        self.trade_log_file: str = "trade_log.json"

        # 예수금 비중 알림 (하루 1번)
        self._cash_alert_40_sent: str = ""
        self._cash_alert_30_sent: str = ""

        # 미국 주식시장 공휴일 (매년 초에 갱신 필요)
        self._us_holidays: Set[date] = {
            # 2026년
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
            date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
            date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
            date(2026, 12, 25),
        }
        self._us_early_close: Set[date] = {
            date(2026, 7, 2), date(2026, 11, 27), date(2026, 12, 24),
        }

        # 기존 보유 종목 자동 슬롯 등록 (슬롯이 비어있을 때만)
        self._auto_register_holdings()

    @property
    def symbols(self) -> List[str]:
        return self.slot_manager.get_symbols()

    @property
    def base_assets(self) -> Dict[str, str]:
        return self.slot_manager.get_base_assets()

    def _rebuild_positions_df(self) -> None:
        """슬롯 변경 시 포지션 DataFrame을 재구축합니다."""
        syms: List[str] = self.symbols
        n: int = len(syms)
        if n == 0:
            self.positions = pd.DataFrame(
                columns=['symbol', 'avg_price', 'quantity', 'current_price', 'return_rate']
            )
            return
        self.positions = pd.DataFrame({
            'symbol': syms,
            'avg_price': [0.0] * n,
            'quantity': [0.0] * n,
            'current_price': [0.0] * n,
            'return_rate': [0.0] * n,
        })

    def is_us_market_holiday(self, now_et: datetime) -> bool:
        return now_et.date() in self._us_holidays

    def get_early_close_time(self, now_et: datetime) -> Optional[datetime]:
        if now_et.date() in self._us_early_close:
            return now_et.replace(hour=13, minute=0, second=0, microsecond=0)
        return None

    def _load_daily_state(self) -> Dict[str, Any]:
        """재시작 시 중복 매수를 방지하기 위해 파일에서 daily_state를 복원합니다."""
        try:
            if os.path.exists(self.daily_state_file):
                with open(self.daily_state_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"[daily_state 로드 오류] {e}")
        return {'date': ''}

    def _save_daily_state(self) -> None:
        """daily_state를 파일에 저장합니다."""
        try:
            with open(self.daily_state_file, 'w', encoding='utf-8') as f:
                json.dump(self.daily_state, f, indent=2)
        except Exception as e:
            print(f"[daily_state 저장 오류] {e}")

    def _load_strategy_mode(self) -> str:
        try:
            if os.path.exists(self.strategy_mode_file):
                with open(self.strategy_mode_file, 'r', encoding='utf-8') as f:
                    data: Dict[str, str] = json.load(f)
                    return data.get("mode", "auto")
        except Exception:
            pass
        return "auto"

    def _apply_strategy_mode(self) -> None:
        active: str = self.strategy_mode if self.strategy_mode in self.strategy_modes else self.auto_active_mode
        ratios: Dict[str, float] = self.strategy_modes[active]
        self.base_buy_ratio: float = ratios["base"]
        self.w2_ratio: float = ratios["w2"]
        self.w4_ratio: float = ratios["w4"]
        self.w8_ratio: float = ratios["w8"]

    def set_strategy_mode(self, mode: str) -> bool:
        if mode not in ("auto", "aggressive", "defensive"):
            return False
        self.strategy_mode = mode
        if mode == "auto":
            self._check_auto_mode()
        self._apply_strategy_mode()
        try:
            with open(self.strategy_mode_file, 'w', encoding='utf-8') as f:
                json.dump({"mode": mode}, f)
        except Exception as e:
            self.log(f"[전략 모드 저장 오류] {e}")
        label_map: Dict[str, str] = {"auto": f"자동 (현재: {'공격적' if self.auto_active_mode == 'aggressive' else '방어적'})", "aggressive": "공격적", "defensive": "방어적"}
        self.log(f"[전략 모드 변경] {label_map[mode]} 모드 적용")
        return True

    def _check_auto_mode(self) -> None:
        if self.strategy_mode != "auto":
            return
        prev_active: str = self.auto_active_mode
        should_defend: bool = False
        total_equity: float = self.last_usd_balance
        if not self.positions.empty:
            total_equity += (self.positions['quantity'] * self.positions['current_price']).sum()
        if total_equity > 0:
            cash_ratio: float = self.last_usd_balance / total_equity
            if cash_ratio <= 0.35:
                should_defend = True
        current_symbols: List[str] = self.symbols
        below_sma200: int = sum(1 for sym in current_symbols if not self.is_uptrend.get(sym, True))
        if below_sma200 >= 2:
            should_defend = True
        self.auto_active_mode = "defensive" if should_defend else "aggressive"
        if self.auto_active_mode != prev_active:
            self._apply_strategy_mode()
            label: str = "방어적" if should_defend else "공격적"
            reason: str = ""
            if should_defend:
                reasons: list = []
                if total_equity > 0 and self.last_usd_balance / total_equity <= 0.35:
                    reasons.append(f"예수금 비중 {self.last_usd_balance / total_equity * 100:.1f}%")
                if below_sma200 >= 2:
                    reasons.append(f"SMA200 하회 {below_sma200}종목")
                reason = f" ({', '.join(reasons)})"
            self.send_telegram_message(f"🔄 [전략 자동 전환] {label} 모드{reason}")
            self.log(f"[전략 자동 전환] {label} 모드{reason}")

    def _load_hwm(self) -> Dict[str, float]:
        """로컬 파일에서 최고점(HWM) 데이터를 불러옵니다."""
        try:
            if os.path.exists(self.hwm_file):
                with open(self.hwm_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"[HWM 로드 오류] {e}")
        return {}

    def _save_hwm(self) -> None:
        """최고점(HWM) 데이터를 로컬 파일에 저장합니다."""
        try:
            with open(self.hwm_file, 'w', encoding='utf-8') as f:
                json.dump(self.hwm, f, indent=4)
        except Exception as e:
            print(f"[HWM 저장 오류] {e}")

    def update_hwm(self, symbol: str, current_price: float) -> None:
        """현재가가 기존 최고점보다 높으면 갱신합니다. 기존 HWM이 0이면 초기화합니다."""
        if self.hwm.get(symbol, 0.0) == 0.0 and current_price > 0:
            self.hwm[symbol] = current_price
            self.log(f"📌 [{symbol}] 수동 매수 물량 감지 (최초 최고점 ${current_price:.2f} 세팅)")
            self._save_hwm()
        elif current_price > self.hwm.get(symbol, 0.0):
            self.hwm[symbol] = current_price
            self._save_hwm()
            
    def _auto_register_holdings(self) -> None:
        """슬롯이 비어있을 때 기존 보유 종목을 자동으로 슬롯에 등록합니다."""
        if self.slot_manager.get_active_slots():
            return
        try:
            data: Dict[str, Any] = self.api.get_balance_and_positions()
            held: List[Dict[str, Any]] = [p for p in data["positions"] if p.get("quantity", 0) > 0]
            if not held:
                print("[자동 등록] 보유 종목이 없습니다.")
                return

            print(f"[자동 등록] 슬롯이 비어있고 보유 종목 {len(held)}개 감지. 자동 등록합니다.")
            for pos in held:
                symbol: str = pos["symbol"]
                if self.slot_manager.is_full():
                    print(f"[자동 등록] 슬롯 가득 참 — {symbol} 등록 불가")
                    break
                is_leveraged: bool = symbol in LEVERAGED_ETF_MAP
                base_asset: str = LEVERAGED_ETF_MAP.get(symbol, symbol)
                self.slot_manager.add_slot(symbol, base_asset, is_leveraged)
                self.is_uptrend[symbol] = False
                self.is_rsi_oversold[symbol] = False
                self.prev_close[symbol] = 0.0
                cur_price: float = pos.get("current_price", 0.0)
                existing_hwm: float = self.hwm.get(symbol, 0.0)
                if existing_hwm > 0:
                    print(f"[자동 등록] {symbol} 기존 HWM 유지: ${existing_hwm:.2f}")
                else:
                    self.hwm[symbol] = cur_price if cur_price > 0 else 0.0
                print(f"[자동 등록] {symbol} 슬롯 등록 완료 (보유 {int(pos['quantity'])}주)")

            self._save_hwm()
            self._rebuild_positions_df()
            print(f"[자동 등록] 총 {len(self.symbols)}개 슬롯 등록 완료")
        except Exception as e:
            print(f"[자동 등록 오류] {e}")

    def add_symbol(self, symbol: str, buy_percent: float = 0.0) -> Dict[str, Any]:
        """슬롯에 종목을 추가합니다. buy_percent > 0이면 총자산 대비 해당 비율만큼 매수."""
        symbol = symbol.upper().strip()
        now_et: datetime = datetime.now(ZoneInfo("America/New_York"))
        if not self.is_regular_market_open(now_et):
            return {"success": False, "message": "정규장 시간에만 종목을 추가할 수 있습니다."}
        if self.slot_manager.is_full():
            return {"success": False, "message": f"슬롯이 가득 찼습니다. (최대 {self.slot_manager.max_slots}개)"}
        if self.slot_manager.has_symbol(symbol):
            return {"success": False, "message": f"{symbol}은(는) 이미 추가된 종목입니다."}

        try:
            ticker = yf.Ticker(symbol)
            info: Dict[str, Any] = ticker.info
            name: str = info.get('shortName', info.get('longName', symbol))
            if not info.get('regularMarketPrice') and not info.get('previousClose'):
                return {"success": False, "message": f"{symbol} 종목 정보를 찾을 수 없습니다."}
        except Exception as e:
            return {"success": False, "message": f"{symbol} 종목 검증 실패: {e}"}

        is_leveraged: bool = symbol in LEVERAGED_ETF_MAP
        base_asset: str = LEVERAGED_ETF_MAP.get(symbol, symbol)

        try:
            current_price: float = self.api.get_current_price(symbol)
            if current_price <= 0:
                return {"success": False, "message": f"{symbol} 현재가 조회 실패 (한투 API에서 거래 불가)"}
        except Exception as e:
            return {"success": False, "message": f"{symbol} 거래소 조회 실패: {e}"}

        already_held: bool = False
        bal_data: Dict[str, Any] = {}
        try:
            bal_data = self.api.get_balance_and_positions()
            for pos in bal_data.get("positions", []):
                if pos["symbol"] == symbol and pos.get("quantity", 0) > 0:
                    already_held = True
                    break
        except Exception:
            pass

        if already_held and buy_percent <= 0:
            self.slot_manager.add_slot(symbol, base_asset, is_leveraged)
            self.is_uptrend[symbol] = False
            self.is_rsi_oversold[symbol] = False
            self.prev_close[symbol] = 0.0
            self.hwm[symbol] = current_price
            self._save_hwm()
            self._rebuild_positions_df()
            try:
                self._check_single_symbol_trend(symbol)
            except Exception:
                pass
            self.log(f"✅ [슬롯 추가] {symbol} ({name}) — 기존 보유 종목 등록 (매수 없음)", send_tg=True)
            return {
                "success": True,
                "message": f"{symbol} 슬롯 추가 완료 (기존 보유 종목)",
                "symbol": symbol, "name": name,
                "is_leveraged": is_leveraged, "base_asset": base_asset,
                "price": current_price,
            }

        buy_qty: int = 1
        if buy_percent > 0:
            try:
                if not bal_data:
                    bal_data = self.api.get_balance_and_positions()
                total_assets: float = bal_data.get("usd_balance", 0.0) + bal_data.get("tot_stck_evlu", 0.0)
                buy_amount: float = total_assets * (buy_percent / 100.0)
                buy_qty = int(buy_amount / current_price)
                if buy_qty < 1:
                    return {"success": False, "message": f"{symbol} 매수 금액(${buy_amount:.0f})이 1주 가격(${current_price:.2f})보다 적습니다."}
            except Exception as e:
                return {"success": False, "message": f"매수 수량 계산 실패: {e}"}

        buy_price: float = round(current_price * 1.005, 2)
        success: bool = self.api.place_order(symbol, buy_qty, buy_price, is_buy=True)
        if not success:
            return {"success": False, "message": f"{symbol} {buy_qty}주 매수 주문 실패"}

        self.slot_manager.add_slot(symbol, base_asset, is_leveraged)
        self.is_uptrend[symbol] = False
        self.is_rsi_oversold[symbol] = False
        self.prev_close[symbol] = 0.0
        self.hwm[symbol] = 0.0
        self._save_hwm()
        self._rebuild_positions_df()

        try:
            self._check_single_symbol_trend(symbol)
        except Exception:
            pass

        est_amount: float = buy_qty * buy_price
        pct_label: str = f" ({buy_percent}%)" if buy_percent > 0 else ""
        self.log(f"✅ [슬롯 추가] {symbol} ({name}) {buy_qty}주 매수 주문{pct_label} ≈ ${est_amount:,.0f}", send_tg=True)
        self._log_trade(symbol, "매수", buy_qty, buy_price, est_amount, f"[슬롯 추가] 초기 매수{pct_label}")

        return {
            "success": True,
            "message": f"{symbol} 슬롯 추가 완료 ({buy_qty}주 매수)",
            "symbol": symbol, "name": name,
            "is_leveraged": is_leveraged, "base_asset": base_asset,
            "price": current_price, "quantity": buy_qty,
            "est_amount": est_amount,
        }

    def remove_symbol(self, symbol: str, sell_all: bool = True) -> Dict[str, Any]:
        """슬롯에서 종목을 제거합니다. sell_all=True이면 전량 매도 후 제거."""
        symbol = symbol.upper().strip()
        if not self.slot_manager.has_symbol(symbol):
            return {"success": False, "message": f"{symbol}은(는) 슬롯에 없습니다."}

        if sell_all:
            try:
                data: Dict[str, Any] = self.api.get_balance_and_positions()
                position: Optional[Dict[str, Any]] = None
                for pos in data["positions"]:
                    if pos["symbol"] == symbol and pos.get("quantity", 0) > 0:
                        position = pos
                        break

                if position:
                    qty: int = int(position["quantity"])
                    price: float = position.get("current_price", 0.0)
                    if price <= 0:
                        price = self.api.get_current_price(symbol)
                    sell_price: float = round(price * 0.99, 2)
                    order_ok: bool = self.api.place_order(symbol, qty, sell_price, is_buy=False)
                    if order_ok:
                        self._log_trade(symbol, "매도", qty, sell_price, qty * sell_price, "[슬롯 제거] 전량 매도")
                        self.log(f"📤 [슬롯 제거] {symbol} {qty}주 전량 매도 주문", send_tg=True)
                    else:
                        return {"success": False, "message": f"{symbol} 매도 주문 실패"}
            except Exception as e:
                return {"success": False, "message": f"매도 처리 오류: {e}"}

        self.slot_manager.remove_slot(symbol)
        self.is_uptrend.pop(symbol, None)
        self.is_rsi_oversold.pop(symbol, None)
        self.prev_close.pop(symbol, None)
        self.hwm.pop(symbol, None)
        self._save_hwm()
        self.daily_state.pop(symbol, None)
        self._save_daily_state()
        self._rebuild_positions_df()

        action: str = "전량 매도 후 제거" if sell_all else "감시 중단"
        self.log(f"🗑️ [슬롯 제거] {symbol} {action}", send_tg=True)
        return {"success": True, "message": f"{symbol} 슬롯 제거 완료 ({action})"}

    def _check_single_symbol_trend(self, symbol: str) -> None:
        """단일 종목의 SMA200 및 RSI를 확인합니다."""
        base_sym: str = self.base_assets.get(symbol, symbol)
        try:
            ticker = yf.Ticker(base_sym)
            hist: pd.DataFrame = ticker.history(period="1y")
            if len(hist) < 200:
                self.is_uptrend[symbol] = False
                self.is_rsi_oversold[symbol] = False
                return

            sma_200: float = float(hist['Close'].tail(200).mean())
            current_price: float = float(hist['Close'].iloc[-1])
            delta: pd.Series = hist['Close'].diff()
            gain: pd.Series = delta.where(delta > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
            loss_raw: pd.Series = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
            loss_safe: pd.Series = loss_raw.replace(0.0, 1e-10)
            rs: pd.Series = gain / loss_safe
            current_rsi: float = float((100 - (100 / (1 + rs))).iloc[-1])
            if pd.isna(current_rsi):
                current_rsi = 50.0

            self.is_uptrend[symbol] = current_price > sma_200
            self.is_rsi_oversold[symbol] = current_rsi < 30.0
        except Exception:
            self.is_uptrend[symbol] = False
            self.is_rsi_oversold[symbol] = False

        try:
            etf_ticker = yf.Ticker(symbol)
            etf_hist: pd.DataFrame = etf_ticker.history(period="5d")
            if len(etf_hist) >= 2:
                self.prev_close[symbol] = float(etf_hist['Close'].iloc[-2])
            elif len(etf_hist) == 1:
                self.prev_close[symbol] = float(etf_hist['Close'].iloc[-1])
        except Exception:
            pass

    def search_ticker(self, symbol: str) -> Dict[str, Any]:
        """티커를 검색하고 종목 정보를 반환합니다."""
        symbol = symbol.upper().strip()
        try:
            ticker = yf.Ticker(symbol)
            info: Dict[str, Any] = ticker.info
            name: str = info.get('shortName', info.get('longName', ''))
            price: float = info.get('regularMarketPrice', info.get('previousClose', 0.0)) or 0.0
            if not name and price <= 0:
                return {"found": False, "message": f"{symbol} 종목을 찾을 수 없습니다."}

            is_leveraged: bool = symbol in LEVERAGED_ETF_MAP
            base_asset: str = LEVERAGED_ETF_MAP.get(symbol, symbol)
            already_added: bool = self.slot_manager.has_symbol(symbol)

            tradeable: bool = False
            try:
                kis_price: float = self.api.get_current_price(symbol)
                tradeable = kis_price > 0
                if tradeable:
                    price = kis_price
            except Exception:
                pass

            return {
                "found": True, "symbol": symbol, "name": name,
                "price": price, "is_leveraged": is_leveraged,
                "base_asset": base_asset, "tradeable": tradeable,
                "already_added": already_added,
                "currency": info.get('currency', 'USD'),
                "exchange": info.get('exchange', ''),
            }
        except Exception as e:
            return {"found": False, "message": f"검색 실패: {e}"}

    def update_exchange_rate(self) -> None:
        """yfinance 폴백 환율 (sync_positions 호출 전 초기값 용도)"""
        try:
            ticker = yf.Ticker("KRW=X")
            hist = ticker.history(period="1d")
            if not hist.empty:
                self.exchange_rate = float(hist['Close'].iloc[-1])
        except Exception:
            pass
            
    def check_trend_and_momentum(self) -> None:
        """Strategy E: 기초자산 SMA200 필터 + RSI<30 과매도 감지 + 레버리지 ETF 전일종가 저장"""
        self.log("📊 [추세 판단] 기초 자산 200일 SMA 및 14일 RSI 확인 중...")
        for etf_sym, base_sym in self.base_assets.items():
            try:
                ticker = yf.Ticker(base_sym)
                hist = ticker.history(period="1y")
                if len(hist) < 200:
                    self.log(f"⚠️ [{base_sym}] 데이터 부족. (매수 불가 처리)")
                    self.is_uptrend[etf_sym] = False
                    self.is_rsi_oversold[etf_sym] = False
                    continue
                
                sma_200: float = float(hist['Close'].tail(200).mean())
                current_price: float = float(hist['Close'].iloc[-1])
                
                delta: pd.Series = hist['Close'].diff()
                gain: pd.Series = delta.where(delta > 0, 0.0)
                loss: pd.Series = -delta.where(delta < 0, 0.0)
                avg_gain: pd.Series = gain.ewm(alpha=1/14, adjust=False).mean()
                avg_loss: pd.Series = loss.ewm(alpha=1/14, adjust=False).mean()
                avg_loss_safe: pd.Series = avg_loss.replace(0.0, 1e-10)
                rs: pd.Series = avg_gain / avg_loss_safe
                rsi_14: pd.Series = 100 - (100 / (1 + rs))
                current_rsi: float = float(rsi_14.iloc[-1])
                if pd.isna(current_rsi):
                    current_rsi = 50.0
                
                is_up: bool = current_price > sma_200
                is_oversold: bool = current_rsi < 30.0
                
                self.is_uptrend[etf_sym] = is_up
                self.is_rsi_oversold[etf_sym] = is_oversold
                
                status: str = "SMA200 위 (매수 허용) 🟢" if is_up else "SMA200 아래 (매수 보류) 🔴"
                oversold_tag: str = " | ⚡ RSI 과매도!" if is_oversold else ""
                self.log(f"📈 [{base_sym} (for {etf_sym})] 현재가: ${current_price:.2f} / SMA: ${sma_200:.2f} / RSI: {current_rsi:.1f} -> {status}{oversold_tag}", send_tg=False)
                
            except Exception as e:
                error_msg: str = f"🔥 [긴급 에러 발생]\n사유: 지표 계산 실패 ({e})\n위치: check_trend_and_momentum ({base_sym})\n상태: 해당 종목 매수 보류"
                self.send_error_telegram(error_msg)
                self.log(f"❌ [{base_sym}] 지표 계산 오류: {e}")
                self.is_uptrend[etf_sym] = False
                self.is_rsi_oversold[etf_sym] = False
        
        for etf_sym in self.symbols:
            try:
                etf_ticker = yf.Ticker(etf_sym)
                etf_hist = etf_ticker.history(period="5d")
                if len(etf_hist) >= 2:
                    self.prev_close[etf_sym] = float(etf_hist['Close'].iloc[-2])
                elif len(etf_hist) == 1:
                    self.prev_close[etf_sym] = float(etf_hist['Close'].iloc[-1])
                self.log(f"📌 [{etf_sym}] 전일종가: ${self.prev_close[etf_sym]:.2f}", send_tg=False)
            except Exception as e:
                self.log(f"⚠️ [{etf_sym}] 전일종가 조회 실패: {e}")

    def _recheck_sma200_intraday(self) -> None:
        """장중 기초자산 현재가를 SMA200과 비교하여 is_uptrend를 실시간 갱신합니다."""
        now: float = time.time()
        if now - self._last_sma_recheck < self._sma_recheck_interval:
            return
        self._last_sma_recheck = now

        for etf_sym, base_sym in self.base_assets.items():
            try:
                ticker = yf.Ticker(base_sym)
                hist: pd.DataFrame = ticker.history(period="1y")
                if len(hist) < 200:
                    continue
                sma_200: float = float(hist['Close'].tail(200).mean())
                current_price: float = float(hist['Close'].iloc[-1])
                was_up: bool = self.is_uptrend.get(etf_sym, False)
                is_up: bool = current_price > sma_200

                if was_up and not is_up:
                    self.is_uptrend[etf_sym] = False
                    self.log(f"⚠️ [{base_sym}] 장중 SMA200 하향 돌파 감지 (${current_price:.2f} < ${sma_200:.2f}) → {etf_sym} 매수 보류", send_tg=True)
                elif not was_up and is_up:
                    self.is_uptrend[etf_sym] = True
                    self.log(f"✅ [{base_sym}] 장중 SMA200 상향 돌파 감지 (${current_price:.2f} > ${sma_200:.2f}) → {etf_sym} 매수 허용", send_tg=True)
            except Exception as e:
                self.log(f"[SMA200 장중 재체크 오류] {base_sym}: {e}", send_tg=False)

    def get_eastern_time(self) -> datetime:
        return datetime.now(ZoneInfo("America/New_York"))

    def is_regular_market_open(self, now_et: datetime) -> bool:
        if now_et.weekday() >= 5 or self.is_us_market_holiday(now_et):
            return False

        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        early_close = self.get_early_close_time(now_et)
        market_close = early_close if early_close else now_et.replace(hour=16, minute=0, second=0, microsecond=0)

        return market_open <= now_et < market_close

    def is_active_trading_time(self, now_et: datetime) -> bool:
        if now_et.weekday() >= 5 or self.is_us_market_holiday(now_et):
            return False

        pre_market_open = now_et.replace(hour=4, minute=0, second=0, microsecond=0)
        early_close = self.get_early_close_time(now_et)
        market_close = early_close if early_close else now_et.replace(hour=16, minute=0, second=0, microsecond=0)

        return pre_market_open <= now_et < market_close

    def send_telegram_message(self, message: str, max_retries: int = 3) -> None:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            print(f"[텔레그램] 토큰/채팅ID 없음 (token={bool(token)}, chat_id={bool(chat_id)})")
            return

        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        for attempt in range(max_retries):
            try:
                resp = requests.post(url, json=payload, timeout=5)
                if resp.status_code == 200:
                    return
                print(f"[텔레그램 전송 실패] HTTP {resp.status_code} (시도 {attempt+1}/{max_retries})")
            except Exception as e:
                print(f"[텔레그램 전송 실패] {e} (시도 {attempt+1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

    def send_error_telegram(self, message: str) -> None:
        """에러 메시지를 10분 쓰로틀링 적용하여 전송합니다."""
        key: str = message[:50]
        now: float = time.time()
        last_sent: float = self._error_throttle.get(key, 0.0)
        if now - last_sent < self._error_throttle_seconds:
            return
        self._error_throttle[key] = now
        self.send_telegram_message(message)

    def _send_heartbeat(self) -> None:
        now: float = time.time()
        if now - self.last_heartbeat < self.heartbeat_interval:
            return
        if self.get_eastern_time().weekday() >= 5:
            return
        self.last_heartbeat = now

        uptime_hours: float = (now - self._start_time) / 3600
        held_count: int = int((self.positions['quantity'] > 0).sum())
        total_equity: float = self._calc_total_equity()

        msg: str = f"💓 [봇 헬스체크]\n"
        msg += f"상태: 정상 작동 중\n"
        msg += f"가동 시간: {uptime_hours:.1f}시간\n"
        msg += f"보유 종목: {held_count}개\n"
        msg += f"총 자산: ${total_equity:,.2f} (약 {total_equity * self.exchange_rate:,.0f}원)\n"
        msg += f"예수금: ${self.last_usd_balance:,.2f} (약 {self.last_krw_balance:,.0f}원)"
        self.send_telegram_message(msg)

    def _save_equity_snapshot(self, total_equity: float, cash: float, positions_value: float) -> None:
        if total_equity <= 0:
            return
        try:
            log: List[Dict[str, Any]] = []
            if os.path.exists(self.equity_log_file):
                with open(self.equity_log_file, 'r', encoding='utf-8') as f:
                    log = json.load(f)

            today: str = self.get_eastern_time().strftime('%Y-%m-%d')
            log = [entry for entry in log if entry.get('date') != today]
            log.append({
                "date": today,
                "equity_usd": round(total_equity, 2),
                "cash_usd": round(cash, 2),
                "positions_value": round(positions_value, 2),
                "exchange_rate": round(self.exchange_rate, 2)
            })

            if len(log) > 365:
                log = log[-365:]

            with open(self.equity_log_file, 'w', encoding='utf-8') as f:
                json.dump(log, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[자산 스냅샷 저장 오류] {e}")

    def _log_trade(self, symbol: str, side: str, qty: int, price: float, amount: float, reason: str) -> None:
        """매매 내역을 trade_log.json에 기록합니다."""
        try:
            log: List[Dict[str, Any]] = []
            if os.path.exists(self.trade_log_file):
                with open(self.trade_log_file, 'r', encoding='utf-8') as f:
                    log = json.load(f)

            log.append({
                "timestamp": datetime.now(ZoneInfo("Asia/Seoul")).strftime('%Y-%m-%d %H:%M:%S'),
                "timestamp_et": self.get_eastern_time().strftime('%Y-%m-%d %H:%M:%S'),
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": round(price, 2),
                "amount": round(amount, 2),
                "reason": reason,
                "balance_after": round(self.last_usd_balance, 2)
            })

            if len(log) > 1000:
                log = log[-1000:]

            with open(self.trade_log_file, 'w', encoding='utf-8') as f:
                json.dump(log, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[매매기록 저장 오류] {e}")

    def _send_premarket_briefing(self) -> None:
        try:
            self.sync_positions()
            total_equity: float = self._calc_total_equity()

            msg = "🌅 [일일 봇 상태 리포트]\n"
            msg += f"✅ 한투 API 연결 및 토큰 갱신 성공\n"
            msg += f"💵 예수금: ${self.last_usd_balance:,.2f} (약 {self.last_krw_balance:,.0f}원)\n"
            msg += f"💰 총 자산: ${total_equity:,.2f} (약 {total_equity * self.exchange_rate:,.0f}원)\n"
            msg += f"💱 환율: {self.exchange_rate:,.2f}원/USD\n"
            msg += f"📌 매수 가능 여부는 본장 30분 전(ET 09:00) 재검수에서 확정됩니다."
            self.send_telegram_message(msg)
        except Exception as e:
            self.log(f"브리핑 전송 실패: {e}")

    def _recheck_trend_realtime(self) -> None:
        """본장 30분 전(ET 09:00) 실시간 프리장 가격으로 SMA200 재검수"""
        self.log("🔄 [본장 전 재검수] 실시간 가격으로 SMA200 재확인 중...")
        changes: List[str] = []

        for etf_sym, base_sym in self.base_assets.items():
            try:
                ticker = yf.Ticker(base_sym)
                hist = ticker.history(period="1y")
                if len(hist) < 200:
                    self.log(f"⚠️ [{base_sym}] 데이터 부족. (매수 불가 처리)")
                    self.is_uptrend[etf_sym] = False
                    continue

                sma_200: float = float(hist['Close'].tail(200).mean())
                realtime_price: float = self.api.get_current_price(base_sym)

                if realtime_price <= 0:
                    realtime_price = float(hist['Close'].iloc[-1])
                    self.log(f"⚠️ [{base_sym}] 실시간 가격 조회 실패, 전일종가 사용: ${realtime_price:.2f}")

                delta: pd.Series = hist['Close'].diff()
                gain: pd.Series = delta.where(delta > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
                loss_raw: pd.Series = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
                loss_safe: pd.Series = loss_raw.replace(0.0, 1e-10)
                rs: pd.Series = gain / loss_safe
                current_rsi: float = float((100 - (100 / (1 + rs))).iloc[-1])
                if pd.isna(current_rsi):
                    current_rsi = 50.0
                is_oversold: bool = current_rsi < 30.0
                self.is_rsi_oversold[etf_sym] = is_oversold

                prev_status: bool = self.is_uptrend.get(etf_sym, False)
                new_status: bool = realtime_price > sma_200
                self.is_uptrend[etf_sym] = new_status

                rsi_tag: str = f" | RSI: {current_rsi:.1f}" + (" ⚡과매도" if is_oversold else "")
                status_str: str = ("🟢 SMA200 위 (매수 가능)" if new_status else "🔴 SMA200 아래 (매수 차단)") + rsi_tag
                self.log(f"🔄 [{base_sym} ({etf_sym})] 실시간: ${realtime_price:.2f} / SMA200: ${sma_200:.2f} -> {status_str}")

                if prev_status != new_status:
                    arrow: str = "🟢→🔴" if not new_status else "🔴→🟢"
                    changes.append(f" - {base_sym} ({etf_sym}): {arrow} 실시간 ${realtime_price:.2f} vs SMA200 ${sma_200:.2f}{rsi_tag}")

            except Exception as e:
                self.send_error_telegram(f"🔥 [재검수 오류] {base_sym}: {e}")
                self.log(f"❌ [{base_sym}] 재검수 오류: {e}")
                self.is_uptrend[etf_sym] = False

        if changes:
            msg: str = "🔄 [본장 전 재검수 - 상태 변경 감지!]\n"
            msg += f"⏰ 시각: {self.get_eastern_time().strftime('%H:%M')} ET\n"
            msg += "\n".join(changes)
            self.send_telegram_message(msg)
        else:
            summary_lines: List[str] = []
            for etf_sym, base_sym in self.base_assets.items():
                icon: str = "🟢" if self.is_uptrend.get(etf_sym, False) else "🔴"
                summary_lines.append(f" - {base_sym} ({etf_sym}): {icon} 변동 없음")
            msg = "🔄 [본장 전 재검수 완료]\n"
            msg += f"⏰ 시각: {self.get_eastern_time().strftime('%H:%M')} ET\n"
            msg += "✅ 자정 브리핑 대비 변동 없음\n"
            msg += "\n".join(summary_lines)
            self.send_telegram_message(msg)

    def _check_cash_ratio(self) -> None:
        try:
            total_equity: float = self._calc_total_equity()
            if total_equity <= 0:
                return

            cash_ratio: float = self.last_usd_balance / total_equity
            today_str: str = self.get_eastern_time().strftime('%Y-%m-%d')
            krw_balance: float = self.last_krw_balance
            krw_total: float = total_equity * self.exchange_rate

            if cash_ratio <= 0.30 and self._cash_alert_30_sent != today_str:
                msg: str = (
                    f"🚨 [예수금 위험] 현금 비중 {cash_ratio*100:.1f}%\n"
                    f"💵 예수금: ${self.last_usd_balance:,.2f} ({krw_balance:,.0f}원)\n"
                    f"💰 총 자산: ${total_equity:,.2f} ({krw_total:,.0f}원)\n"
                    f"⚠️ 수동 매도 또는 추가 입금을 권장합니다."
                )
                self.send_telegram_message(msg)
                self._cash_alert_30_sent = today_str
                self._cash_alert_40_sent = today_str
            elif cash_ratio <= 0.40 and self._cash_alert_40_sent != today_str:
                msg = (
                    f"⚠️ [예수금 주의] 현금 비중 {cash_ratio*100:.1f}%\n"
                    f"💵 예수금: ${self.last_usd_balance:,.2f} ({krw_balance:,.0f}원)\n"
                    f"💰 총 자산: ${total_equity:,.2f} ({krw_total:,.0f}원)\n"
                    f"📌 예수금 비중을 모니터링하세요."
                )
                self.send_telegram_message(msg)
                self._cash_alert_40_sent = today_str
        except Exception as e:
            self.log(f"[예수금 비중 체크 오류] {e}")

    def _send_closing_report(self) -> None:
        try:
            self.fetch_market_data()
            positions_value: float = self.tot_stck_evlu if self.tot_stck_evlu > 0 else float((self.positions['quantity'] * self.positions['current_price']).sum())
            total_equity: float = self.last_usd_balance + positions_value
            
            cash_ratio = (self.last_usd_balance / total_equity * 100) if total_equity > 0 else 0
            
            msg = "🌙 [장 마감 일일 결산]\n"
            msg += f"💰 총 평가 자산: ${total_equity:,.2f} (약 {total_equity * self.exchange_rate:,.0f}원)\n"
            msg += f"💵 현금: ${self.last_usd_balance:,.2f} (약 {self.last_krw_balance:,.0f}원) | 비중: {cash_ratio:.1f}%\n"
            msg += "📉 보유 종목 트레일링 스탑 위험도 (최고점 대비):\n"
            
            has_positions = False
            for symbol in self.symbols:
                mask_sym = self.positions['symbol'] == symbol
                if not mask_sym.any():
                    continue
                qty = self.positions.loc[mask_sym, 'quantity'].values[0]
                if qty > 0:
                    price = self.positions.loc[mask_sym, 'current_price'].values[0]
                    hwm_price = self.hwm.get(symbol, 0.0)
                    drawdown = 0.0
                    if hwm_price > 0:
                        drawdown = (price - hwm_price) / hwm_price * 100
                    msg += f" - {symbol}: {drawdown:.2f}% (T.S 기준 {self.trailing_stop_threshold*100:.0f}%)\n"
                    has_positions = True
            
            if not has_positions:
                msg += " - 보유 중인 종목 없음\n"
                
            self.send_telegram_message(msg)
            self._save_equity_snapshot(total_equity, self.last_usd_balance, positions_value)
        except Exception as e:
            self.log(f"결산 전송 실패: {e}")

    def log(self, message: str, send_tg: bool = False, print_stdout: bool = True) -> None:
        if print_stdout:
            print(message)
        timestamp: str = datetime.now(ZoneInfo("Asia/Seoul")).strftime('%H:%M:%S')
        self.logs.insert(0, f"[{timestamp}] {message}")
        if len(self.logs) > 50:
            self.logs.pop()
            
        if send_tg:
            self.send_telegram_message(message)

    def _calc_total_equity(self) -> float:
        """총 자산 계산 - KIS API tot_stck_evlu 우선, 폴백으로 로컬 계산"""
        if self.tot_stck_evlu > 0:
            return self.last_usd_balance + self.tot_stck_evlu
        if not self.positions.empty:
            return self.last_usd_balance + float((self.positions['quantity'] * self.positions['current_price']).sum())
        return self.last_usd_balance

    def sync_positions(self) -> None:
        item_cd: str = self.symbols[0] if self.symbols else "AAPL"
        data: Dict[str, Any] = self.api.get_balance_and_positions(item_cd=item_cd)
        new_usd: float = data["usd_balance"]
        if new_usd <= 0 and self.last_usd_balance > 100:
            self.log(f"⚠️ [API 이상] USD 예수금 $0 반환 (기존 ${self.last_usd_balance:,.2f} 유지)", send_tg=False)
        else:
            self.last_usd_balance = new_usd

        api_exrt: float = data.get("exchange_rate", 0.0)
        if api_exrt > 0:
            self.exchange_rate = api_exrt

        api_krw: float = data.get("krw_balance", 0.0)
        if api_krw > 0:
            self.last_krw_balance = api_krw
        else:
            self.last_krw_balance = self.last_usd_balance * self.exchange_rate

        new_evlu_pfls: float = data.get("tot_evlu_pfls", 0.0)
        new_pchs_amt: float = data.get("tot_pchs_amt", 0.0)
        new_stck_evlu: float = data.get("tot_stck_evlu", 0.0)
        if new_stck_evlu <= 0 and self.tot_stck_evlu > 100 and len(self.symbols) > 0:
            self.log(f"⚠️ [API 이상] 주식 평가액 $0 반환 (기존 ${self.tot_stck_evlu:,.2f} 유지)", send_tg=False)
        else:
            self.tot_evlu_pfls = new_evlu_pfls
            self.tot_pchs_amt = new_pchs_amt
            self.tot_stck_evlu = new_stck_evlu

        current_symbols: List[str] = self.symbols
        if self.positions.empty or set(self.positions['symbol'].tolist()) != set(current_symbols):
            self._rebuild_positions_df()

        if not self.positions.empty:
            self.positions['quantity'] = 0.0
            self.positions['avg_price'] = 0.0
            self.positions['current_price'] = 0.0

        for pos in data["positions"]:
            symbol: str = pos["symbol"]
            if symbol in current_symbols and not self.positions.empty:
                idx: pd.Series = self.positions['symbol'] == symbol
                if idx.any():
                    self.positions.loc[idx, 'quantity'] = pos["quantity"]
                    self.positions.loc[idx, 'avg_price'] = pos["avg_price"]
                    cur_price: float = pos.get("current_price", 0.0)
                    if cur_price > 0:
                        self.positions.loc[idx, 'current_price'] = cur_price
                    api_return: float = pos.get("return_rate", 0.0)
                    if api_return != 0.0:
                        self.positions.loc[idx, 'return_rate'] = api_return

    def fetch_market_data(self) -> None:
        self.sync_positions()
        if self.positions.empty:
            if not self._sync_logged:
                self.log(f"데이터 동기화 완료 | 예수금: ${self.last_usd_balance:.2f} (슬롯 없음)", print_stdout=False)
                self._sync_logged = True
            return

        for sym in self.symbols:
            mask: pd.Series = self.positions['symbol'] == sym
            if not mask.any():
                continue
            cur: float = float(self.positions.loc[mask, 'current_price'].values[0])
            if cur <= 0:
                self.positions.loc[mask, 'current_price'] = self.api.get_current_price(sym)
        
        need_calc: pd.Series = (self.positions['avg_price'] > 0) & (self.positions['return_rate'] == 0.0)
        if need_calc.any():
            self.positions.loc[need_calc, 'return_rate'] = (
                self.positions.loc[need_calc, 'current_price'] - self.positions.loc[need_calc, 'avg_price']
            ) / self.positions.loc[need_calc, 'avg_price']
        
        self.positions.fillna(0.0, inplace=True)
        if not self._sync_logged:
            self.log(f"데이터 동기화 완료 | 예수금: ${self.last_usd_balance:.2f} | 슬롯: {len(self.symbols)}개", print_stdout=False)
            self._sync_logged = True
        
        now_et: datetime = self.get_eastern_time()
        for symbol in self.symbols:
            mask_sym: pd.Series = self.positions['symbol'] == symbol
            if not mask_sym.any():
                continue
            price: float = float(self.positions.loc[mask_sym, 'current_price'].values[0])
            qty: float = float(self.positions.loc[mask_sym, 'quantity'].values[0])
            
            if qty > 0 and price > 0:
                self.update_hwm(symbol, price)
                if self.is_regular_market_open(now_et):
                    self.check_trailing_stop(symbol, price, qty)
        
    def check_trailing_stop(self, symbol: str, current_price: float, qty: float) -> None:
        """Strategy E: 최고점 대비 -25% 하락 시 보유량의 50%만 매도 (부분 매도)"""
        hwm_price: float = self.hwm.get(symbol, 0.0)
        if hwm_price <= 0:
            return
            
        drawdown: float = (current_price - hwm_price) / hwm_price
        
        if drawdown <= self.trailing_stop_threshold:
            sell_qty: int = max(1, int(qty * self.trailing_sell_pct))
            sell_price: float = current_price * 0.99
            self.log(f"🚨 [트레일링 스탑 발동] {symbol} 최고점 대비 {sell_qty}주 부분 매도!", send_tg=False)
            success: bool = self.api.place_order(symbol, sell_qty, sell_price, is_buy=False)
            if success:
                self.hwm[symbol] = current_price
                self._save_hwm()
                
                sold_amount: float = sell_qty * sell_price
                self.last_usd_balance += sold_amount
                self.last_krw_balance += sold_amount * self.exchange_rate
                self._log_trade(symbol, "매도", sell_qty, sell_price, sold_amount, f"[{self._get_mode_label()}] 트레일링 스탑 ({drawdown*100:.1f}%)")
                
                msg: str = f"🛡️ [부분 매도 체결]\n"
                msg += f"종목: {symbol}\n"
                msg += f"단가/수량: ${sell_price:.2f} x {sell_qty}주 (보유의 {self.trailing_sell_pct*100:.0f}%)\n"
                msg += f"총액: ${sold_amount:,.2f} (약 {sold_amount * self.exchange_rate:,.0f}원)\n"
                msg += f"사유: 최고점(${hwm_price:.2f}) 대비 {drawdown*100:.2f}% 하락 (트레일링 스탑)\n"
                msg += f"잔여 보유: {int(qty - sell_qty)}주 | 잔여 현금: ${self.last_usd_balance:,.2f} (약 {self.last_krw_balance:,.0f}원)"
                self.send_telegram_message(msg)
        
    def _get_mode_label(self) -> str:
        if self.strategy_mode == "auto":
            return f"자동({'공격' if self.auto_active_mode == 'aggressive' else '방어'})"
        return "공격" if self.strategy_mode == "aggressive" else "방어"

    def _place_calculated_order(self, symbol: str, price: float, target_budget: float, tier_name: str) -> bool:
        buy_price: float = round(price * 1.005, 2)
        qty_to_buy: int = int(target_budget / buy_price)
        if target_budget > 0 and qty_to_buy == 0:
            qty_to_buy = 1
            
        required_cash: float = qty_to_buy * buy_price
        
        if qty_to_buy > 0 and self.last_usd_balance >= required_cash:
            success: bool = self.api.place_order(symbol, qty_to_buy, buy_price, is_buy=True)
            if success:
                self.last_usd_balance -= required_cash
                self.last_krw_balance -= required_cash * self.exchange_rate
                mode_label: str = self._get_mode_label()
                self.log(f"✅ [{mode_label}|{tier_name}] {symbol} 매수 체결: {qty_to_buy}주 (${required_cash:.2f})")
                self._log_trade(symbol, "매수", qty_to_buy, buy_price, required_cash, f"[{mode_label}] {tier_name}")
                
                state: Dict[str, Any] = self.daily_state.get(symbol, {})
                used: int = sum([state.get('base', False), state.get('t2', False), state.get('t4', False), state.get('t8', False)])
                rem: int = max(0, 4 - used)
                
                tg_msg: str = f"🛒 [매수 체결]\n"
                tg_msg += f"종목: {symbol}\n"
                tg_msg += f"모드: {mode_label} | 구분: {tier_name}\n"
                tg_msg += f"단가/수량: ${buy_price:.2f} x {qty_to_buy}주\n"
                tg_msg += f"총액: ${required_cash:,.2f} (약 {required_cash * self.exchange_rate:,.0f}원)\n"
                tg_msg += f"금일 남은 매수 기회: {rem}회\n"
                tg_msg += f"잔여 현금: ${self.last_usd_balance:,.2f} (약 {self.last_krw_balance:,.0f}원)"
                self.send_telegram_message(tg_msg)
            else:
                self.log(f"❌ [{self._get_mode_label()}|{tier_name}] {symbol} 매수 실패: {qty_to_buy}주 (${required_cash:.2f})")
                
            return success
        return False

    def execute_strategy(self, now_et: datetime) -> None:
        """Strategy E: SMA200 필터 + 전일종가 기준 DCA + RSI<30 과매도 보너스"""
        if not self.is_regular_market_open(now_et):
            return
        
        market_open_time: datetime = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        morning_end_time: datetime = now_et.replace(hour=10, minute=0, second=0, microsecond=0)
        market_close_soon: datetime = now_et.replace(hour=15, minute=50, second=0, microsecond=0)
        
        is_morning_session: bool = market_open_time <= now_et < morning_end_time
        is_after_morning: bool = now_et >= morning_end_time
        is_market_close_soon: bool = now_et >= market_close_soon

        total_equity: float = self.last_usd_balance
        if not self.positions.empty:
            total_equity += (self.positions['quantity'] * self.positions['current_price']).sum()

        for symbol in self.symbols:
            mask_sym: pd.Series = self.positions['symbol'] == symbol
            if not mask_sym.any():
                continue
            price: float = float(self.positions.loc[mask_sym, 'current_price'].values[0])
            qty: float = float(self.positions.loc[mask_sym, 'quantity'].values[0])
            
            if price <= 0:
                continue

            block_ts: float = self.manual_sell_block.get(symbol, 0.0)
            if block_ts > 0 and (time.time() - block_ts) < self.manual_sell_block_seconds:
                continue
                
            state: Dict[str, Any] = self.daily_state.get(symbol, {'base': False, 't2': False, 't4': False, 't8': False, 'rsi_bonus': False, 'morning_low': 999999.0})
            
            if is_morning_session:
                if state['morning_low'] == 999999.0 or price < state['morning_low']:
                    state['morning_low'] = price
            
            is_buy_allowed: bool = self.is_uptrend.get(symbol, False)
            
            # 1. 매일 1회 기본 적립 (SMA200 필터만 적용)
            if not state['base'] and is_buy_allowed:
                buy_now: bool = False
                if is_after_morning and (state['morning_low'] == 999999.0 or price <= state['morning_low'] * 1.005):
                    buy_now = True
                if is_market_close_soon:
                    buy_now = True
                if buy_now:
                    base_buy_amt: float = total_equity * self.base_buy_ratio
                    if self._place_calculated_order(symbol, price, base_buy_amt, "매일 기본적립(저점)"):
                        state['base'] = True
                        self.daily_state[symbol] = state
                        self._save_daily_state()
                        total_equity = self.last_usd_balance + (self.positions['quantity'] * self.positions['current_price']).sum()
            
            # 2. 전일종가 대비 당일 하락률 기준 DCA (SMA200 필터만 적용)
            prev_close: float = self.prev_close.get(symbol, 0.0)
            intraday_drop: float = (price - prev_close) / prev_close if prev_close > 0 else 0.0
            
            if qty > 0 and is_buy_allowed:
                if intraday_drop <= self.dca_2_threshold and not state['t2']:
                    w2_amt: float = total_equity * self.w2_ratio
                    if self._place_calculated_order(symbol, price, w2_amt, f"-2% 물타기(전일종가 대비 {intraday_drop*100:.1f}%)"):
                        state['t2'] = True
                        self.daily_state[symbol] = state
                        self._save_daily_state()
                        total_equity = self.last_usd_balance + (self.positions['quantity'] * self.positions['current_price']).sum()
                        
                if intraday_drop <= self.dca_4_threshold and not state['t4']:
                    w4_amt: float = total_equity * self.w4_ratio
                    if self._place_calculated_order(symbol, price, w4_amt, f"-4% 물타기(전일종가 대비 {intraday_drop*100:.1f}%)"):
                        state['t4'] = True
                        self.daily_state[symbol] = state
                        self._save_daily_state()
                        total_equity = self.last_usd_balance + (self.positions['quantity'] * self.positions['current_price']).sum()
                        
                if intraday_drop <= self.dca_8_threshold and not state['t8']:
                    w8_amt: float = total_equity * self.w8_ratio
                    if self._place_calculated_order(symbol, price, w8_amt, f"-8% 물타기(전일종가 대비 {intraday_drop*100:.1f}%)"):
                        state['t8'] = True
                        self.daily_state[symbol] = state
                        self._save_daily_state()
                        total_equity = self.last_usd_balance + (self.positions['quantity'] * self.positions['current_price']).sum()
            
            # 3. RSI<30 과매도 보너스 매수 (하루 1회, SMA200 무관)
            if qty > 0 and not state.get('rsi_bonus', False) and self.is_rsi_oversold.get(symbol, False):
                rsi_amt: float = total_equity * self.w4_ratio
                if self._place_calculated_order(symbol, price, rsi_amt, "⚡ RSI 과매도 보너스"):
                    state['rsi_bonus'] = True
                    self.daily_state[symbol] = state
                    self._save_daily_state()
                    total_equity = self.last_usd_balance + (self.positions['quantity'] * self.positions['current_price']).sum()
                        
            self.daily_state[symbol] = state

    def close_all_positions(self) -> None:
        self.log("[Graceful Shutdown] 포지션 청산을 진행합니다...")
        self.api.cancel_all_orders()
        
        self.sync_positions()
        active_positions: pd.DataFrame = self.positions[self.positions['quantity'] > 0]
        
        symbols_to_close: List[str] = active_positions['symbol'].tolist()
        quants: List[float] = active_positions['quantity'].tolist()
        prices: List[float] = active_positions['current_price'].tolist()
        
        for sym, qty, prc in zip(symbols_to_close, quants, prices):
            self.log(f"⚠️ [강제 청산] {sym} 매도 주문: {int(qty)}주", send_tg=True)
            self.api.place_order(sym, int(qty), prc, is_buy=False)
            
        self.log("[Graceful Shutdown] 모든 포지션 정리 완료")

    def run_loop(self) -> None:
        self.is_running = True
        self.log("▶️ 봇 자동매매 루프가 시작되었습니다.", send_tg=True)

        if self.symbols and self.prev_close.get(self.symbols[0], 0.0) == 0.0:
            self.log("📊 [시작] 시장 데이터 초기 로딩 중...")
            self.check_trend_and_momentum()
            self.update_exchange_rate()

        while self.is_running:
            try:
                now_et = self.get_eastern_time()
                today_str = now_et.strftime('%Y-%m-%d')
                
                # 1. 날짜 변경 감지 및 상태 초기화 (미국 동부 시간 기준)
                if self.daily_state.get('date') != today_str:
                    self.daily_state = {'date': today_str, 'closing_report_sent': False, 'premarket_recheck_done': False}
                    for sym in self.symbols:
                        self.daily_state[sym] = {'base': False, 't2': False, 't4': False, 't8': False, 'rsi_bonus': False, 'morning_low': 999999.0}
                    self._save_daily_state()
                    self._sync_logged = False
                    
                    if now_et.weekday() < 5:
                        self.check_trend_and_momentum()
                        self.update_exchange_rate()
                        self._send_premarket_briefing()

                # 1-1. 본장 30분 전(ET 09:00) 실시간 가격 기반 SMA200 재검수
                if (now_et.weekday() < 5
                        and now_et.hour >= 9
                        and not self.daily_state.get('premarket_recheck_done', False)):
                    self._recheck_trend_realtime()
                    self.daily_state['premarket_recheck_done'] = True
                    self._save_daily_state()

                # 2. 거래 가능 시간인지 확인 (프리장 04:00 ~ 정규장 마감 16:00)
                if self.is_active_trading_time(now_et):
                    # 시장 데이터 가져오기
                    self.fetch_market_data()
                    self._check_cash_ratio()
                    self._recheck_sma200_intraday()
                    self._check_auto_mode()
                    
                    # 전략 실행 (실시간 감시 및 매수)
                    self.execute_strategy(now_et)
                else:
                    if now_et.weekday() < 5 and now_et.hour >= 16 and not self.daily_state.get('closing_report_sent', False):
                        self._send_closing_report()
                        self.daily_state['closing_report_sent'] = True
                        self._save_daily_state()
                    
                self._send_heartbeat()

                for _ in range(30):
                    if not self.is_running:
                        break
                    time.sleep(1)
            except Exception as e:
                error_msg = f"🔥 [긴급 에러 발생]\n사유: {e}\n위치: 메인 루프 (run_loop)\n상태: 10초 대기 후 루프 재개 (봇 중단 안됨)"
                self.send_error_telegram(error_msg)
                self.log(f"[루프 오류] {e}")
                time.sleep(10)
                
        self.log("⏸️ 봇 자동매매 루프가 중지되었습니다.", send_tg=True)

    def stop_loop(self) -> None:
        if self.is_running:
            self.is_running = False
            self.log("⏸️ 봇 정지 명령이 내려졌습니다.", send_tg=True)

    def get_status(self) -> Dict[str, Any]:
        now_et: datetime = datetime.now(ZoneInfo("America/New_York"))
        return {
            "is_running": self.is_running,
            "usd_balance": self.last_usd_balance,
            "exchange_rate": self.exchange_rate,
            "tot_evlu_pfls": self.tot_evlu_pfls,
            "tot_pchs_amt": self.tot_pchs_amt,
            "tot_stck_evlu": self.tot_stck_evlu,
            "total_eval": self.last_usd_balance + self.tot_stck_evlu,
            "positions": self.positions.to_dict(orient="records") if not self.positions.empty else [],
            "logs": self.logs,
            "slots": self.slot_manager.get_active_slots(),
            "max_slots": self.slot_manager.max_slots,
            "market_open": self.is_regular_market_open(now_et),
            "is_dst": bool(now_et.dst()),
            "et_time": now_et.strftime("%H:%M"),
        }
