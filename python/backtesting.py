import logging
import time
import json
import pandas as pd
import multiprocessing
import itertools
from api_v1 import fn_ka10080_get_minute_chart, safe_int
from config import MOCK_TRADE, DEBUG_MODE

# ---------------------------------------------------------
# 1. 로거 설정
# ---------------------------------------------------------
bt_logger = logging.getLogger("Backtest")
level = logging.DEBUG if DEBUG_MODE else logging.INFO
bt_logger.setLevel(level)

# ---------------------------------------------------------
# 2. 유틸리티 함수
# ---------------------------------------------------------
def debug_log(msg):
    """ 디버그 모드일 때만 로그를 출력합니다. """
    if DEBUG_MODE:
        bt_logger.debug(f"🕵️ [Backtest] {msg}")

def format_result(code, bp, bt, sp, st, reason, chart_data=None):
    """ 백테스팅 결과 객체를 생성합니다. """
    if bp == 0: profit = 0.0
    else: profit = ((sp - bp) / bp) * 100
    
    return {
        "stock_code": code,
        "buy_time": str(bt),
        "buy_price": int(bp),
        "sell_time": str(st),
        "sell_price": int(sp),
        "profit_rate": round(profit, 2),
        "sell_reason": reason,
        "chart_data": chart_data or [] 
    }

def fetch_and_prepare_chart(stock_code, entry_date_str):
    """ 차트 데이터를 가져와서 전처리까지 마친 DataFrame을 반환합니다. (API 호출) """
    all_candles_raw = fn_ka10080_get_minute_chart(stock_code, tick="1")
    if not all_candles_raw: return None

    df = pd.DataFrame(all_candles_raw)
    if df.empty: return None

    # 컬럼 매핑 및 전처리
    if 'che_tm' in df.columns:
        df['time_str'] = df['cntr_tm'].fillna(df['che_tm'])
    else:
        df['time_str'] = df['cntr_tm']
    
    cols_map = {
        'open': ['open_pric', 'open_prc'],
        'high': ['high_pric', 'high_prc'],
        'low': ['low_pric', 'low_prc'],
        'close': ['cur_prc', 'stk_prc', 'close_prc'],
        'volume': ['trde_qty', 'vol']
    }
    
    for target, candidates in cols_map.items():
        col_name = next((c for c in candidates if c in df.columns), None)
        if col_name:
            df[target] = pd.to_numeric(df[col_name].astype(str).str.replace(r'[+,\-]', '', regex=True), errors='coerce').fillna(0).astype(int).abs()
        else:
            df[target] = 0

    # 🌟 [추가] 보조지표 계산 (RSI, 거래량이동평균)
    # RSI (14)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss.replace(0, 1)
    df['RSI'] = 100 - (100 / (1 + rs))
    df['RSI'] = df['RSI'].fillna(50) # 초기값 보정

    # Volume MA (5) - 전일 기준 비교를 위해 shift
    df['VolMA5'] = df['volume'].rolling(window=5).mean().shift(1)
    df['VolMA5'] = df['VolMA5'].fillna(0)

    # 날짜 필터링 (진입일 데이터만 사용)
    df = df[df['time_str'].str.startswith(entry_date_str)].copy()
    df = df.sort_values('time_str').reset_index(drop=True)
    
    return df

# ---------------------------------------------------------
# 3. 시뮬레이션 코어 로직
# ---------------------------------------------------------
def run_simulation_logic(df, stock_code, entry_date_str, entry_time_str, settings):
    """ 준비된 데이터(DataFrame)로 매매 로직만 수행합니다. (API 호출 없음) """
    # 전략 파라미터 로드
    stop_loss = float(settings.get('STOP_LOSS_RATE', -1.5))
    trailing_start = float(settings.get('TRAILING_START_RATE', 1.5))
    trailing_stop = float(settings.get('TRAILING_STOP_RATE', -1.0))
    
    # 🌟 [추가] 필터 파라미터 로드
    rsi_limit = settings.get('RSI_LIMIT') # None이면 미사용
    vol_ratio = settings.get('VOLUME_FILTER_RATIO') # None이면 미사용
    
    debug_log(f"시뮬레이션 시작: {stock_code} (진입: {entry_date_str} {entry_time_str})")

    # 결과 포맷용 (최적화 모드에서는 비움)
    all_candles = [] 

    # 3. 진입 시점 찾기
    first_date = df.iloc[0]['time_str']
    last_date = df.iloc[-1]['time_str']
    target_time_full = entry_date_str + entry_time_str
    
    if df.empty:
        return format_result(stock_code, 0, "-", 0, "-", f"해당 날짜({entry_date_str}) 데이터 없음")
    
    # 조건에 맞는 첫 번째 인덱스 탐색
    entry_mask = (df['time_str'] >= target_time_full) & (df['time_str'].str.startswith(entry_date_str))
    entry_indices = df.index[entry_mask]
    
    if entry_indices.empty:
        msg = f"진입불가 (데이터범위: {first_date[:8]}~{last_date[:8]})"
        return format_result(stock_code, 0, "-", 0, "-", msg)

    # 4. 매수 체결 가정 (해당 분봉 종가 기준)
    entry_index = entry_indices[0]
    buy_price = df.at[entry_index, 'close']
    buy_time = df.at[entry_index, 'time_str']

    # 🌟 [추가] 진입 필터 시뮬레이션
    # 1. RSI 필터
    if rsi_limit is not None:
        current_rsi = df.at[entry_index, 'RSI']
        if current_rsi > rsi_limit:
            return None # 필터링됨 (진입 포기)

    # 2. 거래량 필터
    if vol_ratio is not None:
        current_vol = df.at[entry_index, 'volume']
        avg_vol = df.at[entry_index, 'VolMA5']
        if avg_vol > 0 and current_vol < (avg_vol * vol_ratio):
            return None # 필터링됨 (거래량 부족)
    
    if buy_price == 0:
         return format_result(stock_code, 0, "-", 0, "-", "매수 가격 오류 (0원)")

    # 5. 매도 조건 감시 (Loop)
    trailing_active = False
    peak_profit_rate = 0.0
    final_result = None
    
    # itertuples가 iterrows보다 훨씬 빠름
    subset_df = df.iloc[entry_index + 1:]
    for row in subset_df.itertuples():
        current_time = row.time_str
        
        # 날짜가 바뀌면 시초가 청산 (오버나잇)
        if not current_time.startswith(entry_date_str):
             final_result = format_result(stock_code, buy_price, buy_time, row.open, current_time, "오버나잇 청산 (시가)", all_candles)
             break

        # 장 마감 강제 청산 (15:20 ~ 15:30)
        if current_time.endswith("152000") or current_time.endswith("153000"):
             final_result = format_result(stock_code, buy_price, buy_time, row.close, current_time, "장 마감 청산", all_candles)
             break

        # 수익률 계산 (고가/저가 기준)
        low_profit = ((row.low - buy_price) / buy_price) * 100
        high_profit = ((row.high - buy_price) / buy_price) * 100

        # [조건 1] 손절매 (Stop Loss) - 저가가 손절선 터치
        if low_profit <= stop_loss:
            target_sl_price = buy_price * (1 + stop_loss / 100)
            # 갭하락으로 손절가보다 더 낮게 시작했을 경우 시가 매도 처리
            real_sell_price = row.open if row.open < target_sl_price else target_sl_price
            
            final_result = format_result(stock_code, buy_price, buy_time, int(real_sell_price), current_time, f"손절 ({stop_loss}%)", all_candles)
            break

        # [조건 2] 트레일링 스탑 (Trailing Stop)
        if not trailing_active:
            if high_profit >= trailing_start:
                trailing_active = True
                peak_profit_rate = high_profit
                debug_log(f"TS 발동: {stock_code} {high_profit:.2f}%")
        
        if trailing_active:
            if high_profit > peak_profit_rate:
                peak_profit_rate = high_profit

            target_drop_rate = peak_profit_rate + trailing_stop
            target_profit_price = buy_price * (1 + target_drop_rate / 100)
            
            # 현재 봉의 저가가 트레일링 익절선을 건드렸는지 확인
            current_low_rate = ((row.low - buy_price) / buy_price) * 100
            
            if current_low_rate <= target_drop_rate:
                # 갭하락 시 시가 매도
                real_sell_price = row.open if row.open < target_profit_price else target_profit_price
                
                ts_label = "익절(TS)" if target_drop_rate > 0 else "손절(TS)"
                final_result = format_result(stock_code, buy_price, buy_time, int(real_sell_price), current_time, f"{ts_label} ({target_drop_rate:.2f}%)", all_candles)
                break

    # 6. 루프 종료 시까지 매도 안됨
    if not final_result:
        last_row = df.iloc[-1]
        final_result = format_result(stock_code, buy_price, buy_time, last_row['close'], last_row['time_str'], "미청산 종료", all_candles)
        
    return final_result

def simulate_trade(stock_code, entry_date_str, entry_time_str, settings):
    """ (기존 호환용) API 호출 + 로직 수행 """
    df = fetch_and_prepare_chart(stock_code, entry_date_str)
    if df is None or df.empty:
        return format_result(stock_code, 0, "-", 0, "-", "차트 데이터 없음")
    
    res = run_simulation_logic(df, stock_code, entry_date_str, entry_time_str, settings)
    res['chart_data'] = df.to_dict('records') # 단일 실행 시에는 차트 데이터 포함
    return res

def _init_worker():
    """ 멀티프로세싱 워커 초기화: API 세션 재설정 (소켓 충돌 방지) """
    import api_v1
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    
    api_v1.API_SESSION = requests.Session()
    retries = Retry(total=3, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
    api_v1.API_SESSION.mount('http://', HTTPAdapter(max_retries=retries))
    api_v1.API_SESSION.mount('https://', HTTPAdapter(max_retries=retries))

# 🌟 [추가] 백테스팅 실행 상태 플래그
IS_BACKTEST_RUNNING = False

def stop_backtest():
    """ 실행 중인 백테스팅 루프를 중단하도록 플래그를 설정합니다. """
    global IS_BACKTEST_RUNNING
    if IS_BACKTEST_RUNNING:
        bt_logger.warning("🛑 백테스팅 강제 중지 요청! 루프 종료 대기 중...")
        IS_BACKTEST_RUNNING = False

def run_simulation_for_list(signals, settings):
    """ 
    여러 종목의 신호 리스트를 받아 시뮬레이션을 실행합니다.
    signals: [[종목코드, 날짜(YYYYMMDD), 시간(HHMMSS)], ...] 
    """
    results = []
    global IS_BACKTEST_RUNNING
    IS_BACKTEST_RUNNING = True
    
    # 모의투자는 과거 차트 조회 불가 (키움 정책)
    if MOCK_TRADE:
        bt_logger.warning("❌ [백테스팅] 모의투자 API 키로는 과거 차트를 조회할 수 없습니다.")
        if signals and len(signals) > 0:
            first_code = signals[0][0] if len(signals[0]) > 0 else "UNKNOWN"
            err_res = format_result(first_code, 0, "-", 0, "-", "❌ 실전투자 API 전용 기능입니다 (모의투자 불가)")
            results.append(err_res)
        return results

    # [수정] API 속도 제한(Rate Limit) 보호를 위해 멀티프로세싱 대신 순차 처리로 변경
    # 병렬 실행 시 각 프로세스가 독립된 RateLimiter를 가지게 되어 API 요청 폭주로 차단될 위험이 있음
    bt_logger.info(f"📊 총 {len(signals)}건의 시뮬레이션 시작... (API 보호를 위해 순차 실행)")

    for sig in signals:
        if len(sig) >= 3:
            # 중지 요청 확인
            if not IS_BACKTEST_RUNNING:
                break
            
            res = simulate_trade(sig[0], sig[1], sig[2], settings)
            if res:
                results.append(res)
            
            # API 부하 분산을 위한 미세 대기
            time.sleep(0.1)
        
    IS_BACKTEST_RUNNING = False
    bt_logger.info(f"✅ 시뮬레이션 완료: 총 {len(results)}건 결과 생성")
    return results

def run_optimization(signals):
    """ 승률 높은 파라미터 찾기 (API 호출 최소화) """
    if MOCK_TRADE: return [{"error": "모의투자 API 불가"}]

    global IS_BACKTEST_RUNNING
    IS_BACKTEST_RUNNING = True

    # 1. 데이터 선로딩 (API 호출 최소화)
    bt_logger.info("📉 최적화를 위한 차트 데이터 수집 중...")
    data_cache = {}
    unique_items = set((s[0], s[1]) for s in signals if len(s) >= 2)
    
    for code, date in unique_items:
        if not IS_BACKTEST_RUNNING: break
        if (code, date) not in data_cache:
            df = fetch_and_prepare_chart(code, date)
            if df is not None:
                data_cache[(code, date)] = df
            time.sleep(0.2) # API 보호

    if not data_cache:
        return [{"error": "유효한 차트 데이터를 확보하지 못했습니다."}]

    # 2. 파라미터 그리드 정의 (조합 확장)
    # 🌟 [수정] 필터 파라미터 추가 및 그리드 최적화
    stop_losses = [-1.0, -1.5, -2.0, -2.5, -3.0, -4.0] # 손절 범위 세분화
    ts_starts = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 7.0] # 익절 시작 범위 확장
    ts_stops = [-0.5, -0.8, -1.0, -1.5, -2.0] # 트레일링 갭 세분화
    rsi_limits = [None, 75, 70, 60] # RSI 필터 다양화
    vol_ratios = [None, 0.3, 0.5]    # 거래량 필터 다양화
    
    combinations = list(itertools.product(stop_losses, ts_starts, ts_stops, rsi_limits, vol_ratios))
    bt_logger.info(f"🧪 총 {len(combinations)}개 전략 조합 테스트 시작...")

    # 3. 시뮬레이션 (메모리 연산)
    best_results = []
    
    for sl, ts_start, ts_stop, rsi, vol in combinations:
        if not IS_BACKTEST_RUNNING: break
        settings = {
            'STOP_LOSS_RATE': sl, 'TRAILING_START_RATE': ts_start, 'TRAILING_STOP_RATE': ts_stop,
            'RSI_LIMIT': rsi, 'VOLUME_FILTER_RATIO': vol
        }
        
        total_profit = 0.0
        win_cnt = 0
        total_cnt = 0
        
        # MDD 계산을 위한 변수
        balance = 1000000.0 # 가상 원금 100만원
        peak_balance = balance
        max_drawdown = 0.0
        
        for sig in signals:
            if len(sig) < 3: continue
            code, date, time_str = sig[0], sig[1], sig[2]
            
            df = data_cache.get((code, date))
            if df is None: continue
            
            res = run_simulation_logic(df, code, date, time_str, settings)
            if res:
                total_profit += res['profit_rate']
                if res['profit_rate'] > 0: win_cnt += 1
                total_cnt += 1
                
                # MDD 계산 (복리 가정 시뮬레이션)
                profit_amt = balance * (res['profit_rate'] / 100.0)
                balance += profit_amt
                if balance > peak_balance: peak_balance = balance
                drawdown = (peak_balance - balance) / peak_balance * 100
                if drawdown > max_drawdown: max_drawdown = drawdown
        
        if total_cnt > 0:
            win_rate = (win_cnt / total_cnt) * 100
            avg_profit = total_profit / total_cnt
            
            # 최소 승률 필터 (너무 낮은 승률은 제외)
            if win_rate < 20: continue
            
            best_results.append({
                "params": settings,
                "win_rate": round(win_rate, 2),
                "total_profit": round(total_profit, 2),
                "avg_profit": round(avg_profit, 2),
                "trade_count": total_cnt,
                "mdd": round(max_drawdown, 2) # MDD 결과 추가
            })

    # 4. 정렬 (총 수익률 내림차순)
    best_results.sort(key=lambda x: x['total_profit'], reverse=True)
    
    IS_BACKTEST_RUNNING = False
    return best_results[:20] # Top 20 반환 (범위 확대)