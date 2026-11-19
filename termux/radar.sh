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
    REPO="$(grep -E '^GITHUB_REPO=' "$DIR/.env" 2>/dev/null | cut -d= -f2- | tr -d '\r" ')"
    if [ -z "$REPO" ]; then echo ".env 에 GITHUB_REPO 가 없어요. (radar setup)"; exit 1; fi
    echo "GitHub($REPO)에서 최신 코드 받는 중..."
    # curl 대신 파이썬으로 받아요 (Termux curl 라이브러리 문제를 피하려고)
    if ! python - "$REPO" "$DIR" <<'PY'
import io, os, sys, urllib.request, zipfile
repo, d = sys.argv[1], sys.argv[2]
try:
    req = urllib.request.Request(f"https://github.com/{repo}/archive/HEAD.zip", headers={"User-Agent": "invest-radar-update"})
    z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(req, timeout=60).read()))
except Exception as e:
    print(f"오류: {e}")
    sys.exit(1)
top = z.namelist()[0].split("/")[0] + "/"
names = set(z.namelist())
for f in ("monitor.py", "config.json", "termux/radar.sh", "termux/install.sh"):
    if top + f in names:
        dst = os.path.join(d, f)
        with open(dst + ".new", "wb") as out:
            out.write(z.read(top + f))
        os.replace(dst + ".new", dst)  # 실행 중인 radar.sh 를 덮어써도 안전하게
        print("  ✅", f)
PY
    then
      echo "❌ 다운로드 실패 (휴대폰 인터넷 연결을 확인하세요)"; exit 1
    fi
    chmod +x "$DIR/termux/"*.sh
    echo "✅ 최신 코드로 바꿨어요. 다시 시작할게요."
    "$0" restart
    ;;
  *)
    echo "사용법: radar [start|stop|restart|status|log|setup|update]"
    ;;
esac
