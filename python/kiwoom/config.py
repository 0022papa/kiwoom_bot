import os
import json
import time
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
# 2. 파일 경로 설정
# ---------------------------------------------------------
DATA_DIR = "/data"
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# ---------------------------------------------------------
# 3. 설정 로드 (우선순위: settings.json > .env)
# ---------------------------------------------------------
# 기본값 (환경변수)
MOCK_TRADE = str_to_bool(os.getenv("MOCK_TRADE", "True"))
DEBUG_MODE = str_to_bool(os.getenv("DEBUG_MODE", "False"))

# settings.json 파일이 있다면 덮어쓰기 (Node.js 서버와 동기화)
if os.path.exists(SETTINGS_FILE):
    for _ in range(5):  # 최대 5회 재시도 (파일 I/O 충돌 방지)
        try:
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if not content: raise ValueError("Empty File")

                settings = json.loads(content)

                # 모의투자 여부 업데이트
                if "MOCK_TRADE" in settings:
                    MOCK_TRADE = str_to_bool(settings["MOCK_TRADE"])

                # 디버그 모드 업데이트
                if "DEBUG_MODE" in settings:
                    DEBUG_MODE = str_to_bool(settings["DEBUG_MODE"])
            break
        except Exception:
            time.sleep(0.1)

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