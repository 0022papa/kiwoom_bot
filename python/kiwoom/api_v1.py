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
    """
    API 요청 속도 제한을 관리하는 클래스.
    429(Too Many Requests) 응답 시 자동으로 대기 시간을 늘려 조절합니다.
    """
    def __init__(self):
        self.min_interval = 0.5  # 최소 대기 (초) 
        self.max_interval = 5.0   # 최대 대기 (초)
        self.current_interval = 0.33 
        self.last_call_time = 0
        self.decay_rate = 0.95    # 성공 시 대기 시간 감소율
        self.penalty_multiplier = 1.5 # 실패 시 대기 시간 증가율

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
    """ 외부에서 디버그 모드를 켜고 끄는 함수 """
    global CURRENT_DEBUG_MODE
    CURRENT_DEBUG_MODE = mode
    level = logging.DEBUG if mode else logging.INFO
    logger.setLevel(level)

def _safe_int(value):
    """ 
    문자열이나 None을 안전하게 정수로 변환 
    🌟 [중요 수정] 마이너스(-) 기호는 유지해야 손실금액이 정상적으로 나옵니다!
    """
    try:
        if value is None: return 0
        if isinstance(value, int): return value
        # 쉼표, 플러스 기호만 제거 (마이너스는 유지)
        s_val = str(value).replace(',', '').replace('+', '').strip()
        if not s_val: return 0
        return int(s_val)
    except ValueError:
        return 0

def _get_valid_token(force_refresh=False):
    """ 유효한 OAuth 토큰을 반환하거나 재발급 """
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
    """
    API 호출을 수행하는 핵심 함수 (재시도, 로깅, 에러 처리 포함)
    🌟 [수정] cont_yn, next_key 인자 추가 및 Header 처리 수정
    🌟 [수정] return_headers 옵션 추가 (연속 조회를 위해 응답 헤더가 필요함)
    """
    global CACHED_TOKEN
    
    # 우선순위가 낮으면(예: 차트 조회) 잠시 대기하여 주문 처리에 양보
    if not is_high_priority:
        time.sleep(0.05)

    with API_LOCK:
        RATE_LIMITER.wait() 

        token = _get_valid_token(force_refresh=False)
        if not token: return None

        # 엔드포인트 라우팅
        if api_id.startswith('kt10') or api_id.startswith('kt5000'): endpoint = '/api/dostk/ordr'
        elif api_id.startswith('kt00') or api_id.startswith('ka10075'): endpoint = '/api/dostk/acnt'
        elif api_id.startswith('ka10080'): endpoint = '/api/dostk/chart' 
        elif api_id.startswith('ka10001'): endpoint = '/api/dostk/stkinfo'
        elif api_id.startswith('ka10004'): endpoint = '/api/dostk/mrkcond'
        elif api_id.startswith('ka10074'): endpoint = '/api/dostk/acnt' # 일자별실현손익
        else: endpoint = '/api/dostk/stkinfo'
            
        url = KIWOOM_HOST_URL + endpoint
        
        # 🌟 [수정] 연속 조회 키를 Body가 아닌 Header에 설정해야 함 (API 문서 참조)
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',
            'authorization': f"Bearer {token}",
            'api-id': api_id,
            'cont-yn': cont_yn,
            'next-key': next_key
        }

        start_time = time.time() # ⏱️ 소요 시간 측정 시작
        try:
            if CURRENT_DEBUG_MODE:
                logger.debug(f"📤 [REQ] {api_id} Params: {params} | Head(Next): {next_key}")

            response = API_SESSION.post(url, headers=headers, json=params, timeout=10)
            
            duration = (time.time() - start_time) * 1000 # ms 단위

            if CURRENT_DEBUG_MODE:
                data_len = len(response.text) if response.text else 0
                logger.debug(f"📥 [RES] {response.status_code} ({duration:.0f}ms) Size: {data_len}B")

            # 429: 속도 제한
            if response.status_code == 429:
                new_interval = RATE_LIMITER.report_429()
                wait_time = 2.0 * (retry_count + 1)
                logger.warning(f"🔥 [429] 속도제한! 간격 {new_interval:.2f}s로 증가, {wait_time}s 대기")
                time.sleep(wait_time)
                if retry_count < 1:
                    return _call_api(api_id, params, retry_count + 1, is_high_priority, cont_yn, next_key, return_headers)
                return None

            # 401/403: 토큰 만료
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
            
            # 🌟 [수정] 헤더 반환이 필요한 경우 처리 (연속 조회용)
            if return_headers:
                return response.json(), response.headers
            return response.json()

        except Exception as e:
            logger.error(f"API 호출 중 오류 (TR: {api_id}): {e}")
            return None

# ---------------------------------------------------------
# 3. 계좌 관련 API
# ---------------------------------------------------------
def fn_kt00018_get_account_balance():
    """ 계좌 잔고 및 보유 종목 조회 """
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
    """ 예수금 상세 조회 """
    params = { "acnt_no": KIWOOM_ACCOUNT_NO, "qry_tp": "2" }
    response_data = _call_api(api_id="kt00001", params=params)
    if response_data:
        try:
            # 응답 키가 다양할 수 있어 순차적 확인
            deposit = (response_data.get('mny_ord_able_amt') or 
                       response_data.get('ord_psbl_amt') or 
                       response_data.get('entr'))
            return _safe_int(deposit)
        except Exception: return 0
    return 0

def fn_ka10074_get_daily_profit():
    """ 
    [일자별 실현손익] 조회 (ka10074)
    - 문서 184p 참조: Body Parameter (strt_dt, end_dt)
    """
    today_str = datetime.now().strftime('%Y%m%d')
    
    params = { 
        "strt_dt": today_str, 
        "end_dt": today_str,
        "stk_cd": "",  # 전체 조회
    }
    
    response_data = _call_api(api_id="ka10074", params=params)
    
    if response_data:
        try:
            # 1. 단일 응답 확인 (rlzt_pl)
            profit = response_data.get('rlzt_pl')
            if profit is not None:
                return _safe_int(profit)
            
            # 2. 리스트 응답 확인 (dt_rlzt_pl -> tdy_sel_pl)
            data_list = response_data.get('dt_rlzt_pl', [])
            if data_list and len(data_list) > 0:
                return _safe_int(data_list[0].get('tdy_sel_pl', 0))

        except Exception as e:
            logger.error(f"일자별 손익 파싱 실패: {e}")
            return None
            
    return None

# ---------------------------------------------------------
# 4. 시세 및 정보 API
# ---------------------------------------------------------
def fn_ka10001_get_stock_info(stock_code: str):
    """ 주식 기본 정보 (현재가, 기준가 등) 조회 """
    params = { "stk_cd": stock_code }
    response_data = _call_api(api_id="ka10001", params=params)
    if response_data:
        try:
            # 💡 [수정] 시가, 예상체결가 추가 파싱
            info = {
                "종목코드": response_data.get('stk_cd'),
                "종목명": response_data.get('stk_nm'),
                "현재가": _safe_int(response_data.get('cur_prc')), # ka10001은 보통 cur_prc 사용
                "기준가": _safe_int(response_data.get('std_prc') or response_data.get('bf_cls_prc')),
                
                # 🌟 수정된 부분: open_pric이 없으면 open_prc를 찾음
                "시가": _safe_int(response_data.get('open_pric') or response_data.get('open_prc')),
                "예상체결가": _safe_int(response_data.get('exp_cntr_pric') or response_data.get('exp_cntr_prc'))
            }
            return info
        except Exception: return None
    return None

def fn_kt10000_buy_order(stock_code: str, quantity: int, price: int = 0):
    """ 현금 매수 주문 """
    # 🌟 [수정] 키움 API 표준: 지정가("00"), 시장가("03")
    trade_type = "03" if price == 0 else "00" 
    params = {
        "acnt_no": KIWOOM_ACCOUNT_NO,
        "dmst_stex_tp": "KRX", 
        "stk_cd": stock_code, 
        "ord_qty": str(quantity),
        "ord_uv": str(price), 
        "trde_tp": trade_type, 
        "cond_uv": ""
    }
    if MOCK_TRADE: time.sleep(0.1)
    response_data = _call_api(api_id="kt10000", params=params)
    if response_data and response_data.get('ord_no'):
        return response_data.get('ord_no')
    return None

def fn_kt10001_sell_order(stock_code: str, quantity: int, price: int = 0):
    """ 현금 매도 주문 """
    trade_type = "03" if price == 0 else "00"
    params = {
        "acnt_no": KIWOOM_ACCOUNT_NO,
        "dmst_stex_tp": "KRX", 
        "stk_cd": stock_code, 
        "ord_qty": str(quantity),
        "ord_uv": str(price), 
        "trde_tp": trade_type, 
        "cond_uv": ""
    }
    if MOCK_TRADE: time.sleep(0.1)
    response_data = _call_api(api_id="kt10001", params=params)
    if response_data and response_data.get('ord_no'):
        return response_data.get('ord_no')
    return None

def fn_kt10003_cancel_order(stock_code: str, quantity: int, orgn_ord_no: str, is_buy: bool):
    """ 미체결 주문 취소 """
    trde_tp = "03" if is_buy else "04" # 03: 매수취소, 04: 매도취소
    api_id_to_use = "kt10003"
    params = {
        "acnt_no": KIWOOM_ACCOUNT_NO,
        "dmst_stex_tp": "KRX",
        "stk_cd": stock_code,
        "ord_qty": str(quantity),
        "ord_uv": "0", 
        "trde_tp": trde_tp,
        "orgn_ord_no": str(orgn_ord_no), 
        "cond_uv": ""
    }
    if MOCK_TRADE: time.sleep(0.1)
    response_data = _call_api(api_id=api_id_to_use, params=params)
    if response_data and response_data.get('ord_no'):
        return response_data.get('ord_no')
    return None

def fn_ka10004_get_hoga(stock_code: str):
    """ 주식 호가(매수/매도 잔량) 조회 """
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
        except Exception as e:
            logger.error(f"호가 데이터 파싱 실패: {e}")
            return None
    return None

def fn_ka10080_get_minute_chart(stock_code: str, tick: str = "3"):
    """ 
    3분봉 차트 조회 (백테스팅용, 최대 30페이지) 
    🌟 [수정] Header의 next-key를 이용한 올바른 페이징 구현
    """
    MAX_PAGES = 30
    all_chart_data = []
    
    current_next_key = ""
    current_cont_yn = "N"
    
    for page in range(MAX_PAGES):
        # API 문서에 따르면 next-key는 파라미터가 아니라 헤더로 보내야 함
        params = { "stk_cd": stock_code, "tic_scope": tick, "upd_stkpc_tp": "1", "date_type": "1" }
        
        if page > 0: time.sleep(0.3) 
        
        # _call_api를 통해 헤더까지 같이 받음
        result = _call_api(
            api_id="ka10080", 
            params=params, 
            is_high_priority=False,
            cont_yn=current_cont_yn,
            next_key=current_next_key,
            return_headers=True # 헤더 요청
        )
        
        if not result: break
        
        response_data, response_headers = result
        
        if response_data:
            chart_data = (response_data.get('stk_min_pole_chart_qry') or response_data.get('output2') or [])
            if chart_data:
                all_chart_data.extend(chart_data)
                
                # 응답 헤더에서 다음 키 추출
                # 키움 API 응답 헤더 키는 소문자일 수도 있으니 주의
                current_next_key = response_headers.get('next-key', '').strip()
                current_cont_yn = response_headers.get('cont-yn', 'N').strip()
                
                if not current_next_key or current_cont_yn != 'Y': 
                    break
            else: break
        else: break
            
    return all_chart_data if all_chart_data else None

def create_master_stock_file():
    """ 마스터 종목 파일 다운로드 및 갱신 (하루 1회) """
    file_path = "/data/master_stocks.json"
    
    if os.path.exists(file_path):
        try:
            creation_time = os.path.getmtime(file_path)
            creation_dt = datetime.fromtimestamp(creation_time)
            # 오늘 날짜보다 이전이면 삭제 후 갱신
            if creation_dt.date() < datetime.now().date():
                logger.info(f"🔄 마스터 파일이 오래되어({creation_dt.date()}) 삭제 후 갱신합니다.")
                os.remove(file_path)
            else:
                return # 최신이면 패스
        except Exception as e:
            logger.warning(f"마스터 파일 날짜 확인 중 오류: {e}")

    try:
        logger.info("📚 마스터 종목 파일을 다운로드합니다...")
        df_kospi = fdr.StockListing('KOSPI'); df_kosdaq = fdr.StockListing('KOSDAQ')
        master_dict = {row['Code']: row['Name'] for _, row in df_kospi.iterrows()}
        master_dict.update({row['Code']: row['Name'] for _, row in df_kosdaq.iterrows()})
        with open(file_path, 'w', encoding='utf-8') as f: json.dump(master_dict, f, ensure_ascii=False)
        logger.info("✅ 마스터 파일 생성 완료.")
    except Exception as e: logger.error(f"마스터 파일 생성 실패: {e}")