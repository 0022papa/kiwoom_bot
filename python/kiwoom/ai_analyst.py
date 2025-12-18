import os
import logging
import pandas as pd
import mplfinance as mpf
from datetime import datetime
from dotenv import load_dotenv
from PIL import Image  # 이미지를 불러오기 위해 추가

# ---------------------------------------------------------
# ✅ 변경된 구글 GenAI 라이브러리 import
# ---------------------------------------------------------
from google import genai
from google.genai import types

# ---------------------------------------------------------
# 🔑 구글 Gemini API 설정
# ---------------------------------------------------------
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# ✅ 새로운 방식: 클라이언트 인스턴스 생성
client = genai.Client(api_key=GOOGLE_API_KEY)

# 로거 설정
ai_logger = logging.getLogger("AI_Analyst")
ai_logger.setLevel(logging.INFO)
# 콘솔 출력을 확인하고 싶다면 아래 핸들러 추가 (선택사항)
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
        
        # 데이터프레임 변환
        df = pd.DataFrame(candle_data)
        
        # 🌟 [수정] 키움 REST API (ka10080) 응답 필드명에 맞춰 매핑 수정
        df = df.rename(columns={
            'cntr_tm': 'Date',      # 체결시간
            'cur_prc': 'Close',     # 현재가(종가)
            'open_pric': 'Open',    # 시가
            'high_pric': 'High',    # 고가
            'low_pric': 'Low',      # 저가
            'trde_qty': 'Volume'    # 거래량
        })

        # 혹시 모를 예외 필드명 처리 (구버전 호환성 등)
        # 데이터가 비어있지 않은 컬럼을 우선 사용
        if 'open_prc' in df.columns and 'Open' not in df.columns:
            df.rename(columns={'open_prc': 'Open'}, inplace=True)
            
        # 문자열을 숫자로 변환 (쉼표, 부호 제거)
        cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        for col in cols:
            if col in df.columns:
                df[col] = df[col].apply(lambda x: int(str(x).replace('+', '').replace('-', '').replace(',', '')))
        
        # 날짜 인덱스 설정 (과거 -> 현재 순으로 정렬)
        df = df.iloc[::-1] 
        df.index = pd.to_datetime(df['Date'], format='%Y%m%d%H%M%S')
        
        # 차트 스타일 설정
        mc = mpf.make_marketcolors(up='red', down='blue', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc)
        
        # 이미지 저장 경로 (data 폴더가 없으면 에러나므로 확인 필요)
        save_dir = "/data"
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
            
        file_path = f"{save_dir}/{stock_code}_chart.png"
        
        # 차트 그리기 (이동평균선 포함, 볼륨 패널 끔 등 단순화 가능)
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
    (google-genai 최신 라이브러리 사용)
    """
    try:
        # ✅ 이미지 파일을 PIL Image 객체로 엽니다.
        if not os.path.exists(image_path):
            ai_logger.error("이미지 파일이 존재하지 않습니다.")
            return False, "Image Error"

        image = Image.open(image_path)
        
        prompt = """
        You are a professional scalper trading Korean stocks.
        Look at this 3-minute chart.
        The red candle means Close > Open (Up), Blue means Close < Open (Down).
        Lines are Moving Averages (5, 20).
        
        Key Criteria for BUY:
        1. Strong upward trend or clear rebound from support.
        2. Increasing volume on recent up-candles.
        3. Current price is above or supporting at 20 MA.
        4. No long upper shadows (selling pressure) on the very last candle.

        Question: Is this a good timing to BUY right now?
        Answer format: JUST "YES" or "NO" followed by a very short reason (1 sentence).
        Example: YES, Support at 20MA confirmed with volume.
        """
        
        response = client.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=[prompt, image]
        )
        
        result_text = response.text.strip()
        
        ai_logger.info(f"🤖 AI 분석 결과: {result_text}")
        
        if result_text.upper().startswith("YES"):
            return True, result_text
        else:
            return False, result_text
            
    except Exception as e:
        ai_logger.error(f"AI 분석 중 오류: {e}")
        return False, f"AI Error: {str(e)}"

# --- 테스트 실행 코드 (필요 없으면 삭제) ---
if __name__ == "__main__":
    # 테스트용 가짜 데이터 (실제 사용시는 삭제)
    print("이 파일은 모듈로 사용됩니다. 직접 실행하려면 테스트 데이터를 넣어주세요.")