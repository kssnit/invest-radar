#!/data/data/com.termux/files/usr/bin/bash
# 투자 레이더 휴대폰(Termux) 설치 스크립트
# 사용: bash ~/invest-radar/termux/install.sh   (다시 실행하면 키를 바꿀 수 있어요)

DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
ENV_FILE="$DIR/.env"
cd "$DIR" || exit 1

line() { echo "------------------------------------------"; }
getenv() { [ -f "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
clean() { printf '%s' "$1" | tr -d '[:space:]'; }

# ask 변수 "질문" 필수여부(1/0)
ask() {
  local var="$1" q="$2" required="$3" cur ans
  cur="$(getenv "$var")"
  while true; do
    if [ -n "$cur" ]; then
      read -r -p "$q [엔터 = 기존 값 유지]: " ans
      ans="$(clean "$ans")"
      [ -z "$ans" ] && ans="$cur"
    else
      read -r -p "$q: " ans
      ans="$(clean "$ans")"
    fi
    if [ -n "$ans" ] || [ "$required" = "0" ]; then break; fi
    echo "  → 꼭 입력해야 해요. 길게 눌러 붙여넣기 하세요."
  done
  printf -v "$var" '%s' "$ans"
}

echo ""
line
echo "  투자 레이더 휴대폰 설치"
line

# 1) 필요한 프로그램
NEED=""
command -v python >/dev/null 2>&1 || NEED="$NEED python"
command -v pgrep  >/dev/null 2>&1 || NEED="$NEED procps"
command -v curl   >/dev/null 2>&1 || NEED="$NEED curl"
command -v unzip  >/dev/null 2>&1 || NEED="$NEED unzip"
if [ -n "$NEED" ]; then
  echo "프로그램 설치 중:$NEED"
  pkg install -y $NEED || { echo "❌ 설치 실패. 'pkg update' 를 먼저 하고 다시 실행하세요."; exit 1; }
fi

# 2) 키 입력
echo ""
echo "키를 하나씩 붙여넣고 엔터를 누르세요. (붙여넣기: 화면을 길게 누르고 Paste)"
echo ""
ask DART_API_KEY        "① OpenDART 인증키" 1
ask DATA_GO_KR_KEY      "② 공공데이터포털 인증키 (없으면 그냥 엔터 → 나라장터 알림 꺼짐)" 0
ask DISCORD_WEBHOOK_URL "③ 디스코드 웹후크 주소" 1
ask GITHUB_REPO         "④ GitHub 저장소 (예: myid/invest-radar, 대시보드를 안 쓰면 엔터)" 0
GITHUB_TOKEN=""
if [ -n "$GITHUB_REPO" ]; then
  GITHUB_REPO="${GITHUB_REPO#https://github.com/}"
  GITHUB_REPO="${GITHUB_REPO%/}"
  ask GITHUB_TOKEN      "⑤ GitHub 토큰 (github_pat_ 로 시작)" 1
fi

umask 077
cat > "$ENV_FILE" <<EOF
DART_API_KEY=$DART_API_KEY
DATA_GO_KR_KEY=$DATA_GO_KR_KEY
DISCORD_WEBHOOK_URL=$DISCORD_WEBHOOK_URL
GITHUB_REPO=$GITHUB_REPO
GITHUB_TOKEN=$GITHUB_TOKEN
EOF
chmod 600 "$ENV_FILE"
umask 022
echo "✅ 키 저장 완료 ($ENV_FILE, 이 휴대폰에만 저장돼요)"

# 3) 디스코드 확인
echo ""
echo "디스코드 연결 확인 중..."
python monitor.py --test-discord || { echo "❌ 웹후크 주소를 확인하고 다시 실행하세요: radar setup"; exit 1; }

# 4) GitHub 저장소에 파일 올리기
if [ -n "$GITHUB_REPO" ]; then
  echo ""
  echo "GitHub 저장소에 대시보드·설정 파일 올리는 중..."
  python monitor.py --setup-github
fi

# 5) radar 명령어 + 재부팅 시 자동 실행
chmod +x "$DIR/termux/radar.sh"
ln -sf "$DIR/termux/radar.sh" "$PREFIX/bin/radar"
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/invest-radar.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
# 휴대폰이 켜지면 Termux:Boot 가 이 파일을 실행해요.
termux-wake-lock
cd "$DIR" || exit 1
exec python monitor.py --loop >/dev/null 2>&1
EOF
chmod +x "$HOME/.termux/boot/invest-radar.sh"

# 6) 시작
echo ""
radar stop >/dev/null 2>&1
radar start

echo ""
line
echo "  설치 끝! 남은 일 (README 참고)"
line
echo "1. Termux:Boot 앱을 한 번 열었다 닫기 (재부팅 자동 실행용)"
echo "2. 갤럭시 배터리 설정 (README 6번 '갤럭시 S20 설정')"
if [ -n "$GITHUB_REPO" ]; then
  echo "3. GitHub 저장소 Settings → Pages 켜기 (main, /docs)"
  echo "4. GitHub 저장소 Settings → Secrets 에 DISCORD_WEBHOOK_URL 등록 (멈춤 감시용)"
  echo ""
  echo "대시보드 주소: https://${GITHUB_REPO%%/*}.github.io/${GITHUB_REPO#*/}/"
fi
echo ""
echo "자주 쓰는 명령: radar status / radar log / radar stop / radar start"
