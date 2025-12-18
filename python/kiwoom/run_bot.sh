#!/bin/bash

# 🌟 [수정] 루프 진입 전에 한 번만 설치하면 충분합니다.
echo "[System] requirements.txt에 있는 라이브러리를 설치합니다..."
pip install --upgrade pip
pip install --no-cache-dir -r requirements.txt

echo "[Watcher] 봇 감시자(run_bot.sh)가 시작되었습니다."

# 무한 루프
while true; do
    echo "[Watcher] ---------------------------------"
    echo "[Watcher] Python 봇 (strategy.py)을 시작합니다..."
    
    # 봇 실행
    python3 strategy.py 
    
    echo "[Watcher] 봇(strategy.py)이 종료되었습니다. 3초 후에 재시작합니다..."
    sleep 3
done