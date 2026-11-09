#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
维加斯通道提醒 · 多品种 / 多周期 / 多指标 · Telegram 图片卡片推送

本地：  python vegas_alert.py           常驻轮询 60 秒
       python vegas_alert.py --once    只查一遍（GitHub Actions 用这个）
       python vegas_alert.py --test    发一条测试消息
       python vegas_alert.py --demo    发一张示例卡片（看样式）

环境变量：
  TG_TOKEN / TG_CHAT    Telegram
  SYMBOLS               品种，逗号分隔
  INTERVALS             周期，逗号分隔（5m 15m 30m 1h 4h 1d 1w 1M）
  CHECK_EVERY           本机轮询秒数
  STATE_FILE            状态文件路径
"""
import datetime
import io
import json
import os
import sys
import time
from datetime import timedelta, timezone
import urllib.parse
import urllib.request
import uuid

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
SYMBOLS = [s.strip().upper() for s in os.environ.get("SYMBOLS", "BTCUSDT").split(",") if s.strip()]
INTERVALS = [s.strip() for s in os.environ.get("INTERVALS", "1h,4h,1d").split(",") if s.strip()]
CHECK_EVERY = int(os.environ.get("CHECK_EVERY", "60"))
MAX_DELAY_MIN = float(os.environ.get("MAX_DELAY_MIN", "20"))   # 超过这么久才发现的信号直接丢弃，不推过期信号

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(BASE, "state.json")
LOG_FILE = os.path.join(BASE, "alerts.log")
FONT_FILE = os.path.join(BASE, "vegas_font.ttf")
UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# 全部时间统一按北京时间显示（GitHub 的服务器是 UTC，不能直接用 localtime）
try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    TZ = timezone(timedelta(hours=8))


def fmt_time(ts, fmt="%m-%d %H:%M"):
    return datetime.datetime.fromtimestamp(ts, TZ).strftime(fmt)


def fmt_price(p):
    """按价位大小给合适的小数位，避免 0.00001234 被截成 0.00"""
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.2f}".rstrip("0").rstrip(".")
    if p >= 0.01:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.8f}".rstrip("0").rstrip(".")

# 周期 → 各数据源的写法 / 秒数 / 中文名
INTERVAL_META = {
    "5m": {"sec": 300, "label": "5分钟", "binance": "5m", "okx": "5m", "gate": "5m", "yahoo": "5m"},
    "15m": {"sec": 900, "label": "15分钟", "binance": "15m", "okx": "15m", "gate": "15m", "yahoo": "15m"},
    "30m": {"sec": 1800, "label": "30分钟", "binance": "30m", "okx": "30m", "gate": "30m", "yahoo": "30m"},
    "1h": {"sec": 3600, "label": "1小时", "binance": "1h", "okx": "1H", "gate": "1h", "yahoo": "1h"},
    "4h": {"sec": 14400, "label": "4小时", "binance": "4h", "okx": "4H", "gate": "4h", "yahoo": None},
    "1d": {"sec": 86400, "label": "日线", "binance": "1d", "okx": "1D", "gate": "1d", "yahoo": "1d"},
    "1w": {"sec": 604800, "label": "周线", "binance": "1w", "okx": "1W", "gate": "7d", "yahoo": "1wk"},
    "1M": {"sec": 2592000, "label": "月线", "binance": "1M", "okx": "1M", "gate": "30d", "yahoo": "1mo"},
}

QUOTES = ("USDT", "USDC", "FDUSD", "USD", "BTC", "ETH", "BNB")
GOLD_SYMBOLS = {"XAUUSD", "XAU", "GOLD", "黄金", "PAXGUSDT", "XAUTUSDT"}

DISPLAY_NAMES = {
    "XAUUSD": "现货黄金",
    "PAXGUSDT": "现货黄金",
    "SNDKUSDT": "闪迪合约",
    "SOXLBUSDT": "SOXLB",
}

# 信号 → (文案, 方向)  方向: +1 做多 / -1 做空
SIGNAL_META = {
    "long": ("趋势多头确认", 1),

    "short": ("趋势空头确认", -1),


}




def log(msg):
    line = f"[{fmt_time(time.time(), '%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def http_json(url, timeout=20, data=None, headers=None):
    hdrs = dict(UA)
    hdrs.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def split_symbol(symbol):
    up = symbol.replace("/", "").replace("-", "").upper()
    for q in QUOTES:
        if up.endswith(q) and len(up) > len(q):
            return up[: -len(q)], q
    return up[:-4], up[-4:]


def is_crypto(symbol):
    up = symbol.replace("/", "").replace("-", "").upper()
    return up.endswith(("USDT", "USDC", "FDUSD", "BUSD"))


# ============================== 行情源 ==============================
class InsufficientHistory(RuntimeError):
    """K 线不够，EMA144/169 还走不稳（新上线的小币常见）"""


def _bn_klines(url, symbol, interval, limit):
    iv = INTERVAL_META[interval]["binance"]
    data = http_json(f"{url}?symbol={symbol}&interval={iv}&limit={limit}")
    return [(int(k[0]) // 1000, float(k[4])) for k in data]


def spot_binance(symbol, interval, limit):
    return _bn_klines("https://data-api.binance.vision/api/v3/klines", symbol, interval, limit)


def spot_binance_main(symbol, interval, limit):
    return _bn_klines("https://api.binance.com/api/v3/klines", symbol, interval, limit)


def spot_okx(symbol, interval, limit):
    base, quote = split_symbol(symbol)
    iv = INTERVAL_META[interval]["okx"]
    d = http_json(
        f"https://www.okx.com/api/v5/market/candles?instId={base}-{quote}&bar={iv}&limit={min(limit, 300)}"
    )["data"]
    return [(int(k[0]) // 1000, float(k[4])) for k in d][::-1]


def spot_gate(symbol, interval, limit):
    base, quote = split_symbol(symbol)
    iv = INTERVAL_META[interval]["gate"]
    d = http_json(
        "https://api.gateio.ws/api/v4/spot/candlesticks"
        f"?currency_pair={base}_{quote}&interval={iv}&limit={min(limit, 1000)}"
    )
    return [(int(k[0]), float(k[2])) for k in d]


# ---- 永续合约（闪迪这种「股票代币合约」只在合约市场有）----
def perp_binance(symbol, interval, limit):
    return _bn_klines("https://fapi.binance.com/fapi/v1/klines", symbol, interval, limit)


def perp_bybit(symbol, interval, limit):
    minutes = max(1, INTERVAL_META[interval]["sec"] // 60)
    d = http_json(
        f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}"
        f"&interval={minutes}&limit={min(limit, 1000)}"
    )["result"]["list"]
    return [(int(k[0]) // 1000, float(k[4])) for k in d][::-1]


def perp_okx(symbol, interval, limit):
    base, quote = split_symbol(symbol)
    iv = INTERVAL_META[interval]["okx"]
    d = http_json(
        f"https://www.okx.com/api/v5/market/candles?instId={base}-{quote}-SWAP&bar={iv}&limit={min(limit, 300)}"
    )["data"]
    return [(int(k[0]) // 1000, float(k[4])) for k in d][::-1]


def perp_gate(symbol, interval, limit):
    base, quote = split_symbol(symbol)
    iv = INTERVAL_META[interval]["gate"]
    d = http_json(
        "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
        f"?contract={base}_{quote}&interval={iv}&limit={min(limit, 1000)}"
    )
    return [(int(k[0]), float(k["c"])) for k in d]


SPOT_SOURCES = [("币安镜像", spot_binance), ("币安", spot_binance_main), ("OKX", spot_okx), ("Gate", spot_gate)]
PERP_SOURCES = [("币安合约", perp_binance), ("Bybit合约", perp_bybit), ("OKX合约", perp_okx), ("Gate合约", perp_gate)]


# ---- 股票 / 期货（Yahoo，免 key）----
SYMBOL_ALIASES = {
    "US500": "^GSPC", "SPX": "^GSPC", "SP500": "^GSPC",
    "NAS100": "^NDX", "US100": "^NDX", "US30": "^DJI",
    "XAGUSD": "SI=F", "SILVER": "SI=F", "WTI": "CL=F", "OIL": "CL=F",
}
YAHOO_RANGE = {"5m": "1mo", "15m": "1mo", "30m": "3mo", "1h": "1y", "1d": "5y", "1w": "10y", "1M": "max"}

try:
    from zoneinfo import ZoneInfo

    NY = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    NY = None


def ny_anchor(ts, hour, minute):
    if NY is None:
        return (ts // 86400) * 86400
    import datetime as _dt

    et = _dt.datetime.fromtimestamp(ts, NY)
    day = et.date()
    if (et.hour, et.minute) < (hour, minute):
        day -= _dt.timedelta(days=1)
    return int(_dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY).timestamp())


def session_anchor_for(symbol):
    sym = symbol.upper()
    if sym.endswith("=F"):
        return lambda ts: ny_anchor(ts, 18, 0)   # 期货 18:00 ET 开盘
    return lambda ts: ny_anchor(ts, 9, 30)       # 股票 09:30 ET 开盘


def resample(rows, factor, base_sec, anchor_fn=None):
    span = base_sec * factor
    out, ident, start, last = [], None, None, None
    for ts, close in rows:
        if anchor_fn:
            a = anchor_fn(ts)
            k = (ts - a) // span
            s = a + k * span
            cur = (a, k)
        else:
            k = ts // span
            s = k * span
            cur = (0, k)
        if ident is None:
            ident, start, last = cur, s, close
        elif cur != ident:
            out.append((start, last))
            ident, start, last = cur, s, close
        else:
            last = close
    if ident is not None:
        out.append((start, last))
    return out


def fetch_yahoo(symbol, interval, limit):
    sym = SYMBOL_ALIASES.get(symbol.upper(), symbol.upper())
    if interval == "4h":
        base_iv, rng, factor = "1h", "1y", 4
    else:
        base_iv, rng, factor = INTERVAL_META[interval]["yahoo"], YAHOO_RANGE[interval], 1
    if not base_iv:
        raise RuntimeError(f"Yahoo 不支持 {interval}")

    last_err = None
    for host in ("query1", "query2"):
        try:
            d = http_json(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
                f"?interval={base_iv}&range={rng}&includePrePost=false"
            )
            res = d["chart"]["result"][0]
            ts = res.get("timestamp") or []
            closes = res["indicators"]["quote"][0].get("close") or []
            rows = sorted((int(t), float(c)) for t, c in zip(ts, closes) if c is not None)
            if len(rows) < 30:
                raise RuntimeError(f"只有 {len(rows)} 根")
            if factor > 1:
                rows = resample(rows, factor, INTERVAL_META["1h"]["sec"], session_anchor_for(sym))
            return f"Yahoo:{sym}", rows
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"Yahoo 失败：{type(last_err).__name__} {str(last_err)[:50]}")


def get_closed_candles(symbol, interval, limit=1000):
    step = INTERVAL_META[interval]["sec"]
    now = int(time.time())
    errors, short = [], []

    def finish(name, rows):
        rows = [r for r in rows if r[0] + step <= now]
        if len(rows) < 220:
            short.append((len(rows), name))
            raise InsufficientHistory(f"{name} 只有 {len(rows)} 根收盘 K 线（需要 220 根）")
        return name, rows[-limit:]

    up = symbol.upper()

    # 现货黄金 → PAXG（1:1 锚定实物金，24h 交易），再退 XAUT、期货
    if up in GOLD_SYMBOLS:
        for name, fn in (
            ("PAXG@币安", lambda: spot_binance("PAXGUSDT", interval, limit + 2)),
            ("PAXG@OKX", lambda: spot_okx("PAXGUSDT", interval, limit + 2)),
            ("PAXG@Gate", lambda: spot_gate("PAXGUSDT", interval, limit + 2)),
            ("XAUT@币安", lambda: spot_binance("XAUTUSDT", interval, limit + 2)),
        ):
            try:
                return finish(name, fn())
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {type(exc).__name__}")
        return finish(*fetch_yahoo("GC=F", interval, limit))

    if is_crypto(symbol):
        for name, fn in SPOT_SOURCES:      # 先试现货
            try:
                return finish(name, fn(symbol, interval, limit + 2))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {type(exc).__name__}")
        for name, fn in PERP_SOURCES:      # 再试永续合约（股票代币/新币常在这里）
            try:
                return finish(name, fn(symbol, interval, limit + 2))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {type(exc).__name__}")
        base = split_symbol(symbol)[0]
        try:
            name, rows = fetch_yahoo(base, interval, limit)
            log(f"说明：{symbol} 现货/合约都没数据，按美股 {base} 处理")
            return finish(name, rows)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Yahoo:{base}: {type(exc).__name__}")
        if short:
            n, nm = max(short)
            raise InsufficientHistory(f"{nm} 只有 {n} 根")
        raise RuntimeError("行情源全挂 → " + " | ".join(errors))

    return finish(*fetch_yahoo(symbol, interval, limit))


# ============================== 指标 ==============================
def ema(values, length):
    k = 2.0 / (length + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def analyse_vegas(closes, idx=None):
    """维加斯通道：EMA144/169 通道 + EMA12 趋势突破 + 独立金叉死叉"""
    i = len(closes) - 1 if idx is None else idx
    e144, e169, e12 = ema(closes, 144), ema(closes, 169), ema(closes, 12)
    upper = [max(a, b) for a, b in zip(e144, e169)]
    lower = [min(a, b) for a, b in zip(e144, e169)]

    longs, shorts = [], []
    for k in range(len(closes)):
        c = closes[k]
        longs.append(e12[k] > e144[k] and e12[k] > e169[k] and c > e144[k] and c > e169[k])
        shorts.append(e12[k] < e144[k] and e12[k] < e169[k] and c < e144[k] and c < e169[k])

    trend_before = 0
    for j in range(i - 1, -1, -1):
        if longs[j]:
            trend_before = 1
            break
        if shorts[j]:
            trend_before = -1
            break

    return {
        "long": longs[i] and trend_before != 1,
        "short": shorts[i] and trend_before != -1,

        "close": closes[i],
        "ema12": e12[i],
        "upper": upper[i],
        "lower": lower[i],
        "info": f"EMA12 {e12[i]:,.2f} / 通道 {lower[i]:,.2f} ~ {upper[i]:,.2f}",
    }


# 想加指标就写一个 analyse_xxx 再挂到这里，check_one 会自动跑
# 格式： 名称: (函数, 只在哪些周期跑；None 表示所有周期都跑)
INDICATORS = {
    "维加斯": (analyse_vegas, None),
}


# ============================== 卡片 ==============================
def _load_font(size):
    from PIL import ImageFont

    for path in (FONT_FILE,
                 r"C:\Windows\Fonts\msyhbd.ttc",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                 "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"):
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def render_card(symbol, tf_label, fired, direction, price, close_ts, info="", delayed=False):
    """做多=红底，做空=绿底，延迟的用灰底。返回 PNG bytes；Pillow 不可用时返回 None"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    is_long = direction > 0
    if delayed:                                              # 迟到的一律灰底，一眼能看出来
        bg = (97, 97, 97)
    elif is_long:
        bg = (183, 28, 28)                                   # 红涨
    elif direction < 0:
        bg = (27, 94, 32)                                    # 绿跌
    else:
        bg = (55, 71, 79)
    W, H = 900, 600
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    def fit(text, font, max_w):
        """太长就截断加省略号"""
        if d.textlength(text, font=font) <= max_w:
            return text
        while text and d.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        return text + "…"

    f_name, f_tf = _load_font(54), _load_font(30)
    f_action, f_line, f_price = _load_font(104), _load_font(36), _load_font(60)
    f_small = _load_font(24)

    d.rectangle([0, 0, W, 8], fill=(255, 255, 255))

    name = DISPLAY_NAMES.get(symbol.upper(), symbol)
    d.text((48, 44), fit(name, f_name, W - 260), font=f_name, fill=(255, 255, 255))
    tw = d.textlength(tf_label, font=f_tf)
    d.text((W - 48 - tw, 62), tf_label, font=f_tf, fill=(255, 226, 226))

    action = "做多" if is_long else ("做空" if direction < 0 else "信号")
    d.text((44, 128), action, font=f_action, fill=(255, 255, 255))
    d.line([48, 262, W - 48, 262], fill=(255, 255, 255, 80), width=2)

    y = 276
    for line in fired[:3]:
        d.text((48, y), f"· {line}", font=f_line, fill=(255, 255, 255))
        y += 46

    d.text((44, 432), price, font=f_price, fill=(255, 255, 255))
    # 主时间 = 触发那根 K 线的「收盘时间」（信号就是这一刻成立的）
    stamp = f"{fmt_time(close_ts)} 收盘（北京时间）"
    if delayed:
        minutes = max(0, int((time.time() - close_ts) / 60))
        stamp += f" · 延迟 {minutes} 分钟（灰底）"
    d.text((48, 506), stamp, font=f_small, fill=(255, 226, 226))
    if info:
        d.text((48, 542), fit(info, f_small, W - 96), font=f_small, fill=(255, 210, 210))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ============================== Telegram ==============================
def _post_multipart(url, fields, file_field, filename, file_bytes, mime="image/png"):
    boundary = "----vegas" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    return json.load(urllib.request.urlopen(req, timeout=40))


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        log("（没配 Telegram，只打印）\n" + text.replace("<b>", "").replace("</b>", ""))
        return False
    payload = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        r = json.load(urllib.request.urlopen(req, timeout=20))
        if r.get("ok"):
            log("✅ 已推送 Telegram")
            return True
        log(f"❌ Telegram 返回：{r}")
    except Exception as exc:  # noqa: BLE001
        log(f"❌ Telegram 失败：{type(exc).__name__} {exc}")
    return False


def send_card(card_png, caption):
    if not TG_TOKEN or not TG_CHAT:
        log("（没配 Telegram，只打印卡片说明）\n" + caption)
        return False
    try:
        fields = {"chat_id": TG_CHAT, "caption": caption}
        r = _post_multipart(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
            fields, "photo", "signal.png", card_png,
        )
        if r.get("ok"):
            log("✅ 卡片已推送 Telegram")
            return True
        log(f"❌ 发图失败：{r.get('description')}")
    except Exception as exc:  # noqa: BLE001
        log(f"❌ 发图异常：{type(exc).__name__} {exc}")
    return False


# ============================== 状态 & 主循环 ==============================
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
    key = f"{symbol}|{interval}"
    last_bar, done = rows[-1][0], state.get(key, 0)

    if done >= last_bar:
        log(f"{symbol} {interval} | {source} | 已评估过，跳过")
        return False

    closes = [r[1] for r in rows]
    pending = [i for i, r in enumerate(rows) if r[0] > done][-500:]
    baseline_only = done == 0
    if baseline_only:
        log(f"{symbol} {interval} | {source} | 首次运行，记录基线")

    fired_bars = []
    for i in pending:
        merged, infos = {}, []
        for _name, (fn, only) in INDICATORS.items():
            if only and interval not in only:
                continue
            res = fn(closes, i)
            infos.append(res.get("info", ""))
            for field in SIGNAL_META:
                if res.get(field):
                    merged[field] = True
            merged.setdefault("close", res.get("close"))
        merged["info"] = " | ".join(x for x in infos if x)

        fired = [SIGNAL_META[f][0] for f in SIGNAL_META if merged.get(f)]
        log(f"{symbol} {interval} | {source} | {merged['info']} | {('、'.join(fired) if fired else '无信号')}")
        if fired and not baseline_only:
            # 记住这根 K 线自己的收盘价，保证推送价 = 触发那根 K 线的收盘价
            fired_bars.append((rows[i][0], closes[i], merged, fired))

    state[key] = last_bar
    if not fired_bars:
        return False

    # 允许补发：这轮没评估到的 K 线全部补上；太多就只补最近 10 条，防止刷屏
    if len(fired_bars) > 10:
        log(f"{symbol} {interval} | 本次共 {len(fired_bars)} 条，只补发最近 10 条")
        fired_bars = fired_bars[-10:]

    step = INTERVAL_META[interval]["sec"]
    sent = False
    for ts, bar_close, r, fired in fired_bars:
        directions = {SIGNAL_META[f][1] for f in SIGNAL_META if r.get(f)}
        direction = 1 if directions == {1} else (-1 if directions == {-1} else 0)

        close_ts = ts + step                  # K 线「收盘」时间 = 开盘 + 周期
        delay_min = (time.time() - close_ts) / 60
        delayed = delay_min > MAX_DELAY_MIN   # 迟到就灰底 + 标延迟，但绝不丢
        if delayed:
            log(f"{symbol} {interval} | 延迟 {delay_min:.0f} 分钟（灰底推送）")

        price = fmt_price(bar_close)
        card = render_card(symbol, INTERVAL_META[interval]["label"], fired, direction,
                           price, close_ts, r["info"], delayed)
        caption = (
            f"{'做多' if direction > 0 else '做空'} · {DISPLAY_NAMES.get(symbol.upper(), symbol)} · "
            f"{INTERVAL_META[interval]['label']}\n" + "\n".join(fired) +
            f"\n收盘价 {price}\n{fmt_time(close_ts)} 收盘（北京时间）"
            f"｜K线 {fmt_time(ts, '%H:%M')}→{fmt_time(close_ts, '%H:%M')}"
        )
        if delayed:
            caption += f"\n⚠️ 延迟 {delay_min:.0f} 分钟"
        if card and send_card(card, caption):
            sent = True
        else:
            sent = send_telegram(caption) or sent
    return sent


def sweep(state):
    changed = False
    for symbol in SYMBOLS:
        for interval in INTERVALS:
            if interval not in INTERVAL_META:
                log(f"跳过未知周期 {interval}")
                continue
            try:
                changed = check_one(symbol, interval, state) or changed
            except InsufficientHistory as exc:
                log(f"{symbol} {interval} | 暂时跳过（{exc}）")
            except Exception as exc:  # noqa: BLE001
                log(f"{symbol} {interval} 出错：{type(exc).__name__}: {exc}")
    save_state(state)
    return changed


def replay(day_str):
    """把指定日期（北京时间）当天所有触发过的信号重新推一遍，用于核对"""
    day = datetime.datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=TZ)
    start, end = day.timestamp(), day.timestamp() + 86400
    total = 0
    for symbol in SYMBOLS:
        for interval in INTERVALS:
            if interval not in INTERVAL_META:
                continue
            try:
                source, rows = get_closed_candles(symbol, interval)
            except Exception as exc:  # noqa: BLE001
                log(f"{symbol} {interval} | 回放取数失败：{type(exc).__name__}: {exc}")
                continue
            closes = [r[1] for r in rows]
            step = INTERVAL_META[interval]["sec"]
            for i in range(1, len(rows)):
                close_ts = rows[i][0] + step
                if not (start <= close_ts < end):
                    continue
                merged, infos = {}, []
                for _n, (fn, only) in INDICATORS.items():
                    if only and interval not in only:
                        continue
                    res = fn(closes, i)
                    infos.append(res.get("info", ""))
                    for field in SIGNAL_META:
                        if res.get(field):
                            merged[field] = True
                fired = [SIGNAL_META[f][0] for f in SIGNAL_META if merged.get(f)]
                if not fired:
                    continue
                directions = {SIGNAL_META[f][1] for f in SIGNAL_META if merged.get(f)}
                direction = 1 if directions == {1} else (-1 if directions == {-1} else 0)
                info = " | ".join(x for x in infos if x)
                price = fmt_price(closes[i])
                card = render_card(symbol, INTERVAL_META[interval]["label"], fired,
                                   direction, price, close_ts, info)
                caption = (
                    f"【回放】{'做多' if direction > 0 else '做空'} · "
                    f"{DISPLAY_NAMES.get(symbol.upper(), symbol)} · {INTERVAL_META[interval]['label']}\n"
                    + "\n".join(fired)
                    + f"\n收盘价 {price}\n{fmt_time(close_ts)} 收盘（北京时间）"
                    + f"｜K线 {fmt_time(rows[i][0], '%H:%M')}→{fmt_time(close_ts, '%H:%M')}"
                )
                if not (card and send_card(card, caption)):
                    send_telegram(caption)
                total += 1
                time.sleep(1)
    log(f"回放完成：{day_str} 共 {total} 条")


def main():
    if "--replay" in sys.argv:
        idx = sys.argv.index("--replay")
        day = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else fmt_time(time.time(), "%Y-%m-%d")
        replay(day)
        return

    if "--demo" in sys.argv:
        examples = [
            (1, "做多", ["趋势多头确认"]),
            (-1, "做空", ["趋势空头确认"]),
        ]
        now = int(time.time())
        for direction, label, fired in examples:
            card = render_card("BTCUSDT", "15分钟", fired, direction, "76,388.00",
                               now, "EMA12 76,388.00 / 通道 78,649 ~ 78,706")
            send_card(card, f"{label} · 示例卡片 · BTCUSDT · 5分钟\n价格 76,388.00")
        return

    if "--test" in sys.argv:
        log("发送测试消息…")
        send_telegram(f"✅ 维加斯提醒 · 测试\n盯：{','.join(SYMBOLS)}\n周期：{','.join(INTERVALS)}")
        return

    once = "--once" in sys.argv
    log(f"启动：{','.join(SYMBOLS)} × {','.join(INTERVALS)}")
    state = load_state()
    if once:
        sweep(state)
        return
    while True:
        try:
            sweep(state)
        except Exception as exc:  # noqa: BLE001
            log(f"轮询出错：{type(exc).__name__}: {exc}")
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
