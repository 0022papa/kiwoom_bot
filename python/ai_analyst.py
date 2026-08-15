import os
import logging
import pandas as pd
import matplotlib
# 서버(Headless) 환경에서 GUI 창을 띄우지 않도록 설정 (mplfinance import 전 필수)
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.lines as mlines
import mplfinance as mpf
import json
import re
import random
import time
import io
import itertools
from datetime import datetime, timedelta
from dotenv import load_dotenv
from PIL import Image
import threading

from google import genai
from google.genai import types

load_dotenv()

# 핸들러 설정 없이 로거만 생성 (strategy.py의 설정을 따름)
ai_logger = logging.getLogger("AI_Analyst")
ai_logger.setLevel(logging.INFO) 

# 전역 변수로 클라이언트 풀 선언
CLIENT_POOL = []
CLIENT_ITERATOR = None
API_KEY_STATS = {}

# 🌟 [추가] Matplotlib은 Thread-Safe하지 않으므로 락 필요
PLOT_LOCK = threading.Lock()

# 🌟 [최적화] 스타일 객체 전역 재사용 (매번 생성 비용 절감)
MC = mpf.make_marketcolors(up='#F03E3E', down='#0059A6', inherit=True)
STYLE = mpf.make_mpf_style(marketcolors=MC, gridstyle=':', y_on_right=True)

def init_ai_clients():
    """
    환경변수에서 API 키를 로드하고 클라이언트 풀을 초기화합니다.
    메인 로깅 설정이 완료된 후 호출되어야 파일에 로그가 기록됩니다.
    """
    global CLIENT_POOL, CLIENT_ITERATOR, API_KEY_STATS
    
    api_key_list = []

    if os.getenv("GOOGLE_API_KEY"):
        api_key_list.append(os.getenv("GOOGLE_API_KEY"))

    if os.getenv("GOOGLE_API_KEYS"):
        keys = os.getenv("GOOGLE_API_KEYS").split(',')
        for k in keys:
            clean_key = k.strip()
            if clean_key:
                api_key_list.append(clean_key)

    api_key_list = list(set(api_key_list))

    if not api_key_list:
        ai_logger.error("❌ Google API 키가 설정되지 않았습니다. .env 파일을 확인해주세요.")
        CLIENT_POOL = []
    else:
        # 이 시점에는 strategy.py의 로깅 설정이 적용되어 파일에 기록됩니다.
        ai_logger.info(f"🔑 로드된 API 키 개수: {len(api_key_list)}개 (부하 분산 적용됨)")
        CLIENT_POOL = [genai.Client(api_key=k) for k in api_key_list]
        # 객체 대신 인덱스를 순환하도록 변경 (통계 추적용)
        CLIENT_ITERATOR = itertools.cycle(range(len(CLIENT_POOL)))
        
        # 통계 초기화
        API_KEY_STATS = {
            i: {
                'masked': f"{k[:5]}...{k[-3:]}" if len(k) > 8 else "HIDDEN_KEY",
                'success': 0,
                'fail': 0,
                '429': 0
            }
            for i, k in enumerate(api_key_list)
        }

def get_client_count():
    """ 현재 초기화된 API 클라이언트(키)의 개수를 반환합니다. """
    return len(CLIENT_POOL)

def get_api_status_report():
    """ 현재 API 키들의 사용 통계를 문자열로 반환합니다. """
    if not CLIENT_POOL:
        return "❌ API 클라이언트가 초기화되지 않았습니다."
    
    msg = f"📊 <b>Google AI API 상태 ({len(CLIENT_POOL)}개)</b>\n"
    msg += "━━━━━━━━━━━━━━\n"
    
    for idx, stats in API_KEY_STATS.items():
        s = stats['success']
        f = stats['fail']
        e429 = stats['429']
        total = s + f + e429
        limit = 20
        
        status_icon = "🟢"
        if e429 > 0: status_icon = "🟡"
        if f > 0: status_icon = "🔴"
        if total >= limit: status_icon = "⚫"
        
        msg += f"{status_icon} <b>Key-{idx+1}</b> ({stats['masked']})\n"
        msg += f"   사용: {total} / {limit}회\n"
        msg += f"   성공: {s}회 | 실패: {f}회 | 한도초과: {e429}회\n"
        
    return msg

def create_chart_image(stock_code, stock_name, candle_data, hlines_dict=None):
    """
    API로 받은 캔들 데이터를 이미지 객체(BytesIO)로 변환합니다.
    """
    try:
        # 🌟 [최적화] 데이터 전처리는 락 밖에서 수행 (병렬성 향상)
        if not candle_data or len(candle_data) < 2:
            return None
        
        df = pd.DataFrame(candle_data)
        
        df = df.rename(columns={
            'cntr_tm': 'Date',
            'che_tm': 'Date', # 🌟 [추가] 대체 키 지원
            'cur_prc': 'Close',
            'open_pric': 'Open',
            'high_pric': 'High',
            'low_pric': 'Low',
            'trde_qty': 'Volume'
        })

        # 🌟 [추가] 필수 컬럼(Date) 부재 시 방어 로직
        if 'Date' not in df.columns:
            ai_logger.error(f"차트 데이터에 날짜(Date) 컬럼이 없습니다. Columns: {df.columns}")
            return None
            
        # 🌟 [추가] Volume 컬럼이 없으면 0으로 채워서 에러 방지
        if 'Volume' not in df.columns:
            df['Volume'] = 0

        if 'open_prc' in df.columns and 'Open' not in df.columns:
            df.rename(columns={'open_prc': 'Open'}, inplace=True)
            
        cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        for col in cols:
            if col in df.columns:
                # 1. 문자열 변환 및 특수문자 제거, 숫자로 변환 (오류 시 NaN)
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(r'[+,\-]', '', regex=True), errors='coerce')
                df[col] = df[col].abs()
        
        # 날짜순 정렬 (과거 -> 현재)
        df = df.iloc[::-1] 
        
        # 🌟 [수정] 날짜 변환 실패 시 에러 대신 NaT 처리 후 제거 (안전성 강화)
        # int형 날짜(예: 20231025120000)가 들어올 경우를 대비해 astype(str) 추가
        df.index = pd.to_datetime(df['Date'].astype(str).str.strip(), format='%Y%m%d%H%M%S', errors='coerce')
        if df.index.isnull().any():
            df = df[df.index.notna()]
            if df.empty: return None
        
        # 2. 결측치/0값 처리 (시간순 정렬 후 수행해야 올바름)
        price_cols = ['Open', 'High', 'Low', 'Close']
        for col in price_cols:
            if col in df.columns:
                df[col] = df[col].replace(0, float('nan'))
                df[col] = df[col].ffill().bfill() # 전값으로 채움
                df[col] = df[col].fillna(0).astype(int)
        
        if 'Volume' in df.columns:
            df['Volume'] = df['Volume'].fillna(0).astype(int)

        # 이동평균선 계산 (데이터 자르기 전 수행)
        df['MA5'] = df['Close'].rolling(window=5).mean()
        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA60'] = df['Close'].rolling(window=60).mean()
        df['MA200'] = df['Close'].rolling(window=200).mean()

        # 볼린저 밴드 계산 (20, 2)
        df['std20'] = df['Close'].rolling(window=20).std()
        df['Upper'] = df['MA20'] + (df['std20'] * 2)
        df['Lower'] = df['MA20'] - (df['std20'] * 2)
        df['Bandwidth'] = (df['Upper'] - df['Lower']) / df['MA20'] * 100

        # MACD 계산 (12, 26, 9)
        exp12 = df['Close'].ewm(span=12, adjust=False).mean()
        exp26 = df['Close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp12 - exp26
        df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        df['MACD_Hist'] = df['MACD'] - df['Signal']

        # 🌟 [추가] RSI 계산 (14) - AI 판단 보조용
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, 1)
        df['RSI'] = 100 - (100 / (1 + rs))

        # 데이터 과다 방지: 가장 최근 데이터 기준 1일 전까지만 자르기
        if not df.empty:
            # 최근 120개 캔들(1분봉 기준 약 2시간)만 사용하여 차트 가독성 및 AI 분석 정확도 향상
            if len(df) > 120:
                df = df.iloc[-120:]

        # MACD 히스토그램 색상 설정
        macd_hist_colors = ['#F03E3E' if v >= 0 else '#0059A6' for v in df['MACD_Hist']]

        # 🌟 [추가] MACD 골든크로스 지점 계산 (초록색 화살표)
        # 조건: 현재 MACD > Signal 이고, 이전 MACD <= 이전 Signal
        cross_up = (df['MACD'] > df['Signal']) & (df['MACD'].shift(1) <= df['Signal'].shift(1))
        # 마커 위치: Low 값보다 약간 아래 (0.5% 아래)
        marker_position = df['Low'] * 0.995
        marker_data = marker_position.where(cross_up, float('nan'))

        # 이동평균선, 볼린저 밴드, MACD 추가 플롯 설정
        apds = []
        if df['MA5'].notna().any(): apds.append(mpf.make_addplot(df['MA5'], color='green', width=1.0))
        if df['MA20'].notna().any(): apds.append(mpf.make_addplot(df['MA20'], color='red', width=1.0))
        if df['MA60'].notna().any(): apds.append(mpf.make_addplot(df['MA60'], color='orange', width=1.0))
        if df['MA200'].notna().any(): apds.append(mpf.make_addplot(df['MA200'], color='purple', width=1.0))
        if df['Upper'].notna().any(): apds.append(mpf.make_addplot(df['Upper'], color='grey', width=0.8, linestyle='--'))
        if df['Lower'].notna().any(): apds.append(mpf.make_addplot(df['Lower'], color='grey', width=0.8, linestyle='--'))
        if df['MACD'].notna().any(): apds.append(mpf.make_addplot(df['MACD'], panel=2, color='fuchsia', width=1.0, ylabel='MACD'))
        if df['Signal'].notna().any(): apds.append(mpf.make_addplot(df['Signal'], panel=2, color='blue', width=1.0))
        if df['MACD_Hist'].notna().any(): apds.append(mpf.make_addplot(df['MACD_Hist'], type='bar', panel=2, color=macd_hist_colors, alpha=0.5))
        if df['Bandwidth'].notna().any(): apds.append(mpf.make_addplot(df['Bandwidth'], panel=3, color='teal', width=1.0, ylabel='Bandwidth'))
        # 🌟 [추가] RSI 플롯 (Panel 4)
        if df['RSI'].notna().any():
            apds.append(mpf.make_addplot(df['RSI'], panel=4, color='purple', width=1.0, ylabel='RSI'))
            apds.append(mpf.make_addplot([70]*len(df), panel=4, color='red', width=0.5, linestyle='--'))
            apds.append(mpf.make_addplot([30]*len(df), panel=4, color='blue', width=0.5, linestyle='--'))
        # 🌟 [추가] 골든크로스 화살표 (초록색 삼각형)
        if marker_data.notna().any(): apds.append(mpf.make_addplot(marker_data, type='scatter', markersize=100, marker='^', color='#00b894'))

        # 메모리 버퍼 생성
        buf = io.BytesIO()
        
        plot_args = dict(type='candle', volume=True, addplot=apds, style=STYLE, 
                title=f"CODE: {stock_code}", 
                returnfig=True,
                figsize=(8, 5)) # 🌟 [최적화] 이미지 크기 축소 (10,6 -> 8,5)
        
        if hlines_dict:
            plot_args['hlines'] = hlines_dict

        # 🌟 [최적화] 차트 그리기 부분만 락으로 보호
        with PLOT_LOCK:
            # 🌟 [수정 완료] title에서 한글 stock_name을 제거하고 stock_code만 표시하여 폰트 깨짐 방지
            fig, axlist = mpf.plot(df, **plot_args)

            # 범례 추가
            handles = [
                mlines.Line2D([], [], color='green', label='MA5'),
                mlines.Line2D([], [], color='red', label='MA20'),
                mlines.Line2D([], [], color='orange', label='MA60'),
                mlines.Line2D([], [], color='purple', label='MA200'),
                mlines.Line2D([], [], color='grey', linestyle='--', label='Bollinger')
            ]

            # 🌟 [추가] 점선(매수가/목표가/손절가) 범례 추가
            if hlines_dict and isinstance(hlines_dict, dict) and 'colors' in hlines_dict:
                colors = hlines_dict['colors']
                if isinstance(colors, list):
                    if 'green' in colors:
                        handles.append(mlines.Line2D([], [], color='green', linestyle='--', label='Buy'))
                    if 'red' in colors:
                        handles.append(mlines.Line2D([], [], color='red', linestyle='--', label='Target'))
                    if 'blue' in colors:
                        handles.append(mlines.Line2D([], [], color='blue', linestyle='--', label='StopLoss'))

            # 🌟 [수정] 범례가 차트를 가리지 않도록 상단 바깥으로 이동
            axlist[0].legend(
                handles=handles, 
                loc='lower left', 
                bbox_to_anchor=(0.0, 1.02), 
                ncol=4, 
                fontsize='x-small', 
                frameon=False
            )
            
            # MACD 범례 추가 (Panel 2)
            if len(axlist) >= 5:
                handles_macd = [
                    mlines.Line2D([], [], color='fuchsia', label='MACD'),
                    mlines.Line2D([], [], color='blue', label='Signal')
                ]
                axlist[4].legend(handles=handles_macd, loc='upper left', fontsize='x-small', framealpha=0.4)

            # Bandwidth 범례 추가 (Panel 3)
            if len(axlist) >= 7:
                handles_bw = [mlines.Line2D([], [], color='teal', label='Bandwidth')]
                axlist[6].legend(handles=handles_bw, loc='upper left', fontsize='x-small', framealpha=0.4)

            # RSI 범례 추가 (Panel 4)
            if len(axlist) >= 9:
                handles_rsi = [mlines.Line2D([], [], color='purple', label='RSI(14)')]
                axlist[8].legend(handles=handles_rsi, loc='upper left', fontsize='x-small', framealpha=0.4)

            # 🌟 [최적화] DPI 72, 포맷 JPG로 변경 (속도 향상)
            fig.savefig(buf, dpi=72, bbox_inches='tight', format='jpg')
            plt.close(fig)
            buf.seek(0)
            return buf
    except Exception as e:
        ai_logger.error(f"차트 이미지 생성 실패: {e}")
        return None

def create_daily_chart_image(df, stock_code):
    """
    FinanceDataReader로 받은 일봉 DataFrame을 이미지로 변환합니다.
    """
    try:
        # 🌟 [최적화] 데이터 전처리는 락 밖에서 수행
        if df is None or df.empty:
            return None
        
        # 데이터 전처리: 0값이나 결측치 제거 (지표 왜곡 방지)
        cols = ['Open', 'High', 'Low', 'Close']
        for col in cols:
            if col in df.columns:
                df[col] = df[col].replace(0, float('nan')).ffill().bfill()

        # 이동평균선 계산 (데이터 자르기 전 수행)
        df['MA5'] = df['Close'].rolling(window=5).mean()
        df['MA20'] = df['Close'].rolling(window=20).mean()
        df['MA60'] = df['Close'].rolling(window=60).mean()
        df['MA200'] = df['Close'].rolling(window=200).mean()

        # 볼린저 밴드 계산 (20, 2)
        df['std20'] = df['Close'].rolling(window=20).std()
        df['Upper'] = df['MA20'] + (df['std20'] * 2)
        df['Lower'] = df['MA20'] - (df['std20'] * 2)

        # MACD 계산 (12, 26, 9)
        exp12 = df['Close'].ewm(span=12, adjust=False).mean()
        exp26 = df['Close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp12 - exp26
        df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        df['MACD_Hist'] = df['MACD'] - df['Signal']

        # 최근 4개월(약 80봉) 정도만 표시
        df = df.iloc[-80:]
        
        apds = []
        if df['MA5'].notna().any(): apds.append(mpf.make_addplot(df['MA5'], color='green', width=1.0))
        if df['MA20'].notna().any(): apds.append(mpf.make_addplot(df['MA20'], color='red', width=1.0))
        if df['MA60'].notna().any(): apds.append(mpf.make_addplot(df['MA60'], color='orange', width=1.0))
        if df['MA200'].notna().any(): apds.append(mpf.make_addplot(df['MA200'], color='purple', width=1.0))
        
        if df['Upper'].notna().any(): apds.append(mpf.make_addplot(df['Upper'], color='grey', width=0.8, linestyle='--'))
        if df['Lower'].notna().any(): apds.append(mpf.make_addplot(df['Lower'], color='grey', width=0.8, linestyle='--'))
        
        if df['MACD'].notna().any():
            apds.append(mpf.make_addplot(df['MACD'], panel=2, color='fuchsia', width=1.0, ylabel='MACD'))
        if df['Signal'].notna().any():
            apds.append(mpf.make_addplot(df['Signal'], panel=2, color='blue', width=1.0))
        if df['MACD_Hist'].notna().any():
            macd_hist_colors = ['#F03E3E' if (v >= 0) else '#0059A6' for v in df['MACD_Hist'].fillna(0)]
            apds.append(mpf.make_addplot(df['MACD_Hist'], type='bar', panel=2, color=macd_hist_colors, alpha=0.5))

        buf = io.BytesIO()
        
        # 🌟 [최적화] 차트 그리기 부분만 락으로 보호
        with PLOT_LOCK:
            fig, axlist = mpf.plot(df, type='candle', volume=True, addplot=apds, style=STYLE, 
                    title=f"Daily: {stock_code}", 
                    returnfig=True,
                    figsize=(8, 5)) # 🌟 [최적화] 이미지 크기 축소 (10,6 -> 8,5)
            
            handles = [
                mlines.Line2D([], [], color='green', label='MA5'),
                mlines.Line2D([], [], color='red', label='MA20'),
                mlines.Line2D([], [], color='orange', label='MA60'),
                mlines.Line2D([], [], color='purple', label='MA200'),
                mlines.Line2D([], [], color='grey', linestyle='--', label='Bollinger')
            ]
            # 🌟 [수정] 범례 상단 바깥 이동
            axlist[0].legend(
                handles=handles, 
                loc='lower left', 
                bbox_to_anchor=(0.0, 1.02), 
                ncol=4, 
                fontsize='x-small', 
                frameon=False
            )

            # MACD 범례 추가 (Panel 2)
            if len(axlist) >= 5:
                handles_macd = [
                    mlines.Line2D([], [], color='fuchsia', label='MACD'),
                    mlines.Line2D([], [], color='blue', label='Signal')
                ]
                axlist[4].legend(handles=handles_macd, loc='upper left', fontsize='x-small', framealpha=0.4)

            # 🌟 [최적화] DPI 72, 포맷 JPG로 변경
            fig.savefig(buf, dpi=72, bbox_inches='tight', format='jpg')
            plt.close(fig)
            buf.seek(0)
            return buf
    except Exception as e:
        ai_logger.error(f"일봉 차트 이미지 생성 실패: {e}")
        return None

def ask_ai_to_buy(minute_buffer, daily_buffer, condition_id="0"):
    """
    Gemini Vision AI에게 일봉(추세)과 분봉(타이밍) 차트를 모두 보여주고 판단을 요청합니다.
    """
    try:
        if not CLIENT_POOL:
            ai_logger.error("⚠️ Google AI 클라이언트가 초기화되지 않았습니다.")
            return False, "API Client Not Initialized", 0, 0

        if not minute_buffer or not daily_buffer:
            return False, "Image Buffer Empty", 0, 0

        minute_img = Image.open(minute_buffer)
        daily_img = Image.open(daily_buffer)
        
        # 전략별 프롬프트 정의
        prompts = {
            "0": """
            당신은 '정배열 추세 추종 스캘퍼(Perfect Order Trend Follower)'입니다.
            현재 분석 대상 종목은 급등 직전 또는 급등 시작 직후에 포착된 종목입니다.
            가장 중요한 목표는 **'급등 후 눌림목 과정에서 바닥을 확인하지 않고 진입하여 손실을 보는 것을 방지'**하는 것입니다.

            [분석 핵심 가이드]
            1분봉(Minute) 차트를 정밀 분석하여, **'떨어지는 칼날(Falling Knife)'을 잡지 말고**, **'지지와 반전(Support & Reversal)'이 확인된 시점**에만 진입해야 합니다.

            ### ✅ 매수 승인 (YES) 조건 (엄격한 기준 적용)
            1. **눌림목 완성 및 반전 확인 (Pullback & Reversal):**
               - 주가가 상승 후 20이평선(빨강) 또는 60이평선(주황) 부근까지 조정을 받았는가?
               - **핵심:** 조정 후 **확실한 양봉**이 발생하여 하락세가 멈추고 지지받는 모습이 확인되었는가? (음봉이 연속되거나 하락이 진행 중일 때는 절대 진입 금지)
               - 조정 시 거래량이 감소하다가, 반전 양봉 발생 시 거래량이 다시 유입되는가?
            2. **초기 상승 확산 (Early Expansion):**
               - 이평선들이 밀집(수렴)되었다가 정배열로 확산되기 시작하는 초입 구간인가? (이미 급등하여 이격이 벌어진 상태 제외)
            3. **고점 돌파 (Breakout):**
               - 직전 고점을 강력한 거래량과 함께 돌파하였으며, 윗꼬리가 길지 않은가?

            ### ❌ 매수 거절 (NO) 조건 (절대 진입 금지)
            1. **확인되지 않은 바닥:** 주가가 하락 조정 중이며 아직 양봉으로 전환되지 않았거나 지지선 지지가 확인되지 않은 경우.
            2. **과도한 이격 (Overextended):** 이미 단기간에 급등하여 5이평선과 20이평선 간격이 과도하게 벌어진 상태에서, 충분한 기간 조정 없이 매수하려는 경우.
            3. **거래량 없는 고점:** 주가는 오르는데 거래량이 현저히 줄어드는 '하락 다이버전스'가 보이는 경우.
            4. **저항선 붕괴:** 눌림목이라 생각했으나 60이평선이나 중요 지지 라인을 힘없이 하향 이탈하는 경우.

            **결론 도출:** '떨어지는 중'이면 NO, '바닥을 다지고 고개를 드는 순간'이면 YES를 선택하십시오.
            """,
            "1": """
            당신은 '돌파 매매(Breakout Strategy)' 전문가입니다. 주요 맥점 및 시가 돌파를 노립니다.
            가짜 돌파(Fakeout)를 걸러내는 것이 주 임무입니다. 거래량이 동반되지 않은 돌파는 실패할 확률이 높습니다.

            [분석 지표]
            - **거래량**: 돌파 시점의 거래량이 직전 평균 대비 200% 이상 폭증했는지 확인
            - **캔들**: 돌파 캔들이 윗꼬리가 짧은 장대양봉(Full Body)인지 확인
            - **저항선**: 일봉상 전고점이나 주요 매물대를 확실하게 뚫었는지 확인
            - **RSI**: RSI가 상승 추세이며 아직 꺾이지 않았는지 확인

            [매수 기준 (엄격함)]
            1. **거래량 폭증**: 돌파 캔들의 거래량이 압도적인가? (거래량 없는 돌파는 즉시 거절)
            2. **캔들 완성도**: 윗꼬리가 몸통의 1/3 이하인가? (윗꼬리가 길면 매도세가 강함 -> 거절)
            3. **Bandwidth**: 밴드폭이 급격히 확장되며 변동성이 폭발하고 있는가?
            4. **위치**: 바닥권에서 올라오는 초입인가? (이미 3파 이상 진행된 고점에서의 돌파는 설거지일 수 있음 -> 주의)
            """,
            "2": """
            당신은 '종가베팅(Overnight Strategy)' 전문가입니다. 내일 시초가 갭상승 확률이 높은 종목을 선별합니다.
            현재 포착된 종목은 장 막판까지 강한 매수세가 유입되며 종가 관리가 이루어지고 있는 종목입니다.
            제공된 차트를 통해 오버나잇(Overnight) 가치를 검증해주세요.

            [분석 순서]
            1. **일봉 차트(Daily Chart)**: 
               - 캔들이 꽉 찬 장대양봉이거나 윗꼬리가 짧은 양봉으로 마감 임박했는지 확인하세요.
               - 신고가 영역이거나 직전 고점 매물을 소화하고 있는지 확인하세요.
            2. **1분봉 차트(Minute Chart)**: 
               - **종가 관리**: 15:00 이후 주가가 당일 고가 부근에서 밀리지 않고 횡보하거나 상승 마감 중인지 확인하세요.
               - **수급**: 장 막판에 거래량이 줄지 않고 꾸준히 유입되거나, 막판 동시호가 전에 매수세가 들어오는지 확인하세요.
               - **패턴**: 상승 깃발형(Bull Flag) 또는 고가 놀이 패턴이 완성되고 있는지 확인하세요.
               - **급등 여부**: 장 막판에 인위적으로 급등시켜 고가를 만든 형태인지(설거지 파동 의심), 아니면 꾸준히 우상향했는지 확인하세요.

            [매수 기준]
            1. 일봉상 5일선 위에 있으며, 내일 추가 상승 여력(양봉 마감)이 강한가?
            2. 1분봉상 장 막판 투매 없이 고가권(당일 고가 대비 -2% 이내)을 유지하고 있는가?
            3. **Bandwidth**가 안정적으로 유지되며 추세가 꺾이지 않았는가?
            4. 수급 주체(세력)가 종가를 의도적으로 관리하는 모습이 보이는가?
            """,
            "WATERING": """
            당신은 '위기 관리 및 리스크 헷징 트레이더'입니다.
            현재 보유 종목이 손절가 부근까지 하락하여 '물타기(추가 매수)'를 고려 중입니다.
            단순히 가격이 싸졌다고 사는 것이 아니라, **'확실한 반등 신호'**가 있을 때만 진입하여 손실을 최소화하고 탈출해야 합니다.

            [분석 목표]
            하락세가 멈추고 바닥을 다지는지(Support), 아니면 추가 급락이 예상되는지(Crash) 판단하십시오.

            [매수 승인(YES) 기준 - 물타기 적합]
            1. **지지선 확인**: 주가가 전저점, 60일/120일 이평선, 또는 볼린저 밴드 하단 등 의미 있는 지지선에 도달하여 하락이 멈췄는가?
            2. **반전 캔들**: 하락 후 도지(Doji), 망치형(Hammer), 또는 장대양봉이 발생하며 매수세가 유입되었는가?
            3. **과매도 해소**: RSI가 30 이하 과매도 구간을 찍고 상승 반전(Signal Cross) 하였는가?
            4. **거래량**: 하락 시 거래량이 줄어들다가, 반등 시 거래량이 실리는가?

            [매수 거절(NO) 기준 - 즉시 손절 필요]
            1. **지하실 파기**: 지지선 없이 거래량이 실린 음봉으로 계속 추락 중인가?
            2. **데드캣 바운스**: 반등이 미약하고 다시 저점을 깨러 가는가?
            3. **모멘텀 소멸**: 특별한 악재로 인해 투매가 나오고 있는가?

            결론 도출: 반등 가능성이 높으면 YES, 추가 하락 위험이 크면 NO를 선택하세요.
            """
        }

        default_prompt = """
        당신은 주식 단타 전문가입니다. 일봉으로 추세를 보고 분봉으로 타이밍을 잡으세요.
        """

        selected_prompt = prompts.get(str(condition_id), default_prompt)

        final_prompt = f"""
        {selected_prompt}
        
        **입력된 이미지 설명**: 첫 번째 이미지는 '일봉(Daily)', 두 번째 이미지는 '1분봉(Minute)'입니다.

        [필수 요청 사항]
        1. 매수라고 판단했다면, 차트상 직전 저점이나 주요 지지선이 깨지는 가격을 '손절가'로 정해주세요.
        2. 매수라고 판단했다면, 단기 저항선이나 매물대를 고려하여 기대할 수 있는 '목표가'를 정해주세요.
        3. **매우 중요**: 조금이라도 확신이 없거나 위험해 보이면 과감하게 매수 보류(NO)를 선택하세요. 자본을 잃는 것보다 기회를 놓치는 것이 낫습니다.
        3. 매수 보류(NO)라면 손절가와 목표가는 0으로 하세요.
        4. 매수 보류(NO) 시에는 '거래량 부족', '저항선 근접', '이평선 역배열', 'MACD 하락 반전', '윗꼬리 과다', '상승 모멘텀 약화', '이미 급등(이격 과다)' 등 구체적인 기술적 거절 사유를 반드시 포함하세요.
        5. **중요**: 1분봉상 급등하여 이격이 벌어졌더라도, 거래량이 동반된 강력한 상승 추세라면 매수를 승인(YES)하세요. 단, 추세가 꺾이는 신호가 보일 때만 거절하세요.

        [출력 형식]
        반드시 아래의 JSON 형식으로만 응답하세요. (Markdown 코드 블록 없이 순수 JSON만 출력)
        {{
            "decision": "YES" 또는 "NO",
            "reason": "판단의 근거(거절 시 구체적 사유 포함)를 '한글'로 한 문장으로 명확하게 요약해주세요.",
            "stop_loss_price": 15200, (숫자만, 쉼표 제외)
            "target_price": 16500 (숫자만, 쉼표 제외)
        }}
        """
        
        generate_config = types.GenerateContentConfig(
            response_mime_type="application/json"
        )

        global CLIENT_ITERATOR
        if CLIENT_ITERATOR is None:
            if CLIENT_POOL:
                CLIENT_ITERATOR = itertools.cycle(range(len(CLIENT_POOL)))
            else:
                return False, "Client Not Initialized", 0, 0

        max_retries = max(1, len(CLIENT_POOL))
        last_error = None

        for attempt in range(max_retries):
            client_idx = next(CLIENT_ITERATOR)
            selected_client = CLIENT_POOL[client_idx]
            try:
                response = selected_client.models.generate_content(
                    model='gemini-2.5-flash-lite', # 최신 모델 사용 권장
                    contents=[final_prompt, daily_img, minute_img],
                    config=generate_config
                )
                
                result_text = response.text.strip()
                ai_logger.debug(f"🤖 AI Raw Response ({condition_id}번): {result_text}")
                
                try:
                    # 🌟 [수정] JSON 추출 로직 강화 (앞뒤 텍스트/마크다운/추가 괄호 제거)
                    start_idx = result_text.find('{')
                    end_idx = result_text.rfind('}')
                    
                    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                        json_str = result_text[start_idx:end_idx+1]
                        result_json = json.loads(json_str)
                    else:
                        cleaned_text = re.sub(r'```json\s*|\s*```', '', result_text)
                        result_json = json.loads(cleaned_text)
                    
                    decision = result_json.get("decision", "NO").upper()
                    reason = result_json.get("reason", "분석 실패")
                    
                    # 손절가 파싱 (쉼표 제거 및 정수 변환)
                    stop_loss_price = 0
                    try:
                        sl_val = result_json.get("stop_loss_price", 0)
                        stop_loss_price = int(float(str(sl_val).replace(',', '')))
                    except:
                        stop_loss_price = 0
                    
                    # 목표가 파싱 (쉼표 제거 및 정수 변환)
                    target_price = 0
                    try:
                        tp_val = result_json.get("target_price", 0)
                        target_price = int(float(str(tp_val).replace(',', '')))
                    except:
                        target_price = 0
                    
                    # 성공 카운트 증가 (판단 결과와 무관하게 API 호출은 성공)
                    API_KEY_STATS[client_idx]['success'] += 1
                    
                    if decision == "YES":
                        return True, reason, stop_loss_price, target_price
                    else:
                        return False, reason, 0, 0
                        
                except json.JSONDecodeError:
                    API_KEY_STATS[client_idx]['fail'] += 1
                    ai_logger.error(f"AI 응답 JSON 파싱 실패: {result_text}")
                    return False, "AI 응답 파싱 오류", 0, 0

            except Exception as e:
                last_error = e
                error_str = str(e)
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                    API_KEY_STATS[client_idx]['429'] += 1
                    # API 응답에서 권장 대기 시간을 파싱, 없으면 기본 5초
                    delay_seconds = 5
                    try:
                        # "retryDelay": "57s" 또는 "retryDelay": "57.123s" 형식에서 숫자 부분을 추출
                        match = re.search(r"'retryDelay':\s*'(\d+)", error_str)
                        if match:
                            delay_seconds = int(match.group(1)) + 1  # 파싱된 시간에 1초 추가 (안전 마진)
                    except Exception as parse_err:
                        ai_logger.warning(f"딜레이 파싱 오류: {parse_err}. 기본 {delay_seconds}초 대기.")

                    # 다음 키가 남아있다면 굳이 긴 시간을 대기할 필요가 없음 (다른 키는 한도가 남아있을 수 있으므로)
                    if attempt < max_retries - 1:
                        ai_logger.warning(f"🔥 [429] API 한도 초과! 키 교체 후 즉시 재시도 ({attempt+1}/{max_retries})")
                        time.sleep(0.5) # 빠른 전환을 위해 1초만 대기
                    else:
                        ai_logger.warning(f"🔥 [429] 모든 키 한도 초과! {delay_seconds}초 대기 후 종료 ({attempt+1}/{max_retries})")
                        time.sleep(delay_seconds)
                else:
                    API_KEY_STATS[client_idx]['fail'] += 1
                    ai_logger.warning(f"⚠️ AI 분석 중 오류: {e}. 다음 키로 재시도합니다 ({attempt+1}/{max_retries})")
                continue

        ai_logger.error(f"❌ 모든 API 키 시도 실패. 마지막 오류: {last_error}")
        return False, f"AI Error: {str(last_error)}", 0, 0

    except Exception as e:
        ai_logger.error(f"AI 분석 중 오류: {e}")
        return False, f"AI Error: {str(e)}", 0, 0

def combine_chart_images(daily_buffer, minute_buffer):
    """ 일봉과 분봉 차트 이미지를 세로로 결합합니다. """
    try:
        if not daily_buffer or not minute_buffer:
            return None
            
        daily_buffer.seek(0)
        minute_buffer.seek(0)
        
        img_daily = Image.open(daily_buffer)
        img_minute = Image.open(minute_buffer)
        
        w1, h1 = img_daily.size
        w2, h2 = img_minute.size
        
        new_width = max(w1, w2)
        new_height = h1 + h2
        
        new_img = Image.new('RGB', (new_width, new_height), (255, 255, 255))
        
        # 위쪽: 일봉, 아래쪽: 분봉 (가운데 정렬)
        new_img.paste(img_daily, ((new_width - w1) // 2, 0))
        new_img.paste(img_minute, ((new_width - w2) // 2, h1))
        
        # 🌟 [최적화] 최종 이미지 리사이징 (가로 800px -> 640px 제한)
        if new_width > 640:
            ratio = 640 / new_width
            new_height = int(new_height * ratio)
            new_img = new_img.resize((640, new_height), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        # 🌟 [최적화] PNG 대신 JPEG 사용 (품질 85 -> 70) -> 텔레그램 전송 속도 대폭 향상
        new_img.save(buf, format='JPEG', quality=70)
        buf.seek(0)
        
        return buf
    except Exception as e:
        ai_logger.error(f"차트 이미지 병합 실패: {e}")
        return None

def analyze_daily_trades_with_ai(trades_data_text):
    """
    하루 동안의 매매 기록을 텍스트로 받아 AI에게 분석과 피드백을 요청합니다.
    """
    try:
        if not CLIENT_POOL:
            return "⚠️ AI 클라이언트가 초기화되지 않아 매매 복기를 건너뜁니다."

        prompt = f"""
당신은 전문 트레이딩 코치입니다.
아래는 오늘 하루 동안 자동매매 봇이 실행한 매매 내역(매수/매도) 데이터입니다.
이 데이터를 바탕으로 오늘의 매매를 복기하고, 내일 매매 전략을 개선하기 위한 조언을 해주세요.

[오늘의 매매 내역]
{trades_data_text}

[요청 사항]
1. 오늘 매매의 잘된 점과 아쉬운 점을 평가해 주세요.
2. 손실이 큰 종목의 원인을 파악해 주세요.
3. 승률과 수익금을 높이기 위한 구체적인 전략 개선 팁(손절폭 조정, 필터 강화 등)을 제안해 주세요.
4. 마크다운 기호 없이 일반 텍스트와 이모지로만 친절하게 간결하게 작성해 주세요.
"""
        global CLIENT_ITERATOR
        client_idx = next(CLIENT_ITERATOR)
        selected_client = CLIENT_POOL[client_idx]
        
        response = selected_client.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=[prompt]
        )
        
        API_KEY_STATS[client_idx]['success'] += 1
        return response.text.strip()
    except Exception as e:
        ai_logger.error(f"매매 복기 AI 분석 실패: {e}")
        return "⚠️ 매매 복기 AI 분석 중 오류가 발생했습니다."

if __name__ == "__main__":
    print("이 파일은 모듈로 사용됩니다.")