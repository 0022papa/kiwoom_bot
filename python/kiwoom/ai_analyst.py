import os
import logging
import pandas as pd
import mplfinance as mpf
import json
import re
import random
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
    API로 받은 캔들 데이터를 이미지 파일로 저장합니다.
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
                df[col] = df[col].apply(lambda x: int(str(x).replace('+', '').replace('-', '').replace(',', '')))
        
        # 날짜순 정렬 (과거 -> 현재)
        df = df.iloc[::-1] 
        df.index = pd.to_datetime(df['Date'], format='%Y%m%d%H%M%S')
        
        # 🌟 [수정] 데이터 과다 방지: 가장 최근 데이터 기준 2일 전까지만 자르기
        if not df.empty:
            last_date = df.index[-1]
            cutoff_date = last_date - timedelta(days=2)
            df = df[df.index >= cutoff_date]

            # 데이터가 너무 적어졌을 경우 최소한의 개수(예: 30개)는 유지하도록 안전장치
            if len(df) < 30 and len(candle_data) >= 30:
                 # 원본 데이터에서 다시 최근 30개만 가져옴
                 df = pd.DataFrame(candle_data).iloc[::-1].iloc[-30:]
                 # (컬럼 변환 로직 중복 생략을 위해 위에서 처리된 df를 활용하는 것이 좋으나, 
                 #  일반적으로 2일치면 3분봉 기준 충분한 개수가 확보됨)

        mc = mpf.make_marketcolors(up='red', down='blue', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc)
        
        save_dir = "/data"
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
            
        file_path = f"{save_dir}/{stock_code}_chart.png"
        
        # type='candle'로 설정하여 봉 차트 그리기
        mpf.plot(df, type='candle', mav=(5, 20), volume=True, style=s, 
                 title=f"{stock_name} ({stock_code})", 
                 savefig=dict(fname=file_path, dpi=100, bbox_inches='tight'))
        
        return file_path
    except Exception as e:
        ai_logger.error(f"차트 이미지 생성 실패: {e}")
        return None

def ask_ai_to_buy(image_path, condition_id="0"):
    """
    Gemini Vision AI에게 차트를 보여주고 매수 여부를 물어봅니다.
    (조건식 ID에 따른 맞춤형 프롬프트 적용)
    """
    try:
        if not CLIENT_POOL:
            # 혹시 초기화가 안 되었을 경우를 대비해 여기서 시도할 수도 있지만,
            # 원칙적으로 init_ai_clients()가 먼저 호출되어야 합니다.
            ai_logger.error("⚠️ Google AI 클라이언트가 초기화되지 않았습니다.")
            return False, "API Client Not Initialized"

        if not os.path.exists(image_path):
            ai_logger.error("이미지 파일이 존재하지 않습니다.")
            return False, "Image Error"

        image = Image.open(image_path)
        
        # 🌟 전략별 프롬프트 정의
        prompts = {
            "0": """
            당신은 '급등주 돌파 매매(Breakout Strategy)' 전문가입니다. 3분봉 차트를 보고 판단하세요.
            [매수 기준]
            1. 현재 강한 상승세이며, 직전 고점을 돌파했거나 돌파 시도 중인가?
            2. 최근 양봉에 거래량이 크게 실렸는가?
            3. 윗꼬리가 너무 길지 않은가? (매도세가 강하지 않아야 함)
            """,
            "1": """
            당신은 '눌림목 매매(Pullback Strategy)' 전문가입니다. 3분봉 차트를 보고 판단하세요.
            [매수 기준]
            1. 주가가 20일 이동평균선 근처에서 지지를 받고 있는가?
            2. 하락(조정) 구간에서 거래량이 감소했는가?
            3. 지지 라인에서 양봉(반등 신호)이 출현했는가?
            """,
            "2": """
            당신은 '종가베팅(Overnight Strategy)' 전문가입니다. 3분봉 차트를 보고 판단하세요.
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

        [출력 형식]
        반드시 아래의 JSON 형식으로만 응답하세요. (Markdown 코드 블록 없이 순수 JSON만 출력)
        {{
            "decision": "YES" 또는 "NO",
            "reason": "판단의 근거를 '한글'로 한 문장으로 명확하게 요약해주세요."
        }}
        """
        
        generate_config = types.GenerateContentConfig(
            response_mime_type="application/json"
        )

        selected_client = random.choice(CLIENT_POOL)

        response = selected_client.models.generate_content(
            model='gemini-2.0-flash-exp', # 또는 'gemini-1.5-flash'
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
            
            if decision == "YES":
                return True, reason
            else:
                return False, reason
                
        except json.JSONDecodeError:
            ai_logger.error(f"AI 응답 JSON 파싱 실패: {result_text}")
            return False, "AI 응답 파싱 오류"
            
    except Exception as e:
        ai_logger.error(f"AI 분석 중 오류: {e}")
        return False, f"AI Error: {str(e)}"

if __name__ == "__main__":
    print("이 파일은 모듈로 사용됩니다.")