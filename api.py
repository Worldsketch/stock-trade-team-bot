import os
import time
import json
import threading
import re
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


def _mask_sensitive_text(text: str) -> str:
    masked: str = str(text)
    patterns: List[Tuple[str, str]] = [
        (r"(CANO=)\d+", r"\1***"),
        (r"(ACNT_PRDT_CD=)\d+", r"\1**"),
        (r"(PDNO=)\d+", r"\1***"),
        (r'("CANO"\s*:\s*")\d+(")', r"\1***\2"),
        (r'("ACNT_PRDT_CD"\s*:\s*")\d+(")', r"\1**\2"),
        (r'("appkey"\s*:\s*")[^"]+(")', r"\1***\2"),
        (r'("appsecret"\s*:\s*")[^"]+(")', r"\1***\2"),
        (r"(appkey=)[^&\s]+", r"\1***"),
        (r"(appsecret=)[^&\s]+", r"\1***"),
    ]
    for pattern, repl in patterns:
        masked = re.sub(pattern, repl, masked)
    return masked


def _format_safe_error(error: Exception) -> str:
    return _mask_sensitive_text(f"{type(error).__name__}: {error}")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        text = str(value).strip().replace(",", "")
        if text == "":
            return default
        return float(text)
    except Exception:
        return default


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
        self._LONG_TO_SHORT_MAP: Dict[str, str] = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        self._DAYTIME_EXCD_MAP: Dict[str, str] = {"NAS": "BAQ", "NYS": "BAY", "AMS": "BAA"}
        self._quote_cache_ttl_sec: float = 2.0
        daytime_flag: str = os.getenv("KIS_ENABLE_DAYTIME_TRADING", "true").strip().lower()
        self.enable_daytime_trading: bool = daytime_flag not in ("0", "false", "off", "no")
        self._api_fail_window_stats: Dict[str, int] = {}
        self._api_fail_total_stats: Dict[str, int] = {}
        self._api_fail_recent: List[Dict[str, str]] = []
        self._api_fail_recent_limit: int = 500
        self._api_fail_stats_lock = threading.Lock()
        self._api_fail_last_flush_ts: float = time.time()
        self._api_fail_flush_interval_sec: float = 300.0
        self._api_fail_log_file: str = "api_fail_stats.json"
        self._api_fail_last_persist_ts: float = 0.0
        self._api_fail_persist_interval_sec: float = 10.0
        self._foreign_margin_cache: Dict[str, float] = {"usd": 0.0, "exchange_rate": 0.0, "ts": 0.0}
        self._foreign_margin_cache_ttl_sec: float = 2.0
        self._foreign_margin_lock = threading.Lock()

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

    def _normalize_us_exchanges_for_inquiry(self, exchanges: List[str]) -> List[str]:
        """
        실전 조회 API는 NASD로 미국 전체 조회가 가능해(NYSE/AMEX 포함)
        조회 계열 호출에서 미국 거래소 코드를 NASD로 정규화해 호출량과 중복을 줄입니다.
        """
        if self.is_mock:
            # 모의는 거래소별 동작 차이가 있어 기존 유지
            return list(dict.fromkeys(exchanges))

        us_exchanges: set = {"NASD", "NYSE", "AMEX", "NAS"}
        uniq: List[str] = list(dict.fromkeys(exchanges))
        has_us: bool = any(ex in us_exchanges for ex in uniq)
        normalized: List[str] = [ex for ex in uniq if ex not in us_exchanges]
        if has_us:
            normalized.insert(0, "NASD")
        return normalized if normalized else ["NASD"]

    def _default_item_for_exchange(self, excg: str, fallback: str = "AAPL") -> str:
        defaults: Dict[str, str] = {
            "NASD": "AAPL",
            "NAS": "AAPL",
            "NYSE": "BA",
            "AMEX": "SPY",
        }
        base: str = str(fallback or "").strip().upper()
        return defaults.get(excg, base if base else "AAPL")

    def _get_psamount_reference_price(self, symbol: str, excg: str) -> float:
        """
        TTTS3007R 입력용 주문단가를 실시간가 기반으로 보정합니다.
        조회 실패 시 1.0으로 안전 폴백합니다.
        """
        sym: str = str(symbol or "").strip().upper()
        if not sym:
            return 1.0
        try:
            price = float(self.get_current_price(sym, prefer_daytime=self._is_daytime_window_open()) or 0.0)
            if price > 0:
                return price
        except Exception:
            pass
        try:
            short_excd: str = self._LONG_TO_SHORT_MAP.get(excg, "NAS")
            url: str = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
            headers: Dict[str, str] = self.get_headers("HHDFS00000300")
            params: Dict[str, str] = {"AUTH": "", "EXCD": short_excd, "SYMB": sym}
            res = requests.get(url, headers=headers, params=params, timeout=(1.0, 2.0))
            res.raise_for_status()
            data = res.json()
            price2 = _safe_float(data.get("output", {}).get("last", 0.0))
            if price2 > 0:
                return price2
        except Exception:
            pass
        return 1.0

    def _record_api_failure(self, api_name: str, rt_cd: Any, msg_cd: Any, msg1: Any = "") -> None:
        """
        API 실패 코드 집계:
        - 5분 단위 콘솔 요약
        - JSON 파일(api_fail_stats.json) 주기 저장
        """
        key: str = f"{api_name}|rt={str(rt_cd)}|msg={str(msg_cd)}"
        now_ts: float = time.time()
        top_items: List[Tuple[str, int]] = []
        persist_payload: Optional[Dict[str, Any]] = None
        masked_msg1: str = _mask_sensitive_text(str(msg1))

        with self._api_fail_stats_lock:
            self._api_fail_window_stats[key] = self._api_fail_window_stats.get(key, 0) + 1
            self._api_fail_total_stats[key] = self._api_fail_total_stats.get(key, 0) + 1
            self._api_fail_recent.append(
                {
                    "ts": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S"),
                    "api": str(api_name),
                    "rt_cd": str(rt_cd),
                    "msg_cd": str(msg_cd),
                    "msg1": masked_msg1[:200],
                }
            )
            if len(self._api_fail_recent) > self._api_fail_recent_limit:
                self._api_fail_recent = self._api_fail_recent[-self._api_fail_recent_limit :]

            if (now_ts - self._api_fail_last_flush_ts) >= self._api_fail_flush_interval_sec:
                top_items = sorted(self._api_fail_window_stats.items(), key=lambda kv: kv[1], reverse=True)[:8]
                self._api_fail_window_stats = {}
                self._api_fail_last_flush_ts = now_ts

            if (now_ts - self._api_fail_last_persist_ts) >= self._api_fail_persist_interval_sec:
                persist_payload = {
                    "updated_at": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S"),
                    "totals": dict(self._api_fail_total_stats),
                    "recent": list(self._api_fail_recent),
                }
                self._api_fail_last_persist_ts = now_ts

        if top_items:
            joined: str = ", ".join(f"{k} x{v}" for k, v in top_items)
            print(f"[API 실패코드 집계(최근 5분)] {joined}")
        if persist_payload is not None:
            try:
                tmp_path: str = f"{self._api_fail_log_file}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(persist_payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self._api_fail_log_file)
            except Exception as e:
                print(f"[API 실패코드 파일 저장 오류] {_format_safe_error(e)}")

    def _fetch_overseas_balance_pages(self, excg: str, tr_crcy_cd: str = "USD", max_pages: int = 20) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        해외주식 잔고(TTTS3012R) 연속조회 페이지를 모두 가져옵니다.
        """
        url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        items: List[Dict[str, Any]] = []
        summaries: List[Dict[str, Any]] = []
        ctx_fk200: str = ""
        ctx_nk200: str = ""
        tr_cont_req: str = ""
        prev_ctx: Tuple[str, str] = ("", "")

        for _ in range(max_pages):
            headers: Dict[str, str] = self.get_headers("TTTS3012R")
            if tr_cont_req:
                headers["tr_cont"] = tr_cont_req
            params: Dict[str, str] = {
                "CANO": self.account_no,
                "ACNT_PRDT_CD": self.account_code,
                "OVRS_EXCG_CD": excg,
                "TR_CRCY_CD": tr_crcy_cd,
                "CTX_AREA_FK200": ctx_fk200,
                "CTX_AREA_NK200": ctx_nk200,
            }
            res = requests.get(url, headers=headers, params=params, timeout=(2.0, 4.0))
            res.raise_for_status()
            data = res.json()
            if data.get("rt_cd") != "0":
                msg_cd: str = str(data.get("msg_cd", "")).strip()
                msg1: str = str(data.get("msg1", "")).strip()
                self._record_api_failure("TTTS3012R", data.get("rt_cd"), msg_cd, msg1)
                print(
                    f"[실전 포지션 비정상 ({excg})] "
                    f"rt_cd={data.get('rt_cd')} msg_cd={msg_cd} msg1={_mask_sensitive_text(msg1)}"
                )
                break

            output1 = data.get("output1", [])
            if isinstance(output1, list):
                items.extend([x for x in output1 if isinstance(x, dict)])

            o2 = data.get("output2")
            if isinstance(o2, dict):
                summaries.append(o2)
            elif isinstance(o2, list) and o2 and isinstance(o2[0], dict):
                summaries.append(o2[0])

            tr_cont_res: str = str(res.headers.get("tr_cont", "")).strip().upper()
            next_fk200: str = str(data.get("ctx_area_fk200", "")).strip()
            next_nk200: str = str(data.get("ctx_area_nk200", "")).strip()
            if tr_cont_res not in ("F", "M") or not next_fk200 or not next_nk200:
                break
            if (next_fk200, next_nk200) == prev_ctx:
                break
            prev_ctx = (next_fk200, next_nk200)
            ctx_fk200, ctx_nk200 = next_fk200, next_nk200
            tr_cont_req = "N"

        return items, summaries

    def _fetch_pending_pages(self, excg: str, tr_id: str, max_pages: int = 20) -> List[Dict[str, Any]]:
        """
        해외주식 미체결(TTTS3018R/VTTS3018R) 연속조회 페이지를 모두 가져옵니다.
        """
        url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
        items: List[Dict[str, Any]] = []
        ctx_fk200: str = ""
        ctx_nk200: str = ""
        tr_cont_req: str = ""
        prev_ctx: Tuple[str, str] = ("", "")

        for _ in range(max_pages):
            headers: Dict[str, str] = self.get_headers(tr_id)
            if tr_cont_req:
                headers["tr_cont"] = tr_cont_req
            params: Dict[str, str] = {
                "CANO": self.account_no,
                "ACNT_PRDT_CD": self.account_code,
                "OVRS_EXCG_CD": excg,
                "SORT_SQN": "DS",
                "CTX_AREA_FK200": ctx_fk200,
                "CTX_AREA_NK200": ctx_nk200,
            }
            res = requests.get(url, headers=headers, params=params, timeout=(1.5, 3.0))
            try:
                data: Dict[str, Any] = res.json()
            except Exception:
                print(f"[미체결 조회 실패 ({excg})] HTTP {res.status_code}")
                break

            if data.get("rt_cd") != "0":
                msg_cd: str = str(data.get("msg_cd", "")).strip()
                msg1: str = str(data.get("msg1", "")).strip()
                self._record_api_failure(tr_id, data.get("rt_cd"), msg_cd, msg1)
                print(
                    f"[미체결 조회 비정상 ({excg})] "
                    f"rt_cd={data.get('rt_cd')} msg_cd={msg_cd} msg1={_mask_sensitive_text(msg1)}"
                )
                break

            output = data.get("output", [])
            if isinstance(output, list):
                items.extend([x for x in output if isinstance(x, dict)])

            tr_cont_res: str = str(res.headers.get("tr_cont", "")).strip().upper()
            next_fk200: str = str(data.get("ctx_area_fk200", "")).strip()
            next_nk200: str = str(data.get("ctx_area_nk200", "")).strip()
            if tr_cont_res not in ("F", "M") or not next_fk200 or not next_nk200:
                break
            if (next_fk200, next_nk200) == prev_ctx:
                break
            prev_ctx = (next_fk200, next_nk200)
            ctx_fk200, ctx_nk200 = next_fk200, next_nk200
            tr_cont_req = "N"

        return items

    def _extract_psamount_usd(self, output: Dict[str, Any]) -> float:
        """
        TTTS3007R 응답에서 주문가능 외화 금액을 최대치 기준으로 추출합니다.
        문서상 ovrs_ord_psbl_amt가 비어/0일 수 있어 다중 필드 fallback을 사용합니다.
        """
        candidates: List[float] = [
            _safe_float(output.get("ovrs_ord_psbl_amt", 0.0)),
            _safe_float(output.get("ord_psbl_frcr_amt", 0.0)),
            _safe_float(output.get("frcr_ord_psbl_amt1", 0.0)),
        ]
        return max(candidates) if candidates else 0.0

    def _get_usd_from_foreign_margin(self, result: Dict[str, Any]) -> float:
        """
        해외증거금 통화별조회(TTTC2101R)에서 USD 통화 기준 예수금을 fallback 조회합니다.
        """
        if self.is_mock:
            return 0.0

        now_ts: float = time.time()
        with self._foreign_margin_lock:
            cached_age: float = now_ts - float(self._foreign_margin_cache.get("ts", 0.0))
            if cached_age < self._foreign_margin_cache_ttl_sec:
                cached_usd: float = float(self._foreign_margin_cache.get("usd", 0.0))
                cached_exrt: float = float(self._foreign_margin_cache.get("exchange_rate", 0.0))
                if cached_exrt > 0 and result.get("exchange_rate", 0.0) <= 0:
                    result["exchange_rate"] = cached_exrt
                return cached_usd

        url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/foreign-margin"
        headers: Dict[str, str] = self.get_headers("TTTC2101R")
        params: Dict[str, str] = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_code,
        }
        try:
            res = requests.get(url, headers=headers, params=params, timeout=(2.0, 4.0))
            res.raise_for_status()
            data = res.json()
            if data.get("rt_cd") != "0":
                msg_cd: str = str(data.get("msg_cd", "")).strip()
                msg1: str = str(data.get("msg1", "")).strip()
                self._record_api_failure("TTTC2101R", data.get("rt_cd"), msg_cd, msg1)
                print(f"[해외증거금 조회 비정상] rt_cd={data.get('rt_cd')} msg_cd={msg_cd} msg1={_mask_sensitive_text(msg1)}")
                return 0.0

            output = data.get("output", [])
            if not isinstance(output, list):
                return 0.0

            usd_candidates: List[float] = []
            for row in output:
                if not isinstance(row, dict):
                    continue
                crcy_cd: str = str(row.get("crcy_cd", "")).strip().upper()
                if crcy_cd != "USD":
                    continue
                usd_candidates.append(_safe_float(row.get("frcr_gnrl_ord_psbl_amt", 0.0)))
                usd_candidates.append(_safe_float(row.get("frcr_dncl_amt1", 0.0)))
                usd_candidates.append(_safe_float(row.get("frcr_ord_psbl_amt1", 0.0)))

                bass_exrt: float = _safe_float(row.get("bass_exrt", 0.0))
                if bass_exrt > 0 and result.get("exchange_rate", 0.0) <= 0:
                    result["exchange_rate"] = bass_exrt

            usd_value: float = max(usd_candidates) if usd_candidates else 0.0
            with self._foreign_margin_lock:
                self._foreign_margin_cache = {
                    "usd": usd_value,
                    "exchange_rate": float(result.get("exchange_rate", 0.0) or 0.0),
                    "ts": time.time(),
                }
            return usd_value
        except Exception as e:
            print(f"[해외증거금 조회 에러] {_format_safe_error(e)}")
            with self._foreign_margin_lock:
                cached_usd_fallback: float = float(self._foreign_margin_cache.get("usd", 0.0))
                cached_exrt_fallback: float = float(self._foreign_margin_cache.get("exchange_rate", 0.0))
                if cached_exrt_fallback > 0 and result.get("exchange_rate", 0.0) <= 0:
                    result["exchange_rate"] = cached_exrt_fallback
                if cached_usd_fallback > 0:
                    return cached_usd_fallback
            return 0.0

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
                balance_excgs = self._normalize_us_exchanges_for_inquiry(balance_excgs)
                representative_item_by_excg: Dict[str, str] = {}
                for sym in symbols or []:
                    sym_norm: str = str(sym).strip().upper()
                    if not sym_norm or sym_norm == "__ALL__":
                        continue
                    try:
                        representative_item_by_excg.setdefault(self._get_exchange_code(sym_norm, "long"), sym_norm)
                    except Exception:
                        continue
                ref_price_cache: Dict[str, float] = {}
                # 거래소 코드가 종목과 불일치하면 예수금이 0으로 떨어질 수 있어 다중 거래소 재시도
                for excg in balance_excgs:
                    try:
                        ref_item: str = representative_item_by_excg.get(excg, self._default_item_for_exchange(excg, fallback=item_cd))
                        if ref_item not in ref_price_cache:
                            ref_price_cache[ref_item] = self._get_psamount_reference_price(ref_item, excg)
                        ref_price: float = max(ref_price_cache.get(ref_item, 1.0), 1.0)
                        bal_params: Dict[str, str] = {
                            "CANO": self.account_no,
                            "ACNT_PRDT_CD": self.account_code,
                            "OVRS_EXCG_CD": excg,
                            "OVRS_ORD_UNPR": f"{ref_price:.2f}",
                            "ITEM_CD": ref_item
                        }
                        bal_res = requests.get(bal_url, headers=bal_headers, params=bal_params, timeout=(2.0, 4.0))
                        bal_res.raise_for_status()
                        bal_data = bal_res.json()
                        if bal_data.get('rt_cd') != '0':
                            msg_cd: str = str(bal_data.get("msg_cd", "")).strip()
                            msg1: str = str(bal_data.get("msg1", "")).strip()
                            self._record_api_failure("TTTS3007R", bal_data.get("rt_cd"), msg_cd, msg1)
                            print(
                                f"[실전 예수금 비정상 ({excg}/{ref_item})] "
                                f"rt_cd={bal_data.get('rt_cd')} msg_cd={msg_cd} msg1={_mask_sensitive_text(msg1)}"
                            )
                            continue
                        if not isinstance(bal_data.get('output'), dict):
                            print(f"[실전 예수금 비정상 ({excg}/{ref_item})] output 누락")
                            continue
                        cand_balance = self._extract_psamount_usd(bal_data["output"])
                        if cand_balance > usd_balance:
                            usd_balance = cand_balance
                        exrt: float = _safe_float(bal_data['output'].get('exrt', 0.0))
                        if exrt > 0 and result.get("exchange_rate", 0.0) <= 0:
                            result["exchange_rate"] = exrt
                    except Exception as sub_e:
                        print(f"[실전 예수금 조회 에러 ({excg})] {_format_safe_error(sub_e)}")
                        continue
                if usd_balance <= 0:
                    margin_usd: float = self._get_usd_from_foreign_margin(result)
                    if margin_usd > usd_balance:
                        usd_balance = margin_usd
            except Exception as e:
                print(f"[실전 예수금 조회 에러] {_format_safe_error(e)}")

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
                print(f"[실전 원화 예수금 조회 에러] {_format_safe_error(e)}")

            required_excgs: List[str] = self._normalize_us_exchanges_for_inquiry(self._get_required_exchanges(symbols))
            for excg in required_excgs:
                try:
                    pos_items, _pos_summaries = self._fetch_overseas_balance_pages(excg=excg, tr_crcy_cd="USD")
                    for item in pos_items:
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
                except Exception as e:
                    print(f"[실전 포지션 조회 에러 ({excg})] {_format_safe_error(e)}")

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

            # 연속조회 output2(summary)는 페이지별로 반복될 수 있어 포지션 합산값을 기준으로 총계를 계산
            result["tot_evlu_pfls"] = float(sum(float(p.get("evlu_pfls", 0.0) or 0.0) for p in positions))
            result["tot_pchs_amt"] = float(sum(float(p.get("pchs_amt", 0.0) or 0.0) for p in positions))
            result["tot_stck_evlu"] = float(sum(float(p.get("evlu_amt", 0.0) or 0.0) for p in positions))
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

        seen_excd: set = set()

        def _try_price(base_excd: str) -> float:
            candidates: List[str] = []
            if should_try_daytime:
                candidates.append(self._DAYTIME_EXCD_MAP.get(base_excd, base_excd))
            candidates.append(base_excd)
            for excd in candidates:
                if excd in seen_excd:
                    continue
                seen_excd.add(excd)
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
                        return price
                except (KeyError, ValueError, TypeError):
                    continue
            return 0.0

        # 1) 캐시된 거래소 우선
        price = _try_price(short_excd)
        if price > 0:
            self._quote_cache[cache_key] = {"price": price, "ts": now_ts}
            return price

        # 2) 실패 시 3개 거래소 재탐색 (캐시 오염/변경 대응)
        for fallback_excd in ("NAS", "NYS", "AMS"):
            if fallback_excd == short_excd:
                continue
            price = _try_price(fallback_excd)
            if price > 0:
                self._exchange_cache[symbol] = fallback_excd
                self._quote_cache[cache_key] = {"price": price, "ts": now_ts}
                return price
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
    def get_daily_candles(self, symbol: str, period: str = "1y", adjusted: bool = False) -> List[Dict[str, Any]]:
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
            "3y": 1096,
            "5y": 1827,
            "10y": 3653,
            "max": 3653,
        }
        keep_days: int = keep_days_map.get(period, 366)

        max_batches_map: Dict[str, int] = {
            "1mo": 1,
            "3mo": 2,
            "6mo": 3,
            "1y": 5,
            "2y": 8,
            "3y": 12,
            "5y": 20,
            "10y": 40,
            "max": 40,
        }
        max_batches: int = max_batches_map.get(period, 5)
        target_rows_map: Dict[str, int] = {
            "1mo": 20,
            "3mo": 60,
            "6mo": 120,
            "1y": 200,
            "2y": 400,
            "3y": 750,
            "5y": 1300,
            "10y": 2800,
            "max": 2800,
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
                # 0: 미반영, 1: 수정주가 반영(분할/병합 보정)
                "MODP": "1" if adjusted else "0",
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
        required_excgs: List[str] = self._normalize_us_exchanges_for_inquiry(self._get_required_exchanges(symbols))
        for excg in required_excgs:
            try:
                page_items = self._fetch_pending_pages(excg=excg, tr_id=tr_id)
                for item in page_items:
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
                print(f"[미체결 조회 에러 ({excg})] {_format_safe_error(e)}")
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
