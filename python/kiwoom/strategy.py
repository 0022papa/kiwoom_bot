import sys
import time
import logging
import json
import os
import asyncio
import traceback
import signal
import hashlib
import queue
import exchange_calendars as xcals
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from functools import partial
from ai_analyst import create_chart_image, ask_ai_to_buy

# 기존 동기식 API 함수들 임포트
from api_v1 import (
    create_master_stock_file,
    fn_kt00018_get_account_balance,
    fn_kt00001_get_deposit,
    fn_ka10001_get_stock_info,
    fn_kt10000_buy_order,
    fn_kt10001_sell_order,
    fn_kt10003_cancel_order,
    fn_ka10004_get_hoga,
    fn_ka10080_get_minute_chart,
    fn_ka10074_get_daily_profit,
    set_api_debug_mode
)
from config import MOCK_TRADE, KIWOOM_ACCOUNT_NO, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from websocket_manager import KiwoomWebSocketManager
from backtesting import run_simulation_for_list

# ---------------------------------------------------------
# 🌟 [신규] 비동기 속도 제한 클래스 (Proactive Rate Limiter)
# ---------------------------------------------------------
class AsyncRateLimiter:
    def __init__(self, max_calls, period=1.0):
        self.max_calls = max_calls
        self.period = period
        self.timestamps = deque()

    async def wait(self):
        while True:
            now = time.time()
            # 기간 지난 기록 제거
            while self.timestamps and now - self.timestamps[0] > self.period:
                self.timestamps.popleft()
            
            if len(self.timestamps) < self.max_calls:
                self.timestamps.append(now)
                return
            
            # 제한에 걸리면 잠시 대기
            await asyncio.sleep(0.1)

# 🌟 전역 제한 설정: 초당 4회 호출로 제한 (키움 권장: 초당 5회 미만)
GLOBAL_API_LIMITER = AsyncRateLimiter(max_calls=4, period=1.0)
ANALYSIS_SEMAPHORE = asyncio.Semaphore(5) # 동시 분석 종목 수

# ---------------------------------------------------------
# 1. 시스템 환경 설정
# ---------------------------------------------------------
os.environ['TZ'] = 'Asia/Seoul'
try:
    time.tzset()
except AttributeError:
    pass

# 로거 설정 (기본 레벨 INFO)
strategy_logger = logging.getLogger("Strategy")
strategy_logger.setLevel(logging.INFO)

# ---------------------------------------------------------
# 2. 파일 경로 및 전역 변수 설정
# ---------------------------------------------------------
DATA_DIR = "/data"
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
STATUS_FILE = os.path.join(DATA_DIR, "status.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.log")
CURRENT_CONDITIONS_FILE = os.path.join(DATA_DIR, "current_conditions.json")
CONDITIONS_NAME_FILE = os.path.join(DATA_DIR, "conditions.json")

# asyncio 큐 사용
TELEGRAM_QUEUE = asyncio.Queue()

# 실현손익 관련 전역 변수
TODAY_REALIZED_PROFIT = 0
LAST_PROFIT_CHECK_TIME = datetime.min

# 🌟 [최적화] 조건식 이름 캐싱용 전역 변수 (파일 I/O 병목 제거)
CACHED_CONDITION_NAMES = {}

# 🌟 [신규] 동시 분석 제한용 세마포어 (너무 많은 동시 AI/API 요청 방지)
ANALYSIS_SEMAPHORE = asyncio.Semaphore(5)  # 동시에 최대 5종목 분석

# ---------------------------------------------------------
# 3. 전략 및 봇 기본 설정
# ---------------------------------------------------------
STRATEGY_PRESETS = {
    "0": { "DESC": "오전급등(공격형)", "STOP_LOSS_RATE": -2.0, "TRAILING_START_RATE": 1.0, "TRAILING_STOP_RATE": -0.6, "RE_ENTRY_COOLDOWN_MIN": 60, "MIN_BUY_SELL_RATIO": 0.5 },
    "1": { "DESC": "눌림목(안정형)", "STOP_LOSS_RATE": -2.0, "TRAILING_START_RATE": 1.0, "TRAILING_STOP_RATE": -0.6, "RE_ENTRY_COOLDOWN_MIN": 30, "MIN_BUY_SELL_RATIO": 0.8 },
    "2": { "DESC": "종가베팅(오버나잇)", "STOP_LOSS_RATE": -2.0, "TRAILING_START_RATE": 1.0, "TRAILING_STOP_RATE": -0.6, "RE_ENTRY_COOLDOWN_MIN": 0, "MIN_BUY_SELL_RATIO": 0.5 }
}

DEFAULT_SETTINGS = {
    "BOT_STATUS": "STOPPED",
    "MOCK_TRADE": MOCK_TRADE,
    "CONDITION_ID": "0",
    "ORDER_AMOUNT": 100000,
    "STOP_LOSS_RATE": -1.5,
    "TRAILING_START_RATE": 1.5,
    "TRAILING_STOP_RATE": -1.0,
    "RE_ENTRY_COOLDOWN_MIN": 30,
    "USE_MARKET_TIME": True,
    "USE_AUTO_SELL": False,
    "USE_TELEGRAM": True,
    "DEBUG_MODE": False,
    "USE_SCHEDULER": True,
    "MORNING_START": "08:50", "MORNING_COND": "0",
    "LUNCH_START": "11:30", "LUNCH_COND": "1",
    "AFTERNOON_START": "15:10", "AFTERNOON_COND": "2",
    "USE_HOGA_FILTER": True,
    "MIN_BUY_SELL_RATIO": 0.5,
    "OVERNIGHT_COND_IDS": "2"
}
BOT_SETTINGS = DEFAULT_SETTINGS.copy()

# 런타임 상태 변수
TRADING_STATE = {}
RE_ENTRY_COOLDOWN = {}
PROCESSING_STOCKS = set()
LAST_PRICE_CHECK_TIME = {}
LAST_API_CALL_TIME = {}
PENDING_ORDER_CONDITIONS = {}
BUY_ATTEMPT_HISTORY = {}

BOT_START_TIME = datetime.now()
ws_manager = None
last_heartbeat_time = datetime.min
IS_INITIALIZED = False
last_saved_state_hash = ""

# ---------------------------------------------------------
# 4. 비동기 헬퍼 함수 (핵심)
# ---------------------------------------------------------
async def run_blocking(func, *args, **kwargs):
    """
    동기(Blocking) 함수를 별도 스레드 풀에서 실행하여
    asyncio 루프가 멈추지 않게 합니다.
    """
    loop = asyncio.get_running_loop()
    func_call = partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, func_call)

def debug_log(msg):
    if BOT_SETTINGS.get("DEBUG_MODE", False):
        strategy_logger.debug(f"🕵️ [DEBUG] {msg}")

def parse_price(price_str):
    try:
        if price_str is None: return 0
        clean_str = str(price_str).strip().replace('+', '').replace('-', '')
        if not clean_str: return 0
        return int(clean_str)
    except ValueError: return 0

# 🌟 [최적화] 조건식 이름 로드 함수 (파일 읽기 최소화)
async def load_condition_names():
    global CACHED_CONDITION_NAMES
    try:
        if await run_blocking(os.path.exists, CONDITIONS_NAME_FILE):
            def _read_cond_names():
                with open(CONDITIONS_NAME_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return {str(c['id']): c['name'] for c in data.get('conditions', [])}
            CACHED_CONDITION_NAMES = await run_blocking(_read_cond_names)
            strategy_logger.info(f"📁 [캐시] 조건식 이름 로드 완료 ({len(CACHED_CONDITION_NAMES)}개)")
    except Exception as e:
        strategy_logger.error(f"조건식 이름 로드 실패: {e}")

# ---------------------------------------------------------
# 5. 텔레그램 및 리포트 (사진전송 + 리포트 로직 수정)
# ---------------------------------------------------------
async def _telegram_worker():
    """ 텔레그램 메시지(텍스트/사진) 전송 비동기 워커 """
    import requests
    
    # 동기식 사진 전송 함수 (스레드 내부 실행용)
    def _send_photo_sync(token, chat_id, photo_path, caption):
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        with open(photo_path, 'rb') as f:
            files = {'photo': f}
            data = {'chat_id': chat_id, 'caption': caption, 'parse_mode': 'HTML'}
            requests.post(url, data=data, files=files, timeout=10)
            
    while True:
        try:
            item = await TELEGRAM_QUEUE.get()
            if item is None: break

            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                try:
                    # 1. 텍스트 메시지인 경우
                    if isinstance(item, str):
                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                        params = {"chat_id": TELEGRAM_CHAT_ID, "text": item, "parse_mode": "HTML"}
                        await run_blocking(requests.get, url, params=params, timeout=5)
                    
                    # 2. 사진 메시지인 경우 (딕셔너리 형태)
                    elif isinstance(item, dict) and item.get('type') == 'photo':
                        path = item.get('path')
                        caption = item.get('caption')
                        if path and os.path.exists(path):
                            await run_blocking(_send_photo_sync, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, path, caption)
                            # 전송 후 이미지 삭제
                            try: os.remove(path)
                            except: pass
                            
                except Exception as e:
                    strategy_logger.error(f"텔레그램 전송 실패: {e}")

            TELEGRAM_QUEUE.task_done()
            await asyncio.sleep(1.0) # Rate Limit 방지
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(1)

def send_telegram_msg(msg):
    """ 텍스트 메시지를 큐에 넣습니다. """
    if not BOT_SETTINGS.get("USE_TELEGRAM", True): return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        TELEGRAM_QUEUE.put_nowait(msg)
    except Exception: pass

def send_telegram_photo(path, caption):
    """ 사진 메시지(캡션 포함)를 큐에 넣습니다. """
    if not BOT_SETTINGS.get("USE_TELEGRAM", True): return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        TELEGRAM_QUEUE.put_nowait({'type': 'photo', 'path': path, 'caption': caption})
    except Exception: pass

async def send_daily_report():
    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        server_profit = await run_blocking(fn_ka10074_get_daily_profit)

        total_buy_cnt = 0; total_sell_cnt = 0; win_cnt = 0; loss_cnt = 0; log_profit = 0

        if await run_blocking(os.path.exists, TRADES_FILE):
            def read_log():
                with open(TRADES_FILE, 'r', encoding='utf-8') as f:
                    return f.readlines()
            lines = await run_blocking(read_log)

            for line in lines:
                if not line.startswith(f"[{today_str}"): continue
                if "BUY:" in line: total_buy_cnt += 1
                if "SELL:" in line:
                    total_sell_cnt += 1
                    try:
                        if "수익률: " in line:
                            parts = line.split("수익률: ")
                            if len(parts) > 1:
                                rate = float(parts[1].split('%')[0])
                                if rate > 0: win_cnt += 1
                                else: loss_cnt += 1
                        if "손익금: " in line:
                            p_parts = line.split("손익금: ")
                            if len(p_parts) > 1:
                                log_profit += int(p_parts[1].strip())
                    except: pass

        # 매매 내역이 없어도 리포트 전송
        final_profit = server_profit if server_profit is not None else log_profit
        source_msg = "(서버 확정)" if server_profit is not None else "(예상 추정치)"

        win_rate = (win_cnt / total_sell_cnt * 100) if total_sell_cnt > 0 else 0
        profit_emoji = "🔴" if final_profit > 0 else "🔵"

        msg = (
            f"📅 <b>[일별 마감 리포트]</b> {today_str}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🛒 총 매수: {total_buy_cnt}건\n"
            f"👋 총 매도: {total_sell_cnt}건\n"
            f"🏆 승: {win_cnt} / ☠️ 패: {loss_cnt}\n"
            f"📊 승률: {win_rate:.1f}%\n"
            f"{profit_emoji} <b>실현손익: {final_profit:,}원</b>\n"
            f"<span class='text-xs text-gray-400'>{source_msg}</span>\n"
            f"━━━━━━━━━━━━━━\n"
            f"오늘 하루도 수고하셨습니다! ☕"
        )
        send_telegram_msg(msg)
        strategy_logger.info(f"일별 마감 리포트 전송 완료 (손익: {final_profit})")

    except Exception as e:
        strategy_logger.error(f"리포트 생성 실패: {e}")

async def log_trade(stock_code, stk_nm, action, qty, price, reason, profit_rate=0, profit_amt=0, peak_rate=0, image_path=None, ai_reason=None):
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        price_str = f"{price:,}"
        profit_str = f"{profit_rate:.2f}"
        
        # 🌟 [개선] JSONL 포맷으로 저장 (추후 대시보드 호환성 고려)
        log_msg = f"[{timestamp}] {action}: {stk_nm}({stock_code}), 수량: {qty}, 가격: {price_str}원, 사유: {reason}, 수익률: {profit_str}%, 손익금: {int(profit_amt)}\n"

        def _write_log():
            with open(TRADES_FILE, 'a', encoding='utf-8') as f: f.write(log_msg)
        await run_blocking(_write_log)

        print(f"📝 [매매기록] {action} {stk_nm} ({profit_str}%) - {reason}")

        emoji = "🔴 매수" if action == "BUY" else "🔵 매도"
        tg_msg = f"{emoji} <b>체결 알림</b>"
        
        if action == "BUY" and ai_reason:
            tg_msg += f"\n🤖 <b>AI분석:</b> {ai_reason}"
            
        tg_msg += f"\n사유: {reason}\n종목: {stk_nm} ({stock_code})\n가격: {price_str}원\n수량: {qty}주"

        if action == "SELL":
            res_emoji = "💰" if profit_rate > 0 else "💧"
            tg_msg += f"\n{res_emoji} 수익률: {profit_str}%"
            tg_msg += f"\n💵 손익금: {int(profit_amt):,}원"
            tg_msg += f"\n📈 최고점: {peak_rate:.2f}%"

        # 이미지가 있으면 사진 전송, 없으면 텍스트 전송
        if image_path:
            send_telegram_photo(image_path, tg_msg)
        else:
            send_telegram_msg(tg_msg)
            
    except Exception as e: strategy_logger.error(f"로그 작성 실패: {e}")

    # 🌟 [개선] 로그 파일 보존 기간 확대 (1MB -> 10MB)
    try:
        if await run_blocking(os.path.exists, TRADES_FILE):
             size = await run_blocking(os.path.getsize, TRADES_FILE)
             if size > 10 * 1024 * 1024: # 10MB
                backup_name = f"{TRADES_FILE}.{datetime.now().strftime('%Y%m%d%H%M%S')}.bak"
                await run_blocking(os.rename, TRADES_FILE, backup_name)
    except Exception: pass

# ---------------------------------------------------------
# 6. 핵심 로직 및 스케줄러
# ---------------------------------------------------------
def is_market_open():
    use_market_time = BOT_SETTINGS.get("USE_MARKET_TIME", True)
    if not use_market_time: return True

    try:
        now = datetime.now()
        current_time = now.time()
        
        start_time = datetime.strptime("09:00:00", "%H:%M:%S").time()
        end_time = datetime.strptime("15:20:00", "%H:%M:%S").time() 
        
        if current_time < start_time or current_time > end_time:
            return False

        xkrx = xcals.get_calendar("XKRX")
        if not xkrx.is_session(now.strftime("%Y-%m-%d")):
            return False

        return True

    except Exception as e:
        strategy_logger.error(f"장 운영 시간 확인 중 오류: {e}")
        if now.weekday() < 5:
            start = datetime.strptime("09:00:00", "%H:%M:%S").time()
            end = datetime.strptime("15:20:00", "%H:%M:%S").time()
            return start <= current_time <= end
        return False

# 🌟 [수정] 차트 패턴 정밀 분석 함수 (반환값: is_good, image_path, reason)
async def analyze_chart_pattern(stock_code):
    """
    Returns: (is_good, image_path, reason)
    """
    try:
        chart_data = await run_blocking(fn_ka10080_get_minute_chart, stock_code, tick="3")
        
        if not chart_data or len(chart_data) < 20:
            return True, None, None

        # [Step 1] 수식 기반 필터링
        last_candle = chart_data[1] 
        open_p = abs(int(last_candle.get('open_pric', 0)))
        close_p = abs(int(last_candle.get('cur_prc', 0)))
        high_p = abs(int(last_candle.get('high_pric', 0)))
        low_p = abs(int(last_candle.get('low_pric', 0)))
        
        if open_p == 0:
            strategy_logger.warning(f"⚠️ 차트 데이터 필드 오류 ({stock_code})")
            return True, None, None

        total_len = high_p - low_p
        upper_shadow = high_p - close_p if close_p > open_p else high_p - open_p
        
        if total_len > 0 and (upper_shadow / total_len) > 0.4:
            strategy_logger.info(f"🛡️ [1차필터] {stock_code}: 윗꼬리 과다 -> 진입 포기")
            return False, None, "1차필터(윗꼬리) 탈락"

        # [Step 2] AI 시각 분석
        stk_nm = "Stock"
        image_path = await run_blocking(create_chart_image, stock_code, stk_nm, chart_data)
        
        if image_path:
            is_buy, reason = await run_blocking(ask_ai_to_buy, image_path)
            
            if is_buy:
                strategy_logger.info(f"🤖 [AI승인] {stock_code}: 매수 추천! ({reason})")
                return True, image_path, reason
            else:
                strategy_logger.info(f"🛡️ [AI거절] {stock_code}: 매수 보류 ({reason})")
                try: os.remove(image_path)
                except: pass
                return False, None, reason
        
        return True, None, None

    except Exception as e:
        strategy_logger.error(f"차트 분석 중 오류 ({stock_code}): {e}")
        return True, None, None
        
async def apply_condition_preset(target_id):
    if target_id in STRATEGY_PRESETS:
        preset = STRATEGY_PRESETS[target_id]
        changed_msg = []

        for key, val in preset.items():
            if key == "DESC": continue
            if key in BOT_SETTINGS and BOT_SETTINGS[key] != val:
                BOT_SETTINGS[key] = val
                changed_msg.append(f"{key}: {val}")

        strategy_logger.info(f"🎨 [전략변경] 조건식 {target_id}번({preset['DESC']}) 설정 적용됨.")
        if changed_msg:
            debug_log(f"변경된 상세 설정: {', '.join(changed_msg)}")

        await save_settings_to_file()
        return True
    return False

async def check_auto_condition_change():
    if not BOT_SETTINGS.get('USE_SCHEDULER', False): return False
    try:
        now_time = datetime.now().time()
        current_id = str(BOT_SETTINGS.get('CONDITION_ID', '0'))

        m_start_str = BOT_SETTINGS.get('MORNING_START', '09:00')
        l_start_str = BOT_SETTINGS.get('LUNCH_START', '11:30')
        a_start_str = BOT_SETTINGS.get('AFTERNOON_START', '15:10')

        m_cond = str(BOT_SETTINGS.get('MORNING_COND', '0'))
        l_cond = str(BOT_SETTINGS.get('LUNCH_COND', '1'))
        a_cond = str(BOT_SETTINGS.get('AFTERNOON_COND', '2'))

        l_start = datetime.strptime(l_start_str, "%H:%M").time()
        a_start = datetime.strptime(a_start_str, "%H:%M").time()

        target_id = m_cond
        if now_time >= a_start: target_id = a_cond
        elif now_time >= l_start: target_id = l_cond

        if target_id != current_id:
            strategy_logger.info(f"⏰ [스케줄러] 조건식 변경 실행! ({current_id} -> {target_id})")
            await apply_condition_preset(target_id)
            preset_desc = STRATEGY_PRESETS.get(target_id, {}).get("DESC", "")
            msg = f"⏰ [스케줄러] 조건식 변경\n{current_id}번 ➡️ {target_id}번"
            if preset_desc: msg += f"\n({preset_desc} 설정 적용 완료)"
            send_telegram_msg(msg)

            BOT_SETTINGS['CONDITION_ID'] = target_id
            BOT_SETTINGS['BOT_STATUS'] = "RESTARTING"
            BOT_SETTINGS["_INTENDED_STATUS_"] = "RUNNING"
            await save_settings_to_file()
            return True
    except Exception as e:
        strategy_logger.error(f"스케줄러 오류: {e}")
    return False

async def run_self_diagnosis():
    print("\n========================================")
    print("🩺 시스템 자가 진단 (Self Diagnosis)")
    print("========================================")
    try:
        test_file = os.path.join(DATA_DIR, "write_test.tmp")
        def _file_test():
            with open(test_file, "w") as f: f.write("test")
            os.remove(test_file)
        await run_blocking(_file_test)
        print("✅ [파일시스템] /data 디렉토리 쓰기 권한 OK")
    except Exception as e:
        print(f"❌ [파일시스템] 쓰기 권한 오류! ({e})")

    if not await run_blocking(os.path.exists, SETTINGS_FILE):
        print("⚠️ [설정] 설정 파일이 없어 기본값을 생성합니다.")
        await save_settings_to_file()
    print("========================================\n")

async def set_booting_status(status_msg="BOOTING", target_mode=None):
    try:
        await run_blocking(os.makedirs, os.path.dirname(STATUS_FILE), exist_ok=True)
        now = datetime.now()
        is_mock = MOCK_TRADE if target_mode is None else target_mode
        old_trading_state = {}

        if await run_blocking(os.path.exists, STATUS_FILE):
            try:
                def _read():
                    with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                        return json.load(f)
                old_data = await run_blocking(_read)
                old_trading_state = old_data.get('trading_state', {})
            except: pass

        status_data = {
            "bot_status": status_msg,
            "active_mode": "모의투자" if is_mock else "REAL",
            "account_no": KIWOOM_ACCOUNT_NO,
            "last_sync": now.isoformat(),
            "trading_state": old_trading_state,
            "is_offline": False
        }

        def _write(data):
            temp_file = f"{STATUS_FILE}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(temp_file, STATUS_FILE)
        await run_blocking(_write, status_data)
    except Exception as e:
        print(f"⚠️ 부팅 상태 저장 실패: {e}")

async def load_settings_from_file():
    global BOT_SETTINGS
    try:
        def _read_settings():
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f: return json.load(f)
            else:
                new_settings = DEFAULT_SETTINGS.copy()
                with open(SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(new_settings, f, ensure_ascii=False, indent=4)
                return new_settings

        new_settings = await run_blocking(_read_settings)
        saved_mock_mode = new_settings.get("MOCK_TRADE")

        if saved_mock_mode is not None and saved_mock_mode != MOCK_TRADE:
            strategy_logger.warning(f"⚠️ 투자 모드 변경 감지. 재시작합니다...")
            await set_booting_status("RESTARTING", target_mode=saved_mock_mode)
            await asyncio.sleep(1)
            sys.exit(0)

        current_cond_id = str(BOT_SETTINGS.get("CONDITION_ID") or "0")
        new_cond_id = str(new_settings.get("CONDITION_ID"))

        if current_cond_id != new_cond_id and new_cond_id is not None:
             strategy_logger.warning(f"조건검색식 변경 감지 (수동) ({current_cond_id} -> {new_cond_id}).")
             await apply_condition_preset(new_cond_id)
             if new_cond_id in STRATEGY_PRESETS:
                 preset = STRATEGY_PRESETS[new_cond_id]
                 for k, v in preset.items():
                     if k != "DESC": new_settings[k] = v

        for key, default_val in DEFAULT_SETTINGS.items():
            val = new_settings.get(key)
            if key == "CONDITION_ID": val = str(val) if (val is not None and val != "") else "0"
            elif key == "USE_MARKET_TIME": val = bool(val) if val is not None else True
            if key in ["MORNING_START", "MORNING_COND", "LUNCH_START", "LUNCH_COND", "AFTERNOON_START", "AFTERNOON_COND", "OVERNIGHT_COND_IDS"]:
                 if val is not None: BOT_SETTINGS[key] = str(val)
            else:
                 BOT_SETTINGS[key] = val if val is not None else default_val

        debug_val = BOT_SETTINGS.get("DEBUG_MODE", False)
        log_level = logging.DEBUG if debug_val else logging.INFO
        strategy_logger.setLevel(log_level)
        if ws_manager: ws_manager.set_debug_mode(debug_val)
        set_api_debug_mode(debug_val)

        if current_cond_id != new_cond_id:
            BOT_SETTINGS["_INTENDED_STATUS_"] = "RUNNING"
            BOT_SETTINGS["BOT_STATUS"] = "RESTARTING"
            await save_settings_to_file()
            return
    except Exception as e:
        strategy_logger.error(f"설정 로드 실패: {e}")
        BOT_SETTINGS = DEFAULT_SETTINGS.copy()

async def save_settings_to_file():
    def _write():
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f: json.dump(BOT_SETTINGS, f, ensure_ascii=False, indent=4)
    try: await run_blocking(_write)
    except: pass

async def save_status_to_file(force=False):
    global last_heartbeat_time, TRADING_STATE, BOT_SETTINGS, IS_INITIALIZED, RE_ENTRY_COOLDOWN, last_saved_state_hash, TODAY_REALIZED_PROFIT
    if not IS_INITIALIZED: return

    now = datetime.now()
    if not force and (now - last_heartbeat_time).total_seconds() < 2.0: return
    last_heartbeat_time = now

    try:
        await run_blocking(os.makedirs, os.path.dirname(STATUS_FILE), exist_ok=True)
        bot_status = BOT_SETTINGS.get("BOT_STATUS") or "STOPPED"
        display_status = bot_status
        if bot_status == "RUNNING" and not is_market_open():
            display_status = "SLEEPING"

        enriched_state = {}
        total_buy_amt = 0; total_eval_amt = 0; total_profit_amt = 0

        for code, info in TRADING_STATE.items():
            info_copy = info.copy()
            if isinstance(info_copy.get('order_time'), datetime):
                info_copy['order_time'] = info_copy['order_time'].strftime('%Y-%m-%d %H:%M:%S')
            if 'last_cancel_try' in info_copy and isinstance(info_copy['last_cancel_try'], datetime):
                info_copy['last_cancel_try'] = info_copy['last_cancel_try'].strftime('%Y-%m-%d %H:%M:%S')
            info_copy['applied_strategy'] = {
                'sl': BOT_SETTINGS.get('STOP_LOSS_RATE'),
                'ts_start': BOT_SETTINGS.get('TRAILING_START_RATE'),
                'ts_stop': BOT_SETTINGS.get('TRAILING_STOP_RATE')
            }
            enriched_state[code] = info_copy

            if "보유" in info.get('status', ''):
                qty = info.get('buy_qty', 0)
                buy_price = info.get('buy_price', 0)
                current_rate = info.get('current_profit_rate', 0.0)
                if qty > 0 and buy_price > 0:
                    item_buy_amt = buy_price * qty
                    item_eval_amt = item_buy_amt * (1 + current_rate / 100)
                    total_buy_amt += item_buy_amt
                    total_eval_amt += item_eval_amt

        total_profit_amt = total_eval_amt - total_buy_amt
        total_profit_rate = 0.0
        if total_buy_amt > 0:
            total_profit_rate = (total_profit_amt / total_buy_amt) * 100

        account_summary = {
            "total_buy": int(total_buy_amt),
            "total_eval": int(total_eval_amt),
            "total_profit": int(total_profit_amt),
            "total_rate": round(total_profit_rate, 2),
            "realized_profit": int(TODAY_REALIZED_PROFIT)
        }

        cooldown_data = {}
        for code, t in RE_ENTRY_COOLDOWN.items():
            if t > now: cooldown_data[code] = t.strftime('%Y-%m-%d %H:%M:%S')

        status_data = {
            "bot_status": display_status,
            "active_mode": "모의투자" if MOCK_TRADE else "REAL",
            "account_no": KIWOOM_ACCOUNT_NO,
            "last_sync": now.isoformat(),
            "trading_state": enriched_state,
            "account_summary": account_summary,
            "re_entry_cooldown": cooldown_data,
            "is_offline": False
        }

        current_hash = hashlib.md5(json.dumps(status_data, sort_keys=True).encode()).hexdigest()
        if not force and current_hash == last_saved_state_hash: return

        def _write(data):
            temp_file = f"{STATUS_FILE}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(temp_file, STATUS_FILE)
        await run_blocking(_write, status_data)
        last_saved_state_hash = current_hash

    except Exception: pass

# ---------------------------------------------------------
# 7. 매매 및 주문 실행 로직 (비동기화)
# ---------------------------------------------------------
async def _load_initial_balance():
    global TRADING_STATE, IS_INITIALIZED, RE_ENTRY_COOLDOWN
    strategy_logger.info("기존 보유 잔고를 확인합니다...")

    old_condition_map = {}
    RE_ENTRY_COOLDOWN = {}

    if await run_blocking(os.path.exists, STATUS_FILE):
        try:
            def _read_status():
                with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            old_data = await run_blocking(_read_status)
            for code, info in old_data.get('trading_state', {}).items():
                if info.get('condition_from') and info['condition_from'] != "기존보유":
                    old_condition_map[code] = info['condition_from']
            saved_cooldowns = old_data.get('re_entry_cooldown', {})
            now = datetime.now()
            for code, t_str in saved_cooldowns.items():
                try:
                    t = datetime.strptime(t_str, '%Y-%m-%d %H:%M:%S')
                    if t > now: RE_ENTRY_COOLDOWN[code] = t
                except: pass
        except Exception: pass

    initial_stocks = []
    initial_balance = None
    for retry in range(3):
        initial_balance = await run_blocking(fn_kt00018_get_account_balance)
        if initial_balance is not None: break
        strategy_logger.warning(f"잔고 조회 실패. 1초 후 재시도 ({retry+1}/3)...")
        await asyncio.sleep(1)

    TRADING_STATE.clear()

    if initial_balance and initial_balance.get('보유종목'):
        for item in initial_balance['보유종목']:
            try:
                stock_code = item['stk_cd'].strip('A')
                buy_price = int(item['pur_pric'])
                buy_qty = int(item['rmnd_qty'])
                profit_rate = float(item['prft_rt'])
                stk_nm = item.get('stk_nm', stock_code)

                restored_condition = old_condition_map.get(stock_code, "기존보유")
                if restored_condition == "기존보유":
                    restored_condition = PENDING_ORDER_CONDITIONS.get(stock_code, "기존보유")

                TRADING_STATE[stock_code] = {
                    "stk_nm": stk_nm, "buy_price": buy_price, "buy_qty": buy_qty,
                    "trailing_active": False, "peak_profit_rate": max(profit_rate, 0),
                    "status": "보유 (잔고)", "current_profit_rate": profit_rate,
                    "order_time": datetime.now(),
                    "condition_from": restored_condition
                }
                initial_stocks.append((stock_code, "0B"))
            except: pass

    IS_INITIALIZED = True
    return initial_stocks

async def sync_balance_with_server():
    global TRADING_STATE, TODAY_REALIZED_PROFIT, LAST_PROFIT_CHECK_TIME
    try:
        balance = await run_blocking(fn_kt00018_get_account_balance)
        if not balance: return

        if (datetime.now() - LAST_PROFIT_CHECK_TIME).total_seconds() > 60:
            rp = await run_blocking(fn_ka10074_get_daily_profit)
            if rp is not None:
                TODAY_REALIZED_PROFIT = rp
                LAST_PROFIT_CHECK_TIME = datetime.now()

        server_stock_codes = []
        if balance.get('보유종목'):
            for item in balance['보유종목']:
                code = item['stk_cd'].strip('A')
                server_stock_codes.append(code)
                server_profit = float(item['prft_rt'])

                if code in TRADING_STATE:
                    TRADING_STATE[code]['buy_price'] = int(item['pur_pric'])
                    TRADING_STATE[code]['buy_qty'] = int(item['rmnd_qty'])
                    if TRADING_STATE[code]['status'] == '매수주문':
                        TRADING_STATE[code]['status'] = '보유 (체결)'
                        strategy_logger.info(f"🔄 [동기화] {code} 매수주문 -> 보유 상태로 변경됨")
                    if server_profit > TRADING_STATE[code].get('peak_profit_rate', -999):
                         TRADING_STATE[code]['peak_profit_rate'] = server_profit
                else:
                    restored_condition = PENDING_ORDER_CONDITIONS.get(code, "외부매수/동기화")
                    TRADING_STATE[code] = {
                        "stk_nm": item.get('stk_nm', code),
                        "buy_price": int(item['pur_pric']),
                        "buy_qty": int(item['rmnd_qty']),
                        "trailing_active": False, "peak_profit_rate": max(server_profit, 0),
                        "status": "보유 (동기화됨)", "current_profit_rate": server_profit,
                        "order_time": datetime.now(),
                        "condition_from": restored_condition
                    }
                    if ws_manager: ws_manager.add_subscription(code, "0B")

        now_time = datetime.now().time()
        safe_start = datetime.strptime("08:50:00", "%H:%M:%S").time()
        safe_end = datetime.strptime("09:10:00", "%H:%M:%S").time()
        is_market_opening = safe_start <= now_time <= safe_end

        day_safe_start = datetime.strptime("08:30:00", "%H:%M:%S").time()
        day_safe_end = datetime.strptime("16:30:00", "%H:%M:%S").time()
        is_daytime_safe = day_safe_start <= now_time <= day_safe_end

        for code in list(TRADING_STATE.keys()):
            if code in server_stock_codes: continue
            state = TRADING_STATE[code]
            status = state.get('status', '')

            if is_market_opening and "매도" not in status:
                strategy_logger.warning(f"🛡️ [잔고보호] 장시작 폭주로 인한 잔고 누락 추정. 삭제 유예: {code}")
                continue
            if not is_daytime_safe and "매도" not in status:
                 continue

            if status == '매수주문':
                if (datetime.now() - state.get('order_time', datetime.now())).total_seconds() > 300:
                    del TRADING_STATE[code]
                continue

            strategy_logger.info(f"🗑️ [잔고동기화] {code} 잔고 부재(매도완료)로 목록에서 제거")
            cooldown_min = BOT_SETTINGS.get('RE_ENTRY_COOLDOWN_MIN') or 30
            RE_ENTRY_COOLDOWN[code] = datetime.now() + timedelta(minutes=cooldown_min)
            del TRADING_STATE[code]

    except Exception as e:
        strategy_logger.error(f"잔고 동기화 중 오류: {e}")

async def _sync_initial_condition_list():
    cond_id = str(BOT_SETTINGS.get('CONDITION_ID') or "0")
    if ws_manager: ws_manager.request_condition_snapshot(cond_id)

# 🌟 [신규] 개별 종목 처리 로직 분리 (비동기 병렬 실행용)
async def process_single_stock_signal(stock_code, event_type, condition_id, condition_names, initial_price=None):
    global TRADING_STATE, PROCESSING_STOCKS, PENDING_ORDER_CONDITIONS, BUY_ATTEMPT_HISTORY
    
    order_amount = BOT_SETTINGS.get('ORDER_AMOUNT') or 100000
    use_hoga_filter = BOT_SETTINGS.get('USE_HOGA_FILTER', True)
    min_ratio = float(BOT_SETTINGS.get('MIN_BUY_SELL_RATIO') or 0.5)
    
    current_cond_name = condition_names.get(condition_id, "알수없음")
    stk_name = ws_manager.master_stock_names.get(stock_code, stock_code)
    
    async with ANALYSIS_SEMAPHORE:
        try:
            strategy_logger.info(f"🔔 [조건포착] {stk_name} ({stock_code}) 분석 시작")
            
            # 1. 가격 정보 조회 (API 호출 절약 로직)
            stock_info = None
            current_price = 0
            
            # 🌟 WebSocket에서 받은 가격이 있으면 API 호출 생략 (Breakthrough!)
            if initial_price and initial_price > 0:
                current_price = initial_price
                # 종목명만 빠르게 확인 (캐시 사용 권장되지만, 여기선 간단히)
                if stk_name == stock_code: # 종목명이 코드와 같으면(모르면) 조회
                    await GLOBAL_API_LIMITER.wait()
                    stock_info = await run_blocking(fn_ka10001_get_stock_info, stock_code)
                    if stock_info: stk_nm = stock_info.get('종목명', stock_code)
                else:
                    stk_nm = stk_name
                debug_log(f"⚡ [Speed] {stk_nm}: 웹소켓 가격({current_price}) 사용 -> API 생략")
            else:
                # 가격 정보가 없으면 API 호출 (Rate Limit 적용)
                for attempt in range(3):
                    await GLOBAL_API_LIMITER.wait() # 🚦 신호 대기
                    stock_info = await run_blocking(fn_ka10001_get_stock_info, stock_code)
                    if stock_info:
                        current_price = abs(stock_info.get('현재가', 0))
                        if current_price == 0: current_price = abs(stock_info.get('시가', 0))
                        if current_price > 0: break
                    await asyncio.sleep(0.2)
                stk_nm = stock_info.get('종목명', stock_code) if stock_info else stock_code

            if current_price <= 0:
                strategy_logger.warning(f"❌ {stk_nm}({stock_code}) 가격 정보 없음. 스킵.")
                return

            # 2. 호가 필터 (Rate Limit 적용)
            if use_hoga_filter:
                await GLOBAL_API_LIMITER.wait() # 🚦 신호 대기
                hoga_data = await run_blocking(fn_ka10004_get_hoga, stock_code)
                if hoga_data:
                    buy_total = hoga_data['buy_total']
                    sell_total = hoga_data['sell_total']
                    if sell_total > 0:
                        ratio = buy_total / sell_total
                        if ratio < min_ratio:
                            strategy_logger.info(f"🛡️ [호가필터] {stk_nm} 진입 금지 (비율: {ratio:.2f})")
                            return
                    else: return
                else: return

            # 3. 차트 & AI 분석 (Rate Limit 적용 - 차트 조회)
            # 차트 조회는 무겁기 때문에 반드시 제한 필요
            await GLOBAL_API_LIMITER.wait() # 🚦 신호 대기
            is_good_chart, image_path, ai_reason = await analyze_chart_pattern(stock_code)
            
            if not is_good_chart:
                RE_ENTRY_COOLDOWN[stock_code] = datetime.now() + timedelta(minutes=10)
                return

            # 4. 주문 실행 (주문은 Rate Limit 예외 - 즉시 실행)
            buy_qty = int((order_amount * 0.95) // current_price)
            if buy_qty == 0:
                if image_path:
                    try: os.remove(image_path)
                    except: pass
                return

            BUY_ATTEMPT_HISTORY[stock_code] = datetime.now()

            strategy_logger.info(f"🚀 [주문전송] {stk_nm} / {buy_qty}주 / 시장가")
            cond_info_str = f"{condition_id}:{current_cond_name}"
            PENDING_ORDER_CONDITIONS[stock_code] = cond_info_str

            ord_no = await run_blocking(fn_kt10000_buy_order, stock_code, buy_qty, price=0)

            if ord_no:
                await log_trade(stock_code, stk_nm, "BUY", buy_qty, current_price, f"조건검색({condition_id})", image_path=image_path, ai_reason=ai_reason)
                TRADING_STATE[stock_code] = {
                    "stk_nm": stk_nm, "buy_price": current_price, "buy_qty": buy_qty,
                    "trailing_active": False, "peak_profit_rate": 0.0,
                    "status": "매수주문", "current_profit_rate": 0.0,
                    "order_time": datetime.now(),
                    "condition_from": cond_info_str,
                    "ord_no": ord_no
                }
                ws_manager.add_subscription(stock_code, "0B")
                print(f"✅ [주문성공] 주문번호: {ord_no}")
            else:
                strategy_logger.error(f"❌ [주문실패] {stk_nm}: API 응답 없음")
                if image_path:
                    try: os.remove(image_path)
                    except: pass

            await save_status_to_file(force=True)
            
        except Exception as e:
            strategy_logger.error(f"종목 처리 중 오류 ({stock_code}): {e}")
            if 'image_path' in locals() and image_path:
                try: os.remove(image_path)
                except: pass
        finally:
            if stock_code in PROCESSING_STOCKS: 
                PROCESSING_STOCKS.discard(stock_code)


async def check_for_new_stocks():
    global TRADING_STATE, PROCESSING_STOCKS, PENDING_ORDER_CONDITIONS, BUY_ATTEMPT_HISTORY
    global CACHED_CONDITION_NAMES # [최적화] 전역 캐시 사용

    condition_id = str(BOT_SETTINGS.get('CONDITION_ID') or "0")
    
    # [최적화] 파일 매번 읽지 않고 캐시 사용
    condition_names = CACHED_CONDITION_NAMES

    while True:
        event = ws_manager.pop_condition_event()
        if not event: break

        stock_code = event.get('stock_code', '').strip('AJ')
        if event.get('type') != 'I': continue
        
        # 🌟 WebSocket에서 추출한 가격 정보 가져오기
        initial_price = event.get('price')

        if stock_code in TRADING_STATE: continue
        if stock_code in PROCESSING_STOCKS: continue
        if stock_code in RE_ENTRY_COOLDOWN:
            if datetime.now() < RE_ENTRY_COOLDOWN[stock_code]:
                continue
            else: del RE_ENTRY_COOLDOWN[stock_code]

        if stock_code in BUY_ATTEMPT_HISTORY:
            elapsed = (datetime.now() - BUY_ATTEMPT_HISTORY[stock_code]).total_seconds()
            if elapsed < 60: continue
            else: del BUY_ATTEMPT_HISTORY[stock_code]

        PROCESSING_STOCKS.add(stock_code)
        
        # 🌟 가격 정보(initial_price)를 함께 전달
        asyncio.create_task(process_single_stock_signal(stock_code, "I", condition_id, condition_names, initial_price))
        
        await asyncio.sleep(0.01)

async def try_market_close_liquidation():
    global TRADING_STATE
    now = datetime.now()
    if now.hour == 15 and (10 <= now.minute < 20):
        if not TRADING_STATE: return

        raw_ids = str(BOT_SETTINGS.get("OVERNIGHT_COND_IDS", "2"))
        OVERNIGHT_CONDITION_IDS = [x.strip() for x in raw_ids.split(',') if x.strip()]

        for stock_code, state in list(TRADING_STATE.items()):
            if "매도" in state.get('status', ''): continue
            cond_info = state.get('condition_from', '')
            cond_id = cond_info.split(':')[0] if ':' in cond_info else '999'
            if cond_id in OVERNIGHT_CONDITION_IDS: continue

            stk_nm = state.get('stk_nm', stock_code)
            buy_qty = state.get('buy_qty', 0)
            if buy_qty > 0:
                strategy_logger.info(f"📉 [강제청산] {stk_nm} 시장가 매도")
                ord_no = await run_blocking(fn_kt10001_sell_order, stock_code, buy_qty, price=0)
                if ord_no:
                    TRADING_STATE[stock_code]['status'] = "매도주문중(일괄)"
                    TRADING_STATE[stock_code]['ord_no'] = ord_no
                    await save_status_to_file(force=True)

async def try_morning_liquidation():
    global TRADING_STATE
    now = datetime.now()
    if now.hour == 9 and 0 <= now.minute <= 2:
        if not TRADING_STATE: return

        raw_ids = str(BOT_SETTINGS.get("OVERNIGHT_COND_IDS", "2"))
        OVERNIGHT_CONDITION_IDS = [x.strip() for x in raw_ids.split(',') if x.strip()]

        for stock_code, state in list(TRADING_STATE.items()):
            if "매도" in state.get('status', '') or state.get('trailing_active', False):
                continue
            cond_info = state.get('condition_from', '')
            cond_id = cond_info.split(':')[0] if ':' in cond_info else '999'

            if cond_id in OVERNIGHT_CONDITION_IDS:
                stk_nm = state.get('stk_nm', stock_code)
                buy_qty = state.get('buy_qty', 0)
                buy_price = state.get('buy_price', 0)

                if buy_qty > 0 and buy_price > 0:
                    current_price = 0
                    price_data = ws_manager.get_realtime_data(stock_code, "0B")
                    if not price_data: price_data = ws_manager.get_realtime_data(stock_code, "00")
                    if price_data:
                        raw_price = price_data.get('10') or price_data.get('cur_prc')
                        current_price = parse_price(raw_price)

                    if current_price == 0:
                        info = await run_blocking(fn_ka10001_get_stock_info, stock_code)
                        if info: current_price = abs(info.get('현재가', 0))

                    if current_price == 0: continue
                    profit_rate = ((current_price - buy_price) / buy_price) * 100

                    if profit_rate <= 0:
                        strategy_logger.info(f"📉 [시초가 청산] {stk_nm} 약세 출발({profit_rate:.2f}%) -> 시장가 매도 실행")
                        ord_no = await run_blocking(fn_kt10001_sell_order, stock_code, buy_qty, price=0)
                        if ord_no:
                            TRADING_STATE[stock_code]['status'] = "매도주문중(시초가손절)"
                            TRADING_STATE[stock_code]['ord_no'] = ord_no
                            await save_status_to_file(force=True)
                    else:
                        strategy_logger.info(f"📈 [시초가 홀딩] {stk_nm} 상승 출발({profit_rate:.2f}%) -> 트레일링 스탑(TS) ON")
                        TRADING_STATE[stock_code]['trailing_active'] = True
                        TRADING_STATE[stock_code]['peak_profit_rate'] = profit_rate
                        await save_status_to_file(force=True)

async def process_bulk_sell():
    global TRADING_STATE
    if not TRADING_STATE: return
    strategy_logger.warning("🚨 [명령 수신] 일괄 청산 시작!")
    send_telegram_msg("🚨 [알림] 사용자 요청 일괄 청산 시작")

    for stock_code, state in list(TRADING_STATE.items()):
        if "매도" in state.get('status', ''): continue
        buy_qty = state.get('buy_qty', 0)
        if buy_qty > 0:
            debug_log(f"일괄매도 주문: {stock_code} {buy_qty}주")
            ord_no = await run_blocking(fn_kt10001_sell_order, stock_code, buy_qty, price=0)
            if ord_no:
                TRADING_STATE[stock_code]['status'] = "매도주문중(일괄)"
                TRADING_STATE[stock_code]['ord_no'] = ord_no
                await save_status_to_file(force=True)
                await asyncio.sleep(0.2)

async def manage_unfilled_orders():
    global TRADING_STATE
    now = datetime.now()
    for stock_code, state in list(TRADING_STATE.items()):
        status = state.get('status', '')
        ord_no = state.get('ord_no')
        if status in ['매수주문', '매도주문', '매도주문중'] and ord_no:
            order_time = state.get('order_time')
            if isinstance(order_time, str):
                try: order_time = datetime.strptime(order_time, '%Y-%m-%d %H:%M:%S')
                except: continue

            if order_time and (now - order_time).total_seconds() > 20:
                last_cancel = state.get('last_cancel_try')
                if last_cancel and (now - last_cancel).total_seconds() < 10: continue

                debug_log(f"미체결 주문 취소 실행: {stock_code}")
                state['last_cancel_try'] = now
                is_buy = '매수' in status
                qty = state.get('buy_qty', 0)
                await run_blocking(fn_kt10003_cancel_order, stock_code, qty, ord_no, is_buy)

                if is_buy: del TRADING_STATE[stock_code]
                else:
                    TRADING_STATE[stock_code]['status'] = '보유 (체결)'
                    TRADING_STATE[stock_code].pop('ord_no', None)
                await save_status_to_file(force=True)

async def manage_open_positions():
    global TRADING_STATE, RE_ENTRY_COOLDOWN, LAST_PRICE_CHECK_TIME, LAST_API_CALL_TIME
    if not TRADING_STATE: return

    apply_sl = float(BOT_SETTINGS.get('STOP_LOSS_RATE') or -1.5)
    apply_ts_start = float(BOT_SETTINGS.get('TRAILING_START_RATE') or 1.5)
    apply_ts_stop = float(BOT_SETTINGS.get('TRAILING_STOP_RATE') or -1.0)
    cooldown_min = BOT_SETTINGS.get('RE_ENTRY_COOLDOWN_MIN') or 30
    is_auto_sell_on = BOT_SETTINGS.get("USE_AUTO_SELL", False)

    R_BUY_FEE_RATE = 0.0035 if MOCK_TRADE else 0.00015
    R_SELL_FEE_RATE = 0.0035 if MOCK_TRADE else 0.00015
    R_TAX_RATE = 0.0015

    now = datetime.now()

    for stock_code, state in list(TRADING_STATE.items()):
        try:
            if "매도" in state.get('status', ''): continue

            price_data = ws_manager.get_realtime_data(stock_code, "0B")
            if not price_data: price_data = ws_manager.get_realtime_data(stock_code, "00")

            raw_price = price_data.get('10') or price_data.get('cur_prc')
            current_price = parse_price(raw_price)

            if current_price == 0:
                if (now - BOT_START_TIME).total_seconds() < 5.0: continue
                last_api_call = LAST_API_CALL_TIME.get(stock_code)
                if not last_api_call or (now - last_api_call).total_seconds() > 60.0:
                    if ws_manager: ws_manager.add_subscription(stock_code, "0B")
                    stock_info = await run_blocking(fn_ka10001_get_stock_info, stock_code)
                    if stock_info:
                        current_price = abs(stock_info.get('현재가', 0))
                        LAST_API_CALL_TIME[stock_code] = now
                        await asyncio.sleep(0.1)

            if current_price == 0: continue

            buy_price = state.get('buy_price', 0)
            buy_qty = state.get('buy_qty', 0)
            if buy_price == 0 or buy_qty == 0: continue

            pure_buy_amt = buy_price * buy_qty
            eval_amt = current_price * buy_qty
            total_cost = int(pure_buy_amt * R_BUY_FEE_RATE) + int(eval_amt * R_SELL_FEE_RATE) + int(eval_amt * R_TAX_RATE)
            net_profit = eval_amt - pure_buy_amt - total_cost
            profit_rate = (net_profit / pure_buy_amt) * 100

            state['current_profit_rate'] = round(profit_rate, 2)

            if not is_auto_sell_on: continue

            sell_reason = None
            if profit_rate <= apply_sl: sell_reason = f"손절 ({profit_rate:.2f}%)"

            if not sell_reason:
                if not state.get('trailing_active', False):
                    if profit_rate >= apply_ts_start:
                        state['trailing_active'] = True
                        state['peak_profit_rate'] = profit_rate
                        await save_status_to_file(force=True)

                if state.get('trailing_active', False):
                    if profit_rate > state.get('peak_profit_rate', 0.0):
                        state['peak_profit_rate'] = profit_rate

                    drop_from_peak = profit_rate - state.get('peak_profit_rate', 0.0)
                    if drop_from_peak <= apply_ts_stop:
                        sell_reason = f"익절 ({profit_rate:.2f}%)"

            if sell_reason:
                stk_nm = state.get('stk_nm', stock_code)
                ord_no = await run_blocking(fn_kt10001_sell_order, stock_code, buy_qty, price=0)
                if ord_no:
                    peak = state.get('peak_profit_rate', 0.0)
                    est_profit = (current_price * buy_qty) - (buy_price * buy_qty) - (current_price * buy_qty * 0.0023)
                    await log_trade(stock_code, stk_nm, "SELL", buy_qty, current_price, sell_reason, profit_rate, profit_amt=est_profit, peak_rate=peak)

                    TRADING_STATE[stock_code]['status'] = "매도주문중"
                    TRADING_STATE[stock_code]['ord_no'] = ord_no
                    RE_ENTRY_COOLDOWN[stock_code] = datetime.now() + timedelta(minutes=cooldown_min)
                    await save_status_to_file(force=True)

        except Exception as e:
            strategy_logger.error(f"종목 감시 오류 ({stock_code}): {e}")

async def _handle_realtime_account(account_data_type):
    global TRADING_STATE
    data = ws_manager.get_realtime_data(account_data_type, "ACCOUNT")
    if not data: return

    if account_data_type == "00":
        stock_code = data.get('9001', '').strip('AJ')
        order_status = data.get('913', '').strip()
        order_type = data.get('905', '')

        if stock_code in TRADING_STATE and "체결" in order_status:
            debug_log(f"실시간 체결 확인: {stock_code} {order_status}")
            trade_price = parse_price(data.get('910', '0'))
            trade_qty = int(data.get('911', '0'))
            if trade_price > 0 and "+매수" in order_type:
                TRADING_STATE[stock_code]['buy_price'] = trade_price
                TRADING_STATE[stock_code]['buy_qty'] = trade_qty
                TRADING_STATE[stock_code]['status'] = "보유 (체결)"
                TRADING_STATE[stock_code].pop('ord_no', None)
                await save_status_to_file(force=True)

    elif account_data_type == "04":
        stock_code = data.get('9001', '').strip('AJ')
        if stock_code in TRADING_STATE:
            holding_qty = int(data.get('930', '0') or 0)
            if holding_qty == 0:
                strategy_logger.info(f"✨ [실시간 잔고] {stock_code} 전량 매도 확인 -> 목록 삭제")
                del TRADING_STATE[stock_code]
                await save_status_to_file(force=True)

def setup_logging():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    for handler in logger.handlers[:]: logger.removeHandler(handler)
    formatter = logging.Formatter('[%(asctime)s] (%(name)s) %(levelname)s: %(message)s', datefmt='%H:%M:%S')

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_dir = "/data/logs"
    os.makedirs(log_dir, exist_ok=True)
    file_handler = TimedRotatingFileHandler(filename=os.path.join(log_dir, "bot_daily.log"), when="midnight", interval=1, backupCount=7, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logging.getLogger("WebSocket").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

# ---------------------------------------------------------
# 8. 메인 실행부 (asyncio)
# ---------------------------------------------------------
async def main():
    global ws_manager, BOT_SETTINGS, TRADING_STATE

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_exit():
        print("\n[System] 종료 신호 수신! 정리 작업 시작...")
        stop_event.set()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, _handle_exit)
        loop.add_signal_handler(signal.SIGINT, _handle_exit)
    else:
        signal.signal(signal.SIGINT, lambda s, f: _handle_exit())
        signal.signal(signal.SIGTERM, lambda s, f: _handle_exit())

    setup_logging()
    telegram_task = asyncio.create_task(_telegram_worker())

    await run_self_diagnosis()
    await set_booting_status("BOOTING", target_mode=MOCK_TRADE)
    await run_blocking(create_master_stock_file)

    BOT_SETTINGS = DEFAULT_SETTINGS.copy()
    await load_settings_from_file()

    if MOCK_TRADE:
        mode_log = "✅ [투자모드] 모의투자 (Virtual Trading)"
        strategy_logger.info(f"🚀 {mode_log} - 시스템이 안전하게 시작되었습니다.")
        send_telegram_msg(f"🖥️ [봇 시작] {mode_log}")
    else:
        mode_log = "🚨 [투자모드] 실전투자 (REAL TRADING)"
        print(f"🔥 경고: 현재 '실전 투자' 모드입니다! 🔥")
        strategy_logger.warning(f"🚀 {mode_log} - 주의: 실제 자금이 운용됩니다.")
        send_telegram_msg(f"🔥 [경고] 실전투자 모드로 봇이 시작되었습니다!")

    if BOT_SETTINGS.get("BOT_STATUS") == "RESTARTING":
        intended_status = BOT_SETTINGS.get("_INTENDED_STATUS_", "RUNNING")
        BOT_SETTINGS["BOT_STATUS"] = intended_status
        BOT_SETTINGS.pop("_INTENDED_STATUS_", None)
        await save_settings_to_file()

    initial_stocks = await _load_initial_balance()
    ws_manager = KiwoomWebSocketManager()
    ws_manager.start(stock_list=initial_stocks, account_list=["00", "04"])

    await asyncio.sleep(5)
    await _sync_initial_condition_list()

    # 🌟 [신규] 봇 시작 시 조건식 이름 목록 한 번만 로드 (병목 제거)
    await load_condition_names()

    strategy_logger.info("🚀 [메인 루프 시작] 비동기 봇이 정상적으로 실행되었습니다.")

    last_balance_sync = datetime.now()
    last_alive_log = datetime.now()
    last_slow_check = datetime.now()
    last_force_save = datetime.now()
    last_stopped_log = datetime.now()
    last_report_date = None

    while not stop_event.is_set():
        try:
            # 1. 백테스팅 요청 확인
            backtest_req_file = os.path.join(DATA_DIR, "backtest_req.json")
            if await run_blocking(os.path.exists, backtest_req_file):
                try:
                    def _read_bt_req():
                        with open(backtest_req_file, 'r', encoding='utf-8') as f: return f.read().strip()
                    content = await run_blocking(_read_bt_req)

                    if content:
                        req_data = json.loads(content)
                        try: await run_blocking(os.remove, backtest_req_file)
                        except: pass
                        strategy_logger.info("📊 백테스팅 요청 감지! 시뮬레이션 시작...")

                        def run_bt(signals, settings):
                            try:
                                results = run_simulation_for_list(signals, settings)
                                res_file = os.path.join(DATA_DIR, "backtest_res.json")
                                with open(res_file, 'w', encoding='utf-8') as f:
                                    json.dump(results, f, ensure_ascii=False)
                            except Exception as e:
                                strategy_logger.error(f"백테스팅 오류: {e}")

                        await run_blocking(run_bt, req_data.get('signals', []), BOT_SETTINGS)
                except Exception as e:
                    try: await run_blocking(os.remove, backtest_req_file)
                    except: pass

            # 2. 일괄 매도 요청
            bulk_sell_file = os.path.join(DATA_DIR, "bulk_sell_req.json")
            if await run_blocking(os.path.exists, bulk_sell_file):
                try:
                    await process_bulk_sell()
                except Exception as e: strategy_logger.error(f"일괄 청산 오류: {e}")
                finally:
                    try: await run_blocking(os.remove, bulk_sell_file)
                    except: pass

            # 3. 설정 및 상태
            await load_settings_from_file()
            bot_status = BOT_SETTINGS.get("BOT_STATUS", "STOPPED")

            if (datetime.now() - last_force_save).total_seconds() > 5.0:
                await save_status_to_file(force=True)
                last_force_save = datetime.now()

            # 🌟 [수정] 리포트 발송 로직을 이곳으로 이동 (상태와 무관하게 실행) 🌟
            try:
                # 15시 40분 이후라면 리포트 체크
                if (datetime.now().hour == 15 and datetime.now().minute >= 40) or (datetime.now().hour > 15):
                    current_date_str = datetime.now().strftime('%Y-%m-%d')
                    if last_report_date != current_date_str:
                        # 주말/공휴일 제외 로직이 필요하다면 여기에 추가 가능 (현재는 매일 체크)
                        await send_daily_report()
                        last_report_date = current_date_str
            except Exception as e:
                strategy_logger.error(f"리포트 체크 중 오류: {e}")

            if await check_auto_condition_change(): break
            if bot_status == "RESTARTING": break

            elif bot_status == "RUNNING":
                if not is_market_open():
                    now_time = datetime.now().time()
                    
                    if (datetime.now() - last_alive_log).total_seconds() > 1800:
                        msg = f"💤 [장마감] 대기 모드\n보유: {len(TRADING_STATE)}종목"
                        strategy_logger.info(msg.replace("\n", " / "))
                        send_telegram_msg(msg)
                        last_alive_log = datetime.now()

                    start_buffer = datetime.strptime("08:30:00", "%H:%M:%S").time()
                    end_buffer = datetime.strptime("15:35:00", "%H:%M:%S").time()

                    if now_time < start_buffer or now_time > end_buffer:
                         while ws_manager.pop_condition_event(): pass

                    sync_start_limit = datetime.strptime("08:40:00", "%H:%M:%S").time()
                    if now_time >= sync_start_limit:
                        if (datetime.now() - last_balance_sync).total_seconds() > 20:
                             await sync_balance_with_server()
                             last_balance_sync = datetime.now()

                    await save_status_to_file()
                    await asyncio.sleep(1)
                    continue

                current_time = datetime.now().time()
                market_start_guard = datetime.strptime("09:00:30", "%H:%M:%S").time()
                if current_time < market_start_guard:
                    await manage_open_positions()
                    await save_status_to_file()
                    await asyncio.sleep(1)
                    continue

                if (datetime.now() - last_alive_log).total_seconds() > 1800:
                    msg = f"💓 [생존신고] 봇 작동 중\n보유: {len(TRADING_STATE)}종목"
                    strategy_logger.info(msg.replace("\n", " / "))
                    send_telegram_msg(msg)
                    last_alive_log = datetime.now()

                await check_for_new_stocks()

                if (datetime.now() - last_slow_check).total_seconds() > 2.0:
                    await manage_open_positions()
                    await try_market_close_liquidation()
                    await try_morning_liquidation()
                    await manage_unfilled_orders()
                    await _handle_realtime_account("00")
                    await _handle_realtime_account("04")
                    await save_status_to_file()

                    if (datetime.now() - last_balance_sync).total_seconds() > 20:
                        await sync_balance_with_server()
                        last_balance_sync = datetime.now()
                    last_slow_check = datetime.now()

                await asyncio.sleep(0.1)

            elif bot_status == "STOPPED":
                while ws_manager.pop_condition_event(): pass
                await manage_open_positions()
                await _handle_realtime_account("00")
                await _handle_realtime_account("04")

                if is_market_open() and (datetime.now() - last_balance_sync).total_seconds() > 30:
                    await sync_balance_with_server()
                    last_balance_sync = datetime.now()

                if (datetime.now() - last_stopped_log).total_seconds() > 60:
                    if BOT_SETTINGS.get("USE_AUTO_SELL", False):
                        strategy_logger.info("🛡️ [매수중지] 상태지만 매도 감시는 가동 중입니다.")
                    last_stopped_log = datetime.now()

                if (datetime.now() - last_alive_log).total_seconds() > 1800:
                     send_telegram_msg("⏸ [대기중] 봇 정지 상태입니다.")
                     last_alive_log = datetime.now()

                await save_status_to_file()
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            break
        except Exception as e:
            strategy_logger.error(f"🔥 메인 루프 치명적 오류:\n{traceback.format_exc()}")
            send_telegram_msg(f"🔥 [오류 발생] 봇이 멈췄습니다!\n{str(e)}")
            await asyncio.sleep(5)

    if ws_manager and BOT_SETTINGS.get("BOT_STATUS") != "RESTARTING":
        ws_manager.stop()
    await save_status_to_file(force=True)
    telegram_task.cancel()
    try: await telegram_task
    except: pass

if __name__ == "__main__":
    asyncio.run(main())