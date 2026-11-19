#!/data/data/com.termux/files/usr/bin/bash
# 투자 레이더 관리 명령어:  radar [start|stop|restart|status|log|setup|update]

DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
LOG="$DIR/logs/bot.log"

running() { pgrep -f "monitor.py --loop" >/dev/null 2>&1; }

case "${1:-status}" in
  start)
    if running; then echo "이미 실행 중이에요."; exit 0; fi
    termux-wake-lock 2>/dev/null
    cd "$DIR" || exit 1
    nohup python monitor.py --loop >/dev/null 2>&1 </dev/null &
    sleep 4
    if running; then
      echo "✅ 시작했어요. 디스코드에 '▶️ 투자 레이더 시작' 메시지가 올 거예요."
    else
      echo "❌ 시작하지 못했어요. 'radar log' 로 이유를 확인하세요."
    fi
    ;;
  stop)
    if running; then
      pkill -f "monitor.py --loop"
      echo "⏹️ 멈췄어요. (다시 켜기: radar start)"
    else
      echo "실행 중이 아니에요."
    fi
    termux-wake-unlock 2>/dev/null
    ;;
  restart)
    "$0" stop
    sleep 3
    "$0" start
    ;;
  status)
    if running; then echo "✅ 실행 중"; else echo "⛔ 멈춰 있어요 (켜기: radar start)"; fi
    if [ -f "$LOG" ]; then
      echo "--- 최근 기록 ---"
      tail -n 8 "$LOG"
    fi
    ;;
  log)
    if [ -f "$LOG" ]; then tail -n "${2:-50}" "$LOG"; else echo "아직 기록이 없어요."; fi
    ;;
  setup)
    bash "$DIR/termux/install.sh"
    ;;
  update)
    REPO="$(grep -E '^GITHUB_REPO=' "$DIR/.env" 2>/dev/null | cut -d= -f2-)"
    if [ -z "$REPO" ]; then echo ".env 에 GITHUB_REPO 가 없어요. (radar setup)"; exit 1; fi
    TMP="$(mktemp -d)"
    echo "GitHub($REPO)에서 최신 코드 받는 중..."
    if ! curl -fsSL "https://github.com/$REPO/archive/HEAD.zip" -o "$TMP/r.zip"; then
      echo "❌ 다운로드 실패"; rm -rf "$TMP"; exit 1
    fi
    unzip -q "$TMP/r.zip" -d "$TMP/src"
    SRC="$(find "$TMP/src" -mindepth 1 -maxdepth 1 -type d | head -1)"
    cp "$SRC/monitor.py" "$DIR/monitor.py"
    cp "$SRC/config.json" "$DIR/config.json"
    cp "$SRC/termux/"*.sh "$DIR/termux/" 2>/dev/null
    chmod +x "$DIR/termux/"*.sh
    rm -rf "$TMP"
    echo "✅ 최신 코드로 바꿨어요. 다시 시작할게요."
    "$0" restart
    ;;
  *)
    echo "사용법: radar [start|stop|restart|status|log|setup|update]"
    ;;
esac
