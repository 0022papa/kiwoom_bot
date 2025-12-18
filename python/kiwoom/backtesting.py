import logging
import time
import json
from api_v1 import fn_ka10080_get_minute_chart
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

def parse_price(price_str):
    """ 가격 문자열을 정수로 변환합니다. (부호 제거) """
    try:
        if price_str is None: return 0
        clean_str = str(price_str).replace('+', '').replace('-', '').strip()
        if not clean_str: return 0
        return int(clean_str)
    except ValueError:
        return 0

def parse_candle_data(candle):
    """ API 캔들 데이터를 내부 표준 형식으로 변환합니다. """
    try:
        # 시간: cntr_tm 우선, 없으면 che_tm
        time_str = candle.get('cntr_tm') or candle.get('che_tm') or ""
        
        # 🌟 [수정] 올바른 필드명(xxx_pric)을 우선적으로 확인하도록 변경
        open_p = parse_price(candle.get('open_pric') or candle.get('open_prc'))
        high_p = parse_price(candle.get('high_pric') or candle.get('high_prc'))
        low_p = parse_price(candle.get('low_pric') or candle.get('low_prc'))
        close_p = parse_price(candle.get('cur_prc') or candle.get('stk_prc') or candle.get('close_prc'))
        vol = parse_price(candle.get('trde_qty') or candle.get('vol'))
        
        return {
            "time_str": time_str,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "close": close_p,
            "volume": vol
        }
    except Exception:
        return None
        
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

    # 2. 데이터 파싱 및 정렬
    all_candles = []
    for c in all_candles_raw:
        parsed = parse_candle_data(c)
        if parsed: all_candles.append(parsed)
    
    all_candles.sort(key=lambda x: x['time_str'])

    if not all_candles:
        return format_result(stock_code, 0, "-", 0, "-", "유효한 캔들 데이터 없음")

    # 3. 진입 시점 찾기
    first_date = all_candles[0]['time_str']
    last_date = all_candles[-1]['time_str']
    target_time_full = entry_date_str + entry_time_str
    entry_index = -1

    for i, candle in enumerate(all_candles):
        # 날짜가 일치하고 시간이 타겟 시간 이후인 첫 캔들
        if candle['time_str'] >= target_time_full and candle['time_str'].startswith(entry_date_str):
            entry_index = i
            break
            
    if entry_index == -1:
        msg = f"진입불가 (데이터범위: {first_date[:8]}~{last_date[:8]})"
        return format_result(stock_code, 0, "-", 0, "-", msg)

    # 4. 매수 체결 가정 (해당 분봉 종가 기준)
    entry_candle = all_candles[entry_index]
    buy_price = entry_candle['close']
    buy_time = entry_candle['time_str']
    
    if buy_price == 0:
         return format_result(stock_code, 0, "-", 0, "-", "매수 가격 오류 (0원)")

    # 5. 매도 조건 감시 (Loop)
    trailing_active = False
    peak_profit_rate = 0.0
    final_result = None
    
    for i in range(entry_index + 1, len(all_candles)):
        candle = all_candles[i]
        current_time = candle['time_str']
        
        # 날짜가 바뀌면 시초가 청산 (오버나잇)
        if not current_time.startswith(entry_date_str):
             final_result = format_result(stock_code, buy_price, buy_time, candle['open'], current_time, "오버나잇 청산 (시가)", all_candles)
             break

        # 장 마감 강제 청산 (15:20 ~ 15:30)
        if current_time.endswith("152000") or current_time.endswith("153000"):
             final_result = format_result(stock_code, buy_price, buy_time, candle['close'], current_time, "장 마감 청산", all_candles)
             break

        # 수익률 계산 (고가/저가 기준)
        low_profit = ((candle['low'] - buy_price) / buy_price) * 100
        high_profit = ((candle['high'] - buy_price) / buy_price) * 100

        # [조건 1] 손절매 (Stop Loss) - 저가가 손절선 터치
        if low_profit <= stop_loss:
            target_sl_price = buy_price * (1 + stop_loss / 100)
            # 갭하락으로 손절가보다 더 낮게 시작했을 경우 시가 매도 처리
            real_sell_price = candle['open'] if candle['open'] < target_sl_price else target_sl_price
            
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
            current_low_rate = ((candle['low'] - buy_price) / buy_price) * 100
            
            if current_low_rate <= target_drop_rate:
                # 갭하락 시 시가 매도
                real_sell_price = candle['open'] if candle['open'] < target_profit_price else target_profit_price
                
                final_result = format_result(stock_code, buy_price, buy_time, int(real_sell_price), current_time, f"익절 ({target_drop_rate:.2f}%)", all_candles)
                break

    # 6. 루프 종료 시까지 매도 안됨
    if not final_result:
        last = all_candles[-1]
        final_result = format_result(stock_code, buy_price, buy_time, last['close'], last['time_str'], "미청산 종료", all_candles)
        
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