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
import re
import exchange_calendars as xcals
import pandas as pd
import FinanceDataReader as fdr
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from functools import partial
import requests

from ai_analyst import create_chart_image, create_daily_chart_image, ask_ai_to_buy, init_ai_clients, combine_chart_images, get_api_status_report, get_client_count, analyze_daily_trades_with_ai
from database import db 

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
    fn_ka90001_get_top_themes,
    fn_ka90002_get_theme_stocks,
    safe_int,
    set_api_debug_mode
)
from config import MOCK_TRADE, KIWOOM_ACCOUNT_NO, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from websocket_manager import KiwoomWebSocketManager
from backtesting import run_simulation_for_list, stop_backtest, run_optimization

# ---------------------------------------------------------
# 비동기 속도 제한 클래스
# ---------------------------------------------------------
class AsyncRateLimiter:
    def __init__(self, max_calls, period=1.0):
        self.max_calls = max_calls
        self.period = period
        self.timestamps = deque()

    async def wait(self):
        while True:
            now = time.time()
            while self.timestamps and now - self.timestamps[0] > self.period:
                self.timestamps.popleft()
            
            if len(self.timestamps) < self.max_calls:
                self.timestamps.append(now)
                return
            await asyncio.sleep(0.1)

# 🌟 [수정] API 속도 제한 분리 (차트: 1.5초 / 일반: 0.2초)
CHART_API_LIMITER = AsyncRateLimiter(max_calls=1, period=2.0)   # 차트 조회용 (더 느리게)
GENERAL_API_LIMITER = AsyncRateLimiter(max_calls=1, period=0.5) # 🌟 [수정] 정보/호가용 제한 추가 완화 (0.3 -> 0.5)
ANALYSIS_SEMAPHORE = asyncio.Semaphore(1)

# ---------------------------------------------------------
# 1. 시스템 환경 설정 및 로거 초기화
# ---------------------------------------------------------
os.environ['TZ'] = 'Asia/Seoul'
try: time.tzset()
except AttributeError: pass

strategy_logger = logging.getLogger("Strategy")

class DBLoggingHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            db.save_system_log(record.levelname, msg, record.name)
        except Exception:
            self.handleError(record)

# ---------------------------------------------------------
# 2. 전역 변수 설정
# ---------------------------------------------------------
TELEGRAM_QUEUE = asyncio.Queue()

TODAY_REALIZED_PROFIT = 0
LAST_PROFIT_CHECK_TIME = datetime.min
CACHED_CONDITION_NAMES = {}
STOCK_MARKET_MAP = {} 
SETTINGS_LOADED_SUCCESSFULLY = True # [추가] 설정 로드 성공 여부 플래그
BACKTEST_TASK = None # 🌟 [추가] 백테스팅 태스크 추적용
SNAPSHOT_PROGRESS = {} # 🌟 [추가] 스냅샷 진행률 추적용
DELAYED_SNAPSHOT_EVENTS = deque() # 🌟 [추가] 09:00:30 이전 스냅샷 대기열
LAST_SNAPSHOT_REFRESH_DATE = None # 🌟 [추가] 스냅샷 리프레시 추적용

# 🌟 주도 테마 추적용 변수
LEADING_THEME_STOCKS = set()
LEADING_THEME_NAMES = []
LAST_THEME_UPDATE_TIME = datetime.min

# 시장 지수 상태 (코스피/코스닥 분리)
MARKET_STATUS = {
    "001": { "name": "코스피", "is_bullish": True, "price": 0, "ma20": 0 },
    "101": { "name": "코스닥", "is_bullish": True, "price": 0, "ma20": 0 },
    "last_check": datetime.min
}

# ---------------------------------------------------------
# 3. 전략 및 봇 기본 설정
# ---------------------------------------------------------
STRATEGY_PRESETS = {
    "0": { "DESC": "시초가매매(급등)", "STOP_LOSS_RATE": -2.0, "TRAILING_START_RATE": 1.0, "TRAILING_STOP_RATE": -0.6, "RE_ENTRY_COOLDOWN_MIN": 60, "MIN_BUY_SELL_RATIO": 0.3 },
    "1": { "DESC": "돌파매매(맥점)", "STOP_LOSS_RATE": -2.0, "TRAILING_START_RATE": 0.5, "TRAILING_STOP_RATE": -0.4, "RE_ENTRY_COOLDOWN_MIN": 30, "MIN_BUY_SELL_RATIO": 0.5 },
    "2": { "DESC": "종가베팅(오버나잇)", "STOP_LOSS_RATE": -2.0, "TRAILING_START_RATE": 1.0, "TRAILING_STOP_RATE": -0.6, "RE_ENTRY_COOLDOWN_MIN": 0, "MIN_BUY_SELL_RATIO": 0.5 }
}

DEFAULT_SETTINGS = {
    "BOT_STATUS": "RUNNING",
    "MOCK_TRADE": MOCK_TRADE,
    "CONDITION_ID": "0",
    "ORDER_AMOUNT": 100000,
    "STOP_LOSS_RATE": -1.5,
    "TRAILING_START_RATE": 1.5,
    "TRAILING_STOP_RATE": -1.0,
    "RE_ENTRY_COOLDOWN_MIN": 30,
    "AI_REJECTION_COOLDOWN_MIN": 10,
    "USE_MARKET_TIME": True,
    "USE_AUTO_SELL": True,
    "USE_PARTIAL_PROFIT": True,
    "PARTIAL_PROFIT_RATE": 50.0,
    "USE_TELEGRAM": True,
    "DEBUG_MODE": False,
    "USE_SCHEDULER": True,
    "MORNING_START": "08:50", "MORNING_COND": "0",
    "LUNCH_START": "10:30", "LUNCH_COND": "0",
    "AFTERNOON_START": "15:10", "AFTERNOON_COND": "2",
    "USE_HOGA_FILTER": True,
    "MIN_BUY_SELL_RATIO": 0.5,
    "USE_FAKE_BUY_FILTER": True,
    "MAX_BUY_SELL_RATIO": 10.0,
    "OVERNIGHT_COND_IDS": "2",
    "USE_AI_STOP_LOSS": True,
    "AI_STOP_LOSS_SAFETY_LIMIT": -5.0,
    "TIME_CUT_MINUTES": 20, 
    "RSI_LIMIT": 70.0,
    "USE_MARKET_FILTER": True,
    "UPPER_SHADOW_LIMIT": 0.4,
    "DAILY_SHADOW_LIMIT": 0.5,
    "USE_HOGA_FILTER": True,
    "USE_RSI_FILTER": True,
    "USE_TIME_CUT": True,
    "USE_UPPER_SHADOW_FILTER": True,
    "USE_DAILY_SHADOW_FILTER": True,
    "USE_STOP_LOSS": True,
    "USE_THEME_FILTER": True,       # [추가] 주도 테마 필터 사용 여부
    "THEME_TOP_N": 3,               # [추가] 주도 테마 필터 상위 n위
    "USE_TRAILING_STOP": True,
    "USE_RE_ENTRY_COOLDOWN": True,
    "USE_AI_REJECTION_COOLDOWN": True,
    "USE_AI_TRAILING_STOP": True,
    "AI_TS_CHECK_INTERVAL": 60,
    "USE_VOLUME_FILTER": True,
    "VOLUME_FILTER_RATIO": 0.3,
    "USE_DYNAMIC_TS": True,
    "DYN_TS_LV1_TRIGGER": 5.0,
    "DYN_TS_LV1_DROP": -2.0,
    "DYN_TS_LV2_TRIGGER": 10.0,
    "DYN_TS_LV2_DROP": -3.0,
    "DYN_TS_LV3_TRIGGER": 20.0,
    "DYN_TS_LV3_DROP": -5.0,
    "USE_BREAK_TIME": False,
    "BREAK_START": "11:30",
    "BREAK_END": "13:00",
    "USE_WATERING": True,           # [추가] 물타기(추가매수) 사용 여부
    "MAX_WATERING_COUNT": 1,        # [추가] 종목당 최대 물타기 횟수
    "MAX_WATERING_AMOUNT": 2000000  # [추가] 종목당 최대 매수 허용 금액 (물타기 포함)
}
BOT_SETTINGS = DEFAULT_SETTINGS.copy()

# [추가] 조건식별 설정 저장 시 제외할 전역 설정 키 (이 값들은 조건식이 바뀌어도 유지됨)
GLOBAL_SETTINGS_KEYS = [
    "BOT_STATUS", "CONDITION_ID", "MOCK_TRADE", "DEBUG_MODE", "USE_TELEGRAM", 
    "USE_SCHEDULER", "MORNING_START", "MORNING_COND", "LUNCH_START", "LUNCH_COND", 
    "AFTERNOON_START", "AFTERNOON_COND", "OVERNIGHT_COND_IDS", "_INTENDED_STATUS_"
]

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
last_db_save_time = datetime.min
TELEGRAM_UPDATE_OFFSET = 0
IS_INITIALIZED = False
last_saved_state_hash = ""

# ---------------------------------------------------------
# 4. 비동기 헬퍼 함수
# ---------------------------------------------------------
async def run_blocking(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    func_call = partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, func_call)

async def check_telegram_commands():
    """ 텔레그램 명령어를 폴링하여 처리합니다. """
    global TELEGRAM_UPDATE_OFFSET
    if not TELEGRAM_BOT_TOKEN: return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    # 타임아웃을 짧게 주어 메인 루프 블로킹 최소화
    params = {"offset": TELEGRAM_UPDATE_OFFSET, "timeout": 0, "limit": 5}
    
    try:
        response = await run_blocking(requests.get, url, params=params, timeout=5)
        if response.status_code != 200: return
        
        data = response.json()
        if not data.get("ok"): return
        
        for result in data.get("result", []):
            TELEGRAM_UPDATE_OFFSET = result["update_id"] + 1
            
            message = result.get("message", {})
            text = message.get("text", "").strip()
            chat_id = str(message.get("chat", {}).get("id"))
            
            # 보안: 설정된 채팅방 ID만 허용
            if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                continue

            if text == "api":
                report = get_api_status_report()
                send_telegram_msg(report)
                strategy_logger.info("📡 [Telegram] api 상태 조회 요청 처리 완료")

            elif text == "승률":
                try:
                    # 최근 1000건의 매매 기록 조회
                    trades = await run_blocking(db.get_recent_trades, 1000)
                    
                    # 당일 매도 기록만 필터링
                    today_str = datetime.now().strftime('%Y-%m-%d')
                    sell_trades = [t for t in trades if t['action'] == 'SELL' and t['timestamp'].startswith(today_str)]
                    
                    total_count = len(sell_trades)
                    if total_count == 0:
                        send_telegram_msg(f"📊 {today_str} 당일 매도 기록이 없습니다.")
                    else:
                        win_count = sum(1 for t in sell_trades if t['profit_rate'] > 0)
                        loss_count = total_count - win_count
                        win_rate = (win_count / total_count) * 100
                        total_profit = sum(t['profit_amt'] for t in sell_trades)
                        
                        msg = (
                            f"📊 <b>[금일 매매 승률]</b> ({today_str})\n"
                            f"━━━━━━━━━━━━━━\n"
                            f"🏆 승: {win_count}회 / ☠️ 패: {loss_count}회\n"
                            f"📈 승률: {win_rate:.1f}%\n"
                            f"💰 금일 손익: {total_profit:,}원"
                        )
                        send_telegram_msg(msg)
                        strategy_logger.info("📡 [Telegram] 승률 조회 요청 처리 완료")
                except Exception as e:
                    strategy_logger.error(f"승률 조회 중 오류: {e}")
                    send_telegram_msg("❌ 승률 조회 중 오류가 발생했습니다.")
                
    except Exception:
        pass

def debug_log(msg):
    strategy_logger.debug(f"{msg}")

async def load_condition_names():
    global CACHED_CONDITION_NAMES
    try:
        data = await run_blocking(db.get_kv, "conditions")
        if data:
            CACHED_CONDITION_NAMES = {str(c['id']): c['name'] for c in data.get('conditions', [])}
            strategy_logger.info(f"📁 [DB] 조건식 이름 로드 완료 ({len(CACHED_CONDITION_NAMES)}개)")
    except Exception as e:
        strategy_logger.error(f"조건식 이름 로드 실패: {e}")

def is_break_time():
    """ 현재 시간이 매수 중지 시간대인지 확인 """
    if not BOT_SETTINGS.get("USE_BREAK_TIME", False): return False
    try:
        start_str = BOT_SETTINGS.get("BREAK_START", "11:30")
        end_str = BOT_SETTINGS.get("BREAK_END", "13:00")
        now_time = datetime.now().time()
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()
        
        if start <= end: return start <= now_time < end
        else: return start <= now_time or now_time < end
    except: return False

async def load_stock_market_map():
    global STOCK_MARKET_MAP
    try:
        data = await run_blocking(db.get_kv, "stock_market_map")
        if data:
            STOCK_MARKET_MAP = data
            strategy_logger.info(f"📁 [DB] 종목별 시장 정보 로드 완료 ({len(STOCK_MARKET_MAP)}개)")
        else:
            strategy_logger.warning("⚠️ [DB] 종목별 시장 정보가 없습니다. 마스터 파일 생성을 기다립니다.")
    except Exception as e:
        strategy_logger.error(f"시장 정보 로드 실패: {e}")

# ---------------------------------------------------------
# 5. 텔레그램 및 리포트
# ---------------------------------------------------------
async def _telegram_worker():
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
                    if isinstance(item, str):
                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                        params = {"chat_id": TELEGRAM_CHAT_ID, "text": item, "parse_mode": "HTML"}
                        await run_blocking(requests.get, url, params=params, timeout=5)
                    elif isinstance(item, dict) and item.get('type') == 'photo':
                        path = item.get('path')
                        caption = item.get('caption')
                        if path and os.path.exists(path):
                            await run_blocking(_send_photo_sync, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, path, caption)
                            try: os.remove(path)
                            except: pass
                except Exception as e:
                    strategy_logger.error(f"텔레그램 전송 실패: {e}")
            TELEGRAM_QUEUE.task_done()
            await asyncio.sleep(1.0)
        except asyncio.CancelledError: break
        except Exception: await asyncio.sleep(1)

def send_telegram_msg(msg):
    if not BOT_SETTINGS.get("USE_TELEGRAM", True): return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try: TELEGRAM_QUEUE.put_nowait(msg)
    except Exception: pass

def send_telegram_photo(path, caption):
    # [수정] 텔레그램 미사용 시에도 생성된 이미지 파일은 삭제해야 함 (파일 누수 방지)
    if not BOT_SETTINGS.get("USE_TELEGRAM", True) or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        if path and os.path.exists(path):
            try: os.remove(path)
            except: pass
        return

    try: TELEGRAM_QUEUE.put_nowait({'type': 'photo', 'path': path, 'caption': caption})
    except Exception: pass

async def send_daily_report():
    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        server_profit = await run_blocking(fn_ka10074_get_daily_profit)

        trades = await run_blocking(db.get_recent_trades, 1000)
        trades.sort(key=lambda x: x['timestamp'])

        total_buy_cnt = 0; total_sell_cnt = 0; win_cnt = 0; loss_cnt = 0; log_profit = 0
        buy_condition_map = {}
        cond_stats = {}
        ai_trade_logs = [] # 🌟 [추가] AI 복기용 데이터

        for t in trades:
            if t['action'] == "BUY":
                if t['timestamp'].startswith(today_str): 
                    total_buy_cnt += 1
                    ai_trade_logs.append(f"[매수] {t['stock_name']} - 사유:{t['reason']}")
                match = re.search(r"조건검색\((\d+)\)", t['reason'])
                if match: buy_condition_map[t['stock_code']] = match.group(1)
                else: buy_condition_map[t['stock_code']] = "MANUAL"

            elif t['action'] == "SELL" and t['timestamp'].startswith(today_str):
                total_sell_cnt += 1
                rate = t['profit_rate']
                amt = t['profit_amt']
                if rate > 0: win_cnt += 1
                else: loss_cnt += 1
                log_profit += amt
                
                ai_trade_logs.append(f"[매도] {t['stock_name']} - 사유:{t['reason']}, 손익:{rate:.2f}% ({amt}원)")

                cond_id = buy_condition_map.get(t['stock_code'], "UNKNOWN")
                if cond_id not in cond_stats: cond_stats[cond_id] = {'win': 0, 'loss': 0, 'profit': 0}
                if rate > 0: cond_stats[cond_id]['win'] += 1
                else: cond_stats[cond_id]['loss'] += 1
                cond_stats[cond_id]['profit'] += amt

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
            f"<i>{source_msg}</i>\n"
            f"━━━━━━━━━━━━━━\n"
        )
        if cond_stats:
            msg += "📊 <b>[조건식별 성과]</b>\n"
            for cid, stat in cond_stats.items():
                c_name = CACHED_CONDITION_NAMES.get(cid, cid)
                if cid == "MANUAL": c_name = "수동/기타"
                elif cid == "UNKNOWN": c_name = "알수없음"
                c_win = stat['win']; c_loss = stat['loss']
                c_total = c_win + c_loss
                c_rate = (c_win / c_total * 100) if c_total > 0 else 0
                rate_emoji = "🔴" if c_rate >= 50 else "🔵"
                msg += f"{rate_emoji} {c_name}: {c_rate:.0f}% ({c_win}승 {c_loss}패)\n"
            msg += "━━━━━━━━━━━━━━\n"
            
        # 🌟 [추가] AI 매매 복기 요청
        if ai_trade_logs:
            strategy_logger.info("🤖 AI에게 당일 매매 복기를 요청합니다...")
            trades_text = "\n".join(ai_trade_logs)
            # 데이터가 너무 길어지면 토큰을 아끼기 위해 자르기 (최대 1500자)
            if len(trades_text) > 1500:
                trades_text = trades_text[:1500] + "\n... (이하 생략)"
            
            ai_feedback = await run_blocking(analyze_daily_trades_with_ai, trades_text)
            msg += f"🤖 <b>[AI 트레이딩 코치 피드백]</b>\n{ai_feedback}\n━━━━━━━━━━━━━━\n"

        msg += "오늘 하루도 수고하셨습니다! ☕"
        send_telegram_msg(msg)
        strategy_logger.info(f"일별 마감 리포트 전송 완료 (손익: {final_profit})")

    except Exception as e:
        strategy_logger.error(f"리포트 생성 실패: {e}")
        strategy_logger.error(traceback.format_exc())

async def log_trade(stock_code, stk_nm, action, qty, price, reason, profit_rate=0, profit_amt=0, peak_rate=0, image_path=None, ai_reason=None, custom_sl_rate=None, target_price=0):
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        price_str = f"{price:,}"
        profit_str = f"{profit_rate:.2f}"

        trade_data = {
            "timestamp": timestamp,
            "action": action,
            "stock_code": stock_code,
            "stock_name": stk_nm,
            "qty": qty,
            "price": price,
            "reason": reason,
            "profit_rate": profit_rate,
            "profit_amt": int(profit_amt),
            "image_path": image_path,
            "ai_reason": ai_reason
        }
        await run_blocking(db.log_trade, trade_data)

        strategy_logger.info(f"📝 [매매기록] {action} {stk_nm}({stock_code}) ({profit_str}%) - {reason}")

        emoji = "🚀 매수" if action == "BUY" else "📉 매도"
        tg_msg = f"{emoji} <b>주문 접수</b>"
        if action == "BUY" and ai_reason: tg_msg += f"\n🤖 <b>AI분석:</b> {ai_reason}"
        
        if action == "BUY" and custom_sl_rate is not None:
             tg_msg += f"\n📉 <b>설정손절:</b> {custom_sl_rate}%"

        if action == "BUY" and target_price > 0 and price > 0:
            exp_profit = ((target_price - price) / price) * 100
            tg_msg += f"\n🎯 <b>목표가:</b> {target_price:,}원 (예상 +{exp_profit:.2f}%)"

        tg_msg += f"\n사유: {reason}\n종목: {stk_nm} ({stock_code})\n가격: {price_str}원\n수량: {qty}주"

        if "SELL" in action:
            res_emoji = "💰" if profit_rate > 0 else "💧"
            tg_msg += f"\n{res_emoji} 예상수익: {profit_str}%"
            tg_msg += f"\n💵 예상손익: {int(profit_amt):,}원"
            if action == "SELL": tg_msg += f"\n📈 최고점: {peak_rate:.2f}%"

        if image_path: send_telegram_photo(image_path, tg_msg)
        else: send_telegram_msg(tg_msg)
            
    except Exception as e: strategy_logger.error(f"로그 작성 실패: {e}")

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
        
        if current_time < start_time or current_time > end_time: return False

        xkrx = xcals.get_calendar("XKRX")
        if not xkrx.is_session(now.strftime("%Y-%m-%d")): return False
        return True
    except Exception as e:
        if now.weekday() < 5:
            start = datetime.strptime("09:00:00", "%H:%M:%S").time()
            end = datetime.strptime("15:20:00", "%H:%M:%S").time()
            return start <= current_time <= end
        return False

# 지수 필터 체크 (FinanceDataReader 사용)
async def check_market_index_status():
    global MARKET_STATUS
    
    use_filter = BOT_SETTINGS.get("USE_MARKET_FILTER", False)
    if not use_filter:
        for code in ["001", "101"]:
            MARKET_STATUS[code]['is_bullish'] = True
        return

    now = datetime.now()
    # 🌟 [수정] 데이터 소스 차단 방지를 위해 주기를 1분 -> 3분(180초)으로 완화
    if (now - MARKET_STATUS['last_check']).total_seconds() < 180:
        return

    # FinanceDataReader용 심볼 매핑 (001->KS11, 101->KQ11)
    target_indices = {"001": "KS11", "101": "KQ11"}

    for index_code, fdr_symbol in target_indices.items():
        try:
            market_name = MARKET_STATUS[index_code]['name']
            
            # 키움 API 대신 FDR 사용 (속도제한 없음, 데이터 안정적)
            start_date = (now - timedelta(days=100)).strftime("%Y-%m-%d")
            
            # run_blocking을 사용하여 fdr.DataReader 호출 (Blocking I/O 방지)
            # 🌟 [수정] 타임아웃(10초) 추가하여 무한 대기 방지
            try:
                df = await asyncio.wait_for(run_blocking(fdr.DataReader, fdr_symbol, start_date), timeout=10.0)
            except asyncio.TimeoutError:
                df = None

            if df is None or len(df) < 20:
                strategy_logger.warning(f"⚠️ [지수필터] {market_name} 데이터 부족(FDR). 필터 일시 해제.")
                MARKET_STATUS[index_code]['is_bullish'] = True
                continue

            # 🌟 [수정] 데이터 날짜 확인 (지연 데이터 방지)
            last_dt = df.index[-1]
            if hasattr(last_dt, 'date'):
                data_date = last_dt.date()
            else:
                try: data_date = datetime.strptime(str(last_dt)[:10], "%Y-%m-%d").date()
                except: data_date = now.date()

            # 장 중인데 데이터 날짜가 오늘이 아니면 지연된 데이터로 간주 -> 필터 해제
            if is_market_open() and data_date != now.date():
                strategy_logger.warning(f"⚠️ [지수필터] {market_name} 데이터 지연 ({data_date}). 필터 일시 해제.")
                MARKET_STATUS[index_code]['is_bullish'] = True
                continue

            # FDR 데이터는 날짜 오름차순(오래된 것 -> 최신)으로 옴
            df['MA20'] = df['Close'].rolling(window=20).mean()

            current_close = df['Close'].iloc[-1]
            current_open = df['Open'].iloc[-1]
            current_ma20 = df['MA20'].iloc[-1]

            is_bullish = bool(current_close >= current_open)

            MARKET_STATUS[index_code]['is_bullish'] = is_bullish
            MARKET_STATUS[index_code]['price'] = int(current_close)
            MARKET_STATUS[index_code]['ma20'] = float(current_ma20)
            
            status_str = "상승장(매수허용)" if is_bullish else "하락장(매수금지)"
            strategy_logger.info(f"📉 [지수필터] {market_name}({data_date}): 현재 {current_close} / 시가 {current_open} -> {status_str}")

        except Exception as e:
            strategy_logger.error(f"지수 필터 체크 중 오류 ({index_code}): {e}")
            MARKET_STATUS[index_code]['is_bullish'] = True
            
    MARKET_STATUS['last_check'] = now

async def update_leading_themes():
    """ 10분마다 당일 시장 주도 테마 상위 3개와 포함된 종목 리스트를 캐싱합니다. """
    global LEADING_THEME_STOCKS, LEADING_THEME_NAMES, LAST_THEME_UPDATE_TIME
    
    now = datetime.now()
    if (now - LAST_THEME_UPDATE_TIME).total_seconds() < 600: # 10분 단위 갱신
        return
        
    try:
        await GENERAL_API_LIMITER.wait()
        top_n = int(BOT_SETTINGS.get("THEME_TOP_N", 3))
        top_themes = await run_blocking(fn_ka90001_get_top_themes, top_n) # 설정된 상위 N개 테마
        
        if top_themes:
            new_theme_stocks = set()
            new_theme_names = []
            for thm in top_themes:
                await GENERAL_API_LIMITER.wait()
                stocks = await run_blocking(fn_ka90002_get_theme_stocks, thm.get('thema_grp_cd'))
                new_theme_stocks.update(stocks)
                new_theme_names.append(f"{thm.get('thema_nm')} ({thm.get('flu_rt')}%)")
                
            LEADING_THEME_STOCKS = new_theme_stocks
            LEADING_THEME_NAMES = new_theme_names
            LAST_THEME_UPDATE_TIME = now
            strategy_logger.info(f"🔥 [주도테마 갱신] {', '.join(new_theme_names)} (총 {len(new_theme_stocks)}종목 캐싱완료)")
    except Exception as e:
        strategy_logger.error(f"주도 테마 갱신 실패: {e}")

async def capture_snapshot_chart(stock_code, stock_name):
    """ 매도 시점 차트 캡처 (일봉+분봉) """
    try:
        # 1. 분봉 데이터 (API)
        chart_data = await run_blocking(fn_ka10080_get_minute_chart, stock_code, tick="1")
        if not chart_data or len(chart_data) < 2: return None

        # 2. 일봉 데이터 (FDR)
        start_dt = (datetime.now() - timedelta(days=150)).strftime("%Y-%m-%d")
        try:
            df_daily = await asyncio.wait_for(run_blocking(fdr.DataReader, stock_code, start_dt), timeout=5.0)
        except Exception:
            try: 
                df_daily = await asyncio.wait_for(run_blocking(fdr.DataReader, f'KRX:{stock_code}', start_dt), timeout=5.0)
            except Exception: 
                df_daily = pd.DataFrame()

        # 3. 이미지 생성
        minute_buf = await run_blocking(create_chart_image, stock_code, stock_name, chart_data)
        daily_buf = await run_blocking(create_daily_chart_image, df_daily, stock_code)

        if minute_buf and daily_buf:
            combined_buf = await run_blocking(combine_chart_images, daily_buf, minute_buf)
            temp_path = f"/tmp/sell_{stock_code}_{int(time.time())}.png"
            with open(temp_path, "wb") as f:
                if combined_buf: f.write(combined_buf.getbuffer())
                else: f.write(minute_buf.getbuffer())
            return temp_path
        return None
    except Exception as e:
        strategy_logger.error(f"매도 차트 생성 실패: {e}")
        return None

async def analyze_chart_pattern(stock_code, stock_name, condition_id="0", stock_info=None):
    try:
        # 🌟 [수정] 차트 전용 리미터 사용 (1.5초 제한)
        await CHART_API_LIMITER.wait()
        chart_data = await run_blocking(fn_ka10080_get_minute_chart, stock_code, tick="1")
        
        # [수정] 데이터 최소 개수 완화 (장 초반/신규주 대응: 30 -> 2)
        # 데이터가 너무 적어도(2개 미만) 분석 불가하므로 최소한의 안전장치는 유지
        if not chart_data or len(chart_data) < 2: 
            return False, None, "데이터 부족", 0, 0, 0
            
        # 🌟 [추가] 물타기 모드인지 확인 (물타기 시에는 필터 완화)
        is_watering_mode = (str(condition_id) == "WATERING")

        # 🌟 [추가] 일봉 데이터 조회 (FDR 사용)
        start_dt = (datetime.now() - timedelta(days=150)).strftime("%Y-%m-%d")
        try:
            df_daily = await asyncio.wait_for(run_blocking(fdr.DataReader, stock_code, start_dt), timeout=5.0)
        except Exception:
            try:
                df_daily = await asyncio.wait_for(run_blocking(fdr.DataReader, f'KRX:{stock_code}', start_dt), timeout=5.0)
            except Exception as e_fdr:
                strategy_logger.warning(f"⚠️ [FDR] 일봉 데이터 조회 실패 ({stock_code}): {e_fdr}")
                df_daily = pd.DataFrame()

        df = pd.DataFrame(chart_data)
        # safe_int 대신 벡터화 연산 사용 (속도 최적화)
        col_map = {
            'close': ['cur_prc', 'current_price', 'close'],
            'open': ['open_pric', 'open_prc', 'open'],
            'high': ['high_pric', 'high_prc', 'high'],
            'low': ['low_pric', 'low_prc', 'low'],
            'volume': ['trde_qty', 'volume'],
            'date': ['cntr_tm', 'che_tm', 'time'] # 🌟 [추가] 날짜 필터링을 위한 컬럼
        }
        for col, candidates in col_map.items():
            found_key = next((k for k in candidates if k in df.columns), None)
            if found_key:
                # 날짜 컬럼은 숫자로 변환하지 않고 문자열 유지
                if col == 'date': df[col] = df[found_key].astype(str)
                else: df[col] = pd.to_numeric(df[found_key].astype(str).str.replace(r'[+,\-]', '', regex=True), errors='coerce').fillna(0).astype(int).abs()
            else:
                # 🌟 [수정] 날짜 컬럼은 빈 문자열로 초기화하여 .str 접근자 오류 방지
                if col == 'date': df[col] = ""
                else: df[col] = 0
        
        df = df.iloc[::-1].reset_index(drop=True)

        df['MA5'] = df['close'].rolling(window=5).mean()
        df['MA20'] = df['close'].rolling(window=20).mean()
        
        current_idx = len(df) - 1
        last_complete_idx = len(df) - 2

        current_close = int(df.loc[current_idx, 'close'])
        ma5 = df.loc[current_idx, 'MA5']
        ma20 = df.loc[current_idx, 'MA20']
        
        delta = df['close'].diff()
        delta = delta.fillna(0)
        
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        
        rs = gain / loss.replace(0, 1) 
        df['RSI'] = 100 - (100 / (1 + rs))
        
        current_rsi = df.loc[current_idx, 'RSI']
        if pd.isna(current_rsi): current_rsi = 50.0

        val_rsi = BOT_SETTINGS.get('RSI_LIMIT')
        rsi_limit = float(val_rsi) if val_rsi is not None else 70.0
        
        # [수정] 물타기 모드가 아닐 때만 RSI 필터 적용
        if not is_watering_mode and BOT_SETTINGS.get('USE_RSI_FILTER', True) and current_rsi > rsi_limit:
            strategy_logger.info(f"🛡️ [RSI필터] {stock_name}({stock_code}): 과매수 구간(RSI {current_rsi:.1f}) -> 진입 포기")
            return False, None, "RSI 과열", 0, 0, current_close

        last_candle = df.loc[last_complete_idx]
        open_p = last_candle['open']
        close_p = last_candle['close']
        high_p = last_candle['high']
        low_p = last_candle['low']

        total_len = high_p - low_p
        upper_shadow = high_p - max(close_p, open_p)

        upper_shadow_limit = float(BOT_SETTINGS.get('UPPER_SHADOW_LIMIT', 0.4))
        ratio = upper_shadow / total_len if total_len > 0 else 0
        # [수정] 물타기 모드가 아닐 때만 윗꼬리 필터 적용
        if not is_watering_mode and BOT_SETTINGS.get('USE_UPPER_SHADOW_FILTER', True) and ratio > upper_shadow_limit:
            strategy_logger.info(f"🛡️ [분봉필터] {stock_name}({stock_code}): 분봉 윗꼬리 과다({ratio:.2f}) [H:{high_p} L:{low_p} O:{open_p} C:{close_p}] -> 진입 포기")
            return False, None, "분봉 윗꼬리 과다", 0, 0, current_close

        # [수정] stock_info가 없으면 '분봉 데이터(df)'를 집계하여 대체 (일봉 데이터는 전일자일 수 있으므로 위험)
        if stock_info is None and not df.empty:
            try:
                # 🌟 [수정] 전체 데이터가 아닌 '당일' 데이터만 필터링하여 통계 계산
                today_str = datetime.now().strftime("%Y%m%d")
                df_today = df[df['date'].astype(str).str.startswith(today_str)]
                
                if not df_today.empty:
                    stock_info = {
                        '시가': int(df_today['open'].iloc[0]), # 시간순 정렬된 첫 데이터 = 시가
                        '고가': int(df_today['high'].max()),   # 당일 최고가
                        '저가': int(df_today['low'].min()),    # 당일 최저가
                        '현재가': int(df_today['close'].iloc[-1])
                    }
            except: pass

        # [추가] 일봉 기준 윗꼬리 필터 (실시간 정보 활용)
        if stock_info and BOT_SETTINGS.get('USE_DAILY_SHADOW_FILTER', True):
            d_open = abs(stock_info.get('시가', 0))
            d_high = abs(stock_info.get('고가', 0))
            d_low = abs(stock_info.get('저가', 0))
            d_curr = abs(stock_info.get('현재가', 0))
            
            if d_high > 0 and d_low > 0:
                d_range = d_high - d_low
                d_shadow = d_high - max(d_open, d_curr)
                d_ratio = d_shadow / d_range if d_range > 0 else 0
                daily_shadow_limit = float(BOT_SETTINGS.get('DAILY_SHADOW_LIMIT', 0.5))
            # [수정] 물타기 모드가 아닐 때만 일봉 윗꼬리 필터 적용
            if not is_watering_mode and d_ratio > daily_shadow_limit:
                    strategy_logger.info(f"🛡️ [일봉필터] {stock_name}({stock_code}): 일봉 윗꼬리 과다({d_ratio:.2f}) [H:{d_high} L:{d_low} O:{d_open} C:{d_curr}] -> 진입 포기")
                    return False, None, "일봉 윗꼬리 과다", 0, 0, current_close

        avg_vol_5 = df['volume'].iloc[-6:-1].mean()
        current_vol = df.loc[current_idx, 'volume']
        
        vol_ratio = float(BOT_SETTINGS.get('VOLUME_FILTER_RATIO', 0.3))
        # [수정] 물타기 모드가 아닐 때만 거래량 필터 적용 (눌림목에서는 거래량이 줄어들 수 있음)
        if not is_watering_mode and BOT_SETTINGS.get('USE_VOLUME_FILTER', True):
            if avg_vol_5 > 0 and current_vol < (avg_vol_5 * vol_ratio):
                 strategy_logger.info(f"🛡️ [기술적필터] {stock_name}({stock_code}): 거래량 부족(평균대비 {current_vol/avg_vol_5:.2f}) -> 진입 포기")
                 return False, None, "거래량 부족", 0, 0, current_close

        # 이미지 버퍼(BytesIO)를 받음
        minute_buf = await run_blocking(create_chart_image, stock_code, stock_name, chart_data)
        daily_buf = await run_blocking(create_daily_chart_image, df_daily, stock_code)
        
        if minute_buf and daily_buf:
            is_buy, reason, ai_sl_price, ai_target_price = await run_blocking(ask_ai_to_buy, minute_buf, daily_buf, condition_id)
            if is_buy:
                # 🌟 [추가] AI 손절가(파랑)/목표가(빨강) 차트 표시
                hlines = []
                colors = []
                
                # 🌟 [추가] 매수가(현재가) 초록색 점선
                if current_close > 0:
                    hlines.append(int(current_close))
                    colors.append('green')

                if ai_sl_price > 0:
                    hlines.append(int(ai_sl_price))
                    colors.append('blue')
                if ai_target_price > 0:
                    hlines.append(int(ai_target_price))
                    colors.append('red')

                if hlines:
                    hl_opts = dict(hlines=hlines, colors=colors, linestyle='--', linewidths=1.5, alpha=0.7)
                    new_minute_buf = await run_blocking(create_chart_image, stock_code, stock_name, chart_data, hl_opts)
                    if new_minute_buf:
                        minute_buf = new_minute_buf

                # 텔레그램 전송을 위해 이미지를 임시 파일로 저장
                combined_buf = await run_blocking(combine_chart_images, daily_buf, minute_buf)
                
                temp_path = f"/tmp/{stock_code}_{int(time.time())}.png"
                with open(temp_path, "wb") as f:
                    if combined_buf:
                        f.write(combined_buf.getbuffer())
                    else:
                        f.write(minute_buf.getbuffer()) # 병합 실패 시 분봉만 전송

                strategy_logger.info(f"🤖 [AI승인] {stock_name} ({stock_code}): 매수 추천! ({reason}) [손절: {ai_sl_price}, 목표: {ai_target_price}]")
                return True, temp_path, reason, ai_sl_price, ai_target_price, current_close
            else:
                strategy_logger.info(f"🛡️ [AI거절] {stock_name} ({stock_code}): 매수 보류 ({reason})")
                
                # 🌟 [수정] 물타기 모드일 경우 거절 시에도 차트 이미지 저장 (텔레그램 전송용)
                temp_path = None
                if str(condition_id) == "WATERING":
                    combined_buf = await run_blocking(combine_chart_images, daily_buf, minute_buf)
                    temp_path = f"/tmp/{stock_code}_{int(time.time())}_reject.png"
                    with open(temp_path, "wb") as f:
                        if combined_buf: f.write(combined_buf.getbuffer())
                        else: f.write(minute_buf.getbuffer())
                
                return False, temp_path, reason, 0, 0, current_close
        
        # [수정] 이미지 생성 실패 등 분석 불가 시 '거절(False)' 리턴
        return False, None, "차트 이미지 생성 실패", 0, 0, current_close

    except Exception as e:
        strategy_logger.error(f"차트 분석 중 오류 ({stock_name}({stock_code})): {e}")
        return False, None, f"분석 오류: {e}", 0, 0, 0
        
async def save_condition_settings(cond_id):
    """ 현재 설정을 조건식별 설정으로 DB에 저장 """
    if not cond_id: return
    
    # [수정] 전역 설정을 제외한 모든 설정을 저장 대상으로 함
    keys_to_save = [k for k in DEFAULT_SETTINGS.keys() if k not in GLOBAL_SETTINGS_KEYS]
    
    current_vals = {}
    for key in keys_to_save:
        if key in BOT_SETTINGS:
            current_vals[key] = BOT_SETTINGS[key]
    try:
        await run_blocking(db.set_kv, f"cond_settings_{cond_id}", current_vals)
    except: pass

async def load_condition_settings(cond_id):
    try: return await run_blocking(db.get_kv, f"cond_settings_{cond_id}")
    except: return None

async def apply_condition_preset(target_id, save_current=True):
    # 1. 현재 활성화된 조건식의 설정을 먼저 저장 (변경사항 보존)
    current_id = str(BOT_SETTINGS.get("CONDITION_ID", "0"))
    if save_current and current_id != str(target_id):
        await save_condition_settings(current_id)

    # 2. 타겟 조건식의 저장된 설정 불러오기 (없으면 프리셋 기본값)
    saved_settings = await load_condition_settings(target_id)
    
    # 업데이트 대상 키 목록 (전역 설정 제외)
    keys_to_update = [k for k in DEFAULT_SETTINGS.keys() if k not in GLOBAL_SETTINGS_KEYS]

    changed_count = 0
    desc = ""

    if saved_settings:
        # 저장된 설정이 있으면 우선 적용
        desc = "사용자설정(로드됨)"
        for key in keys_to_update:
            new_val = None
            if key in saved_settings:
                new_val = saved_settings[key]
            elif key in DEFAULT_SETTINGS:
                # 저장된 값은 없지만 기본값에는 있는 경우 (새로 추가된 옵션 등)
                new_val = DEFAULT_SETTINGS[key]
            
            if new_val is not None and BOT_SETTINGS.get(key) != new_val:
                BOT_SETTINGS[key] = new_val
                changed_count += 1
    else:
        # 저장된 설정이 없으면 프리셋 + 기본값 적용
        default_preset = STRATEGY_PRESETS.get(str(target_id), {})
        desc = default_preset.get("DESC", "기본설정")
        
        for key in keys_to_update:
            new_val = DEFAULT_SETTINGS.get(key) # 기본값
            if key in default_preset:
                new_val = default_preset[key] # 프리셋 값
            
            if BOT_SETTINGS.get(key) != new_val:
                BOT_SETTINGS[key] = new_val
                changed_count += 1

    strategy_logger.info(f"🎨 [전략변경] 조건식 {target_id}번({desc}) 적용 완료. (설정변경 {changed_count}건)")
    
    BOT_SETTINGS["CONDITION_ID"] = str(target_id)
    await save_settings_to_file()
    return True

async def check_auto_condition_change():
    if not BOT_SETTINGS.get('USE_SCHEDULER', False): return False
    if BOT_SETTINGS.get('BOT_STATUS') == 'RESTARTING': return False
    
    # [추가] 설정 로드 실패 상태라면 스케줄러 동작 중지 (잘못된 기본값으로 덮어쓰기 방지)
    if not SETTINGS_LOADED_SUCCESSFULLY:
        return False

    try:
        now_time = datetime.now().time()
        current_id = str(BOT_SETTINGS.get('CONDITION_ID', '0'))

        l_start_str = BOT_SETTINGS.get('LUNCH_START', '10:30')
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
            strategy_logger.info(f"⏰ [스케줄러] 조건식 변경 실행! ({current_id} -> {target_id}) [현재: {now_time.strftime('%H:%M')}]")
            await apply_condition_preset(target_id)
            msg = f"⏰ [스케줄러] 조건식 변경\n{current_id}번 ➡️ {target_id}번"
            send_telegram_msg(msg)

            # [수정] 스케줄러 변경 시에도 현재 상태 유지
            current_status = BOT_SETTINGS.get("BOT_STATUS", "STOPPED")
            BOT_SETTINGS['BOT_STATUS'] = "RESTARTING"
            BOT_SETTINGS["_INTENDED_STATUS_"] = current_status
            await save_settings_to_file()
            return True
    except Exception as e:
        strategy_logger.error(f"스케줄러 오류: {e}")
    return False

async def run_self_diagnosis():
    strategy_logger.info("🔍 시스템 자가 진단 (Self Diagnosis)")
    try:
        await run_blocking(db.get_kv, "test_key")
        strategy_logger.info("✅ [DB] SQLite 연결 정상")
    except Exception as e:
        strategy_logger.error(f"❌ [DB] 연결 오류! ({e})")
        
    settings = await run_blocking(db.get_kv, "settings")
    if not settings:
        strategy_logger.warning("⚠️ [설정] DB에 설정이 없어 기본값을 저장합니다.")
        await save_settings_to_file()

async def set_booting_status(status_msg="BOOTING", target_mode=None):
    try:
        now = datetime.now()
        is_mock = MOCK_TRADE if target_mode is None else target_mode
        
        old_trading_state = {}
        old_data = await run_blocking(db.get_kv, "status")
        if old_data: old_trading_state = old_data.get('trading_state', {})

        status_data = {
            "bot_status": status_msg,
            "active_mode": "모의투자" if is_mock else "REAL",
            "account_no": KIWOOM_ACCOUNT_NO,
            "last_sync": now.isoformat(),
            "trading_state": old_trading_state,
            "is_offline": False
        }
        await run_blocking(db.set_kv, "status", status_data)
    except Exception as e:
        strategy_logger.error(f"⚠️ 부팅 상태 저장 실패: {e}")

async def load_settings_from_file():
    global BOT_SETTINGS, SETTINGS_LOADED_SUCCESSFULLY
    try:
        # [수정] 설정 로드 실패 시 즉시 초기화하지 않고 재시도 (DB 락 방지)
        saved_settings = None
        for _ in range(3):
            saved_settings = await run_blocking(db.get_kv, "settings")
            if saved_settings: break
            await asyncio.sleep(0.5)

        if not saved_settings:
            # [수정] DB 읽기 실패 시, DB를 덮어쓰지 않고 메모리상에서만 기본값 사용 (설정 초기화 방지)
            strategy_logger.warning("⚠️ [설정] 설정을 불러올 수 없어 메모리상에서 기본값을 사용합니다. (DB 덮어쓰기 방지)")
            saved_settings = DEFAULT_SETTINGS.copy()
            SETTINGS_LOADED_SUCCESSFULLY = False
        else:
            SETTINGS_LOADED_SUCCESSFULLY = True

        saved_mock_mode = saved_settings.get("MOCK_TRADE")
        if saved_mock_mode is not None and saved_mock_mode != MOCK_TRADE:
            strategy_logger.warning(f"⚠️ 투자 모드 변경 감지. 재시작합니다...")
            await set_booting_status("RESTARTING", target_mode=saved_mock_mode)
            await asyncio.sleep(1)
            sys.exit(0)

        current_cond_id = str(BOT_SETTINGS.get("CONDITION_ID") or "0")
        raw_new_id = saved_settings.get("CONDITION_ID")
        new_cond_id = str(raw_new_id) if (raw_new_id is not None and str(raw_new_id).strip() != "") else "0"

        settings_updated_via_preset = False
        if current_cond_id != new_cond_id:
             # [수정] 조건식 변경 시 해당 조건식의 설정 로드 (프로필 전환)
             # 초기화 전(부팅 시)에는 현재(기본값) 설정을 저장하지 않음
             await apply_condition_preset(new_cond_id, save_current=IS_INITIALIZED)
             settings_updated_via_preset = True

             if IS_INITIALIZED:
                 strategy_logger.warning(f"조건검색식 변경 감지 (수동) ({current_cond_id} -> {new_cond_id}).")
                 
                 # [수정] 변경 요청 시의 봇 상태를 유지 (강제 RUNNING 제거)
                 intended_status = saved_settings.get("BOT_STATUS", BOT_SETTINGS.get("BOT_STATUS", "STOPPED"))
                 BOT_SETTINGS["_INTENDED_STATUS_"] = intended_status
                 BOT_SETTINGS["BOT_STATUS"] = "RESTARTING"
                 await save_settings_to_file()
                 return

        # 조건식이 변경되지 않았을 때만 일반 설정 로드 (프로필 로드 시 덮어쓰기 방지)
        if not settings_updated_via_preset:
            for key, default_val in DEFAULT_SETTINGS.items():
                val = saved_settings.get(key)
                if key == "CONDITION_ID": val = str(val) if (val is not None and val != "") else "0"
                elif key == "USE_MARKET_TIME": val = bool(val) if val is not None else True
                elif key == "USE_AI_STOP_LOSS": val = bool(val) if val is not None else True
                elif key == "AI_STOP_LOSS_SAFETY_LIMIT": val = float(val) if val is not None else -5.0
                elif key == "TIME_CUT_MINUTES": val = int(val) if val is not None else 20
                elif key == "RSI_LIMIT": val = float(val) if val is not None else 70.0
                elif key == "USE_MARKET_FILTER": val = bool(val) if val is not None else False
                elif key == "AI_REJECTION_COOLDOWN_MIN": val = int(val) if val is not None else 10
                elif key == "VOLUME_FILTER_RATIO": val = float(val) if val is not None else 0.3
                elif key == "UPPER_SHADOW_LIMIT": val = float(val) if val is not None else 0.4
                elif key == "DAILY_SHADOW_LIMIT": val = float(val) if val is not None else 0.5
                elif key == "THEME_TOP_N": val = int(val) if val is not None else 3
                elif key in ["USE_HOGA_FILTER", "USE_FAKE_BUY_FILTER", "USE_RSI_FILTER", "USE_TIME_CUT", "USE_UPPER_SHADOW_FILTER", "USE_DAILY_SHADOW_FILTER", "USE_STOP_LOSS", "USE_THEME_FILTER", "USE_TRAILING_STOP", "USE_RE_ENTRY_COOLDOWN", "USE_AI_REJECTION_COOLDOWN", "USE_PARTIAL_PROFIT", "USE_VOLUME_FILTER", "USE_DYNAMIC_TS"]:
                    val = bool(val) if val is not None else True
                elif key == "USE_WATERING": val = bool(val) if val is not None else False
                elif key == "MAX_WATERING_COUNT": val = int(val) if val is not None else 1
                elif key == "MAX_WATERING_AMOUNT": val = int(val) if val is not None else 2000000
                elif key == "MAX_BUY_SELL_RATIO": val = float(val) if val is not None else 10.0
                elif key == "PARTIAL_PROFIT_RATE": val = float(val) if val is not None else 50.0
                elif key in ["DYN_TS_LV1_TRIGGER", "DYN_TS_LV1_DROP", "DYN_TS_LV2_TRIGGER", "DYN_TS_LV2_DROP", "DYN_TS_LV3_TRIGGER", "DYN_TS_LV3_DROP"]:
                    val = float(val) if val is not None else default_val
                elif key == "USE_BREAK_TIME": val = bool(val) if val is not None else False
                elif key in ["BREAK_START", "BREAK_END"]: val = str(val) if val is not None else default_val
                
                if key in ["MORNING_START", "MORNING_COND", "LUNCH_START", "LUNCH_COND", "AFTERNOON_START", "AFTERNOON_COND", "OVERNIGHT_COND_IDS"]:
                     if val is not None: BOT_SETTINGS[key] = str(val)
                else:
                     BOT_SETTINGS[key] = val if val is not None else default_val
        
        # [추가] 스케줄러 설정 로드 상태 디버깅 로그
        if IS_INITIALIZED:
            strategy_logger.debug(f"🔍 [설정로드] M={BOT_SETTINGS.get('MORNING_COND')} L={BOT_SETTINGS.get('LUNCH_COND')} A={BOT_SETTINGS.get('AFTERNOON_COND')}")

        debug_val = BOT_SETTINGS.get("DEBUG_MODE", False)
        new_level = logging.DEBUG if debug_val else logging.INFO
        strategy_logger.setLevel(new_level)
        if ws_manager: ws_manager.set_debug_mode(debug_val)
        set_api_debug_mode(debug_val)
        setup_logging(debug_val)
    except Exception as e:
        strategy_logger.error(f"설정 로드 실패: {e}")
        BOT_SETTINGS = DEFAULT_SETTINGS.copy()

async def save_settings_to_file():
    # [수정] 설정 로드에 실패한 상태라면 저장을 막아 DB 덮어쓰기 방지
    if not SETTINGS_LOADED_SUCCESSFULLY:
        strategy_logger.warning("⚠️ [설정저장] 설정 로드 실패 상태이므로 저장을 건너뜁니다. (DB 보호)")
        return
    try: 
        await run_blocking(db.set_kv, "settings", BOT_SETTINGS)
        # 현재 조건식 설정도 함께 저장 (대시보드 변경사항 실시간 반영)
        await save_condition_settings(str(BOT_SETTINGS.get("CONDITION_ID", "0")))
    except: pass

async def save_status_to_file(force=False):
    global last_heartbeat_time, last_db_save_time, TRADING_STATE, BOT_SETTINGS, IS_INITIALIZED, RE_ENTRY_COOLDOWN, last_saved_state_hash, TODAY_REALIZED_PROFIT
    if not IS_INITIALIZED: return

    now = datetime.now()
    if not force and (now - last_heartbeat_time).total_seconds() < 2.0: return
    last_heartbeat_time = now

    try:
        bot_status = BOT_SETTINGS.get("BOT_STATUS") or "STOPPED"
        display_status = bot_status
        if bot_status == "RUNNING" and not is_market_open():
            display_status = "SLEEPING"
        elif bot_status == "RUNNING" and is_break_time():
            display_status = "BREAK_TIME"

        enriched_state = {}
        total_buy_amt = 0; total_eval_amt = 0; 

        for code, info in TRADING_STATE.items():
            info_copy = info.copy()
            if isinstance(info_copy.get('order_time'), datetime):
                info_copy['order_time'] = info_copy['order_time'].strftime('%Y-%m-%d %H:%M:%S')
            if 'last_cancel_try' in info_copy and isinstance(info_copy['last_cancel_try'], datetime):
                info_copy['last_cancel_try'] = info_copy['last_cancel_try'].strftime('%Y-%m-%d %H:%M:%S')
            
            effective_sl = info.get('custom_sl_rate')
            if effective_sl is None:
                effective_sl = BOT_SETTINGS.get('STOP_LOSS_RATE')

            info_copy['applied_strategy'] = {
                'sl': effective_sl,
                'ts_start': BOT_SETTINGS.get('TRAILING_START_RATE'),
                'ts_stop': BOT_SETTINGS.get('TRAILING_STOP_RATE')
            }
            if 'custom_sl_rate' in info:
                info_copy['applied_strategy']['custom_sl'] = info['custom_sl_rate']
            
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
        total_profit_rate = (total_profit_amt / total_buy_amt * 100) if total_buy_amt > 0 else 0.0

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

        # MARKET_STATUS 날짜 객체 안전하게 변환
        market_status_safe = MARKET_STATUS.copy()
        if isinstance(market_status_safe.get('last_check'), datetime):
            market_status_safe['last_check'] = market_status_safe['last_check'].strftime('%Y-%m-%d %H:%M:%S')

        status_data = {
            "bot_status": display_status,
            "active_mode": "모의투자" if MOCK_TRADE else "REAL",
            "account_no": KIWOOM_ACCOUNT_NO,
            "last_sync": now.strftime('%Y-%m-%d %H:%M:%S'),
            "trading_state": enriched_state,
            "account_summary": account_summary,
            "re_entry_cooldown": cooldown_data,
            "leading_themes": LEADING_THEME_NAMES,
            "current_settings": { 
                 "use_ai_sl": BOT_SETTINGS.get("USE_AI_STOP_LOSS", True),
                 "ai_safety_limit": BOT_SETTINGS.get("AI_STOP_LOSS_SAFETY_LIMIT", -5.0),
                 "time_cut": BOT_SETTINGS.get("TIME_CUT_MINUTES", 20),
                 "rsi_limit": BOT_SETTINGS.get("RSI_LIMIT", 70.0),
                 "upper_shadow_limit": BOT_SETTINGS.get("UPPER_SHADOW_LIMIT", 0.4),
                 "daily_shadow_limit": BOT_SETTINGS.get("DAILY_SHADOW_LIMIT", 0.5),
                 "global_sl": BOT_SETTINGS.get("STOP_LOSS_RATE", -1.5),
                 "use_market_filter": BOT_SETTINGS.get("USE_MARKET_FILTER", False),
                 "market_status": market_status_safe,
                 "use_volume_filter": BOT_SETTINGS.get("USE_VOLUME_FILTER", True),
                 "volume_filter_ratio": BOT_SETTINGS.get("VOLUME_FILTER_RATIO", 0.3),
                 "use_hoga_filter": BOT_SETTINGS.get("USE_HOGA_FILTER", True),
                 "use_rsi_filter": BOT_SETTINGS.get("USE_RSI_FILTER", True),
                 "use_time_cut": BOT_SETTINGS.get("USE_TIME_CUT", True),
                 "use_upper_shadow_filter": BOT_SETTINGS.get("USE_UPPER_SHADOW_FILTER", True),
                 "use_daily_shadow_filter": BOT_SETTINGS.get("USE_DAILY_SHADOW_FILTER", True),
                 "use_stop_loss": BOT_SETTINGS.get("USE_STOP_LOSS", True),
                 "use_trailing_stop": BOT_SETTINGS.get("USE_TRAILING_STOP", True),
                 "use_dynamic_ts": BOT_SETTINGS.get("USE_DYNAMIC_TS", True)
            },
            "is_offline": False
        }

        # 🌟 [최적화] last_sync를 제외한 데이터로 해시 계산 (불필요한 DB 쓰기 방지)
        hash_data = status_data.copy()
        hash_data.pop('last_sync', None)

        current_hash = hashlib.md5(json.dumps(hash_data, sort_keys=True).encode()).hexdigest()
        
        # 저장 조건: 강제저장 OR 데이터변경 OR 마지막저장 후 3초 경과(Heartbeat)
        # 대시보드 연결 끊김 방지를 위해 10초 -> 3초로 단축
        should_save = force or (current_hash != last_saved_state_hash) or ((now - last_db_save_time).total_seconds() > 3.0)

        if should_save:
            await run_blocking(db.set_kv, "status", status_data)
            last_saved_state_hash = current_hash
            last_db_save_time = now

    except Exception as e:
        strategy_logger.error(f"상태 저장 실패: {e}")

# ---------------------------------------------------------
# 7. 매매 및 주문 실행 로직
# ---------------------------------------------------------
async def _load_initial_balance():
    global TRADING_STATE, IS_INITIALIZED, RE_ENTRY_COOLDOWN
    strategy_logger.info("기존 보유 잔고를 확인합니다...")

    old_condition_map = {}
    old_overnight_map = {}
    old_sl_map = {}
    old_target_price_map = {}
    old_partial_map = {}
    old_order_time_map = {}
    old_watering_map = {} # [추가] 물타기 횟수 복구용
    old_peak_map = {} # [추가] 고점 수익률 복구용
    RE_ENTRY_COOLDOWN = {}

    try:
        old_data = await run_blocking(db.get_kv, "status")
        if old_data:
            for code, info in old_data.get('trading_state', {}).items():
                if info.get('condition_from') and info['condition_from'] != "기존보유":
                    old_condition_map[code] = info['condition_from']
                if info.get('overnight_approved', False):
                    old_overnight_map[code] = True
                
                if info.get('custom_sl_rate'):
                    old_sl_map[code] = info['custom_sl_rate']
                if info.get('ai_target_price'):
                    old_target_price_map[code] = info['ai_target_price']
                if info.get('partial_profit_taken'):
                    old_partial_map[code] = info['partial_profit_taken']
                if info.get('peak_profit_rate') is not None:
                    old_peak_map[code] = info['peak_profit_rate']
                if info.get('watering_count'):
                    old_watering_map[code] = info['watering_count']
                
                if info.get('order_time'):
                    old_order_time_map[code] = info['order_time']

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
                buy_price = safe_int(item['pur_pric'])
                buy_qty = safe_int(item['rmnd_qty'])
                profit_rate = float(item['prft_rt'])
                stk_nm = item.get('stk_nm', stock_code)

                restored_condition = old_condition_map.get(stock_code, "기존보유")
                if restored_condition == "기존보유":
                    restored_condition = PENDING_ORDER_CONDITIONS.get(stock_code, "기존보유")

                restored_order_time = datetime.now()
                if stock_code in old_order_time_map:
                    try: restored_order_time = datetime.strptime(old_order_time_map[stock_code], '%Y-%m-%d %H:%M:%S')
                    except: pass

                # [추가] 고점 수익률 복구 (현재 수익률이 더 높으면 현재값 사용)
                restored_peak = old_peak_map.get(stock_code, max(profit_rate, 0))
                if profit_rate > restored_peak: restored_peak = profit_rate

                stock_data = {
                    "stk_nm": stk_nm, "buy_price": buy_price, "buy_qty": buy_qty,
                    "trailing_active": False, "peak_profit_rate": restored_peak,
                    "status": "보유 (잔고)", "current_profit_rate": profit_rate,
                    "order_time": restored_order_time,
                    "condition_from": restored_condition,
                    "overnight_approved": old_overnight_map.get(stock_code, False),
                    "ai_target_price": old_target_price_map.get(stock_code, 0),
                    "partial_profit_taken": old_partial_map.get(stock_code, False),
                    "watering_count": old_watering_map.get(stock_code, 0)
                }

                if stock_code in old_sl_map:
                    stock_data['custom_sl_rate'] = old_sl_map[stock_code]
                    strategy_logger.info(f"💾 [복구] {stk_nm}: AI 지정 손절가 {old_sl_map[stock_code]}% 복원됨")

                if stock_data['ai_target_price'] > 0:
                    strategy_logger.info(f"💾 [복구] {stk_nm}: AI 목표가 {stock_data['ai_target_price']}원 복원됨")

                TRADING_STATE[stock_code] = stock_data
                initial_stocks.append((stock_code, "10"))
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
                    # [추가] 수량 변경 감지 (외부 매매/물타기 등) -> 트레일링 고점 리셋
                    # 평단가가 변하면 기존 고점 수익률은 무의미해지므로 현재 기준으로 재설정해야 함
                    old_qty = TRADING_STATE[code].get('buy_qty', 0)
                    new_qty = safe_int(item['rmnd_qty'])
                    
                    if old_qty > 0 and old_qty != new_qty:
                        strategy_logger.info(f"📦 [수량변경] {code}: {old_qty} -> {new_qty}. 트레일링 고점 리셋.")
                        TRADING_STATE[code]['peak_profit_rate'] = max(server_profit, 0)

                    TRADING_STATE[code]['buy_price'] = safe_int(item['pur_pric'])
                    TRADING_STATE[code]['buy_qty'] = new_qty
                    if TRADING_STATE[code]['status'] == '매수주문':
                        TRADING_STATE[code]['status'] = '보유 (체결)'
                        strategy_logger.info(f"🔄 [동기화] {code} 매수주문 -> 보유 상태로 변경됨")
                    
                    # [삭제] 서버 수익률로 Peak를 덮어쓰면 수수료/세금 계산 기준 차이로 인해
                    # 트레일링 스탑이 오작동할 수 있으므로 제거함 (로컬 계산값 신뢰)
                else:
                    restored_condition = PENDING_ORDER_CONDITIONS.get(code, "외부매수/동기화")
                    TRADING_STATE[code] = {
                        "stk_nm": item.get('stk_nm', code),
                        "buy_price": safe_int(item['pur_pric']),
                        "buy_qty": safe_int(item['rmnd_qty']),
                        "trailing_active": False, "peak_profit_rate": max(server_profit, 0),
                        "status": "보유 (동기화됨)", "current_profit_rate": server_profit,
                        "order_time": datetime.now(),
                        "condition_from": restored_condition,
                        "watering_count": 0
                    }
                    if ws_manager: ws_manager.add_subscription(code, "10")

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
            if not is_daytime_safe and "매도" not in status: continue

            if status == '매수주문':
                if (datetime.now() - state.get('order_time', datetime.now())).total_seconds() > 300:
                    del TRADING_STATE[code]
                continue

            strategy_logger.info(f"🗑️ [잔고동기화] {code} 잔고 부재(매도완료)로 목록에서 제거")
            if BOT_SETTINGS.get('USE_RE_ENTRY_COOLDOWN', True):
                try:
                    val = BOT_SETTINGS.get('RE_ENTRY_COOLDOWN_MIN')
                    cooldown_min = int(val) if val is not None else 30
                except: cooldown_min = 30
                RE_ENTRY_COOLDOWN[code] = datetime.now() + timedelta(minutes=cooldown_min)
                strategy_logger.info(f"⏳ [쿨타임설정] {code}: 잔고소멸(매도) -> {cooldown_min}분간 재진입 금지")
            del TRADING_STATE[code]

    except Exception as e:
        strategy_logger.error(f"잔고 동기화 중 오류: {e}")

async def _sync_initial_condition_list():
    target_id = str(BOT_SETTINGS.get('CONDITION_ID') or "0")
    cond_idx = None
    
    # 🌟 [수정] 조건식 목록이 DB에 저장될 때까지 잠시 대기 (최대 5초)
    # 로그인 직후에는 아직 CNSRLST 응답이 안 왔을 수 있음
    conditions = []
    for _ in range(5):
        try:
            data = await run_blocking(db.get_kv, "conditions")
            if data and 'conditions' in data and len(data['conditions']) > 0:
                conditions = data['conditions']
                break
        except: pass
        await asyncio.sleep(1)

    if conditions:
        for idx, cond in enumerate(conditions):
            # ID 매칭 (001 vs 1 등 유연하게)
            c_id = str(cond['id']).strip()
            t_id = target_id.strip()
            
            # 정확한 일치 또는 숫자 변환 일치 확인
            if c_id == t_id or str(int(c_id)) == str(int(t_id)):
                cond_idx = idx
                break
        
        if cond_idx is None:
            strategy_logger.warning(f"⚠️ 설정된 조건식 ID({target_id})를 목록에서 찾을 수 없습니다. 첫 번째 조건식(0번)을 사용합니다.")
            cond_idx = 0
            
        strategy_logger.info(f"🔎 조건식 ID '{target_id}' -> 인덱스 '{cond_idx}' ({conditions[cond_idx]['name']})로 스냅샷 요청")
        if ws_manager: ws_manager.request_condition_snapshot(cond_idx)
    else:
        strategy_logger.warning("⚠️ 조건식 목록을 불러오지 못했습니다(DB 비어있음). 인덱스 0으로 시도합니다.")
        if ws_manager: ws_manager.request_condition_snapshot(0)

def update_snapshot_progress(snapshot_meta, condition_names, condition_id, stock_name, skipped=False):
    """ 스냅샷 진행률 업데이트 및 로그 출력 """
    if not snapshot_meta: return
    
    sid = snapshot_meta['id']
    
    if sid not in SNAPSHOT_PROGRESS:
        # [수정] total 카운트도 함께 초기화
        SNAPSHOT_PROGRESS[sid] = {'done': 0, 'total': snapshot_meta['total']}
    
    SNAPSHOT_PROGRESS[sid]['done'] += 1
    done = SNAPSHOT_PROGRESS[sid]['done']
    total = SNAPSHOT_PROGRESS[sid]['total']
    percent = (done / total) * 100
    
    status_msg = "완료" if not skipped else "스킵"
    c_name = condition_names.get(str(condition_id), str(condition_id))
    
    # [수정] 로그 폭주 방지를 위해 INFO 레벨 로그는 20건마다 또는 스킵/완료 시에만 출력
    log_func = strategy_logger.info if skipped or (done % 20 == 0) or (done == total) else strategy_logger.debug
    log_func(f"📊 [스냅샷] {c_name}: {percent:.1f}% {status_msg} ({done}/{total}) - {stock_name}")
    
    if done >= total:
        strategy_logger.info(f"✅ [스냅샷완료] {c_name} 분석 종료 ({total}종목)")
        if sid in SNAPSHOT_PROGRESS: del SNAPSHOT_PROGRESS[sid]

def get_tick_size(price, market="KOSPI"):
    """ 
    가격대별 호가 단위(Tick Size) 계산 
    - 2023년 1월 개정된 KRX 업무규정 반영 (주식 기준)
    - 참고: ETF/ETN/ELW는 2,000원 이상일 경우 통상 5원 단위임
    """
    if price < 2000: return 1
    if price < 5000: return 5
    if price < 20000: return 10
    if price < 50000: return 50
    if price < 200000: return 100
    if price < 500000:
        return 500 if market == "KOSPI" else 100
    return 1000 if market == "KOSPI" else 100

async def process_single_stock_signal(stock_code, event_type, condition_id, condition_names, initial_price=None, snapshot_meta=None):
    global TRADING_STATE, PROCESSING_STOCKS, PENDING_ORDER_CONDITIONS, BUY_ATTEMPT_HISTORY, SNAPSHOT_PROGRESS
    
    order_amount = BOT_SETTINGS.get('ORDER_AMOUNT') or 100000
    use_hoga_filter = BOT_SETTINGS.get('USE_HOGA_FILTER', True)
    use_fake_buy_filter = BOT_SETTINGS.get('USE_FAKE_BUY_FILTER', True)
    min_ratio = float(BOT_SETTINGS.get('MIN_BUY_SELL_RATIO') or 0.5)
    max_ratio = float(BOT_SETTINGS.get('MAX_BUY_SELL_RATIO') or 10.0)
    
    current_cond_name = condition_names.get(condition_id, "알수없음")
    stk_name = ws_manager.master_stock_names.get(stock_code, stock_code)
    
    # 🌟 [추가] AI 분석 진입 여부 플래그 (불필요한 대기 방지용)
    ai_analysis_triggered = False
    
    try:
        async with ANALYSIS_SEMAPHORE:
            strategy_logger.info(f"🔔 [조건포착] {stk_name} ({stock_code}) 분석 시작")
            
            # 🌟 [추가] 매수 중지 시간대 체크
            if is_break_time():
                start = BOT_SETTINGS.get("BREAK_START", "??:??")
                end = BOT_SETTINGS.get("BREAK_END", "??:??")
                strategy_logger.info(f"⛔ [매수중지시간] {stk_name}({stock_code}): 휴식 시간({start}~{end})입니다.")
                return
            
            # 🌟 [수정] 종목별 시장 구분 후 맞춤형 필터 적용
            if BOT_SETTINGS.get("USE_MARKET_FILTER", False):
                # 1. 종목의 시장 찾기 (기본값 KOSPI)
                market_type = STOCK_MARKET_MAP.get(stock_code, 'KOSPI') 
                index_code = "101" if market_type == "KOSDAQ" else "001"
                
                # 2. 해당 시장의 지수 상태 확인
                market_status = MARKET_STATUS.get(index_code, {})
                is_bullish = market_status.get('is_bullish', True) # 기본값 True(안전)
                
                if not is_bullish:
                    market_name = market_status.get('name', market_type)
                    strategy_logger.warning(f"📉 [지수필터] {stk_name}({market_name}): 지수 하락장(음봉 발생)으로 매수 금지됨")
                    RE_ENTRY_COOLDOWN[stock_code] = datetime.now() + timedelta(minutes=10)
                    return

            # 🌟 [추가] 시장 주도 테마 필터 (선택 옵션)
            if BOT_SETTINGS.get("USE_THEME_FILTER", True) and str(condition_id) != "WATERING":
                if LEADING_THEME_STOCKS and stock_code not in LEADING_THEME_STOCKS:
                    # 상위 N개 테마에 포함되지 않은 종목은 진입 포기
                    top_n = BOT_SETTINGS.get("THEME_TOP_N", 3)
                    strategy_logger.info(f"🛡️ [테마필터] {stk_name}({stock_code}): 당일 주도 테마(상위 {top_n}개)에 속하지 않아 진입 포기")
                    RE_ENTRY_COOLDOWN[stock_code] = datetime.now() + timedelta(minutes=5)
                    return

            stock_info = None
            current_price = 0
            stk_nm = stk_name
            
            if initial_price and initial_price > 0:
                current_price = initial_price
                # [최적화] 이름이 코드와 같을 때(이름 모름) API 호출 대신 마스터 데이터 재확인
                if stk_name == stock_code:
                    master_name = ws_manager.master_stock_names.get(stock_code)
                    if master_name:
                        stk_nm = master_name
                    else:
                        # 마스터에도 없으면 어쩔 수 없이 API 호출 (하지만 빈도는 낮음)
                        await GENERAL_API_LIMITER.wait()
                        stock_info = await run_blocking(fn_ka10001_get_stock_info, stock_code)
                        if stock_info: stk_nm = stock_info.get('종목명', stock_code)
                else: stk_nm = stk_name
                debug_log(f"⚡ [Speed] {stk_nm}: 웹소켓 가격({current_price}) 사용 -> API 생략")
            else:
                for attempt in range(3):
                    await GENERAL_API_LIMITER.wait() # 🌟 일반 리미터 (0.2초)
                    stock_info = await run_blocking(fn_ka10001_get_stock_info, stock_code)
                    if stock_info:
                        current_price = abs(stock_info.get('현재가', 0))
                        if current_price == 0: current_price = abs(stock_info.get('시가', 0))
                        if current_price > 0: break
                    await asyncio.sleep(1.0)
                
                if stock_info and stock_info.get('종목명'):
                    stk_nm = stock_info.get('종목명')

            # [수정] 가격 정보가 없어도 즉시 리턴하지 않고, 차트 분석 단계에서 가격 확보 시도
            if current_price <= 0:
                try:
                    await CHART_API_LIMITER.wait() # 🌟 차트(Fallback)는 차트 리미터 사용
                    fallback_chart = await run_blocking(fn_ka10080_get_minute_chart, stock_code, tick="1")
                    if fallback_chart and len(fallback_chart) > 0:
                        current_price = abs(int(fallback_chart[0]['cur_prc']))
                        strategy_logger.info(f"⚠️ [가격복구] {stock_code}: 기본정보 실패 -> 차트데이터로 가격({current_price}) 확보")
                except Exception as e:
                    strategy_logger.error(f"가격 복구 시도 실패: {e}")
            
            # 가격이 여전히 0이어도 아래 AI 분석에서 차트 데이터를 가져오므로 거기서 가격을 얻을 수 있음.
            # 단, 호가 필터는 가격이 없으면 정확도가 떨어질 수 있으나, 매수 잔량이 있으면 진행.

            if use_hoga_filter:
                hoga_data = None
                # 🌟 [수정] 호가 조회 강화 (3회 시도, 대기 시간 확보)
                for retry in range(3):
                    await GENERAL_API_LIMITER.wait() # 🌟 일반 리미터 (0.3초)
                    hoga_data = await run_blocking(fn_ka10004_get_hoga, stock_code)
                    # [수정] 데이터가 있고 매도 잔량이 0보다 커야 유효한 데이터로 인정 (0이면 재시도)
                    if hoga_data and hoga_data.get('sell_total', 0) > 0: 
                        break
                    if retry < 2: await asyncio.sleep(0.5) # 🌟 [수정] 대기 시간 증가 (0.2 -> 0.5)

                if hoga_data and hoga_data.get('sell_total', 0) > 0:
                    buy_total = hoga_data['buy_total']
                    sell_total = hoga_data['sell_total']
                    
                    ratio = buy_total / sell_total
                    if ratio < min_ratio:
                        strategy_logger.info(f"🛡️ [호가필터] {stk_nm}({stock_code}) 진입 금지 (비율: {ratio:.2f})")
                        RE_ENTRY_COOLDOWN[stock_code] = datetime.now() + timedelta(minutes=5)
                        return
                    if use_fake_buy_filter and ratio > max_ratio:
                        strategy_logger.info(f"🛡️ [호가필터] {stk_nm}({stock_code}) 진입 금지 (비율과다: {ratio:.2f} > {max_ratio}) - 허매수 의심")
                        RE_ENTRY_COOLDOWN[stock_code] = datetime.now() + timedelta(minutes=5)
                        return
                else:
                     # 🌟 [수정] 호가 데이터 조회 실패 시에도 매수 기회를 놓치지 않도록 필터 통과 (로그만 기록)
                     if MOCK_TRADE:
                         strategy_logger.debug(f"ℹ️ [호가미제공] {stk_nm}({stock_code}) -> 모의투자 데이터 부재로 필터 생략")
                     else:
                         strategy_logger.info(f"ℹ️ [호가데이터없음] {stk_nm}({stock_code}) -> 호가 필터 생략")

            # 🌟 [마킹] 여기서부터 AI 분석 진입 (이후에는 Rate Limit 보호를 위해 대기 필요)
            ai_analysis_triggered = True
            
            # AI 분석 및 차트 이미지 경로 획득
            is_good_chart, image_path, ai_reason, ai_sl_price, ai_target_price, chart_price = await analyze_chart_pattern(stock_code, stk_nm, condition_id, stock_info)
            
            # 🌟 [추가] 분석 과정에서 얻은 차트 가격으로 현재가 갱신 (API 실패 대비)
            if current_price <= 0 and chart_price > 0:
                current_price = chart_price
                strategy_logger.info(f"⚠️ [가격최종복구] {stk_nm}({stock_code}): 차트분석 결과로 가격({current_price}) 설정")
            
            # 🌟 [최적화] 로컬 필터(RSI, 윗꼬리 등)로 거절된 경우 API 호출이 없었으므로 쿨타임 대기 스킵
            if not is_good_chart and ai_reason in ["데이터 부족", "RSI 과열", "윗꼬리 과다", "일봉 윗꼬리 과다", "거래량 부족"]:
                ai_analysis_triggered = False

            if not is_good_chart:
                if image_path and os.path.exists(image_path): os.remove(image_path)
                if BOT_SETTINGS.get('USE_AI_REJECTION_COOLDOWN', True):
                    val = BOT_SETTINGS.get('AI_REJECTION_COOLDOWN_MIN')
                    ai_cooldown = int(val) if val is not None else 10
                    
                    # 🌟 [추가] 거절 사유가 '일시적 조정' 등 긍정적인 뉘앙스라면 쿨타임 단축
                    reason_check = ai_reason if ai_reason else ""
                    if any(k in reason_check for k in ["조정", "눌림", "대기", "관망", "재확인"]):
                        ai_cooldown = max(1, ai_cooldown // 3) # 1/3로 단축 (최소 1분)
                        strategy_logger.info(f"⏳ [쿨타임단축] {stk_nm}({stock_code}): '{ai_reason}' -> {ai_cooldown}분 후 재진입 시도")

                    RE_ENTRY_COOLDOWN[stock_code] = datetime.now() + timedelta(minutes=ai_cooldown)
                return

            if current_price <= 0:
                strategy_logger.warning(f"🚫 [진입불가] {stk_nm}({stock_code}): 현재가 오류 ({current_price})")
                if image_path and os.path.exists(image_path): os.remove(image_path)
                return

            buy_qty = int((order_amount * 0.95) // current_price)
            if buy_qty == 0:
                strategy_logger.warning(f"🚫 [진입불가] {stk_nm}({stock_code}): 주문 가능 수량 0주 (예산 부족 또는 고가 종목)")
                if image_path and os.path.exists(image_path): os.remove(image_path)
                # [수정] 예산 부족 시에도 쿨타임을 주어 로그 폭주 방지
                RE_ENTRY_COOLDOWN[stock_code] = datetime.now() + timedelta(minutes=5)
                return

            default_sl_rate = float(BOT_SETTINGS.get('STOP_LOSS_RATE') or -1.5)
            final_sl_rate = default_sl_rate

            # [수수료/세금 설정] 공통 사용을 위해 상위로 이동
            R_BUY_FEE_RATE = 0.0035 if MOCK_TRADE else 0.00015
            R_SELL_FEE_RATE = 0.0035 if MOCK_TRADE else 0.00015
            R_TAX_RATE = 0.0015

            if ai_sl_price > 0 and current_price > 0:
                pure_buy_amt = current_price * buy_qty
                expected_sell_amt = ai_sl_price * buy_qty
                
                buy_fee = int(pure_buy_amt * R_BUY_FEE_RATE)
                sell_fee = int(expected_sell_amt * R_SELL_FEE_RATE)
                tax = int(expected_sell_amt * R_TAX_RATE)
                total_cost = buy_fee + sell_fee + tax
                
                net_profit = expected_sell_amt - pure_buy_amt - total_cost
                calc_rate = (net_profit / pure_buy_amt) * 100
                
                val_safety = BOT_SETTINGS.get('AI_STOP_LOSS_SAFETY_LIMIT')
                ai_safety_limit = float(val_safety) if val_safety is not None else -5.0
                if ai_safety_limit > 0: ai_safety_limit = -ai_safety_limit

                if calc_rate > -2.0:
                    strategy_logger.warning(f"⚠️ [AI보정] {stk_nm}({stock_code}): AI 손절률({calc_rate:.2f}%)이 -2.0%보다 큼 -> -2.0%로 강제 보정")
                    final_sl_rate = -2.0
                    ai_reason = (ai_reason or "") + " [손절보정 -2%]"
                elif calc_rate < ai_safety_limit:
                    strategy_logger.info(f"🚫 [진입불가] {stk_nm}({stock_code}): AI 손절률({calc_rate:.2f}%)이 안전한계({ai_safety_limit}%)보다 낮아 위험합니다. 진입을 포기합니다.")
                    if image_path and os.path.exists(image_path): os.remove(image_path)
                    return
                else:
                    final_sl_rate = round(calc_rate, 2)
                    strategy_logger.info(f"🤖 [AI전략] {stk_nm}({stock_code}): AI가격 {ai_sl_price}원 -> 정밀계산 손절률 {final_sl_rate}% (예상비용 {total_cost}원 포함)")

            # [추가] 목표가 정밀 보정 (수수료/세금 고려하여 손실 방지)
            if ai_target_price > 0 and current_price > 0:
                # 손익분기점(BEP) = 매수가 * (1 + 매수수수료) / (1 - 매도수수료 - 세금)
                bep_price = current_price * (1 + R_BUY_FEE_RATE) / (1 - R_SELL_FEE_RATE - R_TAX_RATE)
                
                # 최소 마진 0.5% 확보 (슬리피지 고려)
                min_target_price = int(bep_price * 1.005)
                
                if ai_target_price < min_target_price:
                    strategy_logger.warning(f"🔧 [목표가보정] {stk_nm}({stock_code}): AI목표가({ai_target_price})가 손익분기({int(bep_price)})보다 낮음 -> {min_target_price}원으로 보정")
                    ai_target_price = min_target_price
                    ai_reason = (ai_reason or "") + " [목표보정]"

            BUY_ATTEMPT_HISTORY[stock_code] = datetime.now()

            # 🌟 [보정] AI 분석 시간 동안 가격이 변동되었을 수 있으므로 최신 가격 재확인
            latest_ws_data = ws_manager.get_realtime_data(stock_code, "10")
            if latest_ws_data and (latest_ws_data.get('10') or latest_ws_data.get('cur_prc')):
                raw_new_price = latest_ws_data.get('10') or latest_ws_data.get('cur_prc')
                new_price = safe_int(raw_new_price)
                if new_price > 0 and new_price != current_price:
                    diff_rate = ((new_price - current_price) / current_price) * 100
                    strategy_logger.info(f"⚡ [가격보정] 분석 중 변동: {current_price} -> {new_price} ({diff_rate:+.2f}%)")
                    current_price = new_price
                    # 수량 재계산 (예산 초과 방지)
                    buy_qty = int((order_amount * 0.95) // current_price)

            # 🌟 [수정] 슬리피지 방지를 위해 현재가 + 1호가 지정가 주문
            market_type = STOCK_MARKET_MAP.get(stock_code, 'KOSPI')
            tick = get_tick_size(current_price, market_type)
            limit_price = current_price + tick

            # [추가] 주문 전송 직전 봇 상태 재확인 (비동기 분석 중 STOPPED로 변경된 경우 방어)
            if BOT_SETTINGS.get("BOT_STATUS") != "RUNNING":
                strategy_logger.warning(f"🛑 [주문취소] {stk_nm}({stock_code}): 분석 완료 후 주문 직전에 봇이 정지되었습니다.")
                if image_path and os.path.exists(image_path): os.remove(image_path)
                return
            
            # 🌟 [추가] Race Condition 방어: 분석 시간 동안 다른 경로(수동/타로직)로 진입했는지 최종 확인
            if stock_code in TRADING_STATE:
                strategy_logger.warning(f"🛑 [중복방지] {stk_nm}({stock_code}): 분석 중 이미 진입한 종목입니다. 주문을 취소합니다.")
                if image_path and os.path.exists(image_path): os.remove(image_path)
                return

            strategy_logger.info(f"🚀 [주문전송] {stk_nm}({stock_code}) / {buy_qty}주 / 지정가({limit_price}) / 예상손절 {final_sl_rate}%")
            cond_info_str = f"{condition_id}:{current_cond_name}"
            PENDING_ORDER_CONDITIONS[stock_code] = cond_info_str

            ord_no = await run_blocking(fn_kt10000_buy_order, stock_code, buy_qty, price=limit_price)
            
            # 🌟 [추가] 주문 실패 시 1회 재시도 (일시적 오류 대응)
            if not ord_no:
                await asyncio.sleep(0.5)
                ord_no = await run_blocking(fn_kt10000_buy_order, stock_code, buy_qty, price=limit_price)

            if ord_no:
                # 🌟 [수정] 상태 업데이트를 먼저 수행하여 실시간 체결 통보 누락 방지 (Race Condition 해결)
                TRADING_STATE[stock_code] = {
                    "stk_nm": stk_nm, "buy_price": current_price, "buy_qty": buy_qty,
                    "trailing_active": False, "peak_profit_rate": 0.0,
                    "status": "매수주문", "current_profit_rate": 0.0,
                    "order_time": datetime.now(),
                    "condition_from": cond_info_str,
                    "ord_no": ord_no,
                    "custom_sl_rate": final_sl_rate,
                    "ai_target_price": ai_target_price,
                    "partial_profit_taken": False
                }
                ws_manager.add_subscription(stock_code, "10")

                # 목표가 정보를 ai_reason에 추가하여 로그/텔레그램에 표시
                if ai_target_price > 0:
                    ai_reason += f" [목표가: {ai_target_price}]"
                
                await log_trade(stock_code, stk_nm, "BUY", buy_qty, current_price, f"조건검색({condition_id})", image_path=image_path, ai_reason=ai_reason, custom_sl_rate=final_sl_rate, target_price=ai_target_price)
                strategy_logger.info(f"✅ [주문성공] 주문번호: {ord_no}")
            else:
                strategy_logger.error(f"❌ [주문실패] {stk_nm}({stock_code}): API 응답 없음")
                if image_path and os.path.exists(image_path): os.remove(image_path)
                # 🌟 [추가] 주문 실패 시에도 쿨타임 적용 (무한 재시도 방지)
                RE_ENTRY_COOLDOWN[stock_code] = datetime.now() + timedelta(minutes=5)

            await save_status_to_file(force=True)
            
    except Exception as e:
        strategy_logger.error(f"종목 처리 중 치명적 오류 ({stock_code}): {e}")
        strategy_logger.error(traceback.format_exc())
    finally:
        # 🌟 [수정] AI 분석을 시도했을 때만 4초 대기 (API Rate Limit 보호)
        # 호가 필터 등 단순 거절 시에는 대기 없이 즉시 종료하여 처리 속도 향상
        if ai_analysis_triggered:
            await asyncio.sleep(4.0) # Gemini API 호출 후 대기
        
        # [수정] 스냅샷 진행률 업데이트 로직을 함수 호출로 통일
        if snapshot_meta:
            update_snapshot_progress(snapshot_meta, condition_names, condition_id, stk_name, skipped=False)
        
        if stock_code in PROCESSING_STOCKS: 
            PROCESSING_STOCKS.discard(stock_code)


async def check_for_new_stocks():
    global TRADING_STATE, PROCESSING_STOCKS, PENDING_ORDER_CONDITIONS, BUY_ATTEMPT_HISTORY, CACHED_CONDITION_NAMES, DELAYED_SNAPSHOT_EVENTS

    condition_id = str(BOT_SETTINGS.get('CONDITION_ID') or "0")
    condition_names = CACHED_CONDITION_NAMES

    # 🌟 [수정] 스냅샷 분석 시작 시간 설정 (09:00:15)
    # 장 시작 직후 15초간은 스냅샷 처리를 보류하고 대기열에 쌓습니다.
    now = datetime.now()
    snapshot_start_time = now.replace(hour=9, minute=0, second=15, microsecond=0)
    is_snapshot_allowed = now >= snapshot_start_time

    processed_count = 0  # [추가] 한 번의 루프에서 처리할 최대 개수 제한
    while True:
        # 1. 시간이 되었고 지연된 스냅샷이 있다면 우선 처리
        if is_snapshot_allowed and DELAYED_SNAPSHOT_EVENTS:
            event = DELAYED_SNAPSHOT_EVENTS.popleft()
            debug_log(f"⏳▶️ [지연처리] {event.get('stock_code')} 스냅샷 분석 시작")
        else:
            # 2. 아니면 실시간 큐에서 가져옴
            event = ws_manager.pop_condition_event()
             # [확인] 스냅샷 데이터인 경우에만 09:00:15까지 보류합니다.
             # 실시간 포착 종목(snapshot_meta 없음)은 이 조건을 통과하여 즉시 분석됩니다.
            if event and event.get('snapshot_meta') and not is_snapshot_allowed:
                DELAYED_SNAPSHOT_EVENTS.append(event)
                # [수정] 로그 과다 출력 방지 및 루프 탈출
                # stk_name = ws_manager.master_stock_names.get(event.get('stock_code', '').strip('AJ'), "알수없음")
                # debug_log(f"⏳ [스냅샷보류] {stk_name}: 09:00:15까지 처리를 지연합니다.")
                
                # 지연 큐에 넣었으면 이번 루프는 종료하고 잠시 대기 (CPU 과부하 방지)
                break 

        if not event: break

        stock_code = event.get('stock_code', '').strip('AJ')
        event_type = str(event.get('type', '')).upper()
        initial_price = event.get('price')
        event_cond_id = str(event.get('condition_id', ''))
        snapshot_meta = event.get('snapshot_meta') # 메타데이터 추출
        
        stk_name = ws_manager.master_stock_names.get(stock_code, stock_code)
        
        # 🌟 [수정] 이벤트에 담긴 조건식 ID를 우선 사용 (없으면 설정값 사용)
        use_cond_id = event_cond_id if event_cond_id else condition_id

        # [추가] 스냅샷 이벤트 처리 로그 (진행 상황 확인용)
        if snapshot_meta:
             debug_log(f"📸 [Snapshot] {stk_name}({stock_code}) 처리 대기")
            
        if event_type != 'I':
            debug_log(f"ℹ️ [EventSkip] {stk_name} ({stock_code}) Type: {event_type} (Not 'I')")
            continue

        if stock_code in TRADING_STATE:
            strategy_logger.info(f"🚫 [진입거절] {stk_name} ({stock_code}): 이미 보유 중")
            if snapshot_meta: update_snapshot_progress(snapshot_meta, condition_names, event_cond_id, stk_name, skipped=True)
            continue
        if stock_code in PROCESSING_STOCKS:
            strategy_logger.info(f"🚫 [진입거절] {stk_name} ({stock_code}): 현재 분석/주문 처리 중")
            if snapshot_meta: update_snapshot_progress(snapshot_meta, condition_names, event_cond_id, stk_name, skipped=True)
            continue
        if stock_code in RE_ENTRY_COOLDOWN:
            if datetime.now() < RE_ENTRY_COOLDOWN[stock_code]:
                remain = RE_ENTRY_COOLDOWN[stock_code] - datetime.now()
                remain_sec = int(remain.total_seconds())
                strategy_logger.info(f"🚫 [진입거절] {stk_name} ({stock_code}): 재진입 쿨타임 중 ({remain_sec}초 남음)")
                if snapshot_meta: update_snapshot_progress(snapshot_meta, condition_names, event_cond_id, stk_name, skipped=True)
                continue
            else: del RE_ENTRY_COOLDOWN[stock_code]

        if stock_code in BUY_ATTEMPT_HISTORY:
            elapsed = (datetime.now() - BUY_ATTEMPT_HISTORY[stock_code]).total_seconds()
            if elapsed < 60:
                strategy_logger.info(f"🚫 [진입거절] {stk_name} ({stock_code}): 최근 매수 시도 이력 있음 (1분 내)")
                if snapshot_meta: update_snapshot_progress(snapshot_meta, condition_names, event_cond_id, stk_name, skipped=True)
                continue
            else: del BUY_ATTEMPT_HISTORY[stock_code]

        PROCESSING_STOCKS.add(stock_code)
        asyncio.create_task(process_single_stock_signal(stock_code, "I", use_cond_id, condition_names, initial_price, snapshot_meta))
        await asyncio.sleep(0.1) # 🌟 [수정] 태스크 생성 간격 완화 (부하 분산)
        
        # [추가] 한 번에 너무 많은 이벤트를 처리하면 메인 루프가 블로킹될 수 있으므로 제한
        processed_count += 1
        if processed_count > 50: 
            break

async def try_market_close_liquidation():
    global TRADING_STATE
    now = datetime.now()
    if now.hour == 15 and (10 <= now.minute < 20):
        if not TRADING_STATE: return

        raw_ids = str(BOT_SETTINGS.get("OVERNIGHT_COND_IDS", "2"))
        OVERNIGHT_CONDITION_IDS = [x.strip() for x in raw_ids.split(',') if x.strip()]

        for stock_code, state in list(TRADING_STATE.items()):
            if "매도" in state.get('status', ''): continue
            
            if state.get('overnight_approved', False): continue

            cond_info = state.get('condition_from', '')
            cond_id = cond_info.split(':')[0] if ':' in cond_info else '999'
            
            if cond_id in OVERNIGHT_CONDITION_IDS: continue

            stk_nm = state.get('stk_nm', stock_code)
            buy_qty = state.get('buy_qty', 0)
            if buy_qty > 0:
                strategy_logger.info(f"🤖 [마감분석] {stk_nm}({stock_code}): 오버나잇 여부 AI 분석 중...")
                
                is_ok, img_path, ai_reason, _, _, chart_price = await analyze_chart_pattern(stock_code, stk_nm, "2")
                
                if is_ok:
                    TRADING_STATE[stock_code]['overnight_approved'] = True
                    strategy_logger.info(f"✅ [오버나잇 승인] {stk_nm}({stock_code}) -> AI 홀딩 전환 ({ai_reason})")
                    send_telegram_msg(f"🌙 <b>[오버나잇 승인]</b>\n종목: {stk_nm}({stock_code})\n사유: {ai_reason}\n➡️ 내일 시초가 매도 대상으로 전환됨")
                    # [수정] 승인 시에는 이미지가 필요 없으므로 삭제
                    if img_path and os.path.exists(img_path):
                        try: os.remove(img_path)
                        except: pass
                    await save_status_to_file(force=True)
                    continue 

                strategy_logger.info(f"📉 [오버나잇 거절] {stk_nm}({stock_code}) -> 청산 진행 ({ai_reason})")
                ord_no = await run_blocking(fn_kt10001_sell_order, stock_code, buy_qty, price=0)
                if ord_no:
                    TRADING_STATE[stock_code]['status'] = "매도주문중(일괄)"
                    TRADING_STATE[stock_code]['ord_no'] = ord_no
                    
                    # [추가] 매매 로그 기록 (대시보드 노출용)
                    try:
                        current_price = chart_price if chart_price > 0 else state.get('buy_price', 0)
                        buy_price = state.get('buy_price', 0)
                        profit_rate = 0.0
                        profit_amt = 0
                        
                        if buy_price > 0 and current_price > 0:
                            # 수수료/세금 계산 (약식)
                            r_buy_fee = 0.0035 if MOCK_TRADE else 0.00015
                            r_sell_fee = 0.0035 if MOCK_TRADE else 0.00015
                            r_tax = 0.0015
                            
                            buy_amt = buy_price * buy_qty
                            sell_amt = current_price * buy_qty
                            cost = int(buy_amt * r_buy_fee) + int(sell_amt * r_sell_fee) + int(sell_amt * r_tax)
                            profit_amt = sell_amt - buy_amt - cost
                            profit_rate = (profit_amt / buy_amt) * 100

                        await log_trade(stock_code, stk_nm, "SELL_REJECT", buy_qty, current_price, 
                                      f"오버나잇거절({ai_reason})", profit_rate, profit_amt, 
                                      peak_rate=state.get('peak_profit_rate', 0), image_path=img_path)
                    except Exception as e:
                        strategy_logger.error(f"오버나잇 거절 로그 기록 실패: {e}")
                        if img_path and os.path.exists(img_path):
                            try: os.remove(img_path)
                            except: pass

                    await save_status_to_file(force=True)
                else:
                    # 주문 실패 시 이미지 삭제
                    if img_path and os.path.exists(img_path):
                        try: os.remove(img_path)
                        except: pass

async def try_morning_liquidation():
    # 🌟 [수정] 09:05 대응 로직이 manage_open_positions로 통합됨에 따라 비활성화
    return

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
            
            # 🌟 [수정] 타임아웃 단축 (매도 10초 / 매수 15초)
            is_buy = '매수' in status
            timeout = 15 if is_buy else 10

            if order_time and (now - order_time).total_seconds() > timeout:
                last_cancel = state.get('last_cancel_try')
                if last_cancel and (now - last_cancel).total_seconds() < 10: continue

                debug_log(f"미체결 주문 취소 실행: {stock_code}")
                state['last_cancel_try'] = now
                qty = state.get('buy_qty', 0)
                await run_blocking(fn_kt10003_cancel_order, stock_code, qty, ord_no, is_buy)

                if is_buy:
                    # [수정] 물타기 주문 미체결 시에는 종목을 삭제하면 안 됨 (보유 중이므로 상태 복구)
                    if "물타기" in status:
                        strategy_logger.warning(f"⚠️ [물타기취소] {stock_code}: 미체결 타임아웃 -> 상태 복구 (재감시)")
                        state['status'] = '보유 (체결)'
                        state['watering_count'] = max(0, state.get('watering_count', 1) - 1) # 횟수 차감 복구
                        state.pop('ord_no', None)
                    else:
                        strategy_logger.info(f"🚫 [매수취소] {stock_code}: 미체결 타임아웃({timeout}s) -> 진입 포기")
                        del TRADING_STATE[stock_code]
                else:
                    # 🌟 [수정] 매도 미체결 시 시장가 재주문 플래그 설정
                    strategy_logger.warning(f"⚡ [매도전환] {stock_code}: 미체결 타임아웃({timeout}s) -> 시장가로 강제 청산 시도")
                    TRADING_STATE[stock_code]['status'] = '보유 (체결)'
                    TRADING_STATE[stock_code]['force_market_exit'] = True # 다음 루프에서 시장가 매도 실행
                    TRADING_STATE[stock_code].pop('ord_no', None)
                await save_status_to_file(force=True)

async def manage_open_positions():
    global TRADING_STATE, RE_ENTRY_COOLDOWN, LAST_PRICE_CHECK_TIME, LAST_API_CALL_TIME
    if not TRADING_STATE: return

    val_sl = BOT_SETTINGS.get('STOP_LOSS_RATE')
    global_sl = float(val_sl) if val_sl is not None else -1.5
    val_ts_start = BOT_SETTINGS.get('TRAILING_START_RATE')
    apply_ts_start = float(val_ts_start) if val_ts_start is not None else 1.5
    val_ts_stop = BOT_SETTINGS.get('TRAILING_STOP_RATE')
    apply_ts_stop = float(val_ts_stop) if val_ts_stop is not None else -1.0
    try:
        val = BOT_SETTINGS.get('RE_ENTRY_COOLDOWN_MIN')
        cooldown_min = int(val) if val is not None else 30
    except: cooldown_min = 30
    is_auto_sell_on = BOT_SETTINGS.get("USE_AUTO_SELL", False)
    
    use_ai_sl = BOT_SETTINGS.get('USE_AI_STOP_LOSS', True)
    market_is_open = is_market_open()

    R_BUY_FEE_RATE = 0.0035 if MOCK_TRADE else 0.00015
    R_SELL_FEE_RATE = 0.0035 if MOCK_TRADE else 0.00015
    R_TAX_RATE = 0.0015

    # 오버나잇 ID 목록 로드
    raw_ids = str(BOT_SETTINGS.get("OVERNIGHT_COND_IDS", "2"))
    OVERNIGHT_CONDITION_IDS = [x.strip() for x in raw_ids.split(',') if x.strip()]

    now = datetime.now()

    for stock_code, state in list(TRADING_STATE.items()):
        try:
            # [수정] 매도 주문뿐만 아니라 매수(물타기) 주문 중일 때도 중복 판단 방지
            status = state.get('status', '')
            if "주문" in status: continue
            
            # 🌟 [추가] 변수 초기화 (stk_nm이 정의되지 않아 발생하는 오류 방지)
            stk_nm = state.get('stk_nm', stock_code)

            price_data = ws_manager.get_realtime_data(stock_code, "10")

            raw_price = price_data.get('10') or price_data.get('cur_prc')
            current_price = safe_int(raw_price)

            if current_price == 0:
                if (now - BOT_START_TIME).total_seconds() < 5.0: continue
                last_api_call = LAST_API_CALL_TIME.get(stock_code)
                if not last_api_call or (now - last_api_call).total_seconds() > 60.0:
                    if ws_manager: ws_manager.add_subscription(stock_code, "10")
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

            # 🌟 [추가] 목표가 도달 시 분할 매도 (50%)
            target_price = state.get('ai_target_price', 0)
            is_partial_taken = state.get('partial_profit_taken', False)
            use_partial_profit = BOT_SETTINGS.get('USE_PARTIAL_PROFIT', True)
            
            if use_partial_profit and target_price > 0 and not is_partial_taken and current_price >= target_price:
                partial_rate = float(BOT_SETTINGS.get('PARTIAL_PROFIT_RATE', 50.0))
                sell_qty = int(buy_qty * (partial_rate / 100.0))
                if sell_qty < 1: sell_qty = 1 # 최소 1주 매도 보장
                
                if sell_qty > 0 and buy_qty >= 2: # 2주 이상일 때만 분할 매도
                    stk_nm = state.get('stk_nm', stock_code)
                    strategy_logger.info(f"💰 [목표가달성] {stk_nm}({stock_code}): 현재가({current_price}) >= 목표가({target_price}) -> {partial_rate}%({sell_qty}주) 분할 익절")
                    
                    # 🌟 [수정] 분할 매도 시에도 현재가 - 1호가 적용
                    market_type = STOCK_MARKET_MAP.get(stock_code, 'KOSPI')
                    tick = get_tick_size(current_price, market_type)
                    limit_price = current_price - tick
                    ord_no = await run_blocking(fn_kt10001_sell_order, stock_code, sell_qty, price=limit_price)
                    if ord_no:
                        state['partial_profit_taken'] = True
                        state['buy_qty'] -= sell_qty # 남은 수량 업데이트
                        
                        # 🌟 [최적화] 로그/차트 처리를 비동기 태스크로 분리 (주문 처리 지연 방지)
                        async def _async_part_sell_log(sc, sn, sq, cp, pr, bp):
                            try:
                                part_sell_img = await capture_snapshot_chart(sc, sn)
                                part_pure_buy = bp * sq
                                part_eval = cp * sq
                                part_cost = int(part_pure_buy * R_BUY_FEE_RATE) + int(part_eval * R_SELL_FEE_RATE) + int(part_eval * R_TAX_RATE)
                                part_net_profit = part_eval - part_pure_buy - part_cost
                                await log_trade(sc, sn, "SELL_PART", sq, cp, f"목표가달성({target_price})", pr, profit_amt=part_net_profit, image_path=part_sell_img)
                            except Exception as e:
                                strategy_logger.error(f"부분매도 로그 오류 ({sc}): {e}")

                        asyncio.create_task(_async_part_sell_log(stock_code, stk_nm, sell_qty, current_price, profit_rate, buy_price))
                        
                        # 🌟 [추가] 남은 물량에 대해 트레일링 스탑 시작 지점 상향
                        state['trailing_active'] = True
                        state['peak_profit_rate'] = profit_rate
                        strategy_logger.info(f"📈 [목표달성->TS전환] {stk_nm}({stock_code}): 분할익절 완료. 남은 물량은 트레일링 스탑으로 극대화 (현재 {profit_rate:.2f}%)")

                        await save_status_to_file(force=True)
                        continue # 부분 매도 주문 후 이번 틱 종료 (중복 주문 방지)

            apply_sl = global_sl
            if use_ai_sl and 'custom_sl_rate' in state:
                apply_sl = state['custom_sl_rate']

            # 오버나잇 종목 식별
            cond_info = state.get('condition_from', '')
            cond_id = cond_info.split(':')[0] if ':' in cond_info else '999'
            is_overnight = (cond_id in OVERNIGHT_CONDITION_IDS) or \
                           state.get('overnight_approved', False) or \
                           (cond_id in ["기존보유", "외부매수/동기화"])

            # 🌟 [수정] 09:05까지는 매도 보류 (무조건 보유)
            # 09:00:00 ~ 09:04:59 (5분간) 보류, 09:05:00부터 감시 시작
            if is_overnight and (now.hour == 9 and 0 <= now.minute < 5):
                continue

            # 🌟 [추가] 매수 시점 확인 (당일 매수 여부)
            order_time = state.get('order_time')
            if isinstance(order_time, str):
                try: order_time = datetime.strptime(order_time, '%Y-%m-%d %H:%M:%S')
                except: order_time = now
            elif not isinstance(order_time, datetime):
                order_time = now
            
            is_bought_today = (order_time.date() == now.date())

            # 09:05 이후 오버나잇 종목: -0.5% 이하 손절 (당일 매수 종목 제외)
            if is_overnight and not is_bought_today:
                sl_activation_time = now.replace(hour=9, minute=5, second=0, microsecond=0)
                if now >= sl_activation_time and market_is_open:
                    apply_sl = -0.5

            sell_reason = None
            
            # 🌟 [추가] 미체결 강제 청산 플래그 확인 (manage_unfilled_orders에서 설정)
            if state.get('force_market_exit') is True:
                sell_reason = "미체결 재주문(시장가)"

            # 🌟 [수정] 손절가 도달 시에만 AI 분석으로 물타기 여부 결정 (강제 청산 아닐 경우)
            if not sell_reason and BOT_SETTINGS.get('USE_STOP_LOSS', True) and profit_rate <= apply_sl:
                # 🌟 [추가] 물타기(추가매수) 판단 로직
                do_watering = False
                
                # 물타기 설정 확인 (사용여부 ON, 횟수 남음, 당일 매수 종목 등)
                if BOT_SETTINGS.get('USE_WATERING', False):
                    current_watering_cnt = state.get('watering_count', 0)
                    max_watering = int(BOT_SETTINGS.get('MAX_WATERING_COUNT', 1))
                    
                    # 오버나잇 종목이나 타임컷 상황이 아닐 때만 물타기 고려
                    if current_watering_cnt < max_watering and not (is_overnight and not is_bought_today):
                        # [추가] 총 매수 금액 제한 체크
                        current_invest = buy_price * buy_qty
                        expected_add_invest = current_price * buy_qty # 1배수 물타기 기준
                        max_invest_limit = int(BOT_SETTINGS.get('MAX_WATERING_AMOUNT', 2000000))

                        if current_invest + expected_add_invest > max_invest_limit:
                            strategy_logger.warning(f"🛡️ [물타기제한] {stk_nm}({stock_code}): 총 매수금액 한도 초과 예상 ({current_invest + expected_add_invest:,} > {max_invest_limit:,}) -> 물타기 포기")
                        else:
                            strategy_logger.info(f"📉 [손절가도달] {stk_nm}({stock_code}): AI 물타기 판단 요청... (현재 {current_watering_cnt}/{max_watering}회)")
                            
                            # AI 분석 요청 (물타기 전용 프롬프트 사용)
                            is_ok, img_path, ai_reason, _, _, _ = await analyze_chart_pattern(stock_code, stk_nm, "WATERING", stock_info={"현재가": current_price})
                            
                            if is_ok:
                                do_watering = True
                                strategy_logger.info(f"🌊 [물타기승인] {stk_nm}({stock_code}): AI가 추가매수를 승인함 ({ai_reason})")
                                
                                # 추가 매수 실행 (기존 수량만큼 1배수 추매)
                                add_qty = buy_qty
                                # 예산 체크 등은 생략하고 과감하게 진행 (또는 잔고 확인 로직 추가 가능)
                                
                                market_type = STOCK_MARKET_MAP.get(stock_code, 'KOSPI')
                                tick = get_tick_size(current_price, market_type)
                                limit_price = current_price + tick # 즉시 체결 위해 1호가 위로
                                
                                ord_no = await run_blocking(fn_kt10000_buy_order, stock_code, add_qty, price=limit_price)
                                if ord_no:
                                    state['status'] = "매수주문(물타기)"
                                    state['ord_no'] = ord_no
                                    state['watering_count'] = current_watering_cnt + 1
                                    state['last_cancel_try'] = None # 주문 관리 초기화
                                    
                                    await log_trade(stock_code, stk_nm, "BUY_ADD", add_qty, current_price, f"물타기({current_watering_cnt+1}차)", image_path=img_path, ai_reason=ai_reason)
                                    await save_status_to_file(force=True)
                                else:
                                    strategy_logger.error(f"❌ [물타기실패] 주문 전송 실패 -> 손절로 전환")
                                    do_watering = False # 주문 실패 시 손절 진행
                            else:
                                strategy_logger.info(f"🛡️ [물타기거절] {stk_nm}({stock_code}): AI가 추가매수를 거절함 ({ai_reason}) -> 손절 진행")
                                
                                # 🌟 [수정] 물타기 거절 결과 텔레그램 전송
                                msg = f"🛡️ <b>[물타기 거절]</b>\n종목: {stk_nm} ({stock_code})\n사유: {ai_reason}\n➡️ AI 판단에 따라 추가매수 없이 기존 손절가로 대응합니다."
                                if img_path: send_telegram_photo(img_path, msg)
                                else: send_telegram_msg(msg)

                if do_watering:
                    continue # 매도 로직 건너뜀
                
                # 기존 손절 로직
                msg_type = "AI지정" if (use_ai_sl and 'custom_sl_rate' in state) else "설정"
                if is_overnight and not is_bought_today: msg_type = "오버나잇(09:05이후)"
                sell_reason = f"손절({msg_type}) ({profit_rate:.2f}%)"

            if not sell_reason:
                # order_time은 위에서 이미 파싱함
                elapsed_min = (now - order_time).total_seconds() / 60
                
                val_time_cut = BOT_SETTINGS.get('TIME_CUT_MINUTES')
                time_cut_min = int(val_time_cut) if val_time_cut is not None else 20
                
                # [수정] 오버나잇 대상 종목은 타임컷(시간 경과에 따른 청산) 제외
                if not is_overnight and BOT_SETTINGS.get('USE_TIME_CUT', True) and elapsed_min > time_cut_min and profit_rate < 0.5:
                    sell_reason = f"타임컷(탄력둔화) ({profit_rate:.2f}%) - {int(elapsed_min)}분 경과"

            # 목표가 미도달 시에도 설정된 수익률(TRAILING_START_RATE) 이상이면 여기서 TS가 발동됩니다.
            if not sell_reason:
                if not state.get('trailing_active', False):
                    # 🌟 [수정] 오버나잇 종목은 09:05 이후 즉시 TS 활성화
                    should_activate_ts = False
                    ts_msg = ""

                    # [수정] 당일 매수한 오버나잇 종목은 장 막판에 TS가 켜지지 않도록 제외 (다음날 09:05부터 적용)
                    if is_overnight and not is_bought_today:
                        ts_activation_time = now.replace(hour=9, minute=5, second=0, microsecond=0)
                        if now >= ts_activation_time and market_is_open:
                            should_activate_ts = True
                            ts_msg = "오버나잇 09:05 경과"
                    
                    if not should_activate_ts and BOT_SETTINGS.get('USE_TRAILING_STOP', True) and profit_rate >= apply_ts_start:
                        should_activate_ts = True
                        ts_msg = f">= {apply_ts_start}%"
                    
                    if should_activate_ts:
                        state['trailing_active'] = True
                        state['peak_profit_rate'] = profit_rate
                        strategy_logger.info(f"📈 [TS발동] {state.get('stk_nm', stock_code)} 수익률 {profit_rate:.2f}% ({ts_msg}) -> 트레일링 스탑 활성화")
                        await save_status_to_file(force=True)

                if state.get('trailing_active', False):
                    current_peak = state.get('peak_profit_rate', 0.0)
                    if profit_rate > current_peak:
                        state['peak_profit_rate'] = profit_rate
                        current_peak = profit_rate

                    # 🌟 [수정] 동적 트레일링 스탑 (설정값 기반)
                    dynamic_ts_stop = apply_ts_stop
                    
                    if BOT_SETTINGS.get('USE_DYNAMIC_TS', False):
                        lv1_trig = float(BOT_SETTINGS.get('DYN_TS_LV1_TRIGGER', 5.0))
                        lv1_drop = float(BOT_SETTINGS.get('DYN_TS_LV1_DROP', -2.0))
                        lv2_trig = float(BOT_SETTINGS.get('DYN_TS_LV2_TRIGGER', 10.0))
                        lv2_drop = float(BOT_SETTINGS.get('DYN_TS_LV2_DROP', -3.0))
                        lv3_trig = float(BOT_SETTINGS.get('DYN_TS_LV3_TRIGGER', 20.0))
                        lv3_drop = float(BOT_SETTINGS.get('DYN_TS_LV3_DROP', -5.0))

                        if current_peak >= lv3_trig: dynamic_ts_stop = min(apply_ts_stop, lv3_drop)
                        elif current_peak >= lv2_trig: dynamic_ts_stop = min(apply_ts_stop, lv2_drop)
                        elif current_peak >= lv1_trig: dynamic_ts_stop = min(apply_ts_stop, lv1_drop)

                    drop_from_peak = profit_rate - current_peak
                    if drop_from_peak <= dynamic_ts_stop:
                        if profit_rate > 0:
                            sell_reason = f"익절(TS) ({profit_rate:.2f}%) [고점{current_peak:.2f}%/낙폭{drop_from_peak:.2f}%/설정{dynamic_ts_stop}%]"
                        else:
                            sell_reason = f"손절(TS) ({profit_rate:.2f}%)"

            if sell_reason:
                stk_nm = state.get('stk_nm', stock_code)
                
                # 🌟 [수정] 손절/TS/타임컷 매도는 즉시 체결을 위해 시장가(0)로 주문
                is_stop_order = "손절" in sell_reason or "익절(TS)" in sell_reason or "타임컷" in sell_reason

                if state.get('force_market_exit') is True or is_stop_order:
                    limit_price = 0 # 시장가 (무조건 체결)
                    strategy_logger.info(f"⚡️ [시장가매도] {stk_nm}: 빠른 청산을 위해 시장가로 전환. 사유: {sell_reason}")
                else:
                    # 그 외의 경우 (e.g. 분할매도)는 지정가로 주문하여 슬리피지 최소화
                    market_type = STOCK_MARKET_MAP.get(stock_code, 'KOSPI')
                    tick = get_tick_size(current_price, market_type)
                    limit_price = current_price - tick

                ord_no = await run_blocking(fn_kt10001_sell_order, stock_code, buy_qty, price=limit_price)
                if ord_no:
                    # 1. 상태 먼저 업데이트 (중복 매도 방지)
                    if stock_code in TRADING_STATE: # 🌟 [추가] 비동기 대기 중 삭제되었을 경우 방어
                        TRADING_STATE[stock_code]['status'] = "매도주문중"
                        TRADING_STATE[stock_code]['ord_no'] = ord_no
                        if BOT_SETTINGS.get('USE_RE_ENTRY_COOLDOWN', True):
                            RE_ENTRY_COOLDOWN[stock_code] = datetime.now() + timedelta(minutes=cooldown_min)
                            strategy_logger.info(f"⏳ [쿨타임설정] {stk_nm}({stock_code}): 매도주문 -> {cooldown_min}분간 재진입 금지")
                        await save_status_to_file(force=True)

                    # 2. 차트 캡처 및 로그 (비동기 태스크로 분리하여 메인 루프 지연 방지)
                    async def _async_sell_log(sc, sn, bq, cp, sr, pr, np, pk):
                        try:
                            sell_image_path = await capture_snapshot_chart(sc, sn)
                            await log_trade(sc, sn, "SELL", bq, cp, sr, pr, profit_amt=np, peak_rate=pk, image_path=sell_image_path)
                        except Exception as e:
                            strategy_logger.error(f"매도 로그 처리 중 오류 ({sc}): {e}")

                    peak = state.get('peak_profit_rate', 0.0)
                    asyncio.create_task(_async_sell_log(stock_code, stk_nm, buy_qty, current_price, sell_reason, profit_rate, net_profit, peak))

        except Exception as e:
            strategy_logger.error(f"종목 감시 오류 ({stock_code}): {e}")

async def process_account_events():
    global TRADING_STATE
    while True:
        event = ws_manager.pop_account_event()
        if not event: break
        
        try:
            data_type = event.get('type')
            data = event.get('values', {})
            
            if data_type == "00": # 주식체결
                stock_code = data.get('9001', '').strip('AJ')
                order_status = data.get('913', '').strip()
                order_type = data.get('905', '')
                trade_qty = int(data.get('911', '0') or 0)

                # [수정] 체결량이 있으면 체결로 간주 (상태값 조건 완화)
                if stock_code in TRADING_STATE and (trade_qty > 0 or "체결" in order_status):
                    trade_price = safe_int(data.get('910', '0'))
                    
                    if trade_price > 0 and trade_qty > 0:
                        # [수정] "+매수" -> "매수" (문자열 포함 여부로 변경하여 호환성 확보)
                        if "매수" in order_type:
                            # [수정] 물타기(추가매수) 고려하여 평단가 가중평균 재계산
                            old_qty = TRADING_STATE[stock_code].get('buy_qty', 0)
                            old_price = TRADING_STATE[stock_code].get('buy_price', 0)
                            
                            # 기존 보유량이 있고 가격이 유효할 때만 가중평균 (물타기)
                            if old_qty > 0 and old_price > 0:
                                new_total_qty = old_qty + trade_qty
                                # 평단가 = (기존총액 + 신규총액) / 신규총수량
                                new_avg_price = int((old_price * old_qty + trade_price * trade_qty) / new_total_qty)
                                
                                TRADING_STATE[stock_code]['buy_price'] = new_avg_price
                                TRADING_STATE[stock_code]['buy_qty'] = new_total_qty
                                
                                # 🌟 [핵심] 평단가가 바뀌었으므로 트레일링 스탑 기준(고점) 리셋
                                TRADING_STATE[stock_code]['peak_profit_rate'] = 0.0
                                TRADING_STATE[stock_code]['trailing_active'] = False
                                
                                strategy_logger.info(f"💧 [물타기체결] {stock_code}: 평단가 {old_price}->{new_avg_price:,}원, 수량 {old_qty}->{new_total_qty}주. TS 리셋.")
                            else:
                                # 신규 매수
                                TRADING_STATE[stock_code]['buy_price'] = trade_price
                                TRADING_STATE[stock_code]['buy_qty'] = trade_qty

                            TRADING_STATE[stock_code]['status'] = "보유 (체결)"
                            TRADING_STATE[stock_code].pop('ord_no', None)
                            await save_status_to_file(force=True)
                            
                            # 🌟 [추가] 매수 체결 완료 알림
                            stk_nm = TRADING_STATE[stock_code].get('stk_nm', stock_code)
                            current_avg = TRADING_STATE[stock_code]['buy_price']
                            
                            msg = f"🔴 <b>매수 체결 완료</b>\n"
                            msg += f"종목: {stk_nm} ({stock_code})\n"
                            msg += f"체결가: {trade_price:,}원 | 수량: {trade_qty}주\n"
                            
                            if old_qty > 0:
                                # [추가] 손익분기점 계산 (수수료/세금 포함)
                                r_buy_fee = 0.0035 if MOCK_TRADE else 0.00015
                                r_sell_fee = 0.0035 if MOCK_TRADE else 0.00015
                                r_tax = 0.0015
                                bep_price = int(current_avg * (1 + r_buy_fee) / (1 - r_sell_fee - r_tax))
                                
                                msg += f"📉 <b>평단가: {old_price:,} → {current_avg:,}원</b>\n"
                                msg += f"⚖️ <b>손익분기: {bep_price:,}원</b> (수수료/세금 포함)"
                            else:
                                msg += f"➡️ 단가: {current_avg:,}원"
                                
                            send_telegram_msg(msg)
                        
                        elif "매도" in order_type:
                            # 🌟 [추가] 매도 체결 시 확정 실현손익 계산 및 로그 출력
                            buy_price = TRADING_STATE[stock_code].get('buy_price', 0)
                            if buy_price > 0:
                                # 수수료/세금 설정 (manage_open_positions와 동일하게 적용)
                                r_buy_fee = 0.0035 if MOCK_TRADE else 0.00015
                                r_sell_fee = 0.0035 if MOCK_TRADE else 0.00015
                                r_tax = 0.0015

                                buy_amt = buy_price * trade_qty
                                sell_amt = trade_price * trade_qty
                                cost = int(buy_amt * r_buy_fee) + int(sell_amt * r_sell_fee) + int(sell_amt * r_tax)
                                realized_profit = sell_amt - buy_amt - cost
                                realized_rate = (realized_profit / buy_amt) * 100
                                
                                stk_nm = TRADING_STATE[stock_code].get('stk_nm', stock_code)
                                strategy_logger.info(f"💰 [체결확정] {stk_nm}({stock_code}) 매도: {trade_price}원 | 실현손익: {realized_profit}원 ({realized_rate:.2f}%)")
                                
                                # 🌟 [추가] 매도 체결 완료 알림 (확정 손익)
                                msg = f"🔵 <b>매도 체결 완료</b>\n"
                                msg += f"종목: {stk_nm} ({stock_code})\n"
                                msg += f"매도가: {trade_price:,}원\n"
                                msg += f"실현손익: {realized_profit:,}원 ({realized_rate:.2f}%)"
                                send_telegram_msg(msg)

            elif data_type == "04": # 잔고
                stock_code = data.get('9001', '').strip('AJ')
                if stock_code in TRADING_STATE:
                    holding_qty = int(data.get('930', '0') or 0)
                    if holding_qty == 0:
                        strategy_logger.info(f"✨ [실시간 잔고] {stock_code} 전량 매도 확인 -> 목록 삭제")
                        del TRADING_STATE[stock_code]
                    else:
                        # 부분 매도 등으로 수량이 변경된 경우 실시간 업데이트
                        if TRADING_STATE[stock_code]['buy_qty'] != holding_qty:
                            TRADING_STATE[stock_code]['buy_qty'] = holding_qty
                            debug_log(f"📉 [잔고갱신] {stock_code} 수량 변경: {holding_qty}주")
                    
                    await save_status_to_file(force=True)
        except Exception as e:
            strategy_logger.error(f"계좌 이벤트 처리 중 오류: {e}")

def setup_logging(debug_mode=False):
    logger = logging.getLogger()
    if logger.hasHandlers(): logger.handlers.clear()

    # 1. 콘솔 핸들러
    stream_handler = logging.StreamHandler(sys.stdout)
    if debug_mode:
        logger.setLevel(logging.DEBUG)
        console_formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(filename)s:%(lineno)d - %(message)s')
    else:
        logger.setLevel(logging.INFO)
        console_formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%H:%M:%S')
    stream_handler.setFormatter(console_formatter)
    logger.addHandler(stream_handler)

    # 2. 파일 핸들러
    log_dir = "/data/logs"
    os.makedirs(log_dir, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        filename=os.path.join(log_dir, "bot_daily.log"), 
        when="midnight", interval=1, backupCount=7, encoding="utf-8"
    )
    file_formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(filename)s:%(lineno)d - %(message)s')
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # 3. DB 핸들러 추가
    db_handler = DBLoggingHandler()
    db_handler.setFormatter(console_formatter)
    logger.addHandler(db_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

# ---------------------------------------------------------
# 8. 메인 실행부
# ---------------------------------------------------------
async def main():
    global ws_manager, BOT_SETTINGS, TRADING_STATE, ANALYSIS_SEMAPHORE, BACKTEST_TASK, LAST_SNAPSHOT_REFRESH_DATE

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_exit():
        strategy_logger.info("종료 신호 수신! 정리 작업 시작...")
        stop_event.set()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, _handle_exit)
        loop.add_signal_handler(signal.SIGINT, _handle_exit)
    else:
        signal.signal(signal.SIGINT, lambda s, f: _handle_exit())
        signal.signal(signal.SIGTERM, lambda s, f: _handle_exit())

    setup_logging(debug_mode=False)
    init_ai_clients()
    
    # 🌟 [성능] AI 분석 병렬 처리 (API 키 개수만큼 동시 실행)
    client_cnt = get_client_count()
    # 키가 여러 개라면 그만큼 동시에 분석해도 429 에러 분산 가능 (최소 1개)
    max_concurrency = max(1, client_cnt)
    ANALYSIS_SEMAPHORE = asyncio.Semaphore(max_concurrency)
    strategy_logger.info(f"ℹ️ [성능] AI 분석 모드: 병렬 처리 (API 키 {client_cnt}개 사용 / 동시 분석 {max_concurrency}개)")

    telegram_task = asyncio.create_task(_telegram_worker())

    await run_self_diagnosis()

    try:
        del_trades, del_logs = await run_blocking(db.cleanup_old_data, 7)
        if del_trades > 0 or del_logs > 0:
            strategy_logger.info(f"🧹 [DB정리] 7일 지난 데이터 삭제 완료 (매매: {del_trades}건, 로그: {del_logs}건)")
    except Exception as e:
        strategy_logger.error(f"⚠️ DB 정리 중 오류 발생: {e}")

    await set_booting_status("BOOTING", target_mode=MOCK_TRADE)
    await run_blocking(create_master_stock_file)
    
    # 🌟 [신규] 시장 정보 로드 (마스터 파일 생성 후 수행해야 함)
    await load_stock_market_map()

    BOT_SETTINGS = DEFAULT_SETTINGS.copy()
    await load_settings_from_file()

    if MOCK_TRADE:
        mode_log = "✅ [투자모드] 모의투자 (Virtual)"
        strategy_logger.info(f"🚀 {mode_log} - 시스템이 안전하게 시작되었습니다.")
        send_telegram_msg(f"🖥️ [봇 시작] {mode_log}")
    else:
        mode_log = "🚨 [투자모드] 실전투자 (REAL TRADING)"
        strategy_logger.warning(f"🔥 경고: 현재 '실전 투자' 모드입니다! 🔥")
        send_telegram_msg(f"🔥 [경고] 실전투자 모드로 봇이 시작되었습니다!")

    if BOT_SETTINGS.get("BOT_STATUS") == "RESTARTING":
        intended_status = BOT_SETTINGS.get("_INTENDED_STATUS_", "RUNNING")
        BOT_SETTINGS["BOT_STATUS"] = intended_status
        BOT_SETTINGS.pop("_INTENDED_STATUS_", None)
        await save_settings_to_file()

    initial_stocks = await _load_initial_balance()
    ws_manager = KiwoomWebSocketManager()
    ws_manager.start(stock_list=initial_stocks, account_list=["00", "04"])

    # [수정] 고정 대기 대신 로그인 상태 확인 후 스냅샷 요청 (요청 유실 방지)
    strategy_logger.info("⏳ WebSocket 로그인 대기 중...")
    for _ in range(30):
        if ws_manager.is_logged_in: break
        await asyncio.sleep(1)
    
    if ws_manager.is_logged_in:
        await _sync_initial_condition_list()
    else:
        strategy_logger.error("❌ WebSocket 로그인 시간 초과! 조건검색 스냅샷을 요청하지 못했습니다.")

    await load_condition_names()

    strategy_logger.info("🚀 [메인 루프 시작] 비동기 봇이 정상적으로 실행되었습니다.")

    last_balance_sync = datetime.now()
    last_alive_log = datetime.now()
    last_slow_check = datetime.now()
    last_force_save = datetime.now()
    last_stopped_log = datetime.now()
    last_telegram_check = datetime.now()

    while not stop_event.is_set():
        try:
            command = await run_blocking(db.pop_command)
            if command:
                if command['cmd_type'] == 'BULK_SELL':
                    await process_bulk_sell()
                elif command['cmd_type'] == 'RESTART_BOT':
                    strategy_logger.warning("🔄 [명령 수신] 봇 재시작 요청! 프로세스를 종료합니다.")
                    BOT_SETTINGS['BOT_STATUS'] = 'RESTARTING'
                    BOT_SETTINGS['_INTENDED_STATUS_'] = 'RUNNING'
                    await save_settings_to_file()
                    break
                elif command['cmd_type'] == 'BACKTEST_REQ':
                    # 🌟 [수정] 백테스팅을 비동기 태스크로 실행 (메인 루프 블로킹 방지)
                    try:
                        payload = json.loads(command['payload'])
                        mode = payload.get('mode', 'simulation')
                        
                        # 기존 실행 중인 태스크가 있다면 취소
                        if BACKTEST_TASK and not BACKTEST_TASK.done():
                            BACKTEST_TASK.cancel()
                            await run_blocking(stop_backtest)

                        async def _full_bt_process(signals, settings, mode):
                            if mode == 'optimize':
                                strategy_logger.info("🧪 전략 최적화 요청 감지! 분석 시작...")
                                res = await run_blocking(run_optimization, signals)
                                # 결과에 타입 정보 추가
                                final_res = {'type': 'optimization', 'data': res}
                            else:
                                strategy_logger.info("📊 백테스팅 시뮬레이션 요청 감지! 시작...")
                                res = await run_blocking(run_simulation_for_list, signals, settings)
                                final_res = res # 기존 호환성 유지 (리스트)

                            await run_blocking(db.set_kv, "backtest_result", final_res)
                            strategy_logger.info("✅ 작업 완료 및 결과 저장됨")
                        
                        BACKTEST_TASK = asyncio.create_task(_full_bt_process(payload.get('signals', []), BOT_SETTINGS, mode))

                    except Exception as e:
                         strategy_logger.error(f"백테스팅 오류: {e}")

                elif command['cmd_type'] == 'BACKTEST_STOP':
                    strategy_logger.warning("🛑 [명령 수신] 백테스팅 중지 요청!")
                    if BACKTEST_TASK and not BACKTEST_TASK.done():
                        BACKTEST_TASK.cancel()
                    
                    # 프로세스 풀 강제 종료
                    await run_blocking(stop_backtest)
                    # 결과 초기화 (중지됨 표시)
                    await run_blocking(db.set_kv, "backtest_result", [])

            await load_settings_from_file()
            bot_status = BOT_SETTINGS.get("BOT_STATUS", "STOPPED")

            if (datetime.now() - last_force_save).total_seconds() > 5.0:
                await save_status_to_file(force=True)
                last_force_save = datetime.now()

            try:
                now = datetime.now()
                if now.hour == 15 and 40 <= now.minute < 50:
                    today_str = now.strftime('%Y-%m-%d')
                    last_sent_date = await run_blocking(db.get_kv, "last_daily_report_date")
                    
                    if last_sent_date != today_str:
                        await send_daily_report()
                        await run_blocking(db.set_kv, "last_daily_report_date", today_str)
            except Exception as e:
                strategy_logger.error(f"리포트 체크 중 오류: {e}")

            # 텔레그램 명령어 확인 (1초 간격)
            if (datetime.now() - last_telegram_check).total_seconds() > 1.0:
                await check_telegram_commands()
                last_telegram_check = datetime.now()

            if await check_auto_condition_change(): break
            if bot_status == "RESTARTING": break

            elif bot_status == "RUNNING":
                if not is_market_open():
                    now_time = datetime.now().time()
                    
                    if (datetime.now() - last_alive_log).total_seconds() > 7200:
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

                now = datetime.now()
                current_time = now.time()

                # 🌟 [추가] 09:00:15 조건식 스냅샷 갱신 (장 초반 데이터 안정화 후 재요청)
                if now.hour == 9 and now.minute == 0 and now.second >= 15:
                    today_str = now.strftime('%Y-%m-%d')
                    if LAST_SNAPSHOT_REFRESH_DATE != today_str:
                        strategy_logger.info("🔄 [09:00:15] 장 초반 조건식 스냅샷 갱신 (기존 대기열/처리목록 초기화)")
                        DELAYED_SNAPSHOT_EVENTS.clear()
                        # PROCESSING_STOCKS.clear() # 🌟 [수정] 실시간 분석 중인 종목 보호를 위해 초기화 제거
                        
                        cond_id = str(BOT_SETTINGS.get('CONDITION_ID') or "0")
                        if ws_manager:
                            ws_manager.request_condition_snapshot(cond_id)
                        LAST_SNAPSHOT_REFRESH_DATE = today_str

                if (datetime.now() - last_alive_log).total_seconds() > 7200:
                    msg = f"💓 [생존신고] 봇 작동 중\n보유: {len(TRADING_STATE)}종목"
                    strategy_logger.info(msg.replace("\n", " / "))
                    send_telegram_msg(msg)
                    last_alive_log = datetime.now()

                await check_for_new_stocks()

                if (datetime.now() - last_slow_check).total_seconds() > 2.0:
                    await check_market_index_status() # 🌟 시장 상태 주기적 체크
                    await update_leading_themes()     # 🌟 주도 테마 주기적 갱신 (10분)
                    
                    await manage_open_positions()
                    await try_market_close_liquidation()
                    await try_morning_liquidation()
                    await manage_unfilled_orders()
                    await process_account_events()
                    await save_status_to_file()

                    if (datetime.now() - last_balance_sync).total_seconds() > 20:
                        await sync_balance_with_server()
                        last_balance_sync = datetime.now()
                    last_slow_check = datetime.now()

                await asyncio.sleep(0.1)

            elif bot_status == "STOPPED":
                while ws_manager.pop_condition_event(): pass
                
                # [추가] 정지 상태에서는 대기 중인 스냅샷 이벤트도 제거
                if DELAYED_SNAPSHOT_EVENTS: DELAYED_SNAPSHOT_EVENTS.clear()
                
                await manage_open_positions()
                await process_account_events()

                if is_market_open() and (datetime.now() - last_balance_sync).total_seconds() > 30:
                    await sync_balance_with_server()
                    last_balance_sync = datetime.now()

                if (datetime.now() - last_stopped_log).total_seconds() > 60:
                    if BOT_SETTINGS.get("USE_AUTO_SELL", False):
                        strategy_logger.info("🛡️ [매수중지] 상태지만 매도 감시는 가동 중입니다.")
                    last_stopped_log = datetime.now()

                if (datetime.now() - last_alive_log).total_seconds() > 7200:
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