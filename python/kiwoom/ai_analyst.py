import os
import logging
import pandas as pd
import mplfinance as mpf
import json
import re
import random
import io
from datetime import datetime, timedelta
from dotenv import load_dotenv
from PIL import Image

from google import genai
from google.genai import types

load_dotenv()

# 핸들러 설정 없이 로거만 생성 (strategy.py의 설정을 따름)
ai_logger = logging.getLogger("AI_Analyst")
ai_logger.setLevel(logging.INFO) 

# 전역 변수로 클라이언트 풀 선언
CLIENT_POOL = []

def init_ai_clients():
    """
    환경변수에서 API 키를 로드하고 클라이언트 풀을 초기화합니다.
    메인 로깅 설정이 완료된 후 호출되어야 파일에 로그가 기록됩니다.
    """
    global CLIENT_POOL
    
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


def create_chart_image(stock_code, stock_name, candle_data):
    """
    API로 받은 캔들 데이터를 이미지 객체(BytesIO)로 변환합니다.
    """
    try:
        if not candle_data or len(candle_data) < 20:
            return None
        
        df = pd.DataFrame(candle_data)
        
        df = df.rename(columns={
            'cntr_tm': 'Date',
            'cur_prc': 'Close',
            'open_pric': 'Open',
            'high_pric': 'High',
            'low_pric': 'Low',
            'trde_qty': 'Volume'
        })

        if 'open_prc' in df.columns and 'Open' not in df.columns:
            df.rename(columns={'open_prc': 'Open'}, inplace=True)
            
        cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        for col in cols:
            if col in df.columns:
                # 벡터화 연산으로 최적화 (apply lambda 제거)
                df[col] = df[col].astype(str).str.replace(r'[+-,]', '', regex=True).astype(int)
        
        # 날짜순 정렬 (과거 -> 현재)
        df = df.iloc[::-1] 
        df.index = pd.to_datetime(df['Date'], format='%Y%m%d%H%M%S')
        
        # 데이터 과다 방지: 가장 최근 데이터 기준 1일 전까지만 자르기
        if not df.empty:
            last_date = df.index[-1]
            cutoff_date = last_date - timedelta(days=1)
            df = df[df.index >= cutoff_date]

            if len(df) < 30 and len(candle_data) >= 30:
                 df = pd.DataFrame(candle_data).iloc[::-1].iloc[-30:]

        mc = mpf.make_marketcolors(up='red', down='blue', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc)
        
        # 메모리 버퍼 생성
        buf = io.BytesIO()
        
        # 🌟 [수정 완료] title에서 한글 stock_name을 제거하고 stock_code만 표시하여 폰트 깨짐 방지
        mpf.plot(df, type='candle', mav=(5, 20), volume=True, style=s, 
                 title=f"CODE: {stock_code}", 
                 savefig=dict(fname=buf, dpi=100, bbox_inches='tight', format='png'))
        
        buf.seek(0)
        return buf
    except Exception as e:
        ai_logger.error(f"차트 이미지 생성 실패: {e}")
        return None

def ask_ai_to_buy(image_buffer, condition_id="0"):
    """
    Gemini Vision AI에게 차트를 보여주고 매수 여부와 손절가를 물어봅니다.
    """
    try:
        if not CLIENT_POOL:
            ai_logger.error("⚠️ Google AI 클라이언트가 초기화되지 않았습니다.")
            return False, "API Client Not Initialized", 0

        if not image_buffer:
            return False, "Image Buffer Empty", 0

        image = Image.open(image_buffer)
        
        # 전략별 프롬프트 정의
        prompts = {
            "0": """
            당신은 '급등주 돌파 매매(Breakout Strategy)' 전문가입니다. 1분봉 차트를 보고 판단하세요.
            [매수 기준]
            1. 5일 이동평균선이 20일 이동평균선 아래에서 수렴하는 구간인가?
            2. 20일 이동평균선의 기울기가 완만(0.2 이하)하여 횡보세이거나 하락세인가?
            3. 5일 이동평균선의 기울기가 최근 양수로 상승 전환했는가?
            4. 현재 캔들이 양봉이고, 양봉의 중간값((고가+저가)/2)이 5일선 위에 위치해 있는가?
            5. 거래량이 동반된 상승인가?
            """,
            "1": """
            당신은 '눌림목 매매(Pullback Strategy)' 전문가입니다. 1분봉 차트를 보고 판단하세요.
            [매수 기준]
            1. 주가가 20일 이동평균선 근처에서 지지를 받고 있는가?
            2. 하락(조정) 구간에서 거래량이 감소했는가?
            3. 지지 라인에서 양봉(반등 신호)이 출현했는가?
            """,
            "2": """
            당신은 '종가베팅(Overnight Strategy)' 전문가입니다. 1분봉 차트를 보고 판단하세요.
            [매수 기준]
            1. 주가가 당일 고가 부근에서 마감하려 하는가? (고가놀이)
            2. 장 막판에 가격이 무너지지 않고 지지되는가?
            3. 내일 시초가 갭상승이 유력해 보이는 차트 패턴인가?
            """
        }

        default_prompt = """
        당신은 주식 단타 전문가입니다.
        [매수 기준] 상승 추세가 뚜렷하고, 이평선 지지를 받으며, 거래량이 실린 양봉이 있는가?
        """

        selected_prompt = prompts.get(str(condition_id), default_prompt)

        final_prompt = f"""
        {selected_prompt}

        [필수 요청 사항]
        1. 매수라고 판단했다면, 차트상 직전 저점이나 주요 지지선이 깨지는 가격을 '손절가'로 정해주세요.
        2. 매수 보류(NO)라면 손절가는 0으로 하세요.

        [출력 형식]
        반드시 아래의 JSON 형식으로만 응답하세요. (Markdown 코드 블록 없이 순수 JSON만 출력)
        {{
            "decision": "YES" 또는 "NO",
            "reason": "판단의 근거를 '한글'로 한 문장으로 명확하게 요약해주세요.",
            "stop_loss_price": 15200 (숫자만, 쉼표 제외)
        }}
        """
        
        generate_config = types.GenerateContentConfig(
            response_mime_type="application/json"
        )

        selected_client = random.choice(CLIENT_POOL)

        response = selected_client.models.generate_content(
            model='gemini-3-flash-preview', # 최신 모델 사용 권장
            contents=[final_prompt, image],
            config=generate_config
        )
        
        result_text = response.text.strip()
        ai_logger.debug(f"🤖 AI Raw Response ({condition_id}번): {result_text}")
        
        try:
            cleaned_text = re.sub(r'```json\s*|\s*```', '', result_text)
            result_json = json.loads(cleaned_text)
            
            decision = result_json.get("decision", "NO").upper()
            reason = result_json.get("reason", "분석 실패")
            
            # 손절가 파싱 (쉼표 제거 및 정수 변환)
            stop_loss_price = 0
            try:
                sl_val = result_json.get("stop_loss_price", 0)
                stop_loss_price = int(str(sl_val).replace(',', ''))
            except:
                stop_loss_price = 0
            
            if decision == "YES":
                return True, reason, stop_loss_price
            else:
                return False, reason, 0
                
        except json.JSONDecodeError:
            ai_logger.error(f"AI 응답 JSON 파싱 실패: {result_text}")
            return False, "AI 응답 파싱 오류", 0
            
    except Exception as e:
        ai_logger.error(f"AI 분석 중 오류: {e}")
        return False, f"AI Error: {str(e)}", 0

if __name__ == "__main__":
    print("이 파일은 모듈로 사용됩니다.")