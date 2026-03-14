import os
import time
import json
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from typing import Dict, Any, Optional, List, Tuple
import ccxt

def retry_api(max_retries: int = 3, delay_sec: float = 1.0) -> Any:
    def decorator(func: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.RequestException as e:
                    if attempt == max_retries - 1:
                        raise ccxt.NetworkError(f"API 네트워크 오류 발생: {str(e)}")
                    if delay_sec > 0:
                        time.sleep(delay_sec)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise ccxt.ExchangeError(f"API 거래소 오류 발생: {str(e)}")
                    if delay_sec > 0:
                        time.sleep(delay_sec)
        return wrapper
    return decorator

class KoreaInvestmentAPI:
    def __init__(self, app_key: str, app_secret: str, account_no: str, account_code: str, is_mock: bool = True) -> None:
        self.app_key: str = app_key
        self.app_secret: str = app_secret
        self.account_no: str = account_no
        self.account_code: str = account_code
        self.is_mock: bool = is_mock
        
        self.base_url: str = "https://openapivts.koreainvestment.com:29443" if is_mock else "https://openapi.koreainvestment.com:9443"
        self.access_token: str = ""
        self.token_expired_at: float = 0.0
        self._token_fail_ts: float = 0.0
        self._token_lock = threading.Lock()
        self._last_token_issue_ts: float = 0.0
        # KIS tokenP 발급 제한(1초 1건) 보호용 최소 간격
        self._token_min_issue_interval_sec: float = 1.1

        self._exchange_cache: Dict[str, str] = {}
        self._quote_cache: Dict[str, Dict[str, float]] = {}
        self._EXCHANGE_MAP: Dict[str, str] = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}
        self._DAYTIME_EXCD_MAP: Dict[str, str] = {"NAS": "BAQ", "NYS": "BAY", "AMS": "BAA"}
        self._quote_cache_ttl_sec: float = 2.0
        daytime_flag: str = os.getenv("KIS_ENABLE_DAYTIME_TRADING", "true").strip().lower()
        self.enable_daytime_trading: bool = daytime_flag not in ("0", "false", "off", "no")

    def _get_exchange_code(self, symbol: str, style: str = "short") -> str:
        if symbol in self._exchange_cache:
            short: str = self._exchange_cache[symbol]
            return short if style == "short" else self._EXCHANGE_MAP.get(short, "NASD")
        discovered: str = self._discover_exchange(symbol)
        return discovered if style == "short" else self._EXCHANGE_MAP.get(discovered, "NASD")

    def _discover_exchange(self, symbol: str) -> str:
        """3개 거래소를 순회하며 유효한 거래소를 찾아 캐시합니다."""
        url: str = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        headers: Dict[str, str] = self.get_headers("HHDFS00000300")
        for excd in ("NAS", "NYS", "AMS"):
            try:
                params: Dict[str, str] = {"AUTH": "", "EXCD": excd, "SYMB": symbol}
                res = requests.get(url, headers=headers, params=params, timeout=(0.8, 1.5))
                if res.status_code == 200:
                    data = res.json()
                    price = float(data.get("output", {}).get("last", 0))
                    if price > 0:
                        self._exchange_cache[symbol] = excd
                        print(f"[거래소 탐색] {symbol} → {excd} (${price:.2f})")
                        return excd
            except Exception:
                continue
        self._exchange_cache[symbol] = "NAS"
        return "NAS"

    @retry_api(max_retries=3)
    def _issue_token(self) -> None:
        url: str = f"{self.base_url}/oauth2/tokenP"
        payload: Dict[str, str] = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        try:
            res = requests.post(url, json=payload, timeout=(2.0, 4.0))
            if res.status_code != 200:
                print(f"[토큰 발급 실패] HTTP 상태 코드: {res.status_code}")
                print(f"[토큰 발급 응답 데이터] {res.text}")
            res.raise_for_status()
            data: Dict[str, Any] = res.json()
        except requests.exceptions.RequestException as e:
            raise ccxt.NetworkError(f"API 네트워크 오류 발생: {str(e)} | Response: {res.text if 'res' in locals() else 'No response'}")

        self.access_token = data.get("access_token", "")
        # 만료 시간도 한국 시간 기준으로 세팅 (여유시간 60초 차감)
        self.token_expired_at = self.get_korean_time() + int(data.get("expires_in", 86400)) - 60

    def get_korean_time(self) -> float:
        """한국 시간(Asia/Seoul) 기준의 현재 타임스탬프를 반환합니다."""
        import pytz
        from datetime import datetime
        kr_tz = pytz.timezone('Asia/Seoul')
        return datetime.now(kr_tz).timestamp()

    def _is_daytime_window_open(self) -> bool:
        if not self.enable_daytime_trading or self.is_mock:
            return False
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        if now_kst.weekday() >= 5:
            return False
        start = now_kst.replace(hour=9, minute=0, second=0, microsecond=0)
        end = now_kst.replace(hour=16, minute=0, second=0, microsecond=0)
        return start <= now_kst < end

    def get_headers(self, tr_id: str) -> Dict[str, str]:
        now_kr: float = self.get_korean_time()
        if now_kr > self.token_expired_at:
            with self._token_lock:
                # 다른 스레드가 이미 갱신했을 수 있으므로 락 안에서 재확인
                now_kr = self.get_korean_time()
                if now_kr > self.token_expired_at:
                    if now_kr - self._token_fail_ts < 65:
                        raise Exception("토큰 발급 쿨다운 중 (65초 대기)")

                    # tokenP 1초 1건 제한 보호
                    wait_sec: float = self._token_min_issue_interval_sec - (now_kr - self._last_token_issue_ts)
                    if wait_sec > 0:
                        time.sleep(wait_sec)

                    try:
                        self._issue_token()
                        self._token_fail_ts = 0.0
                    except Exception:
                        self._token_fail_ts = self.get_korean_time()
                        self._last_token_issue_ts = self._token_fail_ts
                        raise
                    self._last_token_issue_ts = self.get_korean_time()
            
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id
        }

    def _get_required_exchanges(self, symbols: Optional[List[str]] = None) -> List[str]:
        """슬롯 종목들의 거래소를 파악하여 조회가 필요한 거래소 목록을 반환합니다."""
        if not symbols:
            return ["NASD"]
        if "__ALL__" in symbols:
            return ["NASD", "NYSE", "AMEX"]
        excg_set: set = set()
        for sym in symbols:
            excg_set.add(self._get_exchange_code(sym, "long"))
        if not excg_set:
            return ["NASD"]
        return list(excg_set)

    @retry_api(max_retries=2, delay_sec=0.2)
    def get_balance_and_positions(self, item_cd: str = "AAPL", symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        if self.is_mock:
            # 모의투자는 기존 VTTT3012R 사용
            url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance"
            headers: Dict[str, str] = self.get_headers("VTTT3012R")
            params: Dict[str, str] = {
                "CANO": self.account_no,
                "ACNT_PRDT_CD": self.account_code,
                "WCRC_FRCR_DVSN_CD": "02",
                "NATN_CD": "840",
                "TR_MKET_CD": "00",
                "INQR_DVSN_CD": "00",
                "OVRS_EXCG_CD": "NAS",
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": ""
            }
            res = requests.get(url, headers=headers, params=params, timeout=4)
            res.raise_for_status()
            
            try:
                data = res.json()
                if data.get('rt_cd') != '0':
                    return {"usd_balance": 0.0, "positions": []}
                    
                output2 = data.get('output2', {})
                if isinstance(output2, list) and output2:
                    output2 = output2[0]
                elif not isinstance(output2, dict):
                    output2 = {}
                usd_balance_str = output2.get('frcr_dnca2_amt')
                if not usd_balance_str:
                    usd_balance_str = output2.get('frcr_evlu_amt2')
                if not usd_balance_str: 
                    usd_balance_str = output2.get('frcr_buy_amt_smtl1', 0.0)
                usd_balance = float(usd_balance_str)
                
                positions = []
                if 'output1' in data and isinstance(data['output1'], list):
                    for item in data['output1']:
                        symbol = item.get('ovrs_pdno')
                        qty = float(item.get('ord_psbl_qty', 0))
                        avg_price = float(item.get('pchs_avg_pric', 0))
                        if qty > 0:
                            positions.append({"symbol": symbol, "quantity": qty, "avg_price": avg_price})
                
                return {"usd_balance": usd_balance, "positions": positions}
            except Exception as e:
                print(f"[잔고 파싱 에러] {e}")
                return {"usd_balance": 0.0, "positions": []}
        else:
            usd_balance: float = 0.0
            positions: List[Dict[str, Any]] = []
            result: Dict[str, Any] = {"usd_balance": 0.0, "positions": [], "tot_evlu_pfls": 0.0, "tot_pchs_amt": 0.0}

            try:
                bal_url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
                bal_headers: Dict[str, str] = self.get_headers("TTTS3007R")
                balance_excgs: List[str] = self._get_required_exchanges(symbols if symbols else [item_cd])
                # 거래소 코드가 종목과 불일치하면 예수금이 0으로 떨어질 수 있어 다중 거래소 재시도
                for excg in balance_excgs:
                    try:
                        bal_params: Dict[str, str] = {
                            "CANO": self.account_no,
                            "ACNT_PRDT_CD": self.account_code,
                            "OVRS_EXCG_CD": excg,
                            "OVRS_ORD_UNPR": "1",
                            "ITEM_CD": item_cd
                        }
                        bal_res = requests.get(bal_url, headers=bal_headers, params=bal_params, timeout=(2.0, 4.0))
                        bal_res.raise_for_status()
                        bal_data = bal_res.json()
                        if bal_data.get('rt_cd') != '0' or not isinstance(bal_data.get('output'), dict):
                            continue
                        cand_balance = float(bal_data['output'].get('ovrs_ord_psbl_amt', 0.0))
                        if cand_balance > usd_balance:
                            usd_balance = cand_balance
                        exrt: float = float(bal_data['output'].get('exrt', 0.0))
                        if exrt > 0 and result.get("exchange_rate", 0.0) <= 0:
                            result["exchange_rate"] = exrt
                    except Exception as sub_e:
                        print(f"[실전 예수금 조회 에러 ({excg})] {type(sub_e).__name__}: {sub_e}")
                        continue
            except Exception as e:
                print(f"[실전 예수금 조회 에러] {type(e).__name__}: {e}")

            try:
                krw_url: str = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
                krw_headers: Dict[str, str] = self.get_headers("TTTC8908R")
                krw_params: Dict[str, str] = {
                    "CANO": self.account_no,
                    "ACNT_PRDT_CD": self.account_code,
                    "PDNO": "005930",
                    "ORD_UNPR": "0",
                    "ORD_DVSN": "01",
                    "CMA_EVLU_AMT_ICLD_YN": "Y",
                    "OVRS_ICLD_YN": "Y"
                }
                krw_res = requests.get(krw_url, headers=krw_headers, params=krw_params, timeout=(2.0, 4.0))
                krw_res.raise_for_status()
                krw_data = krw_res.json()
                if krw_data.get('rt_cd') == '0' and isinstance(krw_data.get('output'), dict):
                    result["krw_balance"] = float(krw_data['output'].get('max_buy_amt', 0.0))
                    result["krw_cash"] = float(krw_data['output'].get('ord_psbl_cash', 0.0))
            except Exception as e:
                print(f"[실전 원화 예수금 조회 에러] {type(e).__name__}: {e}")

            tot_evlu_pfls_sum: float = 0.0
            tot_pchs_amt_sum: float = 0.0
            tot_stck_evlu_sum: float = 0.0
            required_excgs: List[str] = self._get_required_exchanges(symbols)
            for excg in required_excgs:
                try:
                    pos_url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
                    pos_headers: Dict[str, str] = self.get_headers("TTTS3012R")
                    pos_params: Dict[str, str] = {
                        "CANO": self.account_no,
                        "ACNT_PRDT_CD": self.account_code,
                        "OVRS_EXCG_CD": excg,
                        "TR_CRCY_CD": "USD",
                        "CTX_AREA_FK200": "",
                        "CTX_AREA_NK200": ""
                    }
                    pos_res = requests.get(pos_url, headers=pos_headers, params=pos_params, timeout=(2.0, 4.0))
                    pos_res.raise_for_status()
                    pos_data = pos_res.json()
                    if pos_data.get('rt_cd') == '0' and isinstance(pos_data.get('output1'), list):
                        for item in pos_data['output1']:
                            symbol: Optional[str] = item.get('ovrs_pdno')
                            qty: float = float(item.get('ovrs_cblc_qty', 0))
                            avg_price: float = float(item.get('pchs_avg_pric', 0))
                            current_price: float = float(item.get('now_pric2', 0))
                            evlu_amt: float = float(item.get('ovrs_stck_evlu_amt', 0))
                            evlu_pfls: float = float(item.get('frcr_evlu_pfls_amt', 0))
                            evlu_pfls_rt: float = float(item.get('evlu_pfls_rt', 0))
                            pchs_amt: float = float(item.get('frcr_pchs_amt1', 0))
                            if qty > 0:
                                positions.append({
                                    "symbol": symbol, "quantity": qty, "avg_price": avg_price,
                                    "current_price": current_price, "evlu_amt": evlu_amt,
                                    "evlu_pfls": evlu_pfls, "return_rate": evlu_pfls_rt,
                                    "pchs_amt": pchs_amt
                                })
                        o2 = pos_data.get('output2')
                        if isinstance(o2, dict):
                            summary = o2
                        elif isinstance(o2, list) and len(o2) > 0:
                            summary = o2[0]
                        else:
                            summary = None
                        if summary:
                            tot_evlu_pfls_sum += float(summary.get('ovrs_tot_pfls', 0))
                            tot_pchs_amt_sum += float(summary.get('frcr_pchs_amt1', 0))
                            tot_stck_evlu_sum += float(summary.get('ovrs_stck_evlu_amt', 0))
                except Exception as e:
                    print(f"[실전 포지션 조회 에러 ({excg})] {type(e).__name__}: {e}")
            result["tot_evlu_pfls"] = tot_evlu_pfls_sum
            result["tot_pchs_amt"] = tot_pchs_amt_sum
            result["tot_stck_evlu"] = tot_stck_evlu_sum

            # 거래소별 조회 시 동일 심볼이 중복 응답되는 경우가 있어 심볼 기준으로 정규화
            if positions:
                deduped_positions: Dict[str, Dict[str, Any]] = {}
                for pos in positions:
                    sym: str = str(pos.get("symbol", ""))
                    if not sym:
                        continue
                    prev = deduped_positions.get(sym)
                    if prev is None:
                        deduped_positions[sym] = pos
                        continue
                    prev_score = (
                        float(prev.get("quantity", 0)),
                        float(prev.get("evlu_amt", 0)),
                        float(prev.get("current_price", 0)),
                    )
                    cur_score = (
                        float(pos.get("quantity", 0)),
                        float(pos.get("evlu_amt", 0)),
                        float(pos.get("current_price", 0)),
                    )
                    if cur_score > prev_score:
                        deduped_positions[sym] = pos
                positions = list(deduped_positions.values())

            result["usd_balance"] = usd_balance
            result["positions"] = positions
            return result

    def get_usd_balance(self) -> float:
        return self.get_balance_and_positions()["usd_balance"]

    @retry_api(max_retries=2, delay_sec=0.15)
    def get_current_price(self, symbol: str, prefer_daytime: bool = False) -> float:
        url: str = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        headers: Dict[str, str] = self.get_headers("HHDFS00000300")
        short_excd: str = self._get_exchange_code(symbol, "short")
        should_try_daytime: bool = prefer_daytime or self._is_daytime_window_open()
        cache_key = f"{symbol}_{'day' if should_try_daytime else 'regular'}"
        cached_quote = self._quote_cache.get(cache_key)
        now_ts = time.time()
        if cached_quote and (now_ts - cached_quote.get("ts", 0.0)) < self._quote_cache_ttl_sec:
            cached_price = float(cached_quote.get("price", 0.0))
            if cached_price > 0:
                return cached_price

        excd_candidates: List[str] = []
        if should_try_daytime:
            excd_candidates.append(self._DAYTIME_EXCD_MAP.get(short_excd, short_excd))
        excd_candidates.append(short_excd)

        seen: set = set()
        for excd in excd_candidates:
            if excd in seen:
                continue
            seen.add(excd)
            params: Dict[str, str] = {
                "AUTH": "",
                "EXCD": excd,
                "SYMB": symbol
            }
            res = requests.get(url, headers=headers, params=params, timeout=(1.0, 2.0))
            res.raise_for_status()
            try:
                data = res.json()
                price = float(data.get('output', {}).get('last', 0))
                if price > 0:
                    self._quote_cache[cache_key] = {"price": price, "ts": now_ts}
                    return price
            except (KeyError, ValueError, TypeError):
                continue
        return 0.0

    @retry_api(max_retries=2, delay_sec=0.15)
    def get_intraday_candles(
        self,
        symbol: str,
        interval_min: int = 5,
        nrec: int = 120,
        prefer_daytime: bool = False
    ) -> List[Dict[str, Any]]:
        url: str = f"{self.base_url}/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"
        headers: Dict[str, str] = self.get_headers("HHDFS76950200")
        short_excd: str = self._get_exchange_code(symbol, "short")
        use_daytime: bool = prefer_daytime or self._is_daytime_window_open()
        excd: str = self._DAYTIME_EXCD_MAP.get(short_excd, short_excd) if use_daytime else short_excd

        safe_nmin: int = max(1, min(int(interval_min), 120))
        safe_nrec: int = max(1, min(int(nrec), 120))
        params: Dict[str, str] = {
            "AUTH": "",
            "EXCD": excd,
            "SYMB": symbol,
            "NMIN": str(safe_nmin),
            "PINC": "0",
            "NEXT": "",
            "NREC": str(safe_nrec),
            "FILL": "",
            "KEYB": "",
        }
        res = requests.get(url, headers=headers, params=params, timeout=6)
        res.raise_for_status()
        data = res.json()
        if data.get("rt_cd") != "0":
            msg = data.get("msg1", "Unknown")
            raise Exception(f"분봉 조회 실패({excd}): {msg}")

        rows: List[Dict[str, Any]] = data.get("output2", [])
        candles: List[Dict[str, Any]] = []
        tz_et = ZoneInfo("America/New_York")
        for row in rows:
            try:
                ymd: str = str(row.get("xymd", ""))
                hms: str = str(row.get("xhms", ""))
                if len(ymd) != 8 or len(hms) < 6:
                    continue
                dt_et = datetime(
                    int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8]),
                    int(hms[0:2]), int(hms[2:4]), int(hms[4:6]),
                    tzinfo=tz_et
                )
                candles.append({
                    "time": int(dt_et.timestamp()),
                    "open": float(row.get("open", 0) or 0),
                    "high": float(row.get("high", 0) or 0),
                    "low": float(row.get("low", 0) or 0),
                    "close": float(row.get("last", 0) or 0),
                    "volume": int(float(row.get("evol", 0) or 0)),
                })
            except Exception:
                continue
        candles.sort(key=lambda x: x["time"])
        return candles

    @retry_api(max_retries=2, delay_sec=0.2)
    def get_daily_candles(self, symbol: str, period: str = "1y") -> List[Dict[str, Any]]:
        """해외주식 일봉 조회 (KIS only)"""
        url: str = f"{self.base_url}/uapi/overseas-price/v1/quotations/dailyprice"
        headers: Dict[str, str] = self.get_headers("HHDFS76240000")
        short_excd: str = self._get_exchange_code(symbol, "short")
        now_et = datetime.now(ZoneInfo("America/New_York"))
        keep_days_map: Dict[str, int] = {
            "1mo": 31,
            "3mo": 92,
            "6mo": 184,
            "1y": 366,
            "2y": 731,
        }
        keep_days: int = keep_days_map.get(period, 366)

        max_batches_map: Dict[str, int] = {
            "1mo": 1,
            "3mo": 2,
            "6mo": 3,
            "1y": 5,
            "2y": 8,
        }
        max_batches: int = max_batches_map.get(period, 5)
        target_rows_map: Dict[str, int] = {
            "1mo": 20,
            "3mo": 60,
            "6mo": 120,
            "1y": 200,
            "2y": 400,
        }
        target_rows: int = target_rows_map.get(period, 200)
        cutoff_date = (now_et - timedelta(days=keep_days + 7)).date()

        all_rows: List[Dict[str, Any]] = []
        seen_dates: set = set()
        cursor_date = now_et.date()
        empty_streak: int = 0

        for _ in range(max_batches):
            params: Dict[str, str] = {
                "AUTH": "",
                "EXCD": short_excd,
                "SYMB": symbol,
                "GUBN": "0",
                "BYMD": cursor_date.strftime("%Y%m%d"),
                "MODP": "0",
            }
            res = requests.get(url, headers=headers, params=params, timeout=(2.0, 5.0))
            res.raise_for_status()
            data = res.json()
            if data.get("rt_cd") != "0":
                msg = data.get("msg1", "Unknown")
                raise Exception(f"일봉 조회 실패({short_excd}): {msg}")

            rows: List[Dict[str, Any]] = data.get("output2", [])
            if not rows and isinstance(data.get("output1"), list):
                rows = data.get("output1", [])
            if not rows and isinstance(data.get("output"), list):
                rows = data.get("output", [])

            oldest_ymd: str = ""
            added_count: int = 0
            for row in rows:
                ymd = str(
                    row.get("xymd")
                    or row.get("stck_bsop_date")
                    or row.get("date")
                    or row.get("bas_dt")
                    or ""
                )
                if len(ymd) != 8 or ymd in seen_dates:
                    continue
                seen_dates.add(ymd)
                all_rows.append(row)
                added_count += 1
                if not oldest_ymd or ymd < oldest_ymd:
                    oldest_ymd = ymd

            if added_count <= 0:
                empty_streak += 1
                if empty_streak >= 3:
                    break
                cursor_date = cursor_date - timedelta(days=1)
                continue

            empty_streak = 0
            if oldest_ymd:
                try:
                    cursor_date = datetime.strptime(oldest_ymd, "%Y%m%d").date() - timedelta(days=1)
                except Exception:
                    cursor_date = cursor_date - timedelta(days=1)
            else:
                cursor_date = cursor_date - timedelta(days=1)

            if len(all_rows) >= target_rows and cursor_date <= cutoff_date:
                break

        tz_et = ZoneInfo("America/New_York")
        candles: List[Dict[str, Any]] = []

        def _as_float(value: Any) -> float:
            try:
                return float(value or 0)
            except Exception:
                return 0.0

        for row in all_rows:
            ymd = str(
                row.get("xymd")
                or row.get("stck_bsop_date")
                or row.get("date")
                or row.get("bas_dt")
                or ""
            )
            if len(ymd) != 8:
                continue
            try:
                dt_et = datetime(
                    int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8]),
                    0, 0, 0, tzinfo=tz_et
                )
            except Exception:
                continue

            o = _as_float(row.get("open") or row.get("stck_oprc") or row.get("ovrs_nmix_oprc"))
            h = _as_float(row.get("high") or row.get("stck_hgpr") or row.get("ovrs_nmix_hgpr"))
            l = _as_float(row.get("low") or row.get("stck_lwpr") or row.get("ovrs_nmix_lwpr"))
            c = _as_float(
                row.get("last")
                or row.get("clos")
                or row.get("close")
                or row.get("stck_clpr")
                or row.get("ovrs_nmix_prpr")
            )
            v = int(
                _as_float(
                    row.get("tvol")
                    or row.get("evol")
                    or row.get("acml_vol")
                    or row.get("ovrs_nmix_vol")
                )
            )
            if c <= 0:
                continue
            if o <= 0:
                o = c
            if h <= 0:
                h = max(o, c)
            if l <= 0:
                l = min(o, c)

            candles.append(
                {
                    "time": int(dt_et.timestamp()),
                    "open": round(o, 2),
                    "high": round(h, 2),
                    "low": round(l, 2),
                    "close": round(c, 2),
                    "volume": max(v, 0),
                }
            )

        candles.sort(key=lambda x: x["time"])
        if not candles:
            return []

        # 누적 조회 후 period 기준으로 후행 필터링
        cutoff_ts = int((now_et.timestamp()) - (keep_days * 86400))
        filtered = [c for c in candles if c["time"] >= cutoff_ts]
        return filtered if filtered else candles

    @retry_api(max_retries=3)
    def place_order(self, symbol: str, quantity: float, price: float, is_buy: bool, prefer_daytime: bool = False) -> bool:
        use_daytime: bool = prefer_daytime and (not self.is_mock) and self.enable_daytime_trading
        if use_daytime:
            url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/daytime-order"
            tr_id: str = "TTTS6036U" if is_buy else "TTTS6037U"
            mode_label: str = "주간거래"
        else:
            url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
            tr_id = "TTTT1002U" if is_buy else "TTTT1006U"
            if self.is_mock:
                tr_id = "V" + tr_id[1:]
            mode_label = "일반거래"
        formatted_price: str = f"{price:.2f}"
        ord_dvsn: str = "00"

        headers: Dict[str, str] = self.get_headers(tr_id)
        excg_cd: str = self._get_exchange_code(symbol, "long")
        payload: Dict[str, str] = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_code,
            "OVRS_EXCG_CD": excg_cd,
            "PDNO": symbol,
            "ORD_QTY": str(int(quantity)),
            "OVRS_ORD_UNPR": formatted_price,
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": ord_dvsn
        }
        print(f"[주문 요청] {mode_label} {tr_id} | {symbol} ({excg_cd}) {'매수' if is_buy else '매도'} {int(quantity)}주 @ ${formatted_price}")
        res = requests.post(url, headers=headers, json=payload, timeout=(2.0, 6.0))
        
        try:
            data: Dict[str, Any] = res.json()
        except Exception:
            print(f"[주문 실패] HTTP {res.status_code} | 응답 파싱 불가: {res.text[:200]}")
            return False
        
        if res.status_code != 200:
            msg1: str = data.get('msg1', res.text)
            print(f"[주문 실패] HTTP {res.status_code} | {msg1}")
            return False
        
        if data.get('rt_cd') != '0':
            msg1 = data.get('msg1', 'Unknown Error')
            print(f"[주문 거부] {msg1}")
            return False
            
        return True

    @retry_api(max_retries=2, delay_sec=0.2)
    def get_pending_orders(self, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        orders: List[Dict[str, Any]] = []
        seen_order_keys: set = set()
        tr_id: str = "VTTS3018R" if self.is_mock else "TTTS3018R"
        required_excgs: List[str] = self._get_required_exchanges(symbols)
        for excg in required_excgs:
            try:
                url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
                headers: Dict[str, str] = self.get_headers(tr_id)
                params: Dict[str, str] = {
                    "CANO": self.account_no,
                    "ACNT_PRDT_CD": self.account_code,
                    "OVRS_EXCG_CD": excg,
                    "SORT_SQN": "DS",
                    "CTX_AREA_FK200": "",
                    "CTX_AREA_NK200": ""
                }
                res = requests.get(url, headers=headers, params=params, timeout=(1.5, 3.0))
                try:
                    data: Dict[str, Any] = res.json()
                except Exception:
                    print(f"[미체결 조회 실패 ({excg})] HTTP {res.status_code}")
                    continue

                if data.get('rt_cd') != '0':
                    continue

                for item in data.get('output', []):
                    nccs_qty: int = int(float(item.get('nccs_qty', 0)))
                    if nccs_qty <= 0:
                        continue
                    order_no: str = item.get('odno', '')
                    symbol: str = item.get('pdno', '')
                    side: str = "매수" if item.get('sll_buy_dvsn_cd') == '02' else "매도"
                    dedupe_key = (order_no, symbol, side)
                    if dedupe_key in seen_order_keys:
                        continue
                    seen_order_keys.add(dedupe_key)
                    orders.append({
                        "order_no": order_no,
                        "symbol": symbol,
                        "side": side,
                        "order_qty": int(float(item.get('ft_ord_qty', 0))),
                        "filled_qty": int(float(item.get('ft_ccld_qty', 0))),
                        "remaining_qty": nccs_qty,
                        "order_price": float(item.get('ft_ord_unpr3', 0)),
                        "order_time": item.get('ord_tmd', ''),
                        "orgn_odno": item.get('orgn_odno', ''),
                    })
            except Exception as e:
                print(f"[미체결 조회 에러 ({excg})] {type(e).__name__}: {e}")
        return orders

    @retry_api(max_retries=3)
    def cancel_order(self, order_no: str, symbol: str, remaining_qty: int, prefer_daytime: bool = False) -> bool:
        def _cancel_once(daytime: bool) -> Tuple[bool, str]:
            if daytime:
                tr_id: str = "TTTS6038U"
                url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/daytime-order-rvsecncl"
                mode_label: str = "주간거래"
            else:
                tr_id = "VTTT1004U" if self.is_mock else "TTTT1004U"
                url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order-rvsecncl"
                mode_label = "일반거래"

            headers: Dict[str, str] = self.get_headers(tr_id)
            payload: Dict[str, str] = {
                "CANO": self.account_no,
                "ACNT_PRDT_CD": self.account_code,
                "OVRS_EXCG_CD": self._get_exchange_code(symbol, "long"),
                "PDNO": symbol,
                "ORGN_ODNO": order_no,
                "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": str(remaining_qty),
                "OVRS_ORD_UNPR": "0",
                "ORD_SVR_DVSN_CD": "0"
            }
            res = requests.post(url, headers=headers, json=payload, timeout=(2.0, 6.0))
            try:
                data: Dict[str, Any] = res.json()
            except Exception:
                return False, f"{mode_label} HTTP {res.status_code}"

            if data.get('rt_cd') != '0':
                return False, f"{mode_label} {data.get('msg1', '')}"

            return True, mode_label

        try_daytime_first: bool = prefer_daytime and (not self.is_mock) and self.enable_daytime_trading
        attempt_order: List[bool] = [True, False] if try_daytime_first else [False, True]
        if self.is_mock:
            attempt_order = [False]

        last_error: str = ""
        for daytime in attempt_order:
            ok, detail = _cancel_once(daytime)
            if ok:
                print(f"[주문 취소 성공] {symbol} 주문번호 {order_no} ({detail})")
                return True
            last_error = detail

        try:
            now_kst: str = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            now_kst = "-"
        if last_error:
            print(f"[주문 취소 실패] {last_error} | KST {now_kst}")
            return False
        return False

    def cancel_all_orders(self) -> None:
        print("[API] 모든 미체결 주문 취소 요청을 전송합니다.")
        try:
            orders: List[Dict[str, Any]] = self.get_pending_orders(symbols=["__ALL__"])
            for order in orders:
                self.cancel_order(order['order_no'], order['symbol'], order['remaining_qty'])
                time.sleep(0.5)
        except Exception as e:
            print(f"[전체 취소 오류] {e}")
