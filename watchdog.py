#!/usr/bin/env python3
"""
휴대폰 봇 감시 (GitHub Actions 에서 30분마다 실행)
휴대폰이 올리는 docs/alive.json 이 오래되면 디스코드로 "휴대폰 봇 응답 없음" 을 알립니다.
같은 상황에서는 6시간마다 한 번만 다시 알리고, 살아나면 복구 알림을 보냅니다.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.abspath(__file__))
ALIVE_PATH = os.path.join(ROOT, "docs", "alive.json")
STATE_PATH = os.path.join(ROOT, "state", "watchdog.json")
LIMIT_MIN = int(os.environ.get("WATCHDOG_MINUTES", "60"))
REPEAT_HOURS = 6


def now():
    return datetime.now(KST)


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def post(title, desc, color):
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    payload = {"username": "투자 레이더 감시",
               "embeds": [{"title": title, "description": desc, "color": color,
                           "footer": {"text": "GitHub 감시"}}]}
    if not webhook:
        print("DISCORD_WEBHOOK_URL 없음 → 화면에만 출력\n" + json.dumps(payload, ensure_ascii=False))
        return
    req = urllib.request.Request(webhook, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "User-Agent": "invest-radar-watchdog"})
    urllib.request.urlopen(req, timeout=20).read()


def main():
    alive = load(ALIVE_PATH)
    st = load(STATE_PATH)
    at = alive.get("alive_at")
    if not at:
        print("아직 휴대폰 봇의 생존 신호가 없어요 (설치 전이거나 첫 실행 전).")
        return 0
    last = datetime.fromisoformat(at)
    age = now() - last
    mins = int(age.total_seconds() // 60)
    device = alive.get("device") or "휴대폰"
    print(f"마지막 생존 신호: {at} ({mins}분 전)")

    if age > timedelta(minutes=LIMIT_MIN):
        alerted_at = st.get("alerted_at")
        repeat_due = (alerted_at and now() - datetime.fromisoformat(alerted_at) >= timedelta(hours=REPEAT_HOURS))
        if st.get("alerted_for") != at or repeat_due:
            h, m = divmod(mins, 60)
            ago = f"{h}시간 {m}분" if h else f"{m}분"
            post(f"📵 {device} 봇이 {ago}째 응답이 없어요",
                 f"마지막 확인: {last:%m월 %d일 %H:%M}\n\n"
                 "확인해 보세요:\n"
                 "1. 휴대폰이 켜져 있고 충전기·와이파이에 연결돼 있는지\n"
                 "2. 알림창에 Termux 알림이 떠 있는지\n"
                 "3. Termux를 열고 `radar status` → 멈춰 있으면 `radar start`\n"
                 "자주 멈추면 README의 '자꾸 꺼질 때'를 확인하세요.",
                 0xE5484D)
            save(STATE_PATH, {"alerted_for": at, "alerted_at": now().isoformat(timespec="minutes")})
            print("알림 보냄")
    elif st.get("alerted_for"):
        post(f"✅ {device} 봇이 다시 응답해요", f"마지막 확인: {last:%m월 %d일 %H:%M}", 0x30A46C)
        save(STATE_PATH, {})
        print("복구 알림 보냄")
    return 0


if __name__ == "__main__":
    sys.exit(main())
