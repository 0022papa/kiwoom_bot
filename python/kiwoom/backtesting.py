import logging
import time
import json
import pandas as pd
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

# ---------------------------------------------------------
# 3. 시뮬레이션 코어 로직
# ---------------------------------------------------------
def simulate_trade(stock_code, entry_date_str, entry_time_str, settings):
    """ 단일 종목에 대해 진입 시점부터 매도 시점까지 시뮬레이션합니다. """
    
    # 전략 파라미터 로드
    stop_loss = float(settings.get('STOP_LOSS_RATE', -1.5))
    trailing_start = float(settings.get('TRAILING_START_RATE', 1.5))
    trailing_stop = float(settings.get('TRAILING_STOP_RATE', -1.0))
    
    debug_log(f"시뮬레이션 시작: {stock_code} (진입: {entry_date_str} {entry_time_str})")

    # 1. 차트 데이터 조회 (분봉)
    # api_v1 수정 사항 반영: tick="1" (1분봉)
    all_candles_raw = fn_ka10080_get_minute_chart(stock_code, tick="1")
    if not all_candles_raw:
        return format_result(stock_code, 0, "-", 0, "-", "차트 데이터 수신 실패")

    # 2. 데이터 파싱 및 정렬 (Pandas 최적화)
    df = pd.DataFrame(all_candles_raw)
    if df.empty:
        return format_result(stock_code, 0, "-", 0, "-", "유효한 캔들 데이터 없음")

    # 컬럼 매핑 및 전처리
    df['time_str'] = df['cntr_tm'].fillna(df['che_tm'])
    
    # 벡터화된 정수 변환 (safe_int 로직 대체)
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
            df[target] = df[col_name].astype(str).str.replace(r'[+-,]', '', regex=True).astype(int)
        else:
            df[target] = 0

    df = df.sort_values('time_str').reset_index(drop=True)
    all_candles = df.to_dict('records') # 결과 포맷용 (필요시)

    # 3. 진입 시점 찾기
    first_date = df.iloc[0]['time_str']
    last_date = df.iloc[-1]['time_str']
    target_time_full = entry_date_str + entry_time_str
    
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
                
                final_result = format_result(stock_code, buy_price, buy_time, int(real_sell_price), current_time, f"익절 ({target_drop_rate:.2f}%)", all_candles)
                break

    # 6. 루프 종료 시까지 매도 안됨
    if not final_result:
        last_row = df.iloc[-1]
        final_result = format_result(stock_code, buy_price, buy_time, last_row['close'], last_row['time_str'], "미청산 종료", all_candles)
        
    return final_result

def run_simulation_for_list(signals, settings):
    """ 
    여러 종목의 신호 리스트를 받아 시뮬레이션을 실행합니다.
    signals: [[종목코드, 날짜(YYYYMMDD), 시간(HHMMSS)], ...] 
    """
    results = []
    
    # 모의투자는 과거 차트 조회 불가 (키움 정책)
    if MOCK_TRADE:
        bt_logger.warning("❌ [백테스팅] 모의투자 API 키로는 과거 차트를 조회할 수 없습니다.")
        if signals and len(signals) > 0:
            first_code = signals[0][0] if len(signals[0]) > 0 else "UNKNOWN"
            err_res = format_result(first_code, 0, "-", 0, "-", "❌ 실전투자 API 전용 기능입니다 (모의투자 불가)")
            results.append(err_res)
        return results

    bt_logger.info(f"📊 총 {len(signals)}건의 시뮬레이션 시작...")

    for sig in signals:
        if len(sig) >= 3:
            try:
                # sig: [code, date, time]
                res = simulate_trade(sig[0], sig[1], sig[2], settings)
                if res: 
                    results.append(res)
                    debug_log(f"완료: {sig[0]} -> {res['profit_rate']}%")
            except Exception as e:
                bt_logger.error(f"시뮬레이션 중 에러 ({sig[0]}): {e}")
                results.append(format_result(sig[0], 0, "-", 0, "-", f"에러: {str(e)}"))
        
        # API 호출 제한 고려 (짧은 대기)
        time.sleep(0.1)
        
    bt_logger.info(f"✅ 시뮬레이션 완료: 총 {len(results)}건 결과 생성")
    return results