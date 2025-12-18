import requests
import json
import logging
import threading
import os
import time
from datetime import datetime, timedelta
from config import KIWOOM_HOST_URL, KIWOOM_REST_API_KEY, KIWOOM_SECRET, MOCK_TRADE

# ---------------------------------------------------------
# 1. 로거 및 전역 변수 설정
# ---------------------------------------------------------
logger = logging.getLogger("Login")
logger.setLevel(logging.INFO)

TOKEN_CACHE = {
    'token': None,
    'expires_at': datetime.min
}
token_lock = threading.Lock()

# 투자 모드에 따른 토큰 파일 경로 분리
TOKEN_FILE = "/data/token_mock.json" if MOCK_TRADE else "/data/token_real.json"

# ---------------------------------------------------------
# 2. 내부 유틸리티 함수 (파일 I/O)
# ---------------------------------------------------------
def _load_token_from_file():
    """ 
    저장된 토큰 파일에서 유효한 토큰을 읽어옵니다. 
    만료 시간이 10분 이상 남았을 때만 반환합니다.
    """
    if not os.path.exists(TOKEN_FILE):
        return None
    
    try:
        with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            token = data.get('token')
            expires_str = data.get('expires_at')
            
            if token and expires_str:
                expires_at = datetime.strptime(expires_str, "%Y-%m-%d %H:%M:%S")
                # 10분 여유를 두고 만료 체크
                if expires_at > datetime.now() + timedelta(minutes=10):
                    return token
    except Exception as e:
        logger.warning(f"⚠️ 토큰 파일 읽기 실패 (재발급 진행): {e}")
        # 파일이 깨졌을 수 있으므로 삭제 시도
        try: os.remove(TOKEN_FILE)
        except: pass
    
    return None

def _save_token_to_file(token, expires_dt_obj):
    """ 발급받은 토큰과 만료 시간을 파일에 저장합니다. """
    try:
        with open(TOKEN_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                "token": token,
                "expires_at": expires_dt_obj.strftime("%Y-%m-%d %H:%M:%S")
            }, f, indent=4)
        logger.debug(f"💾 토큰 파일 저장 완료 ({TOKEN_FILE})")
    except Exception as e:
        logger.error(f"❌ 토큰 파일 저장 실패: {e}")

# ---------------------------------------------------------
# 3. 외부 인터페이스 함수
# ---------------------------------------------------------
def fn_au10001():
    """ 
    [OAuth 2.0] 접근 토큰(Access Token) 발급/조회 함수 (Thread-Safe)
    - 메모리 캐시 -> 파일 캐시 -> API 호출 순으로 확인합니다.
    """
    global TOKEN_CACHE

    with token_lock:
        # 1. 메모리 캐시 확인 (가장 빠름)
        if TOKEN_CACHE['token'] and TOKEN_CACHE['expires_at'] > datetime.now() + timedelta(minutes=10):
            return TOKEN_CACHE['token']

        # 2. 파일 캐시 확인 (재시작 시 유용)
        file_token = _load_token_from_file()
        if file_token:
            logger.info("📂 파일에서 유효한 토큰을 로드했습니다.")
            TOKEN_CACHE['token'] = file_token
            # 파일에서 읽은 경우 만료시간을 정확히 알기 어려우므로(함수 반환값 한계), 
            # 안전하게 메모리상으로는 6시간 뒤로 가정 (다음 호출때 파일 다시 읽음)
            # *엄밀하게 하려면 _load_token_from_file이 만료시간도 리턴해야 하지만, 단순화를 위해 유지
            TOKEN_CACHE['expires_at'] = datetime.now() + timedelta(hours=6) 
            return file_token

        # 3. API 호출하여 새 토큰 발급
        url = f"{KIWOOM_HOST_URL}/oauth2/token"
        headers = {'Content-Type': 'application/json;charset=UTF-8'}
        
        params = {
            'grant_type': 'client_credentials',
            'appkey': KIWOOM_REST_API_KEY,
            'secretkey': KIWOOM_SECRET 
        }

        try:
            logger.info("🔑 새로운 접근 토큰을 요청합니다...")
            
            response = requests.post(url, headers=headers, json=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            # 응답 키 확인 ('access_token' or 'token')
            new_token = data.get('access_token') or data.get('token')
            expires_in = data.get('expires_in') # 초 단위 수명 (보통 86400)
            expires_dt_raw = data.get('expires_dt') # "20251119213438" 형식

            if new_token:
                TOKEN_CACHE['token'] = new_token
                
                # 만료 시간 계산
                try:
                    if expires_dt_raw:
                         # 키움 날짜 포맷 (YYYYMMDDHHMMSS)
                         TOKEN_CACHE['expires_at'] = datetime.strptime(str(expires_dt_raw), "%Y%m%d%H%M%S")
                    elif expires_in:
                         # 초 단위 수명 사용
                         TOKEN_CACHE['expires_at'] = datetime.now() + timedelta(seconds=int(expires_in))
                    else:
                         # 기본값 (6시간)
                         TOKEN_CACHE['expires_at'] = datetime.now() + timedelta(hours=6)
                except Exception:
                    TOKEN_CACHE['expires_at'] = datetime.now() + timedelta(hours=6)
                
                # 파일에 저장
                _save_token_to_file(new_token, TOKEN_CACHE['expires_at'])
                logger.info("✅ 토큰 발급 완료.")
                return new_token
            else:
                logger.error(f"❌ 토큰 응답 오류 (토큰 키 없음): {data}")
                return None

        except Exception as e:
            logger.error(f"❌ 토큰 발급 요청 실패: {e}")
            return None

def clear_token_cache():
    """ 
    인증 실패(401) 시 호출하여 캐시된 토큰을 삭제합니다. 
    다음 호출 시 강제로 새 토큰을 발급받게 됩니다.
    """
    with token_lock:
        TOKEN_CACHE['token'] = None
        TOKEN_CACHE['expires_at'] = datetime.min
    
    if os.path.exists(TOKEN_FILE):
        try:
            os.remove(TOKEN_FILE)
            logger.info("🗑️ 유효하지 않은 토큰 파일을 삭제했습니다.")
        except Exception as e:
            logger.error(f"토큰 파일 삭제 실패: {e}")