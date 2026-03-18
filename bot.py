import os
import sys
import signal
import time
import threading
import copy
import re
from datetime import datetime, date
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional, Set, Tuple
import exchange_calendars as xcals
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
    'GGLL': 'GOOG', 'GOOU': 'GOOG', 'METU': 'META', 'CONL': 'COIN',
    'SQQQ': 'QQQ', 'SOXS': 'SOXX', 'SPXU': 'SPY',
    'TECS': 'XLK', 'FAZ': 'XLF', 'TZA': 'IWM',
    'WEBL': 'DJUSTC', 'BITX': 'BTC',
}

SMART_BUY_INITIAL_PREMIUM: float = 0.001  # +0.1%
SMART_BUY_REPRICE_STEPS: List[Tuple[int, float]] = [
    (8, 0.002),    # 8초: 현재가 기준 +0.2%
    (20, 0.003),   # 20초: 현재가 기준 +0.3%
    (35, 0.0045),  # 35초: 현재가 기준 +0.45%
    (55, 0.006),   # 55초: 현재가 기준 +0.6%
    (90, 0.008),   # 90초: 현재가 기준 +0.8%
]
SMART_BUY_MONITOR_SEC: int = 300
TRAILING_SELL_INITIAL_DISCOUNT: float = 0.005  # -0.5%
TRAILING_SELL_REPRICE_STEPS: List[Tuple[int, float]] = [
    (5, 0.007),   # 5초: 현재가 기준 -0.7%
    (12, 0.010),  # 12초: 현재가 기준 -1.0%
    (25, 0.012),  # 25초: 현재가 기준 -1.2%
]
TRAILING_SELL_MONITOR_SEC: int = 300
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def _is_valid_symbol(symbol: str) -> bool:
    return bool(SYMBOL_PATTERN.fullmatch(str(symbol or "").strip().upper()))


def _parse_env_rate(name: str, default: float = 0.0) -> float:
    try:
        value = float(str(os.getenv(name, default)).strip())
        return max(0.0, value)
    except Exception:
        return max(0.0, default)


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
                    loaded_slots = data.get('slots', [])
                    self.slots = []
                    changed: bool = False
                    for raw in loaded_slots:
                        if not isinstance(raw, dict):
                            continue
                        slot = dict(raw)
                        symbol = str(slot.get('symbol', '')).upper()
                        if not symbol:
                            continue
                        slot['symbol'] = symbol
                        slot['base_asset'] = str(slot.get('base_asset', symbol) or symbol).upper()
                        slot['watch_only'] = bool(slot.get('watch_only', False))
                        slot['anchor_price'] = float(slot.get('anchor_price', 0.0) or 0.0)
                        slot['anchor_at'] = str(slot.get('anchor_at', slot.get('added_at', '')) or '')
                        slot['active'] = bool(slot.get('active', True))
                        # 레버리지 ETF는 본주 매핑을 강제 보정 (과거 저장 데이터 정합성 복구)
                        if symbol in LEVERAGED_ETF_MAP:
                            mapped_base = LEVERAGED_ETF_MAP[symbol]
                            if slot.get('base_asset') != mapped_base:
                                slot['base_asset'] = mapped_base
                                changed = True
                            if not bool(slot.get('is_leveraged', False)):
                                slot['is_leveraged'] = True
                                changed = True
                        self.slots.append(slot)
                    self.max_slots = data.get('max_slots', 6)
                    if changed:
                        self._save()
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

    def get_symbols(self, include_watch_only: bool = True) -> List[str]:
        slots = self.get_active_slots()
        if not include_watch_only:
            slots = [s for s in slots if not bool(s.get('watch_only', False))]
        return [s['symbol'] for s in slots]

    def get_base_assets(self, include_watch_only: bool = True) -> Dict[str, str]:
        slots = self.get_active_slots()
        if not include_watch_only:
            slots = [s for s in slots if not bool(s.get('watch_only', False))]
        return {s['symbol']: s.get('base_asset', s['symbol']) for s in slots}

    def is_full(self) -> bool:
        return len(self.get_active_slots()) >= self.max_slots

    def has_symbol(self, symbol: str) -> bool:
        return symbol.upper() in self.get_symbols()

    def add_slot(
        self,
        symbol: str,
        base_asset: Optional[str],
        is_leveraged: bool,
        watch_only: bool = False,
        anchor_price: float = 0.0,
        anchor_at: str = "",
    ) -> bool:
        if self.is_full() or self.has_symbol(symbol):
            return False
        now_iso = datetime.now().isoformat()
        self.slots.append({
            'symbol': symbol.upper(),
            'base_asset': base_asset or symbol.upper(),
            'added_at': now_iso,
            'anchor_at': anchor_at or now_iso,
            'anchor_price': float(anchor_price or 0.0),
            'watch_only': bool(watch_only),
            'is_leveraged': is_leveraged,
            'active': True,
        })
        self._save()
        return True

    def get_slot(self, symbol: str) -> Optional[Dict[str, Any]]:
        upper = str(symbol or "").upper()
        for slot in self.get_active_slots():
            if str(slot.get("symbol", "")).upper() == upper:
                return slot
        return None

    def update_slot(self, symbol: str, **updates: Any) -> bool:
        upper = str(symbol or "").upper()
        updated = False
        for idx, slot in enumerate(self.slots):
            if str(slot.get("symbol", "")).upper() != upper:
                continue
            next_slot = dict(slot)
            for key, value in updates.items():
                next_slot[key] = value
            self.slots[idx] = next_slot
            updated = True
            break
        if updated:
            self._save()
        return updated

    def remove_slot(self, symbol: str) -> bool:
        upper_sym: str = symbol.upper()
        self.slots = [s for s in self.slots if s['symbol'] != upper_sym]
        self._save()
        return True

    def reorder_slots(self, ordered_symbols: List[str]) -> bool:
        """활성 슬롯 순서를 사용자가 지정한 심볼 순서로 재배열합니다."""
        if not ordered_symbols:
            return False
        normalized_order: List[str] = []
        seen: Set[str] = set()
        for sym in ordered_symbols:
            upper = str(sym).upper()
            if not upper or upper in seen:
                continue
            normalized_order.append(upper)
            seen.add(upper)
        if not normalized_order:
            return False

        active_slots: List[Dict[str, Any]] = self.get_active_slots()
        active_map: Dict[str, Dict[str, Any]] = {s['symbol']: s for s in active_slots if s.get('symbol')}
        if not active_map:
            return False

        reordered_active: List[Dict[str, Any]] = []
        used: Set[str] = set()
        for sym in normalized_order:
            slot = active_map.get(sym)
            if not slot or sym in used:
                continue
            reordered_active.append(slot)
            used.add(sym)
        for slot in active_slots:
            sym = str(slot.get('symbol', '')).upper()
            if sym and sym not in used:
                reordered_active.append(slot)
                used.add(sym)

        inactive_slots: List[Dict[str, Any]] = [s for s in self.slots if not s.get('active', True)]
        self.slots = reordered_active + inactive_slots
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
        self.last_krw_cash: float = 0.0
        self.exchange_rate: float = 1400.0
        self.display_exchange_rate: float = 0.0
        self._display_exchange_rate_ts: float = 0.0
        self._display_exchange_rate_ttl_sec: float = 300.0
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
        self._daily_closes_cache: Dict[str, Dict[str, Any]] = {}
        self._daily_closes_cache_ttl_sec: float = 1800.0
        self._live_snapshot_lock = threading.Lock()
        self._live_snapshot: Dict[str, Any] = {}
        self._last_live_snapshot_ts: float = 0.0
        # 무거운 잔고/포지션 동기화 주기와, 가벼운 스냅샷 발행 주기를 분리
        self._portfolio_sync_interval_sec: float = 5.0
        self._portfolio_sync_interval_idle_sec: float = 30.0
        self._snapshot_publish_interval_sec: float = 1.0
        self._quote_refresh_interval_active_sec: float = 1.0
        self._quote_refresh_interval_idle_sec: float = 3.0
        self._quote_refresh_batch_size: int = 3
        self._last_portfolio_sync_ts: float = 0.0
        self._last_quote_refresh_ts: float = 0.0
        self._quote_rr_index: int = 0
        self._slot_quote_cache: Dict[str, Dict[str, float]] = {}
        self._slot_quote_cache_ttl_sec: float = 12.0
        self._live_snapshot_interval_sec: float = self._snapshot_publish_interval_sec
        self._empty_positions_streak: int = 0
        self._empty_positions_confirm_count: int = 3
        
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
        # API 이상 경고 로그 쓰로틀링 (1분)
        self._api_warn_last_ts: Dict[str, float] = {}
        self._api_warn_interval_sec: float = 60.0

        # 일별 자산 추적
        self.equity_log_file: str = "equity_log.json"

        # 매매 내역 기록
        self.trade_log_file: str = "trade_log.json"
        self.sell_fee_rate: float = _parse_env_rate("SELL_FEE_RATE", 0.0025)
        self.sell_tax_rate: float = _parse_env_rate("SELL_TAX_RATE", 0.0)
        self.sell_cost_rate: float = self.sell_fee_rate + self.sell_tax_rate

        # 예수금 비중 알림 (하루 1번)
        self._cash_alert_40_sent: str = ""
        self._cash_alert_30_sent: str = ""

        self._nyse_cal = xcals.get_calendar("XNYS")

        # 기존 보유 종목 자동 슬롯 등록 (슬롯이 비어있을 때만)
        self._auto_register_holdings()

    @property
    def symbols(self) -> List[str]:
        return self.slot_manager.get_symbols(include_watch_only=False)

    @property
    def base_assets(self) -> Dict[str, str]:
        return self.slot_manager.get_base_assets(include_watch_only=False)

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
        d = now_et.date()
        try:
            return not self._nyse_cal.is_session(pd.Timestamp(d))
        except Exception:
            return False

    def get_early_close_time(self, now_et: datetime) -> Optional[datetime]:
        d = now_et.date()
        try:
            ts = pd.Timestamp(d)
            if not self._nyse_cal.is_session(ts):
                return None
            close_utc = self._nyse_cal.session_close(ts)
            close_et = close_utc.tz_convert("America/New_York")
            if close_et.hour < 16:
                return now_et.replace(hour=close_et.hour, minute=close_et.minute, second=0, microsecond=0)
        except Exception:
            pass
        return None

    def _get_kis_daily_closes(self, symbol: str, min_points: int = 1, force_refresh: bool = False) -> List[float]:
        symbol = symbol.upper().strip()
        now: float = time.time()
        cached: Optional[Dict[str, Any]] = self._daily_closes_cache.get(symbol)
        if (
            cached
            and (not force_refresh)
            and (now - float(cached.get("ts", 0.0))) < self._daily_closes_cache_ttl_sec
            and len(cached.get("closes", [])) >= min_points
        ):
            return list(cached.get("closes", []))

        periods: List[str] = ["2y", "1y"] if min_points >= 200 else ["1y", "2y"]
        closes: List[float] = []
        for period in periods:
            candles: List[Dict[str, Any]] = self.api.get_daily_candles(symbol, period=period)
            closes = [float(c.get("close", 0.0)) for c in candles if float(c.get("close", 0.0)) > 0]
            if len(closes) >= min_points or period == periods[-1]:
                break

        if closes:
            self._daily_closes_cache[symbol] = {"closes": closes, "ts": now}
        return closes

    def _compute_sma200_rsi14(self, closes: List[float]) -> Optional[Tuple[float, float]]:
        if len(closes) < 200:
            return None
        close_ser: pd.Series = pd.Series(closes, dtype=float)
        sma_200: float = float(close_ser.tail(200).mean())
        delta: pd.Series = close_ser.diff()
        gain: pd.Series = delta.where(delta > 0, 0.0).ewm(alpha=1/14, adjust=False).mean()
        loss_raw: pd.Series = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
        loss_safe: pd.Series = loss_raw.replace(0.0, 1e-10)
        rs: pd.Series = gain / loss_safe
        rsi_14: float = float((100 - (100 / (1 + rs))).iloc[-1])
        if pd.isna(rsi_14):
            rsi_14 = 50.0
        return sma_200, rsi_14

    def _get_trend_snapshot_from_kis(self, base_symbol: str, force_refresh: bool = False) -> Optional[Dict[str, float]]:
        closes: List[float] = self._get_kis_daily_closes(base_symbol, min_points=200, force_refresh=force_refresh)
        indicator = self._compute_sma200_rsi14(closes)
        if not indicator:
            return None
        sma_200, rsi_14 = indicator
        current_price: float = self.api.get_current_price(base_symbol)
        if current_price <= 0:
            current_price = closes[-1]
        return {
            "sma_200": sma_200,
            "rsi_14": rsi_14,
            "current_price": current_price,
        }

    def _update_prev_close_from_kis(self, symbol: str, force_refresh: bool = False) -> None:
        closes: List[float] = self._get_kis_daily_closes(symbol, min_points=2, force_refresh=force_refresh)
        if len(closes) >= 2:
            self.prev_close[symbol] = closes[-2]
        elif len(closes) == 1:
            self.prev_close[symbol] = closes[-1]

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
        total_equity: float = self._calc_total_equity()
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
            data: Dict[str, Any] = self.api.get_balance_and_positions(symbols=self.symbols)
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

    def add_symbol(self, symbol: str, buy_percent: float = 0.0, watch_only: bool = False) -> Dict[str, Any]:
        """슬롯에 종목을 추가합니다. watch_only=True면 매수 없이 관찰 슬롯으로만 추가합니다."""
        symbol = symbol.upper().strip()
        if not _is_valid_symbol(symbol):
            return {"success": False, "message": "종목 코드는 영문/숫자/.- 만 허용됩니다. (최대 15자)"}
        now_et: datetime = datetime.now(ZoneInfo("America/New_York"))
        now_kst: datetime = now_et.astimezone(ZoneInfo("Asia/Seoul"))
        is_us_session: bool = self.is_active_trading_time(now_et)
        is_daytime_session: bool = self.is_daytime_market_open(now_kst)
        if not (is_us_session or is_daytime_session):
            return {"success": False, "message": "거래 가능 시간에만 종목을 추가할 수 있습니다. (미국장 ET 04:00~20:00 / 데이장 KST 09:00~16:00)"}
        if self.slot_manager.is_full():
            return {"success": False, "message": f"슬롯이 가득 찼습니다. (최대 {self.slot_manager.max_slots}개)"}
        if self.slot_manager.has_symbol(symbol):
            return {"success": False, "message": f"{symbol}은(는) 이미 추가된 종목입니다."}

        name: str = symbol

        is_leveraged: bool = symbol in LEVERAGED_ETF_MAP
        base_asset: str = LEVERAGED_ETF_MAP.get(symbol, symbol)

        try:
            current_price: float = self.api.get_current_price(symbol)
            if current_price <= 0:
                return {"success": False, "message": f"{symbol} 현재가 조회 실패 (한투 API에서 거래 불가)"}
        except Exception as e:
            return {"success": False, "message": f"{symbol} 거래소 조회 실패: {e}"}

        if watch_only:
            ok = self.slot_manager.add_slot(
                symbol,
                base_asset,
                is_leveraged,
                watch_only=True,
                anchor_price=current_price,
                anchor_at=datetime.now().isoformat(),
            )
            if not ok:
                return {"success": False, "message": f"{symbol} 슬롯 추가 실패"}
            self.log(f"👀 [Watch Only] {symbol} 슬롯 추가 (기준가 ${current_price:.2f})", send_tg=True)
            return {
                "success": True,
                "message": f"{symbol} watch-only 슬롯 추가 완료",
                "symbol": symbol,
                "watch_only": True,
                "anchor_price": current_price,
            }

        already_held: bool = False
        bal_data: Dict[str, Any] = {}
        try:
            bal_data = self.api.get_balance_and_positions(symbols=self.symbols + [symbol])
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
        min_qty_override: bool = False
        buy_price: float = round(current_price * (1.0 + SMART_BUY_INITIAL_PREMIUM), 2)
        if buy_percent > 0:
            try:
                if not bal_data:
                    bal_data = self.api.get_balance_and_positions(symbols=self.symbols + [symbol])
                available_cash: float = float(bal_data.get("usd_balance", 0.0) or 0.0)
                buy_amount: float = available_cash * (buy_percent / 100.0)
                buy_qty = int(buy_amount / buy_price)
                if buy_qty < 1:
                    if available_cash >= buy_price:
                        buy_qty = 1
                        min_qty_override = True
                    else:
                        return {
                            "success": False,
                            "message": (
                                f"{symbol} 매수 금액(${buy_amount:.0f}, 예수금 ${available_cash:,.2f}의 {buy_percent:.1f}%)이 "
                                f"주문가(${buy_price:.2f})보다 적고, 1주 매수 예수금도 부족합니다."
                            ),
                        }
            except Exception as e:
                return {"success": False, "message": f"매수 수량 계산 실패: {e}"}
        prefer_daytime: bool = is_daytime_session and (not is_us_session)
        success: bool = self.api.place_order(symbol, buy_qty, buy_price, is_buy=True, prefer_daytime=prefer_daytime)
        if not success:
            return {"success": False, "message": f"{symbol} {buy_qty}주 매수 주문 실패"}

        pct_label: str = f" ({buy_percent}%)" if buy_percent > 0 else ""
        if min_qty_override:
            pct_label = f" ({buy_percent}%, 최소 1주)"
        self._start_smart_buy_manager(
            symbol=symbol,
            total_qty=buy_qty,
            initial_price=buy_price,
            reason=f"[슬롯 추가] 초기 매수{pct_label}",
            prefer_daytime=prefer_daytime,
        )

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

    def buy_watch_slot(self, symbol: str, buy_percent: float = 1.0) -> Dict[str, Any]:
        symbol = symbol.upper().strip()
        if not _is_valid_symbol(symbol):
            return {"success": False, "message": "종목 코드 형식이 올바르지 않습니다."}
        if buy_percent <= 0:
            return {"success": False, "message": "매수 비율은 0보다 커야 합니다."}
        slot = self.slot_manager.get_slot(symbol)
        if not slot:
            return {"success": False, "message": f"{symbol} 슬롯이 없습니다."}
        if not bool(slot.get("watch_only", False)):
            return {"success": False, "message": f"{symbol}은 watch-only 슬롯이 아닙니다."}

        now_et: datetime = datetime.now(ZoneInfo("America/New_York"))
        now_kst: datetime = now_et.astimezone(ZoneInfo("Asia/Seoul"))
        is_us_session: bool = self.is_active_trading_time(now_et)
        is_daytime_session: bool = self.is_daytime_market_open(now_kst)
        if not (is_us_session or is_daytime_session):
            return {"success": False, "message": "거래 가능 시간에만 매수할 수 있습니다."}

        try:
            current_price: float = self.api.get_current_price(symbol)
        except Exception as e:
            return {"success": False, "message": f"{symbol} 현재가 조회 실패: {e}"}
        if current_price <= 0:
            return {"success": False, "message": f"{symbol} 현재가를 가져올 수 없습니다."}

        buy_price: float = round(current_price * (1.0 + SMART_BUY_INITIAL_PREMIUM), 2)
        try:
            bal_data: Dict[str, Any] = self.api.get_balance_and_positions(symbols=self.symbols + [symbol])
            available_cash: float = float(bal_data.get("usd_balance", 0.0) or 0.0)
            buy_amount: float = available_cash * (buy_percent / 100.0)
            buy_qty: int = int(buy_amount / buy_price)
            min_qty_override: bool = False
            if buy_qty < 1:
                if available_cash >= buy_price:
                    buy_qty = 1
                    min_qty_override = True
                else:
                    return {
                        "success": False,
                        "message": (
                            f"{symbol} 매수 금액(${buy_amount:.0f}, 예수금 ${available_cash:,.2f}의 {buy_percent:.1f}%)이 "
                            f"주문가(${buy_price:.2f})보다 적고, 1주 매수 예수금도 부족합니다."
                        ),
                    }
        except Exception as e:
            return {"success": False, "message": f"매수 수량 계산 실패: {e}"}

        prefer_daytime: bool = is_daytime_session and (not is_us_session)
        success: bool = self.api.place_order(symbol, buy_qty, buy_price, is_buy=True, prefer_daytime=prefer_daytime)
        if not success:
            return {"success": False, "message": f"{symbol} {buy_qty}주 매수 주문 실패"}

        pct_label: str = f"({buy_percent}%)"
        if min_qty_override:
            pct_label = f"({buy_percent}%, 최소 1주)"
        self._start_smart_buy_manager(
            symbol=symbol,
            total_qty=buy_qty,
            initial_price=buy_price,
            reason=f"[Watch Only] 매수 전환 {pct_label}",
            prefer_daytime=prefer_daytime,
        )
        self.slot_manager.update_slot(symbol, watch_only=False)
        self.is_uptrend[symbol] = False
        self.is_rsi_oversold[symbol] = False
        self.prev_close[symbol] = 0.0
        self.hwm[symbol] = 0.0
        self._save_hwm()
        self._rebuild_positions_df()

        est_amount: float = buy_qty * buy_price
        self.log(f"✅ [Watch Only 매수] {symbol} {buy_qty}주 {pct_label} ≈ ${est_amount:,.0f}", send_tg=True)
        self._log_trade(symbol, "매수", buy_qty, buy_price, est_amount, f"[Watch Only] 매수 전환 {pct_label}")
        return {
            "success": True,
            "message": f"{symbol} watch-only 매수 완료 ({buy_qty}주)",
            "symbol": symbol,
            "quantity": buy_qty,
            "price": buy_price,
            "est_amount": est_amount,
        }

    def remove_symbol(self, symbol: str, sell_all: bool = True) -> Dict[str, Any]:
        """슬롯에서 종목을 제거합니다. sell_all=True이면 전량 매도 후 제거."""
        symbol = symbol.upper().strip()
        if not _is_valid_symbol(symbol):
            return {"success": False, "message": "종목 코드 형식이 올바르지 않습니다."}
        if not self.slot_manager.has_symbol(symbol):
            return {"success": False, "message": f"{symbol}은(는) 슬롯에 없습니다."}

        if sell_all:
            try:
                data: Dict[str, Any] = self.api.get_balance_and_positions(symbols=self.symbols)
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
                    pos_avg: float = position.get("avg_price", 0.0)
                    now_kst: datetime = self.get_korean_time()
                    prefer_daytime: bool = self.is_daytime_market_open(now_kst)
                    order_ok: bool = self.api.place_order(symbol, qty, sell_price, is_buy=False, prefer_daytime=prefer_daytime)
                    if order_ok:
                        self._log_trade(symbol, "매도", qty, sell_price, qty * sell_price, "[슬롯 제거] 전량 매도", avg_price=pos_avg)
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
            snapshot = self._get_trend_snapshot_from_kis(base_sym, force_refresh=True)
            if not snapshot:
                self.is_uptrend[symbol] = False
                self.is_rsi_oversold[symbol] = False
                return
            self.is_uptrend[symbol] = snapshot["current_price"] > snapshot["sma_200"]
            self.is_rsi_oversold[symbol] = snapshot["rsi_14"] < 30.0
        except Exception:
            self.is_uptrend[symbol] = False
            self.is_rsi_oversold[symbol] = False

        try:
            self._update_prev_close_from_kis(symbol, force_refresh=True)
        except Exception:
            pass

    def search_ticker(self, symbol: str) -> Dict[str, Any]:
        """티커를 검색하고 종목 정보를 반환합니다."""
        symbol = symbol.upper().strip()
        if not _is_valid_symbol(symbol):
            return {"found": False, "message": "종목 코드 형식이 올바르지 않습니다."}
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

    def get_display_exchange_rate(self, force_refresh: bool = False) -> float:
        now_ts: float = time.time()
        if (
            (not force_refresh)
            and self.display_exchange_rate > 0
            and (now_ts - self._display_exchange_rate_ts) < self._display_exchange_rate_ttl_sec
        ):
            return self.display_exchange_rate

        market_rate: float = 0.0
        try:
            ticker = yf.Ticker("KRW=X")
            try:
                # 앱 환산값에 더 근접하도록 bid(호가) 우선 사용
                info = ticker.info or {}
                bid = float(info.get("bid", 0.0) or 0.0)
                ask = float(info.get("ask", 0.0) or 0.0)
                regular_market = float(info.get("regularMarketPrice", 0.0) or 0.0)
                prev_close = float(info.get("previousClose", 0.0) or 0.0)

                if bid > 0:
                    market_rate = bid
                elif ask > 0:
                    market_rate = ask
                elif regular_market > 0:
                    market_rate = regular_market
                elif prev_close > 0:
                    market_rate = prev_close
            except Exception:
                market_rate = 0.0

            if market_rate <= 0:
                fast_info = getattr(ticker, "fast_info", None)
                if fast_info:
                    market_rate = float(
                        fast_info.get("previousClose")
                        or fast_info.get("lastPrice")
                        or fast_info.get("last_price")
                        or 0.0
                    )

            if market_rate <= 0:
                daily = ticker.history(period="5d")
                if not daily.empty:
                    market_rate = float(daily["Close"].dropna().iloc[-1])
        except Exception:
            market_rate = 0.0

        if market_rate > 0:
            self.display_exchange_rate = market_rate
            self._display_exchange_rate_ts = now_ts
            return market_rate

        if self.exchange_rate > 0:
            return self.exchange_rate
        return self.display_exchange_rate if self.display_exchange_rate > 0 else 1400.0
            
    def check_trend_and_momentum(self) -> None:
        """Strategy E: 기초자산 SMA200 필터 + RSI<30 과매도 감지 + 레버리지 ETF 전일종가 저장"""
        self.log("📊 [추세 판단] 기초 자산 200일 SMA 및 14일 RSI 확인 중...")
        for etf_sym, base_sym in self.base_assets.items():
            try:
                snapshot = self._get_trend_snapshot_from_kis(base_sym, force_refresh=True)
                if not snapshot:
                    self.log(f"⚠️ [{base_sym}] 데이터 부족. (매수 불가 처리)")
                    self.is_uptrend[etf_sym] = False
                    self.is_rsi_oversold[etf_sym] = False
                    continue
                sma_200: float = snapshot["sma_200"]
                current_price: float = snapshot["current_price"]
                current_rsi: float = snapshot["rsi_14"]
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
                self._update_prev_close_from_kis(etf_sym, force_refresh=True)
                if self.prev_close.get(etf_sym, 0.0) > 0:
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
                snapshot = self._get_trend_snapshot_from_kis(base_sym, force_refresh=False)
                if not snapshot:
                    continue
                sma_200: float = snapshot["sma_200"]
                current_price: float = snapshot["current_price"]
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

    def get_korean_time(self) -> datetime:
        return datetime.now(ZoneInfo("Asia/Seoul"))

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
        after_market_close = now_et.replace(hour=20, minute=0, second=0, microsecond=0)

        return pre_market_open <= now_et < after_market_close

    def is_daytime_market_open(self, now_kst: Optional[datetime] = None) -> bool:
        if now_kst is None:
            now_kst = self.get_korean_time()
        if now_kst.weekday() >= 5:
            return False

        now_et: datetime = now_kst.astimezone(ZoneInfo("America/New_York"))
        if self.is_us_market_holiday(now_et):
            return False

        daytime_open: datetime = now_kst.replace(hour=9, minute=0, second=0, microsecond=0)
        daytime_close: datetime = now_kst.replace(hour=16, minute=0, second=0, microsecond=0)
        return daytime_open <= now_kst < daytime_close

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

    def _log_api_warning_throttled(self, key: str, message: str) -> None:
        now: float = time.time()
        last_ts: float = self._api_warn_last_ts.get(key, 0.0)
        if now - last_ts < self._api_warn_interval_sec:
            return
        self._api_warn_last_ts[key] = now
        self.log(message, send_tg=False)

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

    def _log_trade(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        amount: float,
        reason: str,
        avg_price: float = 0.0,
        status: str = "filled",
        ordered_qty: int = 0,
    ) -> None:
        """매매 내역을 trade_log.json에 기록합니다."""
        try:
            log: List[Dict[str, Any]] = []
            if os.path.exists(self.trade_log_file):
                with open(self.trade_log_file, 'r', encoding='utf-8') as f:
                    log = json.load(f)

            entry: Dict[str, Any] = {
                "timestamp": datetime.now(ZoneInfo("Asia/Seoul")).strftime('%Y-%m-%d %H:%M:%S'),
                "timestamp_et": self.get_eastern_time().strftime('%Y-%m-%d %H:%M:%S'),
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": round(price, 2),
                "amount": round(amount, 2),
                "reason": reason,
                "balance_after": round(self.last_usd_balance, 2),
                "status": status,
            }
            if ordered_qty > 0:
                entry["ordered_qty"] = int(ordered_qty)

            if side == "매도" and avg_price > 0:
                entry["avg_price"] = round(avg_price, 2)
                entry["sell_fee_rate"] = round(self.sell_fee_rate, 6)
                entry["sell_tax_rate"] = round(self.sell_tax_rate, 6)
                entry["sell_cost_rate"] = round(self.sell_cost_rate, 6)
                sell_cost: float = qty * price * self.sell_cost_rate
                entry["sell_cost"] = round(sell_cost, 2)
                if status == "filled":
                    pnl: float = (price - avg_price) * qty - sell_cost
                    pnl_pct: float = (pnl / (avg_price * qty) * 100) if avg_price > 0 and qty > 0 else 0.0
                    entry["pnl"] = round(pnl, 2)
                    entry["pnl_pct"] = round(pnl_pct, 2)

            log.append(entry)

            if len(log) > 1000:
                log = log[-1000:]

            with open(self.trade_log_file, 'w', encoding='utf-8') as f:
                json.dump(log, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[매매기록 저장 오류] {e}")

    def finalize_pending_sell_trade(
        self,
        symbol: str,
        ordered_qty: int,
        filled_qty: int,
        fill_price: float,
        completed: bool,
    ) -> bool:
        """최근 pending 매도 로그를 체결 결과로 확정 반영합니다."""
        try:
            if not os.path.exists(self.trade_log_file):
                return False
            with open(self.trade_log_file, "r", encoding="utf-8") as f:
                log: List[Dict[str, Any]] = json.load(f)
            if not isinstance(log, list) or not log:
                return False

            target_idx: int = -1
            for i in range(len(log) - 1, -1, -1):
                entry: Dict[str, Any] = log[i]
                if str(entry.get("symbol", "")).upper() != symbol.upper():
                    continue
                if entry.get("side") != "매도":
                    continue
                if str(entry.get("status", "")).lower() not in ("pending", "partially_filled"):
                    continue
                target_idx = i
                break
            if target_idx < 0:
                return False

            entry = log[target_idx]
            entry_ordered: int = int(float(entry.get("ordered_qty", entry.get("qty", ordered_qty) or 0)))
            if entry_ordered <= 0:
                entry_ordered = max(0, int(ordered_qty))

            done_qty: int = max(0, min(entry_ordered, int(filled_qty)))
            now_kst: str = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
            entry["updated_at"] = now_kst
            entry["filled_qty"] = done_qty
            entry["ordered_qty"] = entry_ordered

            if done_qty <= 0:
                entry["status"] = "unfilled"
                entry["amount"] = 0.0
                entry["qty"] = 0
                entry["pnl"] = 0.0
                entry["pnl_pct"] = 0.0
                entry["sell_cost"] = 0.0
            else:
                price: float = round(max(0.0, float(fill_price)), 2)
                if price <= 0:
                    price = float(entry.get("price", 0.0) or 0.0)
                amount: float = done_qty * price
                sell_cost: float = amount * self.sell_cost_rate
                avg_price: float = float(entry.get("avg_price", 0.0) or 0.0)
                pnl: float = (price - avg_price) * done_qty - sell_cost if avg_price > 0 else 0.0
                pnl_pct: float = (pnl / (avg_price * done_qty) * 100) if avg_price > 0 and done_qty > 0 else 0.0
                entry["status"] = "filled" if completed and done_qty >= entry_ordered else "partially_filled"
                entry["qty"] = done_qty
                entry["price"] = round(price, 2)
                entry["amount"] = round(amount, 2)
                entry["sell_fee_rate"] = round(self.sell_fee_rate, 6)
                entry["sell_tax_rate"] = round(self.sell_tax_rate, 6)
                entry["sell_cost_rate"] = round(self.sell_cost_rate, 6)
                entry["sell_cost"] = round(sell_cost, 2)
                entry["pnl"] = round(pnl, 2)
                entry["pnl_pct"] = round(pnl_pct, 2)
                if entry["status"] == "partially_filled":
                    entry["remaining_qty"] = max(0, entry_ordered - done_qty)
                    reason: str = str(entry.get("reason", "")).strip()
                    if "[부분체결]" not in reason:
                        entry["reason"] = f"{reason} [부분체결]".strip()

            with open(self.trade_log_file, "w", encoding="utf-8") as f:
                json.dump(log, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"[매도 체결 반영 오류] {e}")
            return False

    def mark_trade_cancelled(self, symbol: str, cancelled_qty: int, order_no: str = "") -> bool:
        """최근 매수 주문 로그를 매수취소로 정정합니다."""
        if cancelled_qty <= 0:
            return False
        try:
            if not os.path.exists(self.trade_log_file):
                return False
            with open(self.trade_log_file, 'r', encoding='utf-8') as f:
                log: List[Dict[str, Any]] = json.load(f)
            if not isinstance(log, list) or not log:
                return False

            target_idx: int = -1
            for i in range(len(log) - 1, -1, -1):
                entry: Dict[str, Any] = log[i]
                if entry.get("symbol") != symbol:
                    continue
                if entry.get("side") != "매수":
                    continue
                if entry.get("status") in ("cancelled", "partially_cancelled"):
                    continue
                target_idx = i
                break

            if target_idx < 0:
                return False

            entry = log[target_idx]
            entry_qty: int = int(float(entry.get("qty", 0)))
            if entry_qty <= 0:
                return False

            now_kst: str = datetime.now(ZoneInfo("Asia/Seoul")).strftime('%Y-%m-%d %H:%M:%S')
            is_full_cancel: bool = cancelled_qty >= entry_qty
            entry["status"] = "cancelled" if is_full_cancel else "partially_cancelled"
            entry["cancelled_qty"] = int(min(cancelled_qty, entry_qty))
            entry["cancelled_at"] = now_kst
            if order_no:
                entry["cancel_order_no"] = order_no
            base_reason: str = str(entry.get("reason", "")).strip()
            if "취소" not in base_reason:
                entry["reason"] = f"{base_reason} [주문취소]".strip()
            if is_full_cancel:
                entry["side"] = "매수취소"
                entry["amount"] = 0.0

            with open(self.trade_log_file, 'w', encoding='utf-8') as f:
                json.dump(log, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"[매매취소 반영 오류] {e}")
            return False

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
                snapshot = self._get_trend_snapshot_from_kis(base_sym, force_refresh=True)
                if not snapshot:
                    self.log(f"⚠️ [{base_sym}] 데이터 부족. (매수 불가 처리)")
                    self.is_uptrend[etf_sym] = False
                    continue

                sma_200: float = snapshot["sma_200"]
                realtime_price: float = snapshot["current_price"]
                current_rsi: float = snapshot["rsi_14"]
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

    def _build_live_snapshot(self) -> Dict[str, Any]:
        now_ts: float = time.time()
        active_slots = self.slot_manager.get_active_slots()
        slot_map: Dict[str, Dict[str, Any]] = {slot.get("symbol"): slot for slot in active_slots}
        current_symbols: List[str] = [str(slot.get("symbol", "")).upper() for slot in active_slots if slot.get("symbol")]
        row_map: Dict[str, Dict[str, Any]] = {}
        if not self.positions.empty:
            for _, row in self.positions.iterrows():
                sym = str(row.get("symbol", ""))
                if not sym:
                    continue
                row_map[sym] = {
                    "quantity": float(row.get("quantity", 0.0) or 0.0),
                    "avg_price": float(row.get("avg_price", 0.0) or 0.0),
                    "current_price": float(row.get("current_price", 0.0) or 0.0),
                    "return_rate": float(row.get("return_rate", 0.0) or 0.0),
                }

        positions: List[Dict[str, Any]] = []
        calc_tot_stck: float = 0.0
        calc_tot_pchs: float = 0.0
        calc_tot_pfls: float = 0.0
        for sym in current_symbols:
            slot_info = slot_map.get(sym, {})
            row = row_map.get(sym, {})
            qty: float = float(row.get("quantity", 0.0))
            avg_price: float = float(row.get("avg_price", 0.0))
            current_price: float = float(row.get("current_price", 0.0))
            cached_price: float = self._get_slot_quote_cache(sym, now_ts=now_ts)
            if cached_price > 0:
                current_price = cached_price
            evlu_amt: float = max(qty * current_price, 0.0)
            pchs_amt: float = max(qty * avg_price, 0.0)
            evlu_pfls: float = evlu_amt - pchs_amt
            return_rate: float = float(row.get("return_rate", 0.0))
            if avg_price > 0 and current_price > 0:
                return_rate = ((current_price - avg_price) / avg_price) * 100.0

            positions.append(
                {
                    "symbol": sym,
                    "quantity": qty,
                    "avg_price": avg_price,
                    "current_price": current_price,
                    "evlu_amt": evlu_amt,
                    "evlu_pfls": evlu_pfls,
                    "return_rate": return_rate,
                    "pchs_amt": pchs_amt,
                    "is_leveraged": slot_info.get("is_leveraged", False),
                    "base_asset": slot_info.get("base_asset", sym),
                    "watch_only": bool(slot_info.get("watch_only", False)),
                    "anchor_price": float(slot_info.get("anchor_price", 0.0) or 0.0),
                    "anchor_at": str(slot_info.get("anchor_at", "")),
                    "base_price": 0.0,
                }
            )
            calc_tot_stck += evlu_amt
            calc_tot_pchs += pchs_amt
            calc_tot_pfls += evlu_pfls

        tot_stck_evlu: float = self.tot_stck_evlu if self.tot_stck_evlu > 0 else calc_tot_stck
        tot_pchs_amt: float = self.tot_pchs_amt if self.tot_pchs_amt > 0 else calc_tot_pchs
        tot_evlu_pfls: float = self.tot_evlu_pfls if self.tot_stck_evlu > 0 else calc_tot_pfls

        return {
            "ts": now_ts,
            "portfolio_ts": float(self._last_portfolio_sync_ts or time.time()),
            "usd_balance": float(self.last_usd_balance),
            "krw_balance": float(self.last_krw_balance),
            "krw_cash": float(self.last_krw_cash),
            "exchange_rate": float(self.exchange_rate),
            "tot_stck_evlu": float(tot_stck_evlu),
            "tot_pchs_amt": float(tot_pchs_amt),
            "tot_evlu_pfls": float(tot_evlu_pfls),
            "positions": positions,
        }

    def _publish_live_snapshot(self) -> None:
        snapshot: Dict[str, Any] = self._build_live_snapshot()
        with self._live_snapshot_lock:
            self._live_snapshot = snapshot
            self._last_live_snapshot_ts = float(snapshot.get("ts", time.time()))

    def _set_slot_quote_cache(self, symbol: str, price: float, now_ts: Optional[float] = None) -> None:
        sym: str = str(symbol or "").upper().strip()
        if not sym or price <= 0:
            return
        ts: float = float(now_ts if now_ts is not None else time.time())
        self._slot_quote_cache[sym] = {"price": float(price), "ts": ts}
        if len(self._slot_quote_cache) > 128:
            oldest: str = min(self._slot_quote_cache.keys(), key=lambda s: self._slot_quote_cache[s].get("ts", 0.0))
            self._slot_quote_cache.pop(oldest, None)

    def _get_slot_quote_cache(self, symbol: str, now_ts: Optional[float] = None) -> float:
        sym: str = str(symbol or "").upper().strip()
        if not sym:
            return 0.0
        cached = self._slot_quote_cache.get(sym)
        if not cached:
            return 0.0
        now_val: float = float(now_ts if now_ts is not None else time.time())
        ts: float = float(cached.get("ts", 0.0) or 0.0)
        if (now_val - ts) > self._slot_quote_cache_ttl_sec:
            return 0.0
        return float(cached.get("price", 0.0) or 0.0)

    def _refresh_slot_quotes(self, prefer_daytime: bool = False) -> None:
        symbols: List[str] = [str(sym).upper() for sym in self.slot_manager.get_symbols(include_watch_only=True)]
        if not symbols:
            return

        total: int = len(symbols)
        batch: int = min(max(1, self._quote_refresh_batch_size), total)
        start: int = self._quote_rr_index % total
        targets: List[str] = [symbols[(start + i) % total] for i in range(batch)]
        self._quote_rr_index = (start + batch) % total

        updated_any: bool = False
        now_ts: float = time.time()
        for sym in targets:
            try:
                price: float = float(self.api.get_current_price(sym, prefer_daytime=prefer_daytime) or 0.0)
            except Exception:
                continue
            if price <= 0:
                continue
            self._set_slot_quote_cache(sym, price, now_ts=now_ts)
            if not self.positions.empty:
                mask: pd.Series = self.positions["symbol"] == sym
                if mask.any():
                    self.positions.loc[mask, "current_price"] = price
                    avg_price: float = float(self.positions.loc[mask, "avg_price"].values[0] or 0.0)
                    if avg_price > 0:
                        self.positions.loc[mask, "return_rate"] = ((price - avg_price) / avg_price) * 100.0
            updated_any = True

        if updated_any:
            if not self.positions.empty:
                self.positions.fillna(0.0, inplace=True)
            self._publish_live_snapshot()

    def get_live_snapshot(self, max_age_sec: float = 12.0) -> Optional[Dict[str, Any]]:
        with self._live_snapshot_lock:
            snapshot = self._live_snapshot
            if not snapshot:
                return None
            ts = float(snapshot.get("ts", 0.0))
            if (time.time() - ts) > max_age_sec:
                return None
            return copy.deepcopy(snapshot)

    def refresh_live_snapshot(self) -> None:
        self.sync_positions()
        self._publish_live_snapshot()

    def sync_positions(self) -> None:
        item_cd: str = self.symbols[0] if self.symbols else "AAPL"
        data: Dict[str, Any] = self.api.get_balance_and_positions(item_cd=item_cd, symbols=self.symbols)
        self._last_portfolio_sync_ts = time.time()
        incoming_positions: List[Dict[str, Any]] = list(data.get("positions", []))
        had_prev_holdings: bool = (
            (not self.positions.empty)
            and float(self.positions["quantity"].sum()) > 0
        )
        new_usd: float = data["usd_balance"]
        if new_usd <= 0 and self.last_usd_balance > 100:
            self._log_api_warning_throttled(
                "usd_zero_balance",
                f"⚠️ [API 이상] USD 예수금 $0 반환 (기존 ${self.last_usd_balance:,.2f} 유지)",
            )
        else:
            self.last_usd_balance = new_usd

        api_exrt: float = data.get("exchange_rate", 0.0)
        if api_exrt > 0:
            self.exchange_rate = api_exrt
            if self.display_exchange_rate <= 0:
                self.display_exchange_rate = api_exrt

        api_krw: float = data.get("krw_balance", 0.0)
        if api_krw > 0:
            self.last_krw_balance = api_krw
        else:
            self.last_krw_balance = self.last_usd_balance * self.exchange_rate
        api_krw_cash: float = data.get("krw_cash", 0.0)
        if api_krw_cash >= 0:
            self.last_krw_cash = api_krw_cash

        new_evlu_pfls: float = data.get("tot_evlu_pfls", 0.0)
        new_pchs_amt: float = data.get("tot_pchs_amt", 0.0)
        new_stck_evlu: float = data.get("tot_stck_evlu", 0.0)
        if new_stck_evlu <= 0 and self.tot_stck_evlu > 100 and len(self.symbols) > 0:
            self._log_api_warning_throttled(
                "stock_eval_zero",
                f"⚠️ [API 이상] 주식 평가액 $0 반환 (기존 ${self.tot_stck_evlu:,.2f} 유지)",
            )
        else:
            self.tot_evlu_pfls = new_evlu_pfls
            self.tot_pchs_amt = new_pchs_amt
            self.tot_stck_evlu = new_stck_evlu

        # API 순간 이상으로 빈 포지션이 내려오는 경우(실보유 있음)에는 몇 회 확인 전까지 기존 상태를 유지
        suspicious_empty_positions: bool = (
            had_prev_holdings
            and len(incoming_positions) == 0
            and self.tot_stck_evlu > 100
        )
        if suspicious_empty_positions:
            self._empty_positions_streak += 1
            if self._empty_positions_streak < self._empty_positions_confirm_count:
                self._log_api_warning_throttled(
                    "positions_empty_transient",
                    (
                        "⚠️ [API 이상] 포지션 빈 응답 감지 "
                        f"(보유값 유지, {self._empty_positions_streak}/{self._empty_positions_confirm_count})"
                    ),
                )
                self._publish_live_snapshot()
                return
        else:
            self._empty_positions_streak = 0

        current_symbols: List[str] = self.symbols
        if self.positions.empty or set(self.positions['symbol'].tolist()) != set(current_symbols):
            self._rebuild_positions_df()

        if not self.positions.empty:
            self.positions['quantity'] = 0.0
            self.positions['avg_price'] = 0.0
            self.positions['current_price'] = 0.0

        for pos in incoming_positions:
            symbol: str = pos["symbol"]
            if symbol in current_symbols and not self.positions.empty:
                idx: pd.Series = self.positions['symbol'] == symbol
                if idx.any():
                    self.positions.loc[idx, 'quantity'] = pos["quantity"]
                    self.positions.loc[idx, 'avg_price'] = pos["avg_price"]
                    cur_price: float = pos.get("current_price", 0.0)
                    if cur_price > 0:
                        self.positions.loc[idx, 'current_price'] = cur_price
                        self._set_slot_quote_cache(symbol, cur_price)
                    api_return: float = pos.get("return_rate", 0.0)
                    if api_return != 0.0:
                        self.positions.loc[idx, 'return_rate'] = api_return

        self._auto_remove_empty_slots()

    def _auto_remove_empty_slots(self) -> None:
        """보유 수량이 0인 슬롯을 자동 제거 (추가 후 10분 경과 조건)"""
        now: datetime = datetime.now()
        api_symbols: Set[str] = set()
        if not self.positions.empty:
            held: pd.DataFrame = self.positions[self.positions['quantity'] > 0]
            api_symbols = set(held['symbol'].tolist())

        to_remove: List[str] = []
        for slot in self.slot_manager.get_active_slots():
            sym: str = slot['symbol']
            if bool(slot.get('watch_only', False)):
                continue
            if sym in api_symbols:
                continue
            added_at_str: str = slot.get('added_at', '')
            if not added_at_str:
                continue
            try:
                added_at: datetime = datetime.fromisoformat(added_at_str)
            except (ValueError, TypeError):
                continue
            elapsed_min: float = (now - added_at).total_seconds() / 60
            if elapsed_min >= 10:
                to_remove.append(sym)

        # 실제 삭제 전 전체 계좌를 재검증해 API 일시 이상으로 인한 오삭제를 방지
        if to_remove:
            try:
                verify_item_cd: str = self.symbols[0] if self.symbols else "AAPL"
                verify_data: Dict[str, Any] = self.api.get_balance_and_positions(
                    item_cd=verify_item_cd,
                    symbols=["__ALL__"],
                )
                account_held_symbols: Set[str] = {
                    str(pos.get("symbol", "")).upper()
                    for pos in verify_data.get("positions", [])
                    if float(pos.get("quantity", 0.0) or 0.0) > 0
                }
                if (not account_held_symbols) and self.tot_stck_evlu > 100:
                    self.log("⚠️ [자동 정리 보류] 전체계좌 재확인 결과가 비정상(보유 0)으로 보여 삭제를 건너뜁니다.", send_tg=False)
                    return
                filtered_remove: List[str] = []
                for sym in to_remove:
                    if sym in account_held_symbols:
                        self.log(f"🛡️ [자동 정리 방지] {sym} 전체계좌 조회에서 보유 확인", send_tg=False)
                        continue
                    filtered_remove.append(sym)
                to_remove = filtered_remove
            except Exception as verify_error:
                self.log(f"⚠️ [자동 정리 보류] 전체계좌 재확인 실패: {verify_error}", send_tg=False)
                return

        for sym in to_remove:
            self.slot_manager.remove_slot(sym)
            self.is_uptrend.pop(sym, None)
            self.is_rsi_oversold.pop(sym, None)
            self.prev_close.pop(sym, None)
            self.hwm.pop(sym, None)
            self.daily_state.pop(sym, None)
            self.log(f"🗑️ [자동 정리] {sym} 보유 0주 → 슬롯 자동 제거", send_tg=True)

        if to_remove:
            self._save_hwm()
            self._save_daily_state()
            self._rebuild_positions_df()

    def fetch_market_data(self) -> None:
        self.sync_positions()
        if self.positions.empty:
            self._publish_live_snapshot()
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
            ) / self.positions.loc[need_calc, 'avg_price'] * 100.0

        # legacy 보정: 과거 경로에서 return_rate가 비율(0.05)로 저장된 값을 퍼센트(5.0)로 정규화
        valid_price_mask: pd.Series = (self.positions['avg_price'] > 0) & (self.positions['current_price'] > 0)
        if valid_price_mask.any():
            calc_pct: pd.Series = (
                (self.positions.loc[valid_price_mask, 'current_price'] - self.positions.loc[valid_price_mask, 'avg_price'])
                / self.positions.loc[valid_price_mask, 'avg_price']
                * 100.0
            )
            cur_rate: pd.Series = self.positions.loc[valid_price_mask, 'return_rate']
            legacy_ratio_mask: pd.Series = (
                (cur_rate != 0.0)
                & ((cur_rate * 100.0 - calc_pct).abs() < (cur_rate - calc_pct).abs())
            )
            if legacy_ratio_mask.any():
                idx_to_fix = cur_rate.index[legacy_ratio_mask]
                self.positions.loc[idx_to_fix, 'return_rate'] = self.positions.loc[idx_to_fix, 'return_rate'] * 100.0

        self.positions.fillna(0.0, inplace=True)
        if not self._sync_logged:
            self.log(f"데이터 동기화 완료 | 예수금: ${self.last_usd_balance:.2f} | 슬롯: {len(self.symbols)}개", print_stdout=False)
            self._sync_logged = True
        self._publish_live_snapshot()
        
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
        """Strategy E: HWM 하락 임계치 도달 시 부분 매도(추격형 지정가)"""
        hwm_price: float = self.hwm.get(symbol, 0.0)
        if hwm_price <= 0:
            return
            
        drawdown: float = (current_price - hwm_price) / hwm_price
        
        if drawdown <= self.trailing_stop_threshold:
            sell_qty: int = max(1, int(qty * self.trailing_sell_pct))
            sell_price: float = round(current_price * (1.0 - TRAILING_SELL_INITIAL_DISCOUNT), 2)
            now_et: datetime = self.get_eastern_time()
            now_kst: datetime = self.get_korean_time()
            prefer_daytime: bool = self.is_daytime_market_open(now_kst) and (not self.is_active_trading_time(now_et))
            self.log(f"🚨 [트레일링 스탑 발동] {symbol} 최고점 대비 {sell_qty}주 부분 매도!", send_tg=False)
            success: bool = self.api.place_order(
                symbol,
                sell_qty,
                sell_price,
                is_buy=False,
                prefer_daytime=prefer_daytime,
            )
            if success:
                self.hwm[symbol] = current_price
                self._save_hwm()
                
                sold_amount: float = sell_qty * sell_price
                ts_avg: float = float(self.positions.loc[self.positions['symbol'] == symbol, 'avg_price'].values[0]) if not self.positions.empty and (self.positions['symbol'] == symbol).any() else 0.0
                self._log_trade(
                    symbol,
                    "매도",
                    sell_qty,
                    sell_price,
                    sold_amount,
                    f"[{self._get_mode_label()}] 트레일링 스탑 ({drawdown*100:.1f}%)",
                    avg_price=ts_avg,
                    status="pending",
                    ordered_qty=sell_qty,
                )
                
                msg: str = f"🛡️ [트레일링 스탑 주문 접수]\n"
                msg += f"종목: {symbol}\n"
                msg += f"주문가/수량: ${sell_price:.2f} x {sell_qty}주 (보유의 {self.trailing_sell_pct*100:.0f}%)\n"
                msg += "방식: 추격형 지정가 (시작 -0.5%, 최대 -1.2%)\n"
                msg += f"예상 금액: ${sold_amount:,.2f} (약 {sold_amount * self.exchange_rate:,.0f}원)\n"
                msg += f"사유: 최고점(${hwm_price:.2f}) 대비 {drawdown*100:.2f}% 하락 (트레일링 스탑)\n"
                msg += f"잔여 보유(예상): {int(qty - sell_qty)}주"
                self.send_telegram_message(msg)
                self._start_trailing_sell_manager(
                    symbol=symbol,
                    total_qty=sell_qty,
                    initial_price=sell_price,
                    prefer_daytime=prefer_daytime,
                )
        
    def _get_mode_label(self) -> str:
        if self.strategy_mode == "auto":
            return f"자동({'공격' if self.auto_active_mode == 'aggressive' else '방어'})"
        return "공격" if self.strategy_mode == "aggressive" else "방어"

    def _pick_pending_order(self, orders: List[Dict[str, Any]], symbol: str, side: str) -> Optional[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for order in orders:
            if str(order.get("symbol", "")).upper() != symbol.upper():
                continue
            if str(order.get("side", "")) != side:
                continue
            try:
                rem_qty: int = int(float(order.get("remaining_qty", 0)))
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

    def _start_smart_buy_manager(
        self,
        symbol: str,
        total_qty: int,
        initial_price: float,
        reason: str,
        prefer_daytime: bool,
    ) -> None:
        def _worker() -> None:
            start_ts: float = time.time()
            last_price: float = initial_price
            last_remaining: int = total_qty

            try:
                for after_sec, premium in SMART_BUY_REPRICE_STEPS:
                    sleep_sec: float = after_sec - (time.time() - start_ts)
                    if sleep_sec > 0:
                        time.sleep(sleep_sec)

                    orders: List[Dict[str, Any]] = self.api.get_pending_orders(symbols=["__ALL__"])
                    pending: Optional[Dict[str, Any]] = self._pick_pending_order(orders, symbol, "매수")
                    if not pending:
                        return

                    try:
                        remaining_qty: int = int(float(pending.get("remaining_qty", 0)))
                    except Exception:
                        remaining_qty = 0
                    if remaining_qty <= 0:
                        continue
                    last_remaining = remaining_qty

                    order_no: str = str(pending.get("order_no", "")).strip()
                    if not order_no:
                        continue

                    canceled: bool = self.api.cancel_order(
                        order_no=order_no,
                        symbol=symbol,
                        remaining_qty=remaining_qty,
                        prefer_daytime=prefer_daytime,
                    )
                    if not canceled:
                        continue

                    time.sleep(0.25)
                    current_price: float = float(self.api.get_current_price(symbol, prefer_daytime=prefer_daytime) or 0.0)
                    if current_price <= 0:
                        current_price = last_price
                    new_price: float = round(current_price * (1.0 + premium), 2)
                    if new_price <= 0:
                        continue

                    reordered: bool = self.api.place_order(
                        symbol=symbol,
                        quantity=remaining_qty,
                        price=new_price,
                        is_buy=True,
                        prefer_daytime=prefer_daytime,
                    )
                    if reordered:
                        last_price = new_price
                        self.log(
                            f"🔁 [매수 재호가] {symbol} 잔량 {remaining_qty}주 @ ${new_price:.2f} "
                            f"(현재가 기준 +{premium*100:.2f}%)",
                            send_tg=False,
                        )
                    else:
                        self.send_telegram_message(
                            f"⚠️ [매수 재호가 실패]\n종목: {symbol}\n잔량: {remaining_qty}주\n"
                            f"사유: {reason}\n재주문가: ${new_price:.2f}\n수동으로 미체결 주문을 확인해주세요."
                        )
                        self.log(
                            f"⚠️ [매수 재호가 실패] {symbol} 잔량 {remaining_qty}주 @ ${new_price:.2f}",
                            send_tg=False,
                        )
                        return

                while (time.time() - start_ts) < SMART_BUY_MONITOR_SEC:
                    time.sleep(5)
                    orders = self.api.get_pending_orders(symbols=["__ALL__"])
                    pending = self._pick_pending_order(orders, symbol, "매수")
                    if not pending:
                        return
                    try:
                        last_remaining = int(float(pending.get("remaining_qty", last_remaining)))
                    except Exception:
                        pass

                self.send_telegram_message(
                    f"⏰ [매수 미체결]\n종목: {symbol}\n잔량: {last_remaining}주\n"
                    f"사유: {reason}\n최근 주문가: ${last_price:.2f}\n5분간 완전 체결되지 않았습니다."
                )
                self.log(
                    f"⏰ [매수 미체결] {symbol} 잔량 {last_remaining}주 ({reason}) @ ${last_price:.2f}",
                    send_tg=False,
                )
            except Exception as e:
                self.log(f"[매수 추격형 오류] {symbol}: {e}", send_tg=False)

        threading.Thread(target=_worker, daemon=True).start()

    def _start_trailing_sell_manager(
        self,
        symbol: str,
        total_qty: int,
        initial_price: float,
        prefer_daytime: bool,
    ) -> None:
        def _worker() -> None:
            start_ts: float = time.time()
            last_price: float = initial_price
            last_remaining: int = total_qty
            try:
                for after_sec, discount in TRAILING_SELL_REPRICE_STEPS:
                    sleep_sec: float = after_sec - (time.time() - start_ts)
                    if sleep_sec > 0:
                        time.sleep(sleep_sec)

                    orders: List[Dict[str, Any]] = self.api.get_pending_orders(symbols=["__ALL__"])
                    pending: Optional[Dict[str, Any]] = self._pick_pending_order(orders, symbol, "매도")
                    if not pending:
                        filled_qty: int = max(0, total_qty - last_remaining) or total_qty
                        filled_amount: float = filled_qty * last_price
                        self.finalize_pending_sell_trade(
                            symbol=symbol,
                            ordered_qty=total_qty,
                            filled_qty=filled_qty,
                            fill_price=last_price,
                            completed=True,
                        )
                        self.send_telegram_message(
                            f"✅ [트레일링 스탑 체결 확인]\n종목: {symbol}\n수량: {filled_qty}주\n"
                            f"최근 주문가: ${last_price:.2f}\n예상 체결 금액: ${filled_amount:,.2f}"
                        )
                        self.log(f"✅ [트레일링 체결확인] {symbol} {filled_qty}주 @ ${last_price:.2f}", send_tg=False)
                        return

                    try:
                        remaining_qty: int = int(float(pending.get("remaining_qty", 0)))
                    except Exception:
                        remaining_qty = 0
                    if remaining_qty <= 0:
                        continue
                    last_remaining = remaining_qty

                    order_no: str = str(pending.get("order_no", "")).strip()
                    if not order_no:
                        continue

                    canceled: bool = self.api.cancel_order(
                        order_no=order_no,
                        symbol=symbol,
                        remaining_qty=remaining_qty,
                        prefer_daytime=prefer_daytime,
                    )
                    if not canceled:
                        continue

                    time.sleep(0.25)
                    current_price: float = float(self.api.get_current_price(symbol, prefer_daytime=prefer_daytime) or 0.0)
                    if current_price <= 0:
                        current_price = last_price
                    new_price: float = round(current_price * (1.0 - discount), 2)
                    if new_price <= 0:
                        continue

                    reordered: bool = self.api.place_order(
                        symbol=symbol,
                        quantity=remaining_qty,
                        price=new_price,
                        is_buy=False,
                        prefer_daytime=prefer_daytime,
                    )
                    if reordered:
                        last_price = new_price
                        self.log(
                            f"🔁 [트레일링 재호가] {symbol} 잔량 {remaining_qty}주 @ ${new_price:.2f} "
                            f"(현재가 기준 -{discount*100:.1f}%)",
                            send_tg=False,
                        )
                    else:
                        self.send_telegram_message(
                            f"⚠️ [트레일링 재호가 실패]\n종목: {symbol}\n잔량: {remaining_qty}주\n"
                            f"재주문가: ${new_price:.2f}\n수동으로 미체결 주문을 확인해주세요."
                        )
                        self.log(f"⚠️ [트레일링 재호가 실패] {symbol} 잔량 {remaining_qty}주 @ ${new_price:.2f}", send_tg=False)
                        return

                while (time.time() - start_ts) < TRAILING_SELL_MONITOR_SEC:
                    time.sleep(5)
                    orders = self.api.get_pending_orders(symbols=["__ALL__"])
                    pending = self._pick_pending_order(orders, symbol, "매도")
                    if not pending:
                        filled_qty = max(0, total_qty - last_remaining) or total_qty
                        filled_amount = filled_qty * last_price
                        self.finalize_pending_sell_trade(
                            symbol=symbol,
                            ordered_qty=total_qty,
                            filled_qty=filled_qty,
                            fill_price=last_price,
                            completed=True,
                        )
                        self.send_telegram_message(
                            f"✅ [트레일링 스탑 체결 확인]\n종목: {symbol}\n수량: {filled_qty}주\n"
                            f"최근 주문가: ${last_price:.2f}\n예상 체결 금액: ${filled_amount:,.2f}"
                        )
                        self.log(f"✅ [트레일링 체결확인] {symbol} {filled_qty}주 @ ${last_price:.2f}", send_tg=False)
                        return
                    try:
                        last_remaining = int(float(pending.get("remaining_qty", last_remaining)))
                    except Exception:
                        pass

                partial_filled_qty: int = max(0, total_qty - last_remaining)
                self.finalize_pending_sell_trade(
                    symbol=symbol,
                    ordered_qty=total_qty,
                    filled_qty=partial_filled_qty,
                    fill_price=last_price,
                    completed=False,
                )
                self.send_telegram_message(
                    f"⏰ [트레일링 스탑 미체결]\n종목: {symbol}\n잔량: {last_remaining}주\n"
                    f"최근 주문가: ${last_price:.2f}\n5분간 완전 체결되지 않았습니다."
                )
                self.log(f"⏰ [트레일링 미체결] {symbol} 잔량 {last_remaining}주 @ ${last_price:.2f}", send_tg=False)
            except Exception as e:
                self.log(f"[트레일링 추격형 오류] {symbol}: {e}", send_tg=False)

        threading.Thread(target=_worker, daemon=True).start()

    def _place_calculated_order(self, symbol: str, price: float, target_budget: float, tier_name: str, buy_ratio: float = 0.0, prev_close: float = 0.0) -> bool:
        buy_price: float = round(price * (1.0 + SMART_BUY_INITIAL_PREMIUM), 2)
        qty_to_buy: int = int(target_budget / buy_price)
        if target_budget > 0 and qty_to_buy == 0:
            qty_to_buy = 1

        required_cash: float = qty_to_buy * buy_price

        if qty_to_buy > 0 and self.last_usd_balance >= required_cash:
            now_et: datetime = self.get_eastern_time()
            now_kst: datetime = self.get_korean_time()
            prefer_daytime: bool = self.is_daytime_market_open(now_kst) and (not self.is_active_trading_time(now_et))
            success: bool = self.api.place_order(symbol, qty_to_buy, buy_price, is_buy=True, prefer_daytime=prefer_daytime)
            if success:
                self._start_smart_buy_manager(
                    symbol=symbol,
                    total_qty=qty_to_buy,
                    initial_price=buy_price,
                    reason=tier_name,
                    prefer_daytime=prefer_daytime,
                )
                self.last_usd_balance -= required_cash
                self.last_krw_balance -= required_cash * self.exchange_rate
                mode_label: str = self._get_mode_label()
                self.log(f"✅ [{mode_label}|{tier_name}] {symbol} 매수 체결: {qty_to_buy}주 (${required_cash:.2f})")
                self._log_trade(symbol, "매수", qty_to_buy, buy_price, required_cash, f"[{mode_label}] {tier_name}")

                state: Dict[str, Any] = self.daily_state.get(symbol, {})
                used: int = sum([state.get('base', False), state.get('t2', False), state.get('t4', False), state.get('t8', False)])
                rem: int = max(0, 4 - used)

                held_qty: float = 0.0
                held_avg: float = 0.0
                if not self.positions.empty:
                    mask = self.positions['symbol'] == symbol
                    if mask.any():
                        held_qty = float(self.positions.loc[mask, 'quantity'].values[0])
                        held_avg = float(self.positions.loc[mask, 'avg_price'].values[0])

                krw_amt: float = required_cash * self.exchange_rate
                krw_suffix: str = f"{krw_amt/10000:,.0f}만" if krw_amt >= 10000 else f"{krw_amt:,.0f}"

                tg_msg: str = f"🛒 [매수 체결] {symbol}\n"
                tg_msg += f"📊 모드: {mode_label}\n"
                if prev_close > 0:
                    drop_pct: float = (price - prev_close) / prev_close * 100
                    tg_msg += f"📉 사유: 전일종가 ${prev_close:.2f} → 현재 ${price:.2f} ({drop_pct:+.1f}%)\n"
                else:
                    tg_msg += f"📉 사유: {tier_name}\n"
                tg_msg += f"💰 매수: ${buy_price:.2f} x {qty_to_buy}주 = ${required_cash:,.2f} (약 {krw_suffix}원)\n"
                if buy_ratio > 0:
                    tg_msg += f"📐 비율: 총자산의 {buy_ratio*100:.1f}%\n"
                if held_qty > 0:
                    tg_msg += f"📦 보유: 총 {int(held_qty)}주 (평균 ${held_avg:.2f})\n"
                tg_msg += f"🏦 잔여현금: ${self.last_usd_balance:,.2f} (약 {self.last_krw_balance/10000:,.0f}만원) | 남은 매수: {rem}회"
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
                    if self._place_calculated_order(symbol, price, base_buy_amt, "매일 기본적립(저점)", buy_ratio=self.base_buy_ratio):
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
                    reason_t2: str = f"-3% 물타기(전일종가 대비 {intraday_drop*100:.1f}%)"
                    if self._place_calculated_order(symbol, price, w2_amt, reason_t2, buy_ratio=self.w2_ratio, prev_close=prev_close):
                        state['t2'] = True
                        self.daily_state[symbol] = state
                        self._save_daily_state()
                        total_equity = self.last_usd_balance + (self.positions['quantity'] * self.positions['current_price']).sum()
                        
                if intraday_drop <= self.dca_4_threshold and not state['t4']:
                    w4_amt: float = total_equity * self.w4_ratio
                    reason_t4: str = f"-5% 물타기(전일종가 대비 {intraday_drop*100:.1f}%)"
                    if self._place_calculated_order(symbol, price, w4_amt, reason_t4, buy_ratio=self.w4_ratio, prev_close=prev_close):
                        state['t4'] = True
                        self.daily_state[symbol] = state
                        self._save_daily_state()
                        total_equity = self.last_usd_balance + (self.positions['quantity'] * self.positions['current_price']).sum()
                        
                if intraday_drop <= self.dca_8_threshold and not state['t8']:
                    w8_amt: float = total_equity * self.w8_ratio
                    reason_t8: str = f"-7% 물타기(전일종가 대비 {intraday_drop*100:.1f}%)"
                    if self._place_calculated_order(symbol, price, w8_amt, reason_t8, buy_ratio=self.w8_ratio, prev_close=prev_close):
                        state['t8'] = True
                        self.daily_state[symbol] = state
                        self._save_daily_state()
                        total_equity = self.last_usd_balance + (self.positions['quantity'] * self.positions['current_price']).sum()
            
            # 3. RSI<30 과매도 보너스 매수 (하루 1회, SMA200 무관)
            if qty > 0 and not state.get('rsi_bonus', False) and self.is_rsi_oversold.get(symbol, False):
                rsi_amt: float = total_equity * self.w4_ratio
                if self._place_calculated_order(symbol, price, rsi_amt, "⚡ RSI 과매도 보너스", buy_ratio=self.w4_ratio):
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
            now_kst: datetime = self.get_korean_time()
            prefer_daytime: bool = self.is_daytime_market_open(now_kst)
            self.api.place_order(sym, int(qty), prc, is_buy=False, prefer_daytime=prefer_daytime)
            
        self.log("[Graceful Shutdown] 모든 포지션 정리 완료")

    def run_loop(self) -> None:
        self.is_running = True
        self.log("▶️ 봇 자동매매 루프가 시작되었습니다.", send_tg=True)

        if self.symbols and self.prev_close.get(self.symbols[0], 0.0) == 0.0:
            self.log("📊 [시작] 시장 데이터 초기 로딩 중...")
            self.check_trend_and_momentum()
            self.update_exchange_rate()
        try:
            # 재시작 직후에도 현재 포지션 기준으로 auto 모드를 즉시 반영
            self.fetch_market_data()
            self._check_auto_mode()
            self._last_portfolio_sync_ts = time.time()
        except Exception as e:
            self.log(f"[초기 동기화 오류] {e}", send_tg=False)

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
                    now_ts: float = time.time()
                    sync_interval_sec: float = self._portfolio_sync_interval_sec
                    now_loop_et: datetime = self.get_eastern_time()
                    now_loop_kst: datetime = now_loop_et.astimezone(ZoneInfo("Asia/Seoul"))
                    is_daytime_session: bool = self.is_daytime_market_open(now_loop_kst)
                    is_trade_session: bool = self.is_active_trading_time(now_loop_et) or is_daytime_session
                    if not is_trade_session:
                        sync_interval_sec = self._portfolio_sync_interval_idle_sec
                    if (now_ts - self._last_portfolio_sync_ts) >= sync_interval_sec:
                        try:
                            self.sync_positions()
                            self._publish_live_snapshot()
                        except Exception as snapshot_error:
                            self.log(f"[포지션 동기화 오류] {snapshot_error}", send_tg=False)
                    quote_interval_sec: float = self._quote_refresh_interval_active_sec if is_trade_session else self._quote_refresh_interval_idle_sec
                    if (now_ts - self._last_quote_refresh_ts) >= quote_interval_sec:
                        try:
                            self._refresh_slot_quotes(prefer_daytime=is_daytime_session and (not self.is_active_trading_time(now_loop_et)))
                            self._last_quote_refresh_ts = now_ts
                        except Exception as quote_error:
                            self.log(f"[시세 갱신 오류] {quote_error}", send_tg=False)
                    elif (now_ts - self._last_live_snapshot_ts) >= self._snapshot_publish_interval_sec:
                        try:
                            self._publish_live_snapshot()
                        except Exception as snapshot_error:
                            self.log(f"[스냅샷 갱신 오류] {snapshot_error}", send_tg=False)
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
            "market_open": self.is_active_trading_time(now_et),
            "is_dst": bool(now_et.dst()),
            "et_time": now_et.strftime("%H:%M"),
        }
