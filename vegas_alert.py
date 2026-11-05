#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
维加斯通道 · 双信号提醒（Pine 脚本的 Python 复刻版）
原指标：EMA144/169 通道 + EMA12，趋势突破（去重）+ EMA12 独立金叉/死叉，7 个周期过滤。

为什么不用 TradingView 直接推 Telegram：
  Pine 的 alert() 只是「发一条 TradingView 警报」，本身没有联网能力；
  要真的进 Telegram，得开 webhook（付费功能）再把消息转出去。
  这份脚本直接把同一套指标算在服务器上，然后调 Telegram Bot API——免费、不依赖 TradingView。

用法：
  python vegas_alert.py --once     # 检查一遍（GitHub Actions 用这个）
  python vegas_alert.py            # 本机常驻，每 60 秒检查

环境变量：
  TG_TOKEN        Telegram bot token
  TG_CHAT         你的 chat id
  SYMBOLS         BTCUSDT,ETHUSDT
  INTERVALS       15m,1h,4h,1d
  CHECK_EVERY     本机常驻轮询秒数（默认 60）
  STATE_FILE      状态文件路径（默认本目录 state.json）
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
SYMBOLS = [s.strip().upper() for s in os.environ.get("SYMBOLS", "BTCUSDT").split(",") if s.strip()]
INTERVALS = [s.strip() for s in os.environ.get("INTERVALS", "15m,1h,4h,1d").split(",") if s.strip()]
CHECK_EVERY = int(os.environ.get("CHECK_EVERY", "60"))

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(BASE, "state.json")
LOG_FILE = os.path.join(BASE, "alerts.log")
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# 周期 → 各交易所的写法 / 秒数
INTERVAL_META = {
    "5m": {"sec": 300, "label": "5M", "binance": "5m", "okx": "5m", "gate": "5m"},
    "15m": {"sec": 900, "label": "15M", "binance": "15m", "okx": "15m", "gate": "15m"},
    "30m": {"sec": 1800, "label": "30M", "binance": "30m", "okx": "30m", "gate": "30m"},
    "1h": {"sec": 3600, "label": "1H", "binance": "1h", "okx": "1H", "gate": "1h"},
    "4h": {"sec": 14400, "label": "4H", "binance": "4h", "okx": "4H", "gate": "4h"},
    "1d": {"sec": 86400, "label": "日线", "binance": "1d", "okx": "1D", "gate": "1d"},
    "1w": {"sec": 604800, "label": "周线", "binance": "1w", "okx": "1W", "gate": "7d"},
    "1M": {"sec": 2592000, "label": "月线", "binance": "1M", "okx": "1M", "gate": "30d"},
}

QUOTES = ("USDT", "USDC", "FDUSD", "USD", "BTC", "ETH", "BNB")


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def http_json(url, timeout=20):
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout))


def split_symbol(symbol):
    up = symbol.replace("/", "").replace("-", "").upper()
    for q in QUOTES:
        if up.endswith(q) and len(up) > len(q):
            return up[: -len(q)], q
    return up[:-4], up[-4:]


# ------------------------------ 行情 ------------------------------
# 常见写法 → 实际数据源符号（TradingView 的代码在这里不一定通用）
SYMBOL_ALIASES = {
    "XAUUSD": "GC=F", "XAU": "GC=F", "GOLD": "GC=F", "黄金": "GC=F",
    "XAGUSD": "SI=F", "SILVER": "SI=F",
    "US500": "^GSPC", "SPX": "^GSPC", "SP500": "^GSPC",
    "NAS100": "^NDX", "US100": "^NDX",
    "US30": "^DJI",
}

CRYPTO_SUFFIX = ("USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "BNB")


def is_crypto(symbol):
    up = symbol.replace("/", "").replace("-", "").upper()
    return up.endswith(CRYPTO_SUFFIX)


try:
    from zoneinfo import ZoneInfo

    NY = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    NY = None


def ny_anchor(ts, start_hour, start_minute):
    """返回这根 K 线所属「交易时段」的起点时间戳（纽约时间）

    股票 09:30 开盘、黄金/白银期货 18:00 开盘，TradingView 就是按这个切 4h 的。
    """
    if NY is None:
        return (ts // 86400) * 86400
    import datetime as _dt

    et = _dt.datetime.fromtimestamp(ts, NY)
    day = et.date()
    if (et.hour, et.minute) < (start_hour, start_minute):
        day -= _dt.timedelta(days=1)
    anchor = _dt.datetime(day.year, day.month, day.day, start_hour, start_minute, tzinfo=NY)
    return int(anchor.timestamp())


def session_anchor_for(symbol):
    """按品种决定 4h 从几点切起"""
    sym = symbol.upper()
    if sym.endswith("=F"):          # 期货：黄金 GC=F、白银 SI=F、原油 CL=F
        return lambda ts: ny_anchor(ts, 18, 0)
    return lambda ts: ny_anchor(ts, 9, 30)   # 股票：09:30 开盘


def resample(rows, factor, base_sec, anchor_fn=None):
    """把 1h 拼成 4h 这类：同一时间桶里取最后一根的收盘价"""
    span = base_sec * factor
    out = []
    ident = None
    bucket_start = None
    last_close = None

    for ts, close in rows:
        if anchor_fn:
            anchor = anchor_fn(ts)
            key = (ts - anchor) // span
            start = anchor + key * span
            cur = (anchor, key)
        else:
            key = ts // span
            start = key * span
            cur = (0, key)

        if ident is None:
            ident, bucket_start, last_close = cur, start, close
        elif cur != ident:
            out.append((bucket_start, last_close))
            ident, bucket_start, last_close = cur, start, close
        else:
            last_close = close

    if ident is not None:
        out.append((bucket_start, last_close))
    return out


def fetch_binance(symbol, interval, limit, host):
    iv = INTERVAL_META[interval]["binance"]
    data = http_json(f"{host}/api/v3/klines?symbol={symbol}&interval={iv}&limit={limit}")
    return [(int(k[0]) // 1000, float(k[4])) for k in data]  # (时间, 收盘价)


def fetch_okx(symbol, interval, limit):
    base, quote = split_symbol(symbol)
    iv = INTERVAL_META[interval]["okx"]
    data = http_json(
        f"https://www.okx.com/api/v5/market/candles?instId={base}-{quote}&bar={iv}&limit={min(limit, 300)}"
    )["data"]
    rows = [(int(k[0]) // 1000, float(k[4])) for k in data]
    return rows[::-1]


def fetch_gate(symbol, interval, limit):
    base, quote = split_symbol(symbol)
    iv = INTERVAL_META[interval]["gate"]
    data = http_json(
        "https://api.gateio.ws/api/v4/spot/candlesticks"
        f"?currency_pair={base}_{quote}&interval={iv}&limit={min(limit, 1000)}"
    )
    return [(int(k[0]), float(k[2])) for k in data]


SOURCES = [
    ("binance-vision", lambda s, i, n: fetch_binance(s, i, n, "https://data-api.binance.vision")),
    ("binance", lambda s, i, n: fetch_binance(s, i, n, "https://api.binance.com")),
    ("okx", fetch_okx),
    ("gate", fetch_gate),
]

# Yahoo 没有 4h，用 1h 拼；其余周期它原生支持
YAHOO_INTERVAL = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1h", "1d": "1d", "1w": "1wk", "1M": "1mo"}
YAHOO_RANGE = {"5m": "1mo", "15m": "1mo", "30m": "3mo", "1h": "1y", "1d": "5y", "1w": "10y", "1M": "max"}


def fetch_yahoo(symbol, interval, limit):
    """股票 / 黄金 / 股指：Yahoo Finance 免费接口，不需要 key"""
    sym = SYMBOL_ALIASES.get(symbol.upper(), symbol.upper())
    if interval == "4h":
        base_iv, rng, factor = "1h", "1y", 4
    else:
        base_iv, rng, factor = YAHOO_INTERVAL[interval], YAHOO_RANGE[interval], 1

    last_err = None
    for host in ("query1", "query2"):
        try:
            url = (
                f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                f"{urllib.parse.quote(sym)}?interval={base_iv}&range={rng}&includePrePost=false"
            )
            data = http_json(url)
            res = data["chart"]["result"][0]
            ts = res.get("timestamp") or []
            closes = (res["indicators"]["quote"][0].get("close") or [])
            rows = [(int(t), float(c)) for t, c in zip(ts, closes) if c is not None]
            if len(rows) < 30:
                raise RuntimeError(f"只拿到 {len(rows)} 根")
            rows.sort()
            if factor > 1:
                rows = resample(rows, factor, INTERVAL_META["1h"]["sec"], session_anchor_for(sym))
            return f"yahoo:{sym}", rows
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"yahoo 失败：{type(last_err).__name__} {str(last_err)[:60]}")


def get_closed_candles(symbol, interval, limit=1000):
    """只保留已收盘的 K 线；加密走交易所、股票/黄金走 Yahoo"""
    step = INTERVAL_META[interval]["sec"]
    now = int(time.time())

    def finish(name, rows):
        rows = [r for r in rows if r[0] + step <= now]
        if len(rows) < 220:
            raise RuntimeError(f"{name} 只拿到 {len(rows)} 根收盘 K 线")
        return name, rows[-limit:]

    if not is_crypto(symbol):
        return finish(*fetch_yahoo(symbol, interval, limit))

    errors = []
    for name, fn in SOURCES:
        try:
            return finish(name, fn(symbol, interval, limit + 2))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__} {str(exc)[:50]}")
    raise RuntimeError("行情源都失败 → " + " | ".join(errors))


# ------------------------------ 指标 ------------------------------
def ema(values, length):
    """与 Pine 的 ta.ema 一致：首根用 close 做种子，之后递推"""
    k = 2.0 / (length + 1)
    out = []
    prev = None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def analyse(closes):
    """复刻 Pine 逻辑，返回最后一根收盘 K 线上的四类信号"""
    ema144 = ema(closes, 144)
    ema169 = ema(closes, 169)
    ema12 = ema(closes, 12)
    upper = [max(a, b) for a, b in zip(ema144, ema169)]
    lower = [min(a, b) for a, b in zip(ema144, ema169)]

    longs, shorts = [], []
    for i in range(len(closes)):
        c, e12, up, lo = closes[i], ema12[i], upper[i], lower[i]
        longs.append(e12 > ema144[i] and e12 > ema169[i] and c > ema144[i] and c > ema169[i])
        shorts.append(e12 < ema144[i] and e12 < ema169[i] and c < ema144[i] and c < ema169[i])

    i = len(closes) - 1
    # Pine 里 trendState 是 var：取「当前 K 线之前最后一次出现的方向」
    trend_before = 0
    for j in range(i - 1, -1, -1):
        if longs[j]:
            trend_before = 1
            break
        if shorts[j]:
            trend_before = -1
            break

    long_signal = longs[i] and trend_before != 1
    short_signal = shorts[i] and trend_before != -1

    golden_cross = ema12[i - 1] <= upper[i - 1] and ema12[i] > upper[i]
    death_cross = ema12[i - 1] >= lower[i - 1] and ema12[i] < lower[i]

    return {
        "long": long_signal,
        "short": short_signal,
        "golden": golden_cross,
        "death": death_cross,
        "close": closes[i],
        "ema12": ema12[i],
        "ema144": ema144[i],
        "ema169": ema169[i],
        "upper": upper[i],
        "lower": lower[i],
    }


# ------------------------------ 通知 ------------------------------
def fmt_price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.8f}".rstrip("0").rstrip(".")


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        log("（没配 Telegram，只打印）\n" + text)
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=20))
        if resp.get("ok"):
            log("✅ 已推送 Telegram")
            return True
        log(f"❌ Telegram 返回：{resp}")
    except Exception as exc:  # noqa: BLE001
        log(f"❌ Telegram 失败：{type(exc).__name__} {exc}")
    return False


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError:
        pass


def check_one(symbol, interval, state):
    source, rows = get_closed_candles(symbol, interval)
    bar_time = rows[-1][0]
    key = f"{symbol}|{interval}"

    if state.get(key) == bar_time:
        log(f"{symbol} {interval} | 源 {source} | 这根 K 线已评估过，跳过")
        return False

    closes = [r[1] for r in rows]
    r = analyse(closes)
    state[key] = bar_time  # 同一根 K 线不重复评估

    hits = [
        ("🟢 趋势多头确认", r["long"]),
        ("🔴 趋势空头确认", r["short"]),
        ("🟢 EMA12 独立金叉", r["golden"]),
        ("🔴 EMA12 独立死叉", r["death"]),
    ]
    fired = [name for name, ok in hits if ok]
    summary = (
        f"EMA12={fmt_price(r['ema12'])} 通道[{fmt_price(r['lower'])} ~ {fmt_price(r['upper'])}]"
    )
    log(
        f"{symbol} {interval} | 源 {source} | {summary} | "
        f"{'、'.join(n[2:] for n in fired) if fired else '无信号'}"
    )
    if not fired:
        return False

    lines = list(fired) + [
        "",
        f"周期：{INTERVAL_META[interval]['label']}",
        f"交易对：{symbol}",
        f"价格：{fmt_price(r['close'])}",
        f"时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(bar_time))}（K 线收盘）",
    ]
    send_telegram("\n".join(lines))
    return True


def main():
    once = "--once" in sys.argv
    if "--test" in sys.argv:
        log("发送测试消息…")
        send_telegram(
            "✅ 维加斯通道提醒 · 测试消息\n"
            f"盯盘：{','.join(SYMBOLS)}\n周期：{','.join(INTERVALS)}\n"
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return
    log(f"启动：{','.join(SYMBOLS)} × {','.join(INTERVALS)}")
    state = load_state()

    def sweep():
        changed = False
        for symbol in SYMBOLS:
            for interval in INTERVALS:
                if interval not in INTERVAL_META:
                    log(f"跳过未知周期 {interval}")
                    continue
                try:
                    changed = check_one(symbol, interval, state) or changed
                except Exception as exc:  # noqa: BLE001
                    log(f"{symbol} {interval} 出错：{type(exc).__name__}: {exc}")
        save_state(state)
        return changed

    if once:
        sweep()
        return

    while True:
        try:
            sweep()
        except Exception as exc:  # noqa: BLE001
            log(f"轮询出错：{type(exc).__name__}: {exc}")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
