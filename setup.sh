#!/data/data/com.termux/files/usr/bin/bash
# 一键安装：维加斯提醒（在 Termux 里运行）
# 用法： TG_TOKEN=xxx TG_CHAT=yyy bash setup.sh
set -u

: "${TG_TOKEN:?需要 TG_TOKEN 环境变量}"
: "${TG_CHAT:?需要 TG_CHAT 环境变量}"

echo "== 1/5 安装 Python =="
pkg update -y >/dev/null 2>&1 || true
pkg install -y python curl >/dev/null 2>&1 || true

cd "$HOME" || exit 1

echo "== 2/5 下载脚本 =="
curl -fsSL -o vegas_alert.py https://raw.githubusercontent.com/W572743149-cmyk/vegas-alert/main/vegas_alert.py || echo "!! 脚本下载失败"
curl -fsSL -o vegas_font.ttf https://raw.githubusercontent.com/W572743149-cmyk/vegas-alert/main/vegas_font.ttf || echo "!! 字体下载失败"
ls -l vegas_alert.py vegas_font.ttf 2>/dev/null

echo "== 3/5 安装画图库（可选）=="
pkg install -y python-pillow >/dev/null 2>&1 || pip install pillow >/dev/null 2>&1 || echo "!! Pillow 装不上，将用纯文字推送"

echo "== 4/5 写配置和守护脚本 =="
cat > "$HOME/env.sh" <<EOF
export TG_TOKEN="$TG_TOKEN"
export TG_CHAT="$TG_CHAT"
export SYMBOLS="BTCUSDT,SNDKUSDT,XAUUSD,SOXLBUSDT"
export INTERVALS="15m,1h,4h,1d,1w"
export CHECK_EVERY="30"
export MAX_DELAY_MIN="20"
EOF

cat > "$HOME/run.sh" <<'EOS'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
. "$HOME/env.sh"
cd "$HOME" || exit 1
while true; do
  python vegas_alert.py
  echo "[$(date '+%m-%d %H:%M:%S')] 进程退出，10 秒后自动重启"
  sleep 10
done
EOS
chmod +x "$HOME/run.sh"

echo "== 5/5 发一条测试消息 =="
. "$HOME/env.sh"
python vegas_alert.py --test

echo ""
echo "=========================================="
echo " 安装完成。启动命令： bash ~/run.sh"
echo " 日志查看： tail -f ~/alerts.log"
echo "=========================================="
