import os
import time
import json
import requests
from typing import Dict, Any, Optional, List
import ccxt

def retry_api(max_retries: int = 3) -> Any:
    def decorator(func: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.RequestException as e:
                    if attempt == max_retries - 1:
                        raise ccxt.NetworkError(f"API 네트워크 오류 발생: {str(e)}")
                    time.sleep(1)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise ccxt.ExchangeError(f"API 거래소 오류 발생: {str(e)}")
                    time.sleep(1)
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

        self._exchange_cache: Dict[str, str] = {}
        self._EXCHANGE_MAP: Dict[str, str] = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}

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
                res = requests.get(url, headers=headers, params=params)
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
            res = requests.post(url, json=payload)
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

    def get_headers(self, tr_id: str) -> Dict[str, str]:
        now_kr: float = self.get_korean_time()
        if now_kr > self.token_expired_at:
            if now_kr - self._token_fail_ts < 65:
                raise Exception("토큰 발급 쿨다운 중 (65초 대기)")
            try:
                self._issue_token()
            except Exception:
                self._token_fail_ts = now_kr
                raise
            
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id
        }

    @retry_api(max_retries=3)
    def get_balance_and_positions(self, item_cd: str = "AAPL") -> Dict[str, Any]:
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
            res = requests.get(url, headers=headers, params=params)
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
                bal_params: Dict[str, str] = {
                    "CANO": self.account_no,
                    "ACNT_PRDT_CD": self.account_code,
                    "OVRS_EXCG_CD": "NASD",
                    "OVRS_ORD_UNPR": "1",
                    "ITEM_CD": item_cd
                }
                bal_res = requests.get(bal_url, headers=bal_headers, params=bal_params)
                bal_res.raise_for_status()
                bal_data = bal_res.json()
                if bal_data.get('rt_cd') == '0' and isinstance(bal_data.get('output'), dict):
                    usd_balance = float(bal_data['output'].get('ovrs_ord_psbl_amt', 0.0))
                    exrt: float = float(bal_data['output'].get('exrt', 0.0))
                    if exrt > 0:
                        result["exchange_rate"] = exrt
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
                krw_res = requests.get(krw_url, headers=krw_headers, params=krw_params)
                krw_res.raise_for_status()
                krw_data = krw_res.json()
                if krw_data.get('rt_cd') == '0' and isinstance(krw_data.get('output'), dict):
                    result["krw_balance"] = float(krw_data['output'].get('max_buy_amt', 0.0))
                    result["krw_cash"] = float(krw_data['output'].get('ord_psbl_cash', 0.0))
            except Exception as e:
                print(f"[실전 원화 예수금 조회 에러] {type(e).__name__}: {e}")

            try:
                pos_url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
                pos_headers: Dict[str, str] = self.get_headers("TTTS3012R")
                pos_params: Dict[str, str] = {
                    "CANO": self.account_no,
                    "ACNT_PRDT_CD": self.account_code,
                    "OVRS_EXCG_CD": "NASD",
                    "TR_CRCY_CD": "USD",
                    "CTX_AREA_FK200": "",
                    "CTX_AREA_NK200": ""
                }
                pos_res = requests.get(pos_url, headers=pos_headers, params=pos_params)
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
                        summary: Dict[str, Any] = o2
                    elif isinstance(o2, list) and len(o2) > 0:
                        summary: Dict[str, Any] = o2[0]
                    else:
                        summary = None
                    if summary:
                        tot_evlu_pfls: float = float(summary.get('ovrs_tot_pfls', 0))
                        tot_pchs_amt: float = float(summary.get('frcr_pchs_amt1', 0))
                        tot_stck_evlu: float = float(summary.get('ovrs_stck_evlu_amt', 0))
                        result["tot_evlu_pfls"] = tot_evlu_pfls
                        result["tot_pchs_amt"] = tot_pchs_amt
                        result["tot_stck_evlu"] = tot_stck_evlu
            except Exception as e:
                print(f"[실전 포지션 조회 에러] {type(e).__name__}: {e}")

            result["usd_balance"] = usd_balance
            result["positions"] = positions
            return result

    def get_usd_balance(self) -> float:
        return self.get_balance_and_positions()["usd_balance"]

    @retry_api(max_retries=3)
    def get_current_price(self, symbol: str) -> float:
        url: str = f"{self.base_url}/uapi/overseas-price/v1/quotations/price"
        headers: Dict[str, str] = self.get_headers("HHDFS00000300")
        params: Dict[str, str] = {
            "AUTH": "",
            "EXCD": self._get_exchange_code(symbol, "short"),
            "SYMB": symbol
        }
        res = requests.get(url, headers=headers, params=params)
        res.raise_for_status()
        
        try:
            data = res.json()
            return float(data['output']['last'])
        except (KeyError, ValueError, TypeError):
            return 0.0

    @retry_api(max_retries=3)
    def place_order(self, symbol: str, quantity: float, price: float, is_buy: bool) -> bool:
        url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        tr_id: str = "TTTT1002U" if is_buy else "TTTT1006U"
        if self.is_mock:
            tr_id = "V" + tr_id[1:]
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
        print(f"[주문 요청] {tr_id} | {symbol} ({excg_cd}) {'매수' if is_buy else '매도'} {int(quantity)}주 @ ${formatted_price}")
        res = requests.post(url, headers=headers, json=payload)
        
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

    @retry_api(max_retries=3)
    def get_pending_orders(self) -> List[Dict[str, Any]]:
        tr_id: str = "VTTS3018R" if self.is_mock else "TTTS3018R"
        url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-nccs"
        headers: Dict[str, str] = self.get_headers(tr_id)
        params: Dict[str, str] = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_code,
            "OVRS_EXCG_CD": "NASD",
            "SORT_SQN": "DS",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": ""
        }
        res = requests.get(url, headers=headers, params=params)
        try:
            data: Dict[str, Any] = res.json()
        except Exception:
            print(f"[미체결 조회 실패] HTTP {res.status_code}")
            return []

        if data.get('rt_cd') != '0':
            print(f"[미체결 조회 실패] {data.get('msg1', '')}")
            return []

        orders: List[Dict[str, Any]] = []
        for item in data.get('output', []):
            nccs_qty: int = int(float(item.get('nccs_qty', 0)))
            if nccs_qty <= 0:
                continue
            orders.append({
                "order_no": item.get('odno', ''),
                "symbol": item.get('pdno', ''),
                "side": "매수" if item.get('sll_buy_dvsn_cd') == '02' else "매도",
                "order_qty": int(float(item.get('ft_ord_qty', 0))),
                "filled_qty": int(float(item.get('ft_ccld_qty', 0))),
                "remaining_qty": nccs_qty,
                "order_price": float(item.get('ft_ord_unpr3', 0)),
                "order_time": item.get('ord_tmd', ''),
                "orgn_odno": item.get('orgn_odno', ''),
            })
        return orders

    @retry_api(max_retries=3)
    def cancel_order(self, order_no: str, symbol: str, remaining_qty: int) -> bool:
        tr_id: str = "VTTT1004U" if self.is_mock else "TTTT1004U"
        url: str = f"{self.base_url}/uapi/overseas-stock/v1/trading/order-rvsecncl"
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
        res = requests.post(url, headers=headers, json=payload)
        try:
            data: Dict[str, Any] = res.json()
        except Exception:
            print(f"[주문 취소 실패] HTTP {res.status_code}")
            return False

        if data.get('rt_cd') != '0':
            print(f"[주문 취소 실패] {data.get('msg1', '')}")
            return False

        print(f"[주문 취소 성공] {symbol} 주문번호 {order_no}")
        return True

    def cancel_all_orders(self) -> None:
        print("[API] 모든 미체결 주문 취소 요청을 전송합니다.")
        try:
            orders: List[Dict[str, Any]] = self.get_pending_orders()
            for order in orders:
                self.cancel_order(order['order_no'], order['symbol'], order['remaining_qty'])
                time.sleep(0.5)
        except Exception as e:
            print(f"[전체 취소 오류] {e}")
