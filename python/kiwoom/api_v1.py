import FinanceDataReader as fdr
import os
import requests
import json
import logging
import time 
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime

from login import fn_au10001
from config import KIWOOM_HOST_URL, KIWOOM_ACCOUNT_NO, MOCK_TRADE, DEBUG_MODE as ENV_DEBUG

# 🌟 [수정] DB 모듈 임포트
from database import db

# ---------------------------------------------------------
# 1. 로거 및 세션 설정
# ---------------------------------------------------------
logger = logging.getLogger("API")
API_LOCK = threading.RLock()
CACHED_TOKEN = None

# TCP 연결 재사용을 위한 전역 세션 (속도 최적화)
API_SESSION = requests.Session()
retries = Retry(total=3, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
API_SESSION.mount('http://', HTTPAdapter(max_retries=retries))
API_SESSION.mount('https://', HTTPAdapter(max_retries=retries))

# ---------------------------------------------------------
# 2. 유틸리티 클래스 및 함수
# ---------------------------------------------------------
class SmartRateLimiter:
    """ API 요청 속도 제한을 관리하는 클래스 """
    def __init__(self):
        self.min_interval = 0.5
        self.max_interval = 5.0
        self.current_interval = 0.5
        self.last_call_time = 0
        self.decay_rate = 0.95
        self.penalty_multiplier = 1.5

    def wait(self):
        now = time.time()
        elapsed = now - self.last_call_time
        wait_time = self.current_interval - elapsed
        if wait_time > 0:
            time.sleep(wait_time)
        self.last_call_time = time.time()

    def report_success(self):
        if self.current_interval > self.min_interval:
            self.current_interval = max(self.min_interval, self.current_interval * self.decay_rate)

    def report_429(self):
        self.current_interval = min(self.max_interval, self.current_interval * self.penalty_multiplier)
        return self.current_interval

RATE_LIMITER = SmartRateLimiter()
CURRENT_DEBUG_MODE = ENV_DEBUG

def set_api_debug_mode(mode: bool):
    global CURRENT_DEBUG_MODE
    CURRENT_DEBUG_MODE = mode
    level = logging.DEBUG if mode else logging.INFO
    logger.setLevel(level)

def _safe_int(value):
    try:
        if value is None: return 0
        if isinstance(value, int): return value
        s_val = str(value).replace(',', '').replace('+', '').strip()
        if not s_val: return 0
        return int(s_val)
    except ValueError:
        return 0

def _get_valid_token(force_refresh=False):
    global CACHED_TOKEN
    if CACHED_TOKEN and not force_refresh:
        return CACHED_TOKEN
    new_token = fn_au10001()
    if new_token:
        CACHED_TOKEN = new_token
        return CACHED_TOKEN
    else:
        logger.error("❌ 토큰 발급 실패!")
        return None

def _call_api(api_id: str, params: dict, retry_count=0, is_high_priority=True, cont_yn="N", next_key="", return_headers=False):
    global CACHED_TOKEN
    
    if not is_high_priority: time.sleep(0.05)

    with API_LOCK:
        RATE_LIMITER.wait() 

        token = _get_valid_token(force_refresh=False)
        if not token: return None

        if api_id.startswith('kt10') or api_id.startswith('kt5000'): endpoint = '/api/dostk/ordr'
        elif api_id.startswith('kt00') or api_id.startswith('ka10075'): endpoint = '/api/dostk/acnt'
        elif api_id.startswith('ka10080'): endpoint = '/api/dostk/chart' 
        elif api_id.startswith('ka10001'): endpoint = '/api/dostk/stkinfo'
        elif api_id.startswith('ka10004'): endpoint = '/api/dostk/mrkcond'
        elif api_id.startswith('ka10074'): endpoint = '/api/dostk/acnt'
        else: endpoint = '/api/dostk/stkinfo'
            
        url = KIWOOM_HOST_URL + endpoint
        
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',
            'authorization': f"Bearer {token}",
            'api-id': api_id,
            'cont-yn': cont_yn,
            'next-key': next_key
        }

        start_time = time.time()
        try:
            if CURRENT_DEBUG_MODE:
                logger.debug(f"📤 [REQ] {api_id} Params: {params} | Head(Next): {next_key}")

            response = API_SESSION.post(url, headers=headers, json=params, timeout=10)
            duration = (time.time() - start_time) * 1000

            if CURRENT_DEBUG_MODE:
                data_len = len(response.text) if response.text else 0
                logger.debug(f"📥 [RES] {response.status_code} ({duration:.0f}ms) Size: {data_len}B")

            if response.status_code == 429:
                new_interval = RATE_LIMITER.report_429()
                wait_time = 2.0 * (retry_count + 1)
                logger.warning(f"🔥 [429] 속도제한! 간격 {new_interval:.2f}s로 증가, {wait_time}s 대기")
                time.sleep(wait_time)
                if retry_count < 1:
                    return _call_api(api_id, params, retry_count + 1, is_high_priority, cont_yn, next_key, return_headers)
                return None

            if response.status_code == 401 or response.status_code == 403:
                logger.warning("⚠️ 토큰 만료. 재발급 시도...")
                if retry_count < 2:
                    _get_valid_token(force_refresh=True)
                    return _call_api(api_id, params, retry_count + 1, is_high_priority, cont_yn, next_key, return_headers)
                return None

            if response.status_code != 200:
                logger.error(f"API HTTP 오류 ({response.status_code}): {response.text[:100]}...")
                return None

            RATE_LIMITER.report_success()
            
            if return_headers:
                return response.json(), response.headers
            return response.json()

        except Exception as e:
            logger.error(f"API 호출 중 오류 (TR: {api_id}): {e}")
            return None

# ---------------------------------------------------------
# 3. 계좌 관련 API (기존 유지)
# ---------------------------------------------------------
def fn_kt00018_get_account_balance():
    params = { "acnt_no": KIWOOM_ACCOUNT_NO, "qry_tp": "1", "dmst_stex_tp": "KRX" }
    response_data = _call_api(api_id="kt00018", params=params)
    if response_data:
        try:
            summary = {
                "총매입금액": _safe_int(response_data.get('tot_pur_amt')),
                "총평가금액": _safe_int(response_data.get('tot_evlt_amt')),
                "총평가손익": _safe_int(response_data.get('tot_evlt_pl')),
                "총수익률(%)": float(response_data.get('tot_prft_rt', 0.0)),
                "추정예탁자산": _safe_int(response_data.get('prsm_dpst_aset_amt')),
                "보유종목": response_data.get('acnt_evlt_remn_indv_tot', []) 
            }
            return summary
        except Exception: return None
    return None

def fn_kt00001_get_deposit():
    params = { "acnt_no": KIWOOM_ACCOUNT_NO, "qry_tp": "2" }
    response_data = _call_api(api_id="kt00001", params=params)
    if response_data:
        try:
            deposit = (response_data.get('mny_ord_able_amt') or response_data.get('ord_psbl_amt') or response_data.get('entr'))
            return _safe_int(deposit)
        except Exception: return 0
    return 0

def fn_ka10074_get_daily_profit():
    today_str = datetime.now().strftime('%Y%m%d')
    params = { "strt_dt": today_str, "end_dt": today_str, "stk_cd": "" }
    response_data = _call_api(api_id="ka10074", params=params)
    if response_data:
        try:
            profit = response_data.get('rlzt_pl')
            if profit is not None: return _safe_int(profit)
            data_list = response_data.get('dt_rlzt_pl', [])
            if data_list and len(data_list) > 0:
                return _safe_int(data_list[0].get('tdy_sel_pl', 0))
        except Exception as e:
            logger.error(f"일자별 손익 파싱 실패: {e}")
    return None

# ---------------------------------------------------------
# 4. 시세 및 정보 API (기존 유지)
# ---------------------------------------------------------
def fn_ka10001_get_stock_info(stock_code: str):
    params = { "stk_cd": stock_code }
    response_data = _call_api(api_id="ka10001", params=params)
    if response_data:
        try:
            info = {
                "종목코드": response_data.get('stk_cd'),
                "종목명": response_data.get('stk_nm'),
                "현재가": _safe_int(response_data.get('cur_prc')),
                "기준가": _safe_int(response_data.get('std_prc') or response_data.get('bf_cls_prc')),
                "시가": _safe_int(response_data.get('open_pric') or response_data.get('open_prc')),
                "예상체결가": _safe_int(response_data.get('exp_cntr_pric') or response_data.get('exp_cntr_prc'))
            }
            return info
        except Exception: return None
    return None

def fn_kt10000_buy_order(stock_code: str, quantity: int, price: int = 0):
    trade_type = "03" if price == 0 else "00" 
    params = {
        "acnt_no": KIWOOM_ACCOUNT_NO, "dmst_stex_tp": "KRX", "stk_cd": stock_code, 
        "ord_qty": str(quantity), "ord_uv": str(price), "trde_tp": trade_type, "cond_uv": ""
    }
    if MOCK_TRADE: time.sleep(0.1)
    response_data = _call_api(api_id="kt10000", params=params)
    if response_data and response_data.get('ord_no'): return response_data.get('ord_no')
    return None

def fn_kt10001_sell_order(stock_code: str, quantity: int, price: int = 0):
    trade_type = "03" if price == 0 else "00"
    params = {
        "acnt_no": KIWOOM_ACCOUNT_NO, "dmst_stex_tp": "KRX", "stk_cd": stock_code, 
        "ord_qty": str(quantity), "ord_uv": str(price), "trde_tp": trade_type, "cond_uv": ""
    }
    if MOCK_TRADE: time.sleep(0.1)
    response_data = _call_api(api_id="kt10001", params=params)
    if response_data and response_data.get('ord_no'): return response_data.get('ord_no')
    return None

def fn_kt10003_cancel_order(stock_code: str, quantity: int, orgn_ord_no: str, is_buy: bool):
    trde_tp = "03" if is_buy else "04"
    params = {
        "acnt_no": KIWOOM_ACCOUNT_NO, "dmst_stex_tp": "KRX", "stk_cd": stock_code,
        "ord_qty": str(quantity), "ord_uv": "0", "trde_tp": trde_tp, "orgn_ord_no": str(orgn_ord_no), "cond_uv": ""
    }
    if MOCK_TRADE: time.sleep(0.1)
    response_data = _call_api(api_id="kt10003", params=params)
    if response_data and response_data.get('ord_no'): return response_data.get('ord_no')
    return None

def fn_ka10004_get_hoga(stock_code: str):
    params = { "stk_cd": stock_code }
    response_data = _call_api(api_id="ka10004", params=params)
    if response_data:
        try:
            sell_keys = ['tot_sel_req', 'tot_sel_pr_ord_remn_qty', 'tot_sell_remn', 'total_sell_remn_qty']
            buy_keys = ['tot_buy_req', 'tot_buy_pr_ord_remn_qty', 'tot_buy_remn', 'total_buy_remn_qty']
            sell_total = 0; buy_total = 0
            for k in sell_keys:
                if response_data.get(k): sell_total = _safe_int(response_data.get(k)); break
            for k in buy_keys:
                if response_data.get(k): buy_total = _safe_int(response_data.get(k)); break
            return { "sell_total": sell_total, "buy_total": buy_total }
        except Exception: return None
    return None

def fn_ka10080_get_minute_chart(stock_code: str, tick: str = "3"):
    MAX_PAGES = 2
    all_chart_data = []
    current_next_key = ""
    current_cont_yn = "N"
    
    for page in range(MAX_PAGES):
        params = { "stk_cd": stock_code, "tic_scope": tick, "upd_stkpc_tp": "1", "date_type": "1" }
        if page > 0: time.sleep(0.3) 
        
        result = _call_api(api_id="ka10080", params=params, is_high_priority=False, cont_yn=current_cont_yn, next_key=current_next_key, return_headers=True)
        if not result: break
        
        response_data, response_headers = result
        if response_data:
            chart_data = (response_data.get('stk_min_pole_chart_qry') or response_data.get('output2') or [])
            if chart_data:
                all_chart_data.extend(chart_data)
                current_next_key = response_headers.get('next-key') or response_headers.get('Next-Key') or ""
                current_next_key = current_next_key.strip()
                current_cont_yn = response_headers.get('cont-yn', 'N').strip()
                if not current_next_key or current_cont_yn != 'Y': break
            else: break
        else: break
            
    return all_chart_data if all_chart_data else None

# 🌟 [수정] 마스터 파일 생성을 DB 저장으로 변경
def create_master_stock_file():
    """ 마스터 종목 파일 다운로드 및 DB 갱신 (하루 1회) """
    
    # DB에서 마지막 업데이트 확인
    saved_master = db.get_kv("master_stocks")
    if saved_master:
        # 간단하게 체크: 데이터가 있으면 스킵 (필요시 날짜 체크 로직 추가 가능)
        # 하지만 여기서는 항상 최신화를 시도하되, 너무 잦은 호출 방지 로직은 상위에서 처리 권장
        pass

    try:
        logger.info("📚 마스터 종목 데이터를 다운로드합니다...")
        df_kospi = fdr.StockListing('KOSPI')
        df_kosdaq = fdr.StockListing('KOSDAQ')
        
        master_dict = {row['Code']: row['Name'] for _, row in df_kospi.iterrows()}
        master_dict.update({row['Code']: row['Name'] for _, row in df_kosdaq.iterrows()})
        
        # DB에 저장
        db.set_kv("master_stocks", master_dict)
        logger.info(f"✅ 마스터 데이터 DB 저장 완료 ({len(master_dict)}개).")
        
    except Exception as e:
        logger.error(f"마스터 데이터 생성 실패: {e}")