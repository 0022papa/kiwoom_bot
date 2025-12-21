import requests
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

# 현재 디렉토리의 .env 파일 로드
load_dotenv()

# 환경변수에서 토큰 가져오기 (config.py와 동일한 방식)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_test_report():
    print("🚀 텔레그램 리포트 테스트를 시작합니다...")

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ 오류: .env 파일에서 텔레그램 설정을 찾을 수 없습니다.")
        print("   TELEGRAM_BOT_TOKEN과 TELEGRAM_CHAT_ID가 설정되어 있는지 확인해주세요.")
        return

    print(f"🔑 토큰 확인: {TELEGRAM_BOT_TOKEN[:5]}***")
    print(f"🆔 채팅 ID: {TELEGRAM_CHAT_ID}")

    # 1. 가짜(Mock) 데이터 생성
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    # 시뮬레이션 데이터: 3번 이기고 1번 져서 125,000원 번 상황
    total_buy_cnt = 5
    total_sell_cnt = 4
    win_cnt = 3
    loss_cnt = 1
    final_profit = 125000 
    
    win_rate = (win_cnt / total_sell_cnt * 100) if total_sell_cnt > 0 else 0
    profit_emoji = "🔴" if final_profit > 0 else "🔵"
    
    # 실제 strategy.py의 로직과 동일한 포맷
    source_msg = "(테스트 발송 확인용)"

    msg = (
        f"📅 <b>[일별 마감 리포트]</b> {today_str}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🛒 총 매수: {total_buy_cnt}건\n"
        f"👋 총 매도: {total_sell_cnt}건\n"
        f"🏆 승: {win_cnt} / ☠️ 패: {loss_cnt}\n"
        f"📊 승률: {win_rate:.1f}%\n"
        f"{profit_emoji} <b>실현손익: {final_profit:,}원</b>\n"
        f"<span class='text-xs text-gray-400'>{source_msg}</span>\n"
        f"━━━━━━━━━━━━━━\n"
        f"오늘 하루도 수고하셨습니다! ☕"
    )

    # 2. 메시지 전송 (Requests 사용)
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    params = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": msg, 
        "parse_mode": "HTML" # HTML 모드 중요 (굵은 글씨 등)
    }
    
    try:
        print("📨 메시지 전송 시도 중...")
        response = requests.get(url, params=params, timeout=5)
        
        if response.status_code == 200:
            print("\n✅ [성공] 텔레그램으로 리포트가 전송되었습니다! 핸드폰을 확인해보세요.")
        else:
            print(f"\n❌ [실패] 전송 실패. 응답 코드: {response.status_code}")
            print(f"   에러 메시지: {response.text}")
            
    except Exception as e:
        print(f"\n❌ [오류] 연결 중 에러 발생: {e}")

if __name__ == "__main__":
    send_test_report()