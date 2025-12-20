const express = require('express');
const fs = require('fs').promises; // fs.promises 사용
const path = require('path');
const cookieParser = require('cookie-parser');

const app = express();
const port = 3000;

const DASHBOARD_PASSWORD = process.env.DASHBOARD_PASSWORD || "admin1234";
const SESSION_SECRET = process.env.SESSION_SECRET || "secret_key_change_me";

// 데이터 경로 설정
const DATA_DIR = "/data";
const SETTINGS_FILE = path.join(DATA_DIR, "settings.json");
const STATUS_FILE = path.join(DATA_DIR, "status.json");
const CONDITIONS_FILE = path.join(DATA_DIR, "conditions.json");
const TRADES_FILE = path.join(DATA_DIR, "trades.log");
const CURRENT_CONDITIONS_FILE = path.join(DATA_DIR, "current_conditions.json");
const BACKTEST_REQ_FILE = path.join(DATA_DIR, "backtest_req.json");
const BACKTEST_RES_FILE = path.join(DATA_DIR, "backtest_res.json");
const MASTER_STOCKS_FILE = path.join(DATA_DIR, "master_stocks.json");
const BULK_SELL_FILE = path.join(DATA_DIR, "bulk_sell_req.json");

app.use(express.json()); 
app.use(cookieParser(SESSION_SECRET)); 
app.use(express.static(path.join(__dirname, 'public')));

// 1. 로그인/로그아웃
app.post('/api/login', (req, res) => {
    const { password } = req.body;
    if (password === DASHBOARD_PASSWORD) {
        res.cookie('auth', 'true', { signed: true, httpOnly: true, maxAge: 24 * 60 * 60 * 1000 });
        res.status(200).json({ success: true });
    } else {
        res.status(401).json({ success: false, message: '비밀번호 오류' });
    }
});

app.post('/api/logout', (req, res) => {
    res.clearCookie('auth');
    res.status(200).json({ success: true });
});

// 2. 인증 미들웨어
const checkAuth = (req, res, next) => {
    if (req.signedCookies.auth === 'true') next(); 
    else res.status(401).json({ message: 'Unauthorized' });
};

// 메인 페이지
app.get('/', (req, res) => {
    if (req.signedCookies.auth === 'true') {
        res.sendFile(path.join(__dirname, 'public', 'index.html'));
    } else {
        res.redirect('/login.html');
    }
});

// 3. API 라우트
// 일괄 청산 요청 처리
app.post('/api/bulk_sell', checkAuth, async (req, res) => {
    try {
        await fs.writeFile(BULK_SELL_FILE, JSON.stringify({ timestamp: Date.now() }), 'utf-8');
        res.json({ success: true, message: "일괄 청산 명령이 전송되었습니다." });
    } catch (error) {
        console.error("Bulk sell trigger error:", error);
        res.status(500).json({ success: false, message: "명령 전송 실패" });
    }
});

// 봇 상태 조회
app.get('/api/status', checkAuth, async (req, res) => {
    try {
        let statusData;
        try {
            statusData = await fs.readFile(STATUS_FILE, 'utf-8');
        } catch (e) {
            return res.json({ bot_status: 'OFFLINE', active_mode: '⛔ 파일 없음', is_offline: true, last_sync_ago: 999 });
        }

        let status = {};
        try {
            status = JSON.parse(statusData);
        } catch (e) {
            console.error("JSON 파싱 에러:", e);
            return res.json({ bot_status: 'OFFLINE', active_mode: '⛔ JSON 오류', is_offline: true });
        }
        
        let diffSeconds = 0;
        try {
            const stats = await fs.stat(STATUS_FILE);
            const fileTime = new Date(stats.mtime).getTime();
            diffSeconds = (Date.now() - fileTime) / 1000;
        } catch(e) {}

        status.is_offline = false; 
        status.last_sync_ago = Math.abs(Math.round(diffSeconds)); 
        
        if (status.last_sync_ago > 86400) { 
             status.last_sync_ago = 0; 
        }

        res.json(status);

    } catch (error) {
        console.error("Status check error:", error);
        res.json({ bot_status: 'OFFLINE', active_mode: '⛔ 서버 오류', is_offline: true, last_sync_ago: 999 });
    }
});

// 설정 읽기
app.get('/api/settings', checkAuth, async (req, res) => {
    try {
        const settingsData = await fs.readFile(SETTINGS_FILE, 'utf-8');
        res.json(JSON.parse(settingsData));
    } catch (error) {
        console.error("Load settings error:", error);
        res.status(500).json({ message: 'Error loading settings' });
    }
});

// 설정 저장 (Atomic Write)
app.post('/api/settings', checkAuth, async (req, res) => {
    try {
        const settings = req.body;
        const parseNum = (val, def) => (isNaN(parseFloat(val)) ? def : parseFloat(val));
        
        settings.ORDER_AMOUNT = parseNum(settings.ORDER_AMOUNT, 100000);
        settings.STOP_LOSS_RATE = parseNum(settings.STOP_LOSS_RATE, -1.5);
        settings.TRAILING_START_RATE = parseNum(settings.TRAILING_START_RATE, 1.5);
        settings.TRAILING_STOP_RATE = parseNum(settings.TRAILING_STOP_RATE, -1.0);
        settings.RE_ENTRY_COOLDOWN_MIN = parseNum(settings.RE_ENTRY_COOLDOWN_MIN, 30);
        settings.MIN_BUY_SELL_RATIO = parseNum(settings.MIN_BUY_SELL_RATIO, 0.5);
        
        if(settings.OVERNIGHT_COND_IDS === undefined) settings.OVERNIGHT_COND_IDS = "2";
        
        const tempFile = SETTINGS_FILE + ".tmp";
        await fs.writeFile(tempFile, JSON.stringify(settings, null, 4), 'utf-8');
        await fs.rename(tempFile, SETTINGS_FILE);

        res.json({ success: true, message: 'Settings saved' });
    } catch (error) {
        console.error("Settings save error:", error);
        res.status(500).json({ message: 'Error saving settings' });
    }
});

// 기타 API들
app.get('/api/conditions', checkAuth, async (req, res) => {
    try {
        const data = await fs.readFile(CONDITIONS_FILE, 'utf-8');
        res.json(JSON.parse(data));
    } catch (error) { res.json({ conditions: [] }); }
});

// 🌟 [수정] 로그 파일 파싱 로직 개선 (손익금 정보가 있어도 읽을 수 있게 변경)
app.get('/api/trades', checkAuth, async (req, res) => {
    try {
        const logData = await fs.readFile(TRADES_FILE, 'utf-8');
        // 줄바꿈으로 나누고, 비어있지 않은 줄만 JSON 파싱
        const trades = logData
            .split('\n')
            .filter(line => line.trim() !== '')
            .map(line => {
                try { return JSON.parse(line); } 
                catch (e) { return null; }
            })
            .filter(item => item !== null)
            .reverse() // 최신순 정렬
            .slice(0, 100); // 최근 100건만

        res.json({ trades: trades });
    } catch (error) { 
        res.json({ trades: [] }); 
    }
});

app.get('/api/current_conditions', checkAuth, async (req, res) => {
    try {
        try { await fs.access(CURRENT_CONDITIONS_FILE); } catch { return res.json({ stocks: [] }); }
        const data = await fs.readFile(CURRENT_CONDITIONS_FILE, 'utf-8');
        const stocksObj = JSON.parse(data);
        const stocksArray = Object.values(stocksObj).sort((a, b) => b.time.localeCompare(a.time));
        res.json({ stocks: stocksArray });
    } catch (error) { res.json({ stocks: [] }); }
});

app.get('/api/master_stocks', checkAuth, async (req, res) => {
    try {
        try { await fs.access(MASTER_STOCKS_FILE); } catch { return res.json({}); }
        const data = await fs.readFile(MASTER_STOCKS_FILE, 'utf-8');
        const stockDict = JSON.parse(data);
        const stockList = Object.entries(stockDict).map(([code, name]) => ({ code, name }));
        res.json({ stocks: stockList });
    } catch (error) { res.json({ stocks: [] }); }
});

// 백테스팅 요청
app.post('/api/backtest/request', checkAuth, async (req, res) => {
    console.log("📨 [Node.js] 백테스팅 요청 수신함:", JSON.stringify(req.body)); 

    try {
        const { signals } = req.body;
        
        if (!signals || !Array.isArray(signals) || signals.length === 0) {
            console.error("❌ [Node.js] 데이터 오류: signals가 비어있음");
            return res.status(400).json({ success: false, message: "No signals provided" });
        }

        try { await fs.unlink(BACKTEST_RES_FILE); } catch(e) {}
        
        await fs.writeFile(BACKTEST_REQ_FILE, JSON.stringify({ signals }), 'utf-8');
        console.log(`✅ [Node.js] 요청 파일 생성 완료: ${BACKTEST_REQ_FILE}`);
        
        res.json({ success: true });
    } catch (error) { 
        console.error("❌ [Node.js] 파일 작성 중 에러:", error);
        res.status(500).json({ success: false }); 
    }
});

// 백테스팅 결과 조회
app.get('/api/backtest/result', checkAuth, async (req, res) => {
    res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, proxy-revalidate');
    res.setHeader('Pragma', 'no-cache');
    res.setHeader('Expires', '0');

    try {
        const data = await fs.readFile(BACKTEST_RES_FILE, 'utf-8');
        const result = JSON.parse(data);
        res.json({ status: 'complete', results: result });
    } catch (error) { 
        res.json({ status: 'processing' }); 
    }
});

app.listen(port, () => {
    console.log(`[Web] Server running on port ${port}`);
});