import requests
import json
import os
import logging
import traceback
from datetime import datetime, timedelta

from config import (
    KIWOOM_HOST_URL, KIWOOM_REST_API_KEY, KIWOOM_SECRET, 
    MOCK_TRADE, DEBUG_MODE
)

# DB 모듈 임포트
from database import db

# 로거 설정
login_logger = logging.getLogger("Login")
login_logger.setLevel(logging.INFO)

def save_token_to_db(token_data):
    """ 토큰 정보를 DB에 저장합니다. """
    key = "token_mock" if MOCK_TRADE else "token_real"
    try:
        db.set_kv(key, token_data)
        if DEBUG_MODE: login_logger.debug(f"토큰 DB 저장 완료 ({key})")
    except Exception as e:
        login_logger.error(f"토큰 저장 실패: {e}")

def _migrate_token_file_to_db(key):
    """ [복구용] 기존 JSON 파일에 있는 토큰을 DB로 마이그레이션 합니다. """
    try:
        filename = "token_mock.json" if "mock" in key else "token_real.json"
        
        # 가능한 파일 경로들 확인
        candidates = [
            os.path.join("/data", filename),
            os.path.join(os.getcwd(), filename),
            os.path.join("/data/kiwoom_bot_data", filename),
            f"/app/{filename}"
        ]
        
        for p in candidates:
            if os.path.exists(p):
                try:
                    with open(p, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        # 유효성 검사 (토큰과 만료시간이 있는지)
                        if data.get('token') and data.get('expires_at'):
                            # 만료 시간 체크
                            expires_at = datetime.strptime(data['expires_at'], '%Y-%m-%d %H:%M:%S')
                            if datetime.now() < expires_at:
                                save_token_to_db(data)
                                login_logger.info(f"♻️ [마이그레이션] 기존 토큰 파일({filename})을 DB로 복구했습니다.")
                                return data
                            else:
                                login_logger.warning(f"⚠️ 기존 토큰 파일({filename})이 만료되어 마이그레이션 하지 않습니다.")
                except Exception:
                    continue
    except Exception as e:
        login_logger.warning(f"토큰 마이그레이션 중 오류: {e}")
    return None

def load_token_from_db():
    """ DB에서 유효한 토큰을 불러옵니다. """
    key = "token_mock" if MOCK_TRADE else "token_real"
    
    token_data = None
    try:
        token_data = db.get_kv(key)
    except Exception: pass
    
    # DB에 없으면 파일에서 마이그레이션 시도
    if not token_data:
        token_data = _migrate_token_file_to_db(key)

    if token_data:
        try:
            expires_at = datetime.strptime(token_data['expires_at'], '%Y-%m-%d %H:%M:%S')
            # 만료 10분 전까지만 유효한 것으로 간주
            if datetime.now() < expires_at - timedelta(minutes=10):
                return token_data['token']
            else:
                login_logger.info("db 토큰 만료됨")
        except Exception as e:
            login_logger.error(f"토큰 검증 오류: {e}")

    return None

def clear_token_cache():
    """ 만료된 토큰을 DB에서 삭제(초기화)합니다. """
    key = "token_mock" if MOCK_TRADE else "token_real"
    try:
        db.set_kv(key, {}) # 빈 값으로 덮어쓰기
        login_logger.info("토큰 캐시가 초기화되었습니다.")
    except Exception: pass

def fn_au10001():
    """
    [OAuth 인증] 토큰 발급 (au10001)
    - DB에 유효한 토큰이 있으면 재사용
    - 없으면 API 호출하여 신규 발급
    """
    # 1. 캐시된 토큰 확인 (DB + 파일 마이그레이션 포함)
    cached_token = load_token_from_db()
    if cached_token:
        login_logger.info("📂 유효한 토큰을 로드했습니다.")
        return cached_token

    # 2. API 호출
    url = f"{KIWOOM_HOST_URL}/oauth2/token"
    
    headers = {
        "Content-Type": "application/json"
    }
    payload = {
        "grant_type": "client_credentials",
        "appkey": KIWOOM_REST_API_KEY,
        "appsecret": KIWOOM_SECRET
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            access_token = data.get('access_token')
            expires_in = data.get('expires_in', 86400) # 기본 24시간
            
            if access_token:
                expires_at = datetime.now() + timedelta(seconds=int(expires_in))
                expires_str = expires_at.strftime('%Y-%m-%d %H:%M:%S')
                
                save_token_to_db({
                    "token": access_token, 
                    "expires_at": expires_str
                })
                
                login_logger.info(f"✨ 새 토큰 발급 완료 (만료: {expires_str})")
                return access_token
            else:
                # 200 OK지만 토큰이 없는 경우 (에러 메시지 로깅)
                login_logger.error(f"❌ 토큰 응답 내용 오류: {json.dumps(data, ensure_ascii=False)}")
        
        else:
            login_logger.error(f"토큰 발급 실패 (Status: {response.status_code})")
            login_logger.error(f"응답 본문: {response.text}")
        
    except Exception as e:
        login_logger.error(f"인증 요청 중 오류: {e}")
        login_logger.debug(traceback.format_exc())
        
    return None

if __name__ == "__main__":
    token = fn_au10001()
    print("Token:", token)