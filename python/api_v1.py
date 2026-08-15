import FinanceDataReader as fdr
import os
import requests
import json
import logging
import time 
import threading
import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime

from login import fn_au10001
from config import KIWOOM_HOST_URL, KIWOOM_ACCOUNT_NO, MOCK_TRADE, DEBUG_MODE as ENV_DEBUG
from database import db

# ---------------------------------------------------------
# 1. 로거 및 세션 설정
# ---------------------------------------------------------
logger = logging.getLogger("API")
API_LOCK = threading.RLock()
CACHED_TOKEN = None

API_SESSION = requests.Session()
retries = Retry(total=3, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
API_SESSION.mount('http://', HTTPAdapter(max_retries=retries))
API_SESSION.mount('https://', HTTPAdapter(max_retries=retries))

# ---------------------------------------------------------
# 2. 유틸리티 클래스 및 함수
# ---------------------------------------------------------
class SmartRateLimiter:
    def __init__(self):
        self.min_interval = 0.6  # 🌟 [수정] API 제한 안정화 (0.6초)
        self.max_interval = 30.0
        self.current_interval = 1.0
        self.last_call_time = 0
        self.decay_rate = 0.9
        self.penalty_multiplier = 2.0

    def wait(self):
        now = time.time()
        elapsed = now - self.last_call_time
        wait_time = self.current_interval - elapsed
        if wait_time > 0:
            time.sleep(wait_time + random.uniform(0, 0.1))
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

def safe_int(value):
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
        current_retry = retry_count
        max_retries = 3  # 🌟 [수정] 재시도 횟수 증가 (2 -> 3)

        while current_retry <= max_retries:
            RATE_LIMITER.wait()
            token = _get_valid_token(force_refresh=(current_retry > 0))
            if not token: return None

            if api_id.startswith('kt10') or api_id.startswith('kt5000'): endpoint = '/api/dostk/ordr'
            elif api_id.startswith('kt00') or api_id.startswith('ka10075'): endpoint = '/api/dostk/acnt'
            elif api_id.startswith('ka10080'): endpoint = '/api/dostk/chart'
            elif api_id.startswith('ka10005'): endpoint = '/api/dostk/mrkcond'
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
                    wait_time = 2.0 * (current_retry + 1)
                    logger.warning(f"🔥 [429] 속도제한! 간격 {new_interval:.2f}s로 증가, {wait_time}s 대기")
                    time.sleep(wait_time)
                    current_retry += 1
                    continue

                if response.status_code == 401 or response.status_code == 403:
                    logger.warning("⚠️ 토큰 만료. 재발급 시도...")
                    current_retry += 1
                    continue

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
        return None

# ---------------------------------------------------------
# 3. 계좌 및 기타 API
# ---------------------------------------------------------
def fn_kt00018_get_account_balance():
    params = { "acnt_no": KIWOOM_ACCOUNT_NO, "qry_tp": "1", "dmst_stex_tp": "KRX" }
    response_data = _call_api(api_id="kt00018", params=params)
    if response_data:
        try:
            summary = {
                "총매입금액": safe_int(response_data.get('tot_pur_amt')),
                "총평가금액": safe_int(response_data.get('tot_evlt_amt')),
                "총평가손익": safe_int(response_data.get('tot_evlt_pl')),
                "총수익률(%)": float(response_data.get('tot_prft_rt', 0.0)),
                "추정예탁자산": safe_int(response_data.get('prsm_dpst_aset_amt')),
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
            return safe_int(deposit)
        except Exception: return 0
    return 0

def fn_ka10074_get_daily_profit():
    today_str = datetime.now().strftime('%Y%m%d')
    params = { "strt_dt": today_str, "end_dt": today_str, "stk_cd": "" }
    response_data = _call_api(api_id="ka10074", params=params)
    if response_data:
        try:
            profit = response_data.get('rlzt_pl')
            if profit is not None: return safe_int(profit)
            data_list = response_data.get('dt_rlzt_pl', [])
            if data_list and len(data_list) > 0:
                return safe_int(data_list[0].get('tdy_sel_pl', 0))
        except Exception as e:
            logger.error(f"일자별 손익 파싱 실패: {e}")
    return None

def fn_ka10001_get_stock_info(stock_code: str):
    params = { "stk_cd": stock_code }
    response_data = _call_api(api_id="ka10001", params=params)
    if response_data:
        try:
            info = {
                "종목코드": response_data.get('stk_cd'),
                "종목명": (response_data.get('stk_nm') or "").strip(),
                "현재가": safe_int(response_data.get('cur_prc')),
                "기준가": safe_int(response_data.get('std_prc') or response_data.get('bf_cls_prc')),
                "시가": safe_int(response_data.get('open_pric') or response_data.get('open_prc')),
                "고가": safe_int(response_data.get('high_pric') or response_data.get('high_prc')),
                "저가": safe_int(response_data.get('low_pric') or response_data.get('low_prc')),
                "예상체결가": safe_int(response_data.get('exp_cntr_pric') or response_data.get('exp_cntr_prc'))
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
    if response_data:
        if response_data.get('ord_no'): return response_data.get('ord_no')
        else: logger.error(f"❌ 매수주문 실패 (응답): {response_data}")
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
            # 🌟 [수정] API 응답 코드 확인 (성공이 아니면 에러 처리)
            ret_code = response_data.get('return_code')
            ret_msg = response_data.get('return_msg')
            if ret_code is not None and str(ret_code).strip() not in ['0', '0000']:
                logger.warning(f"ℹ️ [호가조회실패] {stock_code} API응답: {ret_msg} [{ret_code}]")
                return None

            # 🌟 [수정] 데이터 위치 유연하게 찾기 (output > root) 및 리스트 처리
            target_data = response_data.get('output')
            if not target_data:
                target_data = response_data # output 없으면 최상위 딕셔너리 사용
            
            if isinstance(target_data, list) and len(target_data) > 0:
                target_data = target_data[0] # 리스트인 경우 첫 번째 요소 사용

            if not isinstance(target_data, dict):
                return None

            sell_keys = ['tot_sel_req', 'tot_sel_pr_ord_remn_qty', 'tot_sell_remn', 'total_sell_remn_qty']
            buy_keys = ['tot_buy_req', 'tot_buy_pr_ord_remn_qty', 'tot_buy_remn', 'total_buy_remn_qty']
            sell_total = 0; buy_total = 0
            for k in sell_keys:
                if target_data.get(k): sell_total = safe_int(target_data.get(k)); break
            for k in buy_keys:
                if target_data.get(k): buy_total = safe_int(target_data.get(k)); break
            
            # [디버깅] 데이터가 0일 경우 키 확인 (모의투자 데이터 확인용)
            if sell_total == 0 and buy_total == 0 and CURRENT_DEBUG_MODE:
                logger.debug(f"🔍 [HOGA_EMPTY] {stock_code} Raw Keys: {list(target_data.keys())}")

            return { "sell_total": sell_total, "buy_total": buy_total }
        except Exception as e:
            if CURRENT_DEBUG_MODE: logger.debug(f"호가 데이터 파싱 실패({stock_code}): {e}")
            return None
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

# 🌟 fn_ka10005_get_daily_chart 삭제됨 (Strategy에서 fdr 사용)

def fn_ka90001_get_top_themes(top_n=3):
    """ 당일 등락률 상위 테마 조회 (ka90001) """
    params = {
        "qry_tp": "0",          # 0:전체검색
        "stk_cd": "",
        "date_tp": "1",         # 1일
        "thema_nm": "",
        "flu_pl_amt_tp": "3",   # 3:상위등락률
        "stex_tp": "1"          # 1:KRX
    }
    response_data = _call_api(api_id="ka90001", params=params)
    if response_data and response_data.get('thema_grp'):
        # 상위 top_n개 반환
        return response_data['thema_grp'][:top_n]
    return []

def fn_ka90002_get_theme_stocks(theme_grp_cd):
    """ 테마 구성 종목 조회 (ka90002) """
    params = {
        "date_tp": "1",
        "thema_grp_cd": str(theme_grp_cd),
        "stex_tp": "1"
    }
    response_data = _call_api(api_id="ka90002", params=params)
    if response_data and response_data.get('thema_comp_stk'):
        return [stk.get('stk_cd') for stk in response_data['thema_comp_stk']]
    return []

# 종목별 시장(코스피/코스닥) 정보도 DB에 저장
def create_master_stock_file():
    """ 마스터 종목 파일 다운로드 및 DB 갱신 (하루 1회) """
    
    saved_master = db.get_kv("master_stocks")
    if saved_master:
        pass
        # 이미 데이터가 있으면 스킵 (필요시 날짜 체크 로직 추가 가능)
        logger.info("📚 기존 마스터 데이터가 존재하여 다운로드를 건너뜁니다.")
        return

    try:
        logger.info("📚 마스터 종목 데이터를 다운로드합니다...")
        df_kospi = fdr.StockListing('KOSPI')
        df_kosdaq = fdr.StockListing('KOSDAQ')
        
        # 1. 기본 마스터 (코드:이름) - 레거시 호환
        master_dict = {row['Code']: row['Name'] for _, row in df_kospi.iterrows()}
        master_dict.update({row['Code']: row['Name'] for _, row in df_kosdaq.iterrows()})
        db.set_kv("master_stocks", master_dict)

        # 2. 🌟 시장 구분 맵 (코드:시장구분) - 신규 기능
        # KOSPI 종목
        market_map = {row['Code']: 'KOSPI' for _, row in df_kospi.iterrows()}
        # KOSDAQ 종목 (덮어쓰기로 혹시 모를 중복 방지)
        market_map.update({row['Code']: 'KOSDAQ' for _, row in df_kosdaq.iterrows()})
        db.set_kv("stock_market_map", market_map)

        logger.info(f"✅ 마스터 데이터 DB 저장 완료 (종목: {len(master_dict)}개, 시장구분맵 생성됨).")
        
    except Exception as e:
        logger.error(f"마스터 데이터 생성 실패: {e}")