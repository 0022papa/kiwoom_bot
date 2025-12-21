import os
import json
import time
import sqlite3
from dotenv import load_dotenv

# ---------------------------------------------------------
# 1. 환경 변수 로드 및 헬퍼 함수
# ---------------------------------------------------------
load_dotenv()

def str_to_bool(val):
    """ 문자열/숫자 값을 Boolean으로 변환 (True/False) """
    if val is None: return False
    return str(val).lower() in ('true', '1', 't', 'yes', 'on')

# ---------------------------------------------------------
# 2. 파일 및 DB 경로 설정
# ---------------------------------------------------------
DATA_DIR = "/data"
DB_PATH = os.path.join(DATA_DIR, "kiwoom_bot.db")

# ---------------------------------------------------------
# 3. 설정 로드 (우선순위: DB > .env)
# ---------------------------------------------------------
# 기본값 (환경변수)
MOCK_TRADE = str_to_bool(os.getenv("MOCK_TRADE", "True"))
DEBUG_MODE = str_to_bool(os.getenv("DEBUG_MODE", "False"))

# 🌟 [수정] 파일 대신 DB에서 설정 로드
if os.path.exists(DB_PATH):
    try:
        # config.py는 의존성 문제 방지를 위해 sqlite3 직접 연결
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM kv_store WHERE key='settings'")
        row = cursor.fetchone()
        
        if row:
            settings = json.loads(row[0])

            # 모의투자 여부 업데이트
            if "MOCK_TRADE" in settings:
                MOCK_TRADE = str_to_bool(settings["MOCK_TRADE"])

            # 디버그 모드 업데이트
            if "DEBUG_MODE" in settings:
                DEBUG_MODE = str_to_bool(settings["DEBUG_MODE"])
        conn.close()
    except Exception as e:
        print(f"[Config] ⚠️ DB 설정 로드 실패 (기본값 사용): {e}")

# ---------------------------------------------------------
# 4. 텔레그램 설정
# ---------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---------------------------------------------------------
# 5. 키움 API 설정 (모의/실전 분기)
# ---------------------------------------------------------
if MOCK_TRADE:
    KIWOOM_REST_API_KEY = os.getenv("MOCK_KIWOOM_REST_API_KEY")
    KIWOOM_SECRET = os.getenv("MOCK_KIWOOM_SECRET")
    KIWOOM_HOST_URL = os.getenv("MOCK_KIWOOM_HOST_URL")
    KIWOOM_SOCKET_URL = os.getenv("MOCK_KIWOOM_SOCKET_URL")
    KIWOOM_ACCOUNT_NO = os.getenv("MOCK_KIWOOM_ACCOUNT_NO")
    MODE_MSG = "🟢 모의투자 (Virtual)"
else:
    KIWOOM_REST_API_KEY = os.getenv("REAL_KIWOOM_REST_API_KEY")
    KIWOOM_SECRET = os.getenv("REAL_KIWOOM_SECRET")
    KIWOOM_HOST_URL = os.getenv("REAL_KIWOOM_HOST_URL")
    KIWOOM_SOCKET_URL = os.getenv("REAL_KIWOOM_SOCKET_URL")
    KIWOOM_ACCOUNT_NO = os.getenv("REAL_KIWOOM_ACCOUNT_NO")
    MODE_MSG = "🔴 실전투자 (REAL)"

# ---------------------------------------------------------
# 6. 설정 상태 출력
# ---------------------------------------------------------
print(f"[Config] ⚙️  투자 모드: {MODE_MSG} | 계좌: {KIWOOM_ACCOUNT_NO}")
if DEBUG_MODE:
    print("[Config] 🕵️  디버그 모드: ON (상세 로그가 출력됩니다)")