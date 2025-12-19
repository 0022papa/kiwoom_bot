import os
import logging
import pandas as pd
import mplfinance as mpf
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
        
        mpf.plot(df, type='candle', mav=(5, 20), volume=True, style=s, 
                 title=f"{stock_name} ({stock_code})", 
                 savefig=file_path)
        
        return file_path
    except Exception as e:
        ai_logger.error(f"차트 이미지 생성 실패: {e}")
        return None

def ask_ai_to_buy(image_path):
    """
    Gemini Vision AI에게 차트를 보여주고 매수 여부를 물어봅니다.
    """
    try:
        if not os.path.exists(image_path):
            ai_logger.error("이미지 파일이 존재하지 않습니다.")
            return False, "Image Error"

        image = Image.open(image_path)
        
        prompt = """
        당신은 한국 주식 시장의 전문 스캘퍼(Scalper)입니다.
        제공된 3분봉 차트를 보고 지금이 매수하기 좋은 타이밍인지 분석해주세요.
        
        차트 정보:
        - 빨간색 캔들: 양봉 (종가 > 시가)
        - 파란색 캔들: 음봉 (종가 < 시가)
        - 선: 이동평균선 (5일, 20일)
        
        매수(BUY) 핵심 기준:
        1. 강력한 상승 추세 또는 지지선에서의 명확한 반등이 있는가?
        2. 최근 양봉에서 거래량이 증가하고 있는가?
        3. 현재가가 20일 이동평균선 위에 있거나 지지를 받고 있는가?
        4. 마지막 캔들에 긴 윗꼬리(매도 압력)가 없는가?

        질문: 지금 당장 매수해야 할까요?
        답변 형식: 반드시 "YES" 또는 "NO"로 시작하고, 그 뒤에 판단 이유를 '한국어'로 한 문장으로 짧게 요약해서 적어주세요.
        예시: YES, 20일 이평선 지지를 받고 거래량이 실린 양봉이 출현하여 상승세가 예상됩니다.
        """
        
        response = client.models.generate_content(
            model='gemini-3-flash-preview',
            contents=[prompt, image]
        )
        
        result_text = response.text.strip()
        
        # 🌟 [수정] 중복 출력 방지: INFO -> DEBUG 레벨로 변경
        # Strategy.py에서 최종 결과를 출력하므로 여기서는 숨김 처리합니다.
        ai_logger.debug(f"🤖 AI 분석 결과(Raw): {result_text}")
        
        if result_text.upper().startswith("YES"):
            return True, result_text
        else:
            return False, result_text
            
    except Exception as e:
        ai_logger.error(f"AI 분석 중 오류: {e}")
        return True, f"AI Error: {str(e)}"

if __name__ == "__main__":
    print("이 파일은 모듈로 사용됩니다.")