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

def load_token_from_db():
    """ DB에서 유효한 토큰을 불러옵니다. """
    key = "token_mock" if MOCK_TRADE else "token_real"
    
    token_data = None
    try:
        token_data = db.get_kv(key)
    except Exception: pass
    
    if token_data:
        try:
            expires_at = datetime.strptime(token_data['expires_at'], '%Y-%m-%d %H:%M:%S')
            if datetime.now() < expires_at - timedelta(minutes=10):
                return token_data['token']
        except Exception as e:
            login_logger.error(f"토큰 검증 오류: {e}")

    return None

def clear_token_cache():
    key = "token_mock" if MOCK_TRADE else "token_real"
    try: db.set_kv(key, {}) 
    except Exception: pass

def fn_au10001():
    """
    [OAuth 인증] 토큰 발급
    """
    # 0. API 키 누락 확인
    if not KIWOOM_REST_API_KEY or not KIWOOM_SECRET:
        login_logger.error("❌ [오류] API Key 또는 Secret이 설정되지 않았습니다! config 로그를 확인하세요.")
        return None

    # 1. 캐시 확인
    cached_token = load_token_from_db()
    if cached_token:
        login_logger.info("📂 유효한 토큰을 로드했습니다.")
        return cached_token

    # 2. API 호출
    url = f"{KIWOOM_HOST_URL}/oauth2/token"
    headers = { "Content-Type": "application/json" }
    
    # 실전투자 API에 맞춘 파라미터 (secretkey)
    payload = {
        "grant_type": "client_credentials",
        "appkey": KIWOOM_REST_API_KEY,
        "secretkey": KIWOOM_SECRET 
    }
    
    # (디버깅용) 키 마스킹 후 페이로드 구조 출력
    safe_payload = payload.copy()
    safe_payload['appkey'] = (payload['appkey'][:5] + "...") if payload['appkey'] else "None"
    safe_payload['secretkey'] = "******" if payload['secretkey'] else "None"
    login_logger.info(f"📤 토큰 발급 요청: {safe_payload}")

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            # 🌟 [수정] access_token 또는 token 키 모두 확인
            access_token = data.get('access_token') or data.get('token')
            expires_str = None

            # 🌟 [수정] 만료 시간 형식 처리 (초 단위 vs 날짜 문자열)
            if 'expires_in' in data:
                # Case A: 초 단위 (예: 86400)
                expires_in = int(data['expires_in'])
                expires_at = datetime.now() + timedelta(seconds=expires_in)
                expires_str = expires_at.strftime('%Y-%m-%d %H:%M:%S')
            elif 'expires_dt' in data:
                # Case B: 날짜 문자열 (예: 20251222234954)
                try:
                    dt_str = data['expires_dt']
                    expires_at = datetime.strptime(dt_str, '%Y%m%d%H%M%S')
                    expires_str = expires_at.strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    login_logger.warning(f"만료시간 포맷 파싱 실패({data.get('expires_dt')}), 기본값(24h) 사용")
            
            # 만료 시간을 못 구했으면 기본 24시간 설정
            if not expires_str:
                expires_at = datetime.now() + timedelta(hours=24)
                expires_str = expires_at.strftime('%Y-%m-%d %H:%M:%S')

            if access_token:
                save_token_to_db({ "token": access_token, "expires_at": expires_str })
                login_logger.info(f"✨ 새 토큰 발급 완료 (만료: {expires_str})")
                return access_token
            else:
                login_logger.error(f"❌ 토큰 응답 내용 오류: {json.dumps(data, ensure_ascii=False)}")
        else:
            login_logger.error(f"토큰 발급 실패 (Status: {response.status_code})")
            login_logger.error(f"응답: {response.text}")
        
    except Exception as e:
        login_logger.error(f"인증 요청 중 오류: {e}")
        login_logger.debug(traceback.format_exc())
        
    return None

if __name__ == "__main__":
    token = fn_au10001()
    if token: print("Token 발급 성공")
    else: print("Token 발급 실패")