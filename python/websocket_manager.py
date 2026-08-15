import websockets
import asyncio
import json
import logging
import threading
import time
import queue
import os
import traceback 
import uuid
from datetime import datetime
from config import KIWOOM_SOCKET_URL
from login import fn_au10001, clear_token_cache
from websockets.exceptions import ConnectionClosed

# DB 모듈 임포트
from database import db

# ---------------------------------------------------------
# 1. 로거 설정
# ---------------------------------------------------------
ws_logger = logging.getLogger("WebSocket")
ws_logger.setLevel(logging.INFO)

# ---------------------------------------------------------
# 2. WebSocket 매니저 클래스
# ---------------------------------------------------------
class KiwoomWebSocketManager:
    """
    키움증권 API와 WebSocket 연결을 관리하고 실시간 데이터를 처리하는 클래스.
    비동기(asyncio)로 통신하며, 메인 스레드와는 큐(Queue)로 소통합니다.
    """
    def __init__(self):
        self.ws_url = KIWOOM_SOCKET_URL
        self._token = None
        self.ws_conn = None
        self.is_logged_in = False
        
        # 실시간 데이터 저장소 (Key: 종목코드_타입, Value: 데이터 딕셔너리)
        self.realtime_data = {}
        
        self.debug_mode = False
        
        # 스레드 간 동기화를 위한 락
        self.data_lock = threading.Lock()
        self.file_lock = threading.Lock() 
        
        # 이벤트 루프 준비 완료 신호용 이벤트
        self.loop_ready_event = threading.Event()
        
        # 메인 로직으로 이벤트를 전달하는 큐
        self.condition_queue = queue.Queue() 
        self.account_queue = queue.Queue() # [추가] 계좌 이벤트 전용 큐
        
        # 재접속 시 복구할 구독 목록
        self._stock_subscriptions = [] 
        self._account_subscriptions = []
        self.last_cond_idx = None 
        
        # 스레드 제어 변수
        self.is_running = False
        self.thread = None
        self._stop_event = None 
        self._loop = None 
        self._command_queue = None 
        
        # 종목 마스터 데이터 로드
        self.master_stock_names = {}
        self._load_master_file()
        
        # 대시보드용 데이터 캐시 및 최적화 (Dirty Check)
        self.dashboard_cache = {}
        self.is_dashboard_dirty = False
        self._clear_current_conditions_file()
        
        # DB 저장 백그라운드 스레드 시작
        threading.Thread(target=self._periodic_dashboard_saver, daemon=True).start()

    def set_debug_mode(self, mode: bool):
        """ 디버그 모드 설정 (상세 로그 출력 여부) """
        self.debug_mode = mode
        level = logging.DEBUG if mode else logging.INFO
        ws_logger.setLevel(level)

    def _load_master_file(self):
        """ 종목 코드-이름 매핑 데이터를 DB에서 로드합니다. """
        try:
            # DB에서 조회
            db_data = db.get_kv("master_stocks")
            if db_data:
                self.master_stock_names = db_data
                ws_logger.info(f"📚 [DB] 마스터 종목 사전 로드 완료 ({len(self.master_stock_names)}개)")
            else:
                ws_logger.warning("⚠️ 마스터 데이터가 없습니다. (api_v1에서 생성 필요)")
        except Exception as e:
            ws_logger.error(f"마스터 데이터 로드 중 오류: {e}")

    def _clear_current_conditions_file(self):
        """ 봇 시작 시 기존 포착 종목 초기화 (DB) """
        try:
            self.dashboard_cache = {} 
            db.set_kv("current_conditions", {})
        except Exception: pass

    def _periodic_dashboard_saver(self):
        """ 
        [최적화] 1초마다 변경사항이 있을 때만 DB에 저장합니다.
        """
        while True:
            try:
                if self.is_dashboard_dirty:
                    self._save_dashboard_file_force()
                    self.is_dashboard_dirty = False
                time.sleep(1.0) 
            except Exception:
                time.sleep(1)

    # ---------------------------------------------------------
    # WebSocket 패킷 전송 함수 (Async)
    # ---------------------------------------------------------
    async def _send_subscription_request(self, ws, item_list, type_list, grp_no="1"):
        """ 실시간 데이터 구독 요청 """
        data_list_of_dicts = []
        if not any(item_list): # 계좌 등 전체 구독
            for sub_type in type_list:
                data_list_of_dicts.append({"item": [""], "type": [sub_type]})
        else: # 개별 종목 구독
            for item, sub_type in zip(item_list, type_list):
                 data_list_of_dicts.append({"item": [item], "type": [sub_type]})
        
        if not data_list_of_dicts: return
        
        payload = { "trnm": "REG", "grp_no": grp_no, "refresh": "1", "data": data_list_of_dicts }
        await ws.send(json.dumps(payload))
        
        if self.debug_mode:
            ws_logger.debug(f"📤 [WS_SEND] 구독요청 (REG) - {len(data_list_of_dicts)}건")
        await asyncio.sleep(0.25)

    async def _send_remove_request(self, ws, item_list, type_list, grp_no="2"):
        """ 실시간 데이터 구독 해지 """
        data_list_of_dicts = []
        for item, sub_type in zip(item_list, type_list):
             data_list_of_dicts.append({"item": [item], "type": [sub_type]})
        
        if not data_list_of_dicts: return
        
        payload = { "trnm": "REMOVE", "grp_no": grp_no, "data": data_list_of_dicts }
        await ws.send(json.dumps(payload))
        
        if self.debug_mode:
            ws_logger.debug("📤 [WS_SEND] 구독해지 (REMOVE)")

    async def _keep_alive_loop(self, ws):
        """ 연결 유지를 위한 Ping 전송 (5초 간격) """
        while True:
            try:
                await asyncio.sleep(5) 
                await ws.ping()
            except (asyncio.CancelledError, Exception): break

    # ---------------------------------------------------------
    # WebSocket 연결 및 이벤트 루프
    # ---------------------------------------------------------
    async def _connect_and_listen(self):
        try:
            loop = asyncio.get_running_loop()
            
            ws_logger.info("🔑 [접속시도] 토큰 발급 요청 중...")
            try:
                self._token = await asyncio.wait_for(
                    loop.run_in_executor(None, fn_au10001),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                ws_logger.error("❌ 토큰 발급 시간 초과. 3초 후 재시도합니다.")
                return 

            if not self._token:
                ws_logger.error("❌ 토큰 발급 실패. 3초 후 재시도합니다.")
                return

            self.is_logged_in = False 
            self._stop_event = asyncio.Event()
            # 🌟 [중요] 연결 성공 시에만 큐가 생성됨
            self._command_queue = asyncio.Queue()

            ws_logger.info(f"🌐 WebSocket 연결 시도: {self.ws_url}")
            
            async with websockets.connect(
                self.ws_url, 
                ping_interval=None, 
                ping_timeout=20,
                close_timeout=10,
                open_timeout=30
            ) as ws:
                self.ws_conn = ws
                ws_logger.info("✅ WebSocket 연결 성공. 로그인 패킷 전송...")
                
                await ws.send(json.dumps({'trnm': 'LOGIN', 'token': self._token}))
                
                if self.debug_mode: ws_logger.debug("📤 [WS_SEND] 로그인 요청 (LOGIN)")
                
                consumer_task = asyncio.create_task(self._message_consumer(ws)) 
                command_task = asyncio.create_task(self._command_processor(ws))
                heartbeat_task = asyncio.create_task(self._keep_alive_loop(ws)) 
                stop_wait_task = asyncio.create_task(self._stop_event.wait())     
                
                done, pending = await asyncio.wait(
                    [consumer_task, command_task, heartbeat_task, stop_wait_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending: task.cancel()
                
            # 연결 종료 시 큐 정리 (선택사항, 안전을 위해 None 처리)
            self._command_queue = None

        except ConnectionRefusedError:
             ws_logger.error("❌ [연결거부] 키움 API 서버가 켜져있지 않거나 포트가 막혔습니다.")
             await asyncio.sleep(5)
        except TimeoutError:
             ws_logger.error("❌ [연결시간초과] WebSocket 서버 연결 시간이 초과되었습니다. (재시도)")
             await asyncio.sleep(3)
        except Exception as e:
            ws_logger.error(f"⚠️ WebSocket 연결 루프 오류:\n{traceback.format_exc()}")
            await asyncio.sleep(3)
        finally:
            ws_logger.info("🔌 WebSocket 세션 종료. 정리 작업 수행.")
            self.ws_conn = None
            self.is_logged_in = False
            self._command_queue = None # 안전하게 None 처리

    async def _message_consumer(self, ws):
        """ 서버로부터 오는 메시지를 수신하고 처리합니다. """
        try:
            async for message in ws:
                try:
                    data = json.loads(message)
                    trnm = data.get('trnm')

                    if self.debug_mode and trnm not in ['REAL', 'PING']:
                        ws_logger.debug(f"📥 [WS_RECV] {trnm}")

                    if trnm == 'LOGIN':
                        if data.get('return_code') == 0:
                            self.is_logged_in = True
                            ws_logger.info("🎉 로그인 승인 완료! 기존 구독을 복구합니다.")
                            await self._request_condition_list(ws)
                            
                            if self._account_subscriptions:
                                await self._send_subscription_request(ws, ["" for _ in self._account_subscriptions], self._account_subscriptions, grp_no="1") 
                            if self._stock_subscriptions:
                                items = [code for code, type in self._stock_subscriptions]
                                types = [type for code, type in self._stock_subscriptions]
                                await self._send_subscription_request(ws, items, types, grp_no="2") 
                            
                            if self.last_cond_idx:
                                ws_logger.info(f"🔄 조건검색식 재등록 (Index: {self.last_cond_idx})")
                                payload = { "trnm": "CNSRREQ", "seq": self.last_cond_idx, "search_type": "1", "stex_tp": "K" }
                                await ws.send(json.dumps(payload))

                        else:
                            err_code = data.get('return_code')
                            err_msg = data.get('return_msg')
                            ws_logger.error(f"🔥 로그인 실패: {err_msg} [CODE={err_code}]")
                            
                            if err_code != 0:
                                ws_logger.warning("♻️ 토큰 만료 감지. 캐시를 삭제합니다.")
                                clear_token_cache() 
                                await ws.close()
                                return 
                                
                    elif trnm == 'CNSRLST': 
                        self._save_conditions_to_db(data)
                    elif trnm == 'CNSRREQ': 
                        self._process_condition_snapshot(data)
                    elif trnm == 'REAL' and self.is_logged_in:
                        self._process_realtime_data(data.get('data', []))
                        
                except json.JSONDecodeError:
                    ws_logger.warning(f"⚠️ [WS] 수신 데이터 JSON 파싱 실패: {message[:100]}...")
                except Exception as e:
                    ws_logger.error(f"데이터 처리 중 오류: {e}")
        
        except ConnectionClosed:
            ws_logger.warning("📉 서버 연결이 종료되었습니다.")
                
    async def _command_processor(self, ws):
        """ 메인 스레드에서 요청한 명령(구독/해지)을 처리합니다. """
        while True:
            try:
                # 큐가 없으면 루프 종료
                if not self._command_queue: break

                command = await self._command_queue.get()
                action = command.get("action")
                
                if not self.ws_conn:
                    self._command_queue.task_done()
                    continue

                if action == "add":
                    await self._send_subscription_request(ws, [command["stock_code"]], [command["sub_type"]], grp_no="2")
                elif action == "remove":
                    await self._send_remove_request(ws, [command["stock_code"]], [command["sub_type"]], grp_no="2")
                elif action == "request_condition": 
                    cond_inx = command.get("cond_inx")
                    self.last_cond_idx = cond_inx 
                    
                    payload = { "trnm": "CNSRREQ", "seq": cond_inx, "search_type": "1", "stex_tp": "K" }
                    await ws.send(json.dumps(payload))
                    
                    if self.debug_mode:
                        ws_logger.debug(f"📤 [WS_SEND] 조건검색 요청 (CNSRREQ) Index: {cond_inx}")
                        
                self._command_queue.task_done()
            except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                break 
            except Exception as e: 
                ws_logger.error(f"명령 처리 중 오류: {e}")

    # ---------------------------------------------------------
    # 데이터 처리 로직
    # ---------------------------------------------------------
    def _process_realtime_data(self, data_list):
        with self.data_lock:
            for data in data_list:
                item_code = data.get('item') or ""
                data_type = data.get('type')
                values = data.get('values', {})
                
                # 계좌 데이터 타입 표준화 (0->00, 1->04)
                is_account_data = False
                if item_code == "":
                    if data_type in ('0', '00'): # 주식체결
                        data_type = '00'; is_account_data = True
                    elif data_type in ('1', '04'): # 잔고
                        data_type = '04'; is_account_data = True

                if is_account_data:
                    # [추가] 계좌 이벤트(체결/잔고)를 큐에 적재
                    self.account_queue.put({
                        "type": data_type,
                        "item": item_code,
                        "values": values
                    })
                
                elif data_type == '02': 
                    item_key = f"CONDITION_{item_code}" 
                    raw_code = values.get('9001', '')
                    stock_code = raw_code.strip('AJ') 
                    
                    if not stock_code:
                        if self.debug_mode: ws_logger.warning(f"⚠️ [조건] 종목코드(9001) 누락. Values: {values}")
                        continue

                    event_type = values.get('843')
                    # [수정] 이벤트 타입이 없으면 기본값 'I' (진입)으로 처리 (API 누락 대응)
                    if not event_type:
                        event_type = 'I'
                        if self.debug_mode:
                            ws_logger.warning(f"⚠️ [조건] {stock_code} 이벤트 타입(843) 누락 -> 'I'로 강제 설정. Values: {values}")
                    
                    stock_name = self.master_stock_names.get(stock_code, stock_code)
                    real_cond_id = values.get('9007', item_code)
                    normalized_cond_id = str(int(real_cond_id)) if real_cond_id.isdigit() else real_cond_id

                    current_price = 0
                    try:
                        raw_price = values.get('10') 
                        if raw_price and isinstance(raw_price, str):
                            current_price = abs(int(raw_price.replace('+', '').replace('-', '')))
                    except: pass

                    event = { 
                        "condition_id": normalized_cond_id, 
                        "stock_code": stock_code, 
                        "type": event_type,
                        "price": current_price 
                    }
                    self.condition_queue.put(event)
                    
                    ws_logger.info(f"[조건포착] {stock_name}({stock_code}) - {event_type} (ID:{normalized_cond_id})")
                    self._update_dashboard_memory(stock_code, stock_name, event_type, normalized_cond_id)
                
                else:
                    item_key = f"{item_code}_{data_type}"
                
                self.realtime_data[item_key] = values
                
                if data_type == '00': 
                    if item_code == "":
                        code = values.get('9001', '')
                        name = self.master_stock_names.get(code, code)
                        msg = values.get('913', '주문체결')
                        ws_logger.info(f"[내주문체결] {name}({code}): {msg}")
                    elif self.debug_mode:
                         code = values.get('9001', '')
                         ws_logger.debug(f"[시세틱] {code} 현재가:{values.get('10')}")

    def _process_condition_snapshot(self, data):
        """ 조건검색 초기 스냅샷(이미 포착된 종목 리스트) 처리 """
        try:
            raw_data = data.get('data')
            now_str = datetime.now().strftime("%H:%M:%S")
            cond_id = data.get('seq', 'init')
            normalized_cond_id = str(int(cond_id)) if str(cond_id).isdigit() else str(cond_id)
            
            stocks_info = []
            if raw_data:
                if isinstance(raw_data, list):
                    for item in raw_data:
                        if isinstance(item, dict):
                            code = item.get('jmcode') or item.get('code') or item.get('9001', '')
                            name = item.get('stock_name') or item.get('name') or code
                            if code: stocks_info.append((code, name))
                        elif isinstance(item, str):
                            if not item.strip(): continue
                            parts = item.split('^')
                            if len(parts) > 0 and parts[0]: 
                                stocks_info.append((parts[0], parts[1] if len(parts) > 1 else parts[0]))
                elif isinstance(raw_data, str):
                    split_data = raw_data.split(';')
                    for item in split_data:
                        if not item.strip(): continue
                        parts = item.split('^')
                        if len(parts) > 0 and parts[0]: 
                            stocks_info.append((parts[0], parts[1] if len(parts) > 1 else parts[0]))

            # 스냅샷 진행률 추적을 위한 ID 생성 및 총 개수 계산
            snapshot_id = f"{normalized_cond_id}_{uuid.uuid4().hex[:8]}"
            total_count = len(stocks_info)
            
            # 🌟 [추가] 메타데이터 생성
            meta = {'id': snapshot_id, 'total': total_count}

            for i, (raw_code, raw_name) in enumerate(stocks_info):
                code = raw_code.replace('A', '').replace('J', '').strip()
                if not code: continue
                final_name = self.master_stock_names.get(code, raw_name)
                
                self.dashboard_cache[code] = { "code": code, "name": final_name, "time": now_str, "cond_id": normalized_cond_id }
                
                event = { "condition_id": normalized_cond_id, "stock_code": code, "type": "I", "snapshot_meta": meta }
                self.condition_queue.put(event)

            if stocks_info:
                ws_logger.info(f"🚀 [초기진입] 기존 포착된 {len(stocks_info)}개 종목을 처리 대기열에 추가했습니다.")
            
            self.is_dashboard_dirty = True
            ws_logger.info(f"✅ 조건검색 스냅샷 처리 완료.")

        except Exception as e:
            ws_logger.error(f"❌ 조건검색 스냅샷 처리 오류: {e}")

    def _update_dashboard_memory(self, code, name, event, cond_id):
        final_name = self.master_stock_names.get(code, name)
        if event == 'I':
            self.dashboard_cache[code] = { "code": code, "name": final_name, "time": datetime.now().strftime("%H:%M:%S"), "cond_id": cond_id }
        elif event == 'D':
            if code in self.dashboard_cache: del self.dashboard_cache[code]
        
        self.is_dashboard_dirty = True

    def _save_dashboard_file_force(self):
        """ 실시간 포착 목록 DB 저장 """
        try:
            with self.file_lock:
                db.set_kv("current_conditions", self.dashboard_cache)
        except Exception: pass

    async def _request_condition_list(self, ws):
        await ws.send(json.dumps({"trnm": "CNSRLST"}))
        if self.debug_mode: ws_logger.debug(f"📤 [WS_SEND] 조건목록요청 (CNSRLST)")

    def _save_conditions_to_db(self, data):
        try:
            conditions = []
            data_list = data.get('data', [])
            if not data_list: return
            
            if isinstance(data_list[0], list):
                for item in data_list:
                    if isinstance(item, list) and len(item) >= 2: conditions.append({"id": item[0], "name": item[1]})
            elif isinstance(data_list[0], str):
                condition_str_list = data_list[0].split(';')
                for cond_str in condition_str_list:
                    parts = cond_str.split('^')
                    if len(parts) == 2: conditions.append({"id": parts[0], "name": parts[1]})
            
            with self.file_lock:
                db.set_kv("conditions", {"conditions": conditions})
            ws_logger.info("조건검색 목록 DB 저장 완료.")
        except Exception as e:
            ws_logger.error(f"조건 목록 저장 실패: {e}")

    # ---------------------------------------------------------
    # 외부 호출용 인터페이스 (Thread-Safe)
    # ---------------------------------------------------------
    def _start_loop_in_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        self.loop_ready_event.set()
        
        ws_logger.info("WebSocket 이벤트 루프 시작")
        
        while self.is_running:
            try: 
                self._loop.run_until_complete(self._connect_and_listen())
            except Exception as e: 
                ws_logger.error(f"이벤트 루프 치명적 오류: {e}")
            
            if self.is_running: 
                ws_logger.info("🔄 5초 후 웹소켓 재연결을 시도합니다...")
                time.sleep(5)

    def start(self, stock_list=None, account_list=None):
        if self.is_running: return
        
        self.loop_ready_event.clear() 
        
        if stock_list: self._stock_subscriptions = stock_list
        if account_list: self._account_subscriptions = account_list
        self.is_running = True
        self.thread = threading.Thread(target=self._start_loop_in_thread, daemon=True)
        self.thread.start()
        
        if not self.loop_ready_event.wait(timeout=5.0):
            ws_logger.error("❌ WebSocket 스레드 시작 시간 초과 (Loop Not Ready)")

    def stop(self):
        self.is_running = False
        if self._loop: 
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self.thread: 
            self.thread.join(timeout=5)
        ws_logger.info("WebSocket 매니저 종료됨.")

    def get_realtime_data(self, item_code, data_type):
        key = f"{item_code}_{data_type}"
        if data_type == "ACCOUNT": key = f"ACCOUNT_{item_code}" 
        elif data_type == 'CONDITION': return None
        with self.data_lock: return self.realtime_data.get(key, {}).copy() 

    def pop_condition_event(self):
        try: return self.condition_queue.get_nowait()
        except queue.Empty: return None

    def pop_account_event(self):
        try: return self.account_queue.get_nowait()
        except queue.Empty: return None

    # 🌟 [수정] 아래 메서드들에 방어 코드 추가 (self._command_queue is not None)
    def add_subscription(self, stock_code, sub_type="0B"):
        if self._loop and self._command_queue: 
            self._loop.call_soon_threadsafe(self._command_queue.put_nowait, {"action": "add", "stock_code": stock_code, "sub_type": sub_type})
        else:
            ws_logger.warning(f"⚠️ [구독실패] 연결이 준비되지 않아 {stock_code} 구독 요청이 무시되었습니다.")

    def remove_subscription(self, stock_code, sub_type="0B"):
        if self._loop and self._command_queue: 
            self._loop.call_soon_threadsafe(self._command_queue.put_nowait, {"action": "remove", "stock_code": stock_code, "sub_type": sub_type})
        else:
            ws_logger.warning(f"⚠️ [해지실패] 연결이 준비되지 않아 {stock_code} 해지 요청이 무시되었습니다.")

    def request_condition_snapshot(self, cond_index):
        if self._loop and self._command_queue:
            self._loop.call_soon_threadsafe(self._command_queue.put_nowait, {"action": "request_condition", "cond_inx": cond_index})