import os
import logging
import pandas as pd
import mplfinance as mpf
import json
import re
from datetime import datetime
from dotenv import load_dotenv
from PIL import Image

from google import genai
from google.genai import types

load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

client = genai.Client(api_key=GOOGLE_API_KEY)

ai_logger = logging.getLogger("AI_Analyst")
ai_logger.setLevel(logging.INFO)

if not ai_logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    ai_logger.addHandler(handler)


def create_chart_image(stock_code, stock_name, candle_data):
    """
    API로 받은 캔들 데이터를 이미지 파일로 저장합니다.
    """
    try:
        if not candle_data or len(candle_data) < 20:
            return None
        
        df = pd.DataFrame(candle_data)
        
        # 키움 REST API 응답 필드명 매핑
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
        
        df = df.iloc[::-1] 
        df.index = pd.to_datetime(df['Date'], format='%Y%m%d%H%M%S')
        
        mc = mpf.make_marketcolors(up='red', down='blue', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc)
        
        save_dir = "/data"
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
            
        file_path = f"{save_dir}/{stock_code}_chart.png"
        
        # 차트 저장 설정 (이평선 포함)
        mpf.plot(df, type='candle', mav=(5, 20), volume=True, style=s, 
                 title=f"{stock_name} ({stock_code})", 
                 savefig=dict(fname=file_path, dpi=100, bbox_inches='tight'))
        
        return file_path
    except Exception as e:
        ai_logger.error(f"차트 이미지 생성 실패: {e}")
        return None

def ask_ai_to_buy(image_path):
    """
    Gemini Vision AI에게 차트를 보여주고 매수 여부를 물어봅니다.
    (JSON 모드 및 한글 프롬프트 적용)
    """
    try:
        if not os.path.exists(image_path):
            ai_logger.error("이미지 파일이 존재하지 않습니다.")
            return False, "Image Error"

        image = Image.open(image_path)
        
        # 🌟 [수정] 프롬프트를 한글로 변경하여 출력 언어를 명확히 지정
        prompt = """
        당신은 한국 주식 시장의 초단타 매매(Scalping) 전문가입니다.
        제공된 3분봉 차트 이미지를 분석하여 지금 매수할지 결정해주세요.
        
        [매수 판단 핵심 기준]
        1. 상승 추세가 뚜렷하거나 주요 지지선에서 반등이 확인되는가?
        2. 최근 양봉(빨간색)에서 거래량이 증가하고 있는가?
        3. 주가가 20일 이동평균선 위에 있거나 지지를 받고 있는가?
        4. 윗꼬리가 긴 캔들(매도 압력)이 없는가?

        [출력 형식]
        반드시 아래의 JSON 형식으로만 응답하세요. (Markdown 코드 블록 없이 순수 JSON만 출력)
        {
            "decision": "YES" 또는 "NO",
            "reason": "판단의 근거를 '한글'로 한 문장으로 명확하게 요약해주세요. (예: 20일 이평선 지지 및 거래량 실린 양봉 출현으로 상승 예상)"
        }
        """
        
        # 모델 설정 (JSON 응답 강제)
        generate_config = types.GenerateContentConfig(
            response_mime_type="application/json"
        )

        response = client.models.generate_content(
            model='gemini-3-flash-preview',
            contents=[prompt, image],
            config=generate_config
        )
        
        result_text = response.text.strip()
        ai_logger.debug(f"🤖 AI Raw Response: {result_text}")
        
        # JSON 파싱 및 예외 처리
        try:
            # Markdown code block 제거 (혹시 포함될 경우를 대비)
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