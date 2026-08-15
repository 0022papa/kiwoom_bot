const express = require('express');
const sqlite3 = require('sqlite3').verbose();
const { exec } = require('child_process');
const path = require('path');
const cookieParser = require('cookie-parser');

const app = express();
const port = 3000;

const DASHBOARD_PASSWORD = process.env.DASHBOARD_PASSWORD || "admin1234";
const SESSION_SECRET = process.env.SESSION_SECRET || "secret_key_change_me";

// 데이터 경로 설정 (DB 파일 위치)
const DATA_DIR = "/data";
const DB_PATH = path.join(DATA_DIR, "kiwoom_bot.db");

// DB 연결 및 초기화
const db = new sqlite3.Database(DB_PATH);

db.serialize(() => {
    // [중요] Python 프로세스와의 동시성 확보를 위해 WAL 모드 활성화 및 타임아웃 설정
    db.run("PRAGMA journal_mode = WAL");
    db.run("PRAGMA busy_timeout = 5000");

    // 1. 키-값 저장소 (Settings, Status, Conditions 등)
    db.run(`CREATE TABLE IF NOT EXISTS kv_store (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT
    )`);
    
    // 2. 매매 로그
    db.run(`CREATE TABLE IF NOT EXISTS trade_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        action TEXT,
        stock_code TEXT,
        stock_name TEXT,
        qty INTEGER,
        price REAL,
        reason TEXT,
        profit_rate REAL,
        profit_amt INTEGER,
        image_path TEXT,
        ai_reason TEXT
    )`);

    // 3. 명령 큐
    db.run(`CREATE TABLE IF NOT EXISTS command_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cmd_type TEXT,
        payload TEXT,
        status TEXT DEFAULT 'PENDING',
        created_at TEXT
    )`);

    // 4. 시스템 로그 (봇 실행/에러 로그)
    db.run(`CREATE TABLE IF NOT EXISTS system_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        level TEXT,
        module TEXT,
        message TEXT
    )`);
});

// --- DB Helper Functions ---
const getKV = (key) => {
    return new Promise((resolve, reject) => {
        db.get("SELECT value FROM kv_store WHERE key = ?", [key], (err, row) => {
            if (err) reject(err);
            else resolve(row ? JSON.parse(row.value) : null);
        });
    });
};

const setKV = (key, value) => {
    return new Promise((resolve, reject) => {
        const valStr = JSON.stringify(value);
        const now = new Date().toISOString();
        db.run("INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)", 
            [key, valStr, now], (err) => {
                if (err) reject(err);
                else resolve(true);
            });
    });
};

const sendCommand = (type, payload) => {
    return new Promise((resolve, reject) => {
        const now = new Date().toISOString();
        const payloadStr = JSON.stringify(payload);
        db.run("INSERT INTO command_queue (cmd_type, payload, created_at) VALUES (?, ?, ?)",
            [type, payloadStr, now], (err) => {
                if (err) reject(err);
                else resolve(true);
            });
    });
};

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

// 일괄 청산 요청 처리 (Command Queue 사용)
app.post('/api/bulk_sell', checkAuth, async (req, res) => {
    try {
        await sendCommand("BULK_SELL", { timestamp: Date.now() });
        res.json({ success: true, message: "일괄 청산 명령이 전송되었습니다." });
    } catch (error) {
        console.error("Bulk sell trigger error:", error);
        res.status(500).json({ success: false, message: "명령 전송 실패" });
    }
});

// 봇 재시작 요청 (Command Queue)
app.post('/api/restart', checkAuth, async (req, res) => {
    try {
        await sendCommand("RESTART_BOT", { timestamp: Date.now() });
        res.json({ success: true, message: "봇 재시작 명령이 전송되었습니다." });
    } catch (error) {
        console.error("Restart trigger error:", error);
        res.status(500).json({ success: false, message: "명령 전송 실패" });
    }
});

// 봇 상태 조회 (KV Store)
app.get('/api/status', checkAuth, async (req, res) => {
    try {
        db.get("SELECT value, updated_at FROM kv_store WHERE key='status'", [], (err, row) => {
            if (err || !row) {
                return res.json({ bot_status: 'OFFLINE', active_mode: '⛔ 데이터 없음', is_offline: true, last_sync_ago: 999 });
            }

            let status = {};
            try { status = JSON.parse(row.value); } catch (e) {}

            let diffSeconds = 0;
            if (row.updated_at) {
                // Python 봇(KST)과 Node.js(UTC) 간 시간대 차이 보정 (+09:00 명시)
                let timeStr = row.updated_at.replace(' ', 'T');
                if (!timeStr.includes('+') && !timeStr.endsWith('Z')) {
                    timeStr += '+09:00';
                }
                const lastUpdate = new Date(timeStr).getTime();
                diffSeconds = (Date.now() - lastUpdate) / 1000;
            }

            status.is_offline = diffSeconds > 60; // 60초 이상 갱신 없으면 오프라인
            status.last_sync_ago = Math.floor(Math.abs(diffSeconds)); 
            
            if (status.last_sync_ago > 86400) status.last_sync_ago = 0; 

            res.json(status);
        });
    } catch (error) {
        console.error("Status check error:", error);
        res.json({ bot_status: 'OFFLINE', active_mode: '⛔ 서버 오류', is_offline: true, last_sync_ago: 999 });
    }
});

// 설정 읽기 (KV Store)
app.get('/api/settings', checkAuth, async (req, res) => {
    try {
        const settings = await getKV("settings");
        res.json(settings || {});
    } catch (error) {
        console.error("Load settings error:", error);
        res.status(500).json({ message: 'Error loading settings' });
    }
});

// 설정 저장 (KV Store)
app.post('/api/settings', checkAuth, async (req, res) => {
    try {
        // [수정] 기존 설정을 먼저 읽어옴 (누락된 키 보존을 위해)
        let currentSettings = {};
        try {
            currentSettings = await getKV("settings") || {};
        } catch (e) { console.error("Existing settings load fail:", e); }

        // 기존 설정 위에 새 설정을 덮어씌움 (Merge)
        const settings = { ...currentSettings, ...req.body };
        
        const parseNum = (val, def) => (isNaN(parseFloat(val)) ? def : parseFloat(val));
        
        settings.ORDER_AMOUNT = parseNum(settings.ORDER_AMOUNT, 1000000);   // 1회 매수금
        settings.STOP_LOSS_RATE = parseNum(settings.STOP_LOSS_RATE, -2.0);  // 손절률
        settings.TRAILING_START_RATE = parseNum(settings.TRAILING_START_RATE, 4.0); // 익절 시작
        settings.TRAILING_STOP_RATE = parseNum(settings.TRAILING_STOP_RATE, -1.0);   // 트레일링 갭 (음수여야 함)
        settings.RE_ENTRY_COOLDOWN_MIN = parseNum(settings.RE_ENTRY_COOLDOWN_MIN, 30); // 쿨다운 (봇 기본값 30분)
        settings.AI_REJECTION_COOLDOWN_MIN = parseNum(settings.AI_REJECTION_COOLDOWN_MIN, 10); // AI 거절 쿨다운
        settings.MIN_BUY_SELL_RATIO = parseNum(settings.MIN_BUY_SELL_RATIO, 0.5);    // 호가 비율
        settings.MAX_BUY_SELL_RATIO = parseNum(settings.MAX_BUY_SELL_RATIO, 10.0);   // 허매수 비율
        settings.VOLUME_FILTER_RATIO = parseNum(settings.VOLUME_FILTER_RATIO, 0.3);   // 거래량 급감 비율
        
        // 동적 트레일링 스탑 설정
        settings.DYN_TS_LV1_TRIGGER = parseNum(settings.DYN_TS_LV1_TRIGGER, 5.0);
        settings.DYN_TS_LV1_DROP = parseNum(settings.DYN_TS_LV1_DROP, -2.0);
        settings.DYN_TS_LV2_TRIGGER = parseNum(settings.DYN_TS_LV2_TRIGGER, 10.0);
        settings.DYN_TS_LV2_DROP = parseNum(settings.DYN_TS_LV2_DROP, -3.0);
        settings.DYN_TS_LV3_TRIGGER = parseNum(settings.DYN_TS_LV3_TRIGGER, 20.0);
        settings.DYN_TS_LV3_DROP = parseNum(settings.DYN_TS_LV3_DROP, -5.0);

        settings.RSI_LIMIT = parseNum(settings.RSI_LIMIT, 70.0);       // 과매수 제한
        settings.TIME_CUT_MINUTES = parseNum(settings.TIME_CUT_MINUTES, 20); // 타임컷
        settings.UPPER_SHADOW_LIMIT = parseNum(settings.UPPER_SHADOW_LIMIT, 0.4); // 윗꼬리 제한
        settings.DAILY_SHADOW_LIMIT = parseNum(settings.DAILY_SHADOW_LIMIT, 0.5); // 일봉 윗꼬리 제한

        if(settings.OVERNIGHT_COND_IDS === undefined) settings.OVERNIGHT_COND_IDS = "";
        
        // AI 손절가 및 안전장치 값 저장
        if(settings.USE_AI_STOP_LOSS === undefined) settings.USE_AI_STOP_LOSS = true; 
        settings.AI_STOP_LOSS_SAFETY_LIMIT = parseNum(settings.AI_STOP_LOSS_SAFETY_LIMIT, -5.0);

        // 🌟 [추가] 지수 필터 설정 (기본값 false)
        if(settings.USE_MARKET_FILTER === undefined) settings.USE_MARKET_FILTER = false;

        // 🌟 [추가] 테마 필터 설정
        if(settings.USE_THEME_FILTER === undefined) settings.USE_THEME_FILTER = true;
        settings.THEME_TOP_N = parseNum(settings.THEME_TOP_N, 3);

        if(settings.USE_VOLUME_FILTER === undefined) settings.USE_VOLUME_FILTER = true;
        if(settings.USE_HOGA_FILTER === undefined) settings.USE_HOGA_FILTER = true;
        if(settings.USE_FAKE_BUY_FILTER === undefined) settings.USE_FAKE_BUY_FILTER = true;
        if(settings.USE_RSI_FILTER === undefined) settings.USE_RSI_FILTER = true;
        if(settings.USE_TIME_CUT === undefined) settings.USE_TIME_CUT = true;
        if(settings.USE_UPPER_SHADOW_FILTER === undefined) settings.USE_UPPER_SHADOW_FILTER = true;
        if(settings.USE_DAILY_SHADOW_FILTER === undefined) settings.USE_DAILY_SHADOW_FILTER = true;
        if(settings.USE_STOP_LOSS === undefined) settings.USE_STOP_LOSS = true;
        if(settings.USE_TRAILING_STOP === undefined) settings.USE_TRAILING_STOP = true;
        if(settings.USE_RE_ENTRY_COOLDOWN === undefined) settings.USE_RE_ENTRY_COOLDOWN = true;
        if(settings.USE_AI_REJECTION_COOLDOWN === undefined) settings.USE_AI_REJECTION_COOLDOWN = true;
        if(settings.USE_PARTIAL_PROFIT === undefined) settings.USE_PARTIAL_PROFIT = true;
        settings.PARTIAL_PROFIT_RATE = parseNum(settings.PARTIAL_PROFIT_RATE, 50.0);
        if(settings.USE_DYNAMIC_TS === undefined) settings.USE_DYNAMIC_TS = false;

        // [추가] 물타기 설정
        if(settings.USE_WATERING === undefined) settings.USE_WATERING = false;
        settings.MAX_WATERING_COUNT = parseNum(settings.MAX_WATERING_COUNT, 1);
        settings.MAX_WATERING_AMOUNT = parseNum(settings.MAX_WATERING_AMOUNT, 2000000);

        await setKV("settings", settings);
        res.json({ success: true, message: 'Settings saved' });
    } catch (error) {
        console.error("Settings save error:", error);
        res.status(500).json({ message: 'Error saving settings' });
    }
});

// 기타 API들 (KV Store)
app.get('/api/conditions', checkAuth, async (req, res) => {
    try {
        const data = await getKV("conditions");
        res.json(data || { conditions: [] });
    } catch (error) { res.json({ conditions: [] }); }
});

// 매매 로그 조회 (DB)
app.get('/api/trades', checkAuth, (req, res) => {
    db.all("SELECT * FROM trade_logs ORDER BY id DESC LIMIT 100", [], (err, rows) => {
        if (err) {
            console.error("Trades fetch error:", err);
            res.json({ trades: [] });
        } else {
            res.json({ trades: rows });
        }
    });
});

// 시스템 로그 조회 (DB)
app.get('/api/logs', checkAuth, (req, res) => {
    db.all("SELECT * FROM system_logs ORDER BY id DESC LIMIT 200", [], (err, rows) => {
        if (err) res.json({ logs: [] });
        else res.json({ logs: rows });
    });
});

app.get('/api/current_conditions', checkAuth, async (req, res) => {
    try {
        const stocksObj = await getKV("current_conditions");
        if (!stocksObj) return res.json({ stocks: [] });

        const stocksArray = Object.values(stocksObj).sort((a, b) => b.time.localeCompare(a.time));
        res.json({ stocks: stocksArray });
    } catch (error) { res.json({ stocks: [] }); }
});

app.get('/api/master_stocks', checkAuth, async (req, res) => {
    try {
        const stockDict = await getKV("master_stocks");
        if (!stockDict) return res.json({ stocks: [] });

        const stockList = Object.entries(stockDict).map(([code, name]) => ({ code, name }));
        res.json({ stocks: stockList });
    } catch (error) { res.json({ stocks: [] }); }
});

// 백테스팅 요청 (Command Queue)
app.post('/api/backtest/request', checkAuth, async (req, res) => {
    console.log("📨 [Node.js] 백테스팅 요청 수신함:", JSON.stringify(req.body)); 

    try {
        const { signals, mode } = req.body; // mode 추가 (simulation / optimize)
        
        if (!signals || !Array.isArray(signals) || signals.length === 0) {
            return res.status(400).json({ success: false, message: "No signals provided" });
        }

        // 결과 초기화
        await setKV("backtest_result", null);
        
        await sendCommand("BACKTEST_REQ", { signals, mode });
        console.log(`✅ [Node.js] 백테스팅 명령 DB 전송 완료`);
        
        res.json({ success: true });
    } catch (error) { 
        console.error("❌ [Node.js] 명령 전송 에러:", error);
        res.status(500).json({ success: false }); 
    }
});

// 🌟 [추가] 백테스팅 중지 요청 API
app.post('/api/backtest/stop', checkAuth, async (req, res) => {
    try {
        await sendCommand("BACKTEST_STOP", { timestamp: Date.now() });
        res.json({ success: true, message: "중지 명령 전송 완료" });
    } catch (error) {
        console.error("Stop backtest error:", error);
        res.status(500).json({ success: false, message: "중지 실패" });
    }
});

// 백테스팅 추천 종목 (등락률 상위) API
app.get('/api/backtest/recommend', checkAuth, (req, res) => {
    exec('python3 /home/incjswo/kiwoom_bot/python/kiwoom/get_top_movers.py', (error, stdout, stderr) => {
        if (error) {
            console.error(`exec error: ${error}`);
            return res.status(500).json({ success: false, message: "스크립트 실행 실패" });
        }
        try {
            const data = JSON.parse(stdout);
            if (data.error) {
                return res.status(500).json({ success: false, message: data.error });
            }
            res.json({ success: true, stocks: data });
        } catch (e) {
            console.error("JSON parse error:", e);
            res.status(500).json({ success: false, message: "데이터 파싱 오류" });
        }
    });
});

// 백테스팅 결과 조회 (KV Store)
app.get('/api/backtest/result', checkAuth, async (req, res) => {
    res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, proxy-revalidate');
    
    try {
        const result = await getKV("backtest_result");
        if (result) {
            res.json({ status: 'complete', results: result });
        } else {
            res.json({ status: 'processing' });
        }
    } catch (error) { 
        res.json({ status: 'processing' }); 
    }
});

app.listen(port, () => {
    console.log(`[Web] Server running on port ${port}`);
});