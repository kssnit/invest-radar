#!/usr/bin/env python3
"""
투자 레이더 봇 (휴대폰 상시 실행 버전)

- DART 공시 (OpenDART API)           : 평일 낮 1분마다, 그 외 10분마다
- 나라장터 개찰결과 (공공데이터포털)   : 평일 낮 10분마다, 그 외 30분마다
- 전시회/결정 시한 같은 일정 알림      : config.json 의 events
새 소식은 디스코드로 알리고, 대시보드 데이터(docs/data.json)와 생존 신호(docs/alive.json)를
GitHub 저장소에 올려 GitHub Pages 대시보드가 갱신되게 합니다.

파이썬 표준 라이브러리만 사용합니다 (설치할 패키지 없음).

  python monitor.py --loop           # 계속 실행 (휴대폰에서 쓰는 방식)
  python monitor.py --once           # 한 번만 확인
  python monitor.py --once --dry-run # 디스코드·GitHub 로 보내지 않고 화면에만 출력
  python monitor.py --test-discord   # 디스코드 연결 테스트
  python monitor.py --setup-github   # 저장소에 대시보드·설정·감시 파일 올리기
"""
import argparse
import base64
import io
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta, timezone

try:
    import fcntl  # 안드로이드(Termux)·리눅스·맥
except ImportError:  # 윈도우
    fcntl = None

VERSION = "2.0-phone"
KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state", "state.json")
LOCK_PATH = os.path.join(ROOT, "state", "bot.lock")
DATA_PATH = os.path.join(ROOT, "docs", "data.json")
ALIVE_PATH = os.path.join(ROOT, "docs", "alive.json")
LOG_PATH = os.path.join(ROOT, "logs", "bot.log")
ENV_PATH = os.path.join(ROOT, ".env")

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
DART_CORP_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
DART_VIEW_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="
G2B_BASE = "https://apis.data.go.kr/1230000/as/ScsbidInfoService"
GITHUB_API = "https://api.github.com"
USER_AGENT = "invest-radar-bot/" + VERSION

COLOR = {"danger": 0xE5484D, "good": 0x30A46C, "info": 0x8B8D98,
         "g2b": 0x3E63DD, "event": 0xF5A524, "system": 0x6E56CF}
EMOJI = {"danger": "🚨", "good": "📈", "info": "📄", "g2b": "🏛️", "event": "📅"}
G2B_KIND = {"Servc": "용역", "Thng": "물품", "Cnstwk": "공사", "Frgcpt": "외자"}
LABEL = {"dart": "DART 공시", "g2b": "나라장터", "github": "대시보드 업로드"}

FAIL_WARN_MINUTES = 30      # 이 시간 넘게 계속 실패하면 디스코드로 경고
PENDING_MAX = 60            # 전송 못 한 알림을 최대 몇 개까지 쌓아 둘지
LOG_MAX_BYTES = 1_000_000   # 로그 파일이 이 크기를 넘으면 새 파일로

# 저장소에 올리는 파일 (--setup-github)
SETUP_FILES = ["README.md", "config.json", "monitor.py", "watchdog.py", ".gitignore",
               "docs/index.html", "docs/data.json",
               "termux/install.sh", "termux/radar.sh",
               ".github/workflows/watchdog.yml"]

DEFAULT_SCHEDULE = {
    "fast_hours": [7, 22], "fast_weekdays_only": True,
    "dart_fast_seconds": 60, "dart_slow_seconds": 600,
    "g2b_fast_seconds": 600, "g2b_slow_seconds": 1800,
    "upload_min_seconds": 60, "alive_minutes": 20, "config_sync_minutes": 10,
}

_log_to_file = False


def now():
    return datetime.now(KST)


def log(msg):
    line = f"[{now():%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    if not _log_to_file:
        return
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 파일 입출력
def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        if path == CONFIG_PATH:
            sys.exit(f"config.json 형식 오류: {e} (쉼표나 따옴표를 확인하세요)")
        log(f"{os.path.basename(path)} 를 읽지 못해 새로 시작합니다: {e}")
        return default


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def save_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_dotenv():
    """.env 파일(KEY=VALUE)을 읽어 환경변수로 씁니다."""
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def validate_config(cfg):
    if not isinstance(cfg, dict):
        return "최상위가 { } 가 아님"
    comps = cfg.get("dart", {}).get("companies", [])
    if not isinstance(comps, list):
        return "dart.companies 가 목록이 아님"
    for c in comps:
        if not isinstance(c, dict) or not str(c.get("stock_code", "")).isdigit():
            return f"종목 항목 오류: {c}"
    for ev in cfg.get("events", []):
        try:
            date.fromisoformat(ev["start"])
        except (KeyError, ValueError, TypeError):
            return f"일정 날짜 오류: {ev}"
    return ""


# ---------------------------------------------------------------- 네트워크
def http_get(url, params=None, timeout=25, raw=False):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return body if raw else body.decode("utf-8", errors="replace")


def discord_post(webhook, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(4):
        req = urllib.request.Request(
            webhook, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                try:
                    wait = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                except ValueError:
                    wait = 2
                time.sleep(min(wait, 15) + 0.5)
                continue
            detail = e.read()[:300].decode("utf-8", "replace")
            raise RuntimeError(f"디스코드 전송 실패 HTTP {e.code}: {detail}")


def github_request(method, url, token, body=None, timeout=25):
    """(상태코드, JSON) 을 돌려줍니다. 네트워크 오류는 예외로 올립니다."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT,
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"message": raw[:200].decode("utf-8", "replace")}


class GitHub:
    """저장소 파일 읽기/쓰기 (Contents API). git 설치가 필요 없습니다."""

    def __init__(self, token, repo):
        self.token = token
        self.repo = repo.strip().strip("/")
        self.sha = {}

    def _url(self, path):
        return f"{GITHUB_API}/repos/{self.repo}/contents/{urllib.parse.quote(path)}"

    def check(self):
        code, obj = github_request("GET", f"{GITHUB_API}/repos/{self.repo}", self.token)
        if code == 200:
            return ""
        if code in (401, 403):
            return f"토큰이 거부됐어요 (HTTP {code}). 토큰 값과 권한을 확인하세요."
        if code == 404:
            return "저장소를 찾지 못했어요. '아이디/저장소이름' 이 맞는지, 토큰에 이 저장소 권한을 줬는지 확인하세요."
        return f"GitHub 오류 HTTP {code}: {obj.get('message')}"

    def get(self, path):
        code, obj = github_request("GET", self._url(path), self.token)
        if code == 404:
            self.sha.pop(path, None)
            return None
        if code != 200:
            raise RuntimeError(f"GitHub 읽기 실패 {path} HTTP {code}: {obj.get('message')}")
        self.sha[path] = obj.get("sha")
        return base64.b64decode(obj.get("content", "")).decode("utf-8")

    def put(self, path, text, message):
        body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        for attempt in range(2):
            if path not in self.sha:
                try:
                    self.get(path)
                except RuntimeError:
                    pass
            if self.sha.get(path):
                body["sha"] = self.sha[path]
            else:
                body.pop("sha", None)
            code, obj = github_request("PUT", self._url(path), self.token, body)
            if code in (200, 201):
                self.sha[path] = obj.get("content", {}).get("sha")
                return
            if code in (409, 422) and attempt == 0:  # 파일이 그사이 바뀜 → sha 새로 받아 재시도
                self.sha.pop(path, None)
                continue
            raise RuntimeError(f"GitHub 업로드 실패 {path} HTTP {code}: {obj.get('message')}")

    def delete(self, path, message):
        """파일이 있으면 지웁니다. 지웠으면 True, 원래 없으면 False."""
        if self.get(path) is None:
            return False
        code, obj = github_request("DELETE", self._url(path), self.token,
                                   {"message": message, "sha": self.sha[path]})
        if code != 200:
            raise RuntimeError(f"GitHub 삭제 실패 {path} HTTP {code}: {obj.get('message')}")
        self.sha.pop(path, None)
        return True


# ---------------------------------------------------------------- DART
def classify_report(name, dcfg):
    for kw in dcfg.get("danger_keywords", []):
        if kw in name:
            return "danger"
    for kw in dcfg.get("good_keywords", []):
        if kw in name:
            return "good"
    return "info"


def resolve_corp_codes(dcfg, state, key):
    """종목코드(6자리) → DART 고유번호(8자리). 한 번 찾으면 기억해 둡니다."""
    companies = dcfg.get("companies", [])
    cache = state.setdefault("corp_codes", {})
    missing = {c["stock_code"] for c in companies
               if not c.get("corp_code") and c.get("stock_code") not in cache}
    if missing:
        log(f"DART 고유번호 목록 내려받는 중 (처음 1회 또는 종목 추가 시): {sorted(missing)}")
        raw = http_get(DART_CORP_URL, {"crtfc_key": key}, timeout=120, raw=True)
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise RuntimeError("DART 고유번호 다운로드 실패: " + raw[:300].decode("utf-8", "replace"))
        with zf.open(zf.namelist()[0]) as fp:
            for _, el in ET.iterparse(fp):
                if el.tag != "list":
                    continue
                sc = (el.findtext("stock_code") or "").strip()
                if sc in missing:
                    cache[sc] = (el.findtext("corp_code") or "").strip()
                el.clear()
        for sc in missing:
            cache.setdefault(sc, "")  # 못 찾은 코드는 빈 값으로 기억 (config 를 고치면 다시 찾음)
    out, problems = [], []
    for c in companies:
        code = c.get("corp_code") or cache.get(c.get("stock_code"))
        if code:
            out.append({**c, "corp_code": code})
        else:
            problems.append(f"{c.get('name')}({c.get('stock_code')})")
    return out, problems, bool(missing)


class RateLimited(RuntimeError):
    pass


def fetch_dart(cfg, state, key):
    dcfg = cfg.get("dart", {})
    companies, problems, cache_changed = resolve_corp_codes(dcfg, state, key)
    end = now().date()
    bgn = end - timedelta(days=int(dcfg.get("lookback_days", 3)))
    items = []
    for c in companies:
        params = {"crtfc_key": key, "corp_code": c["corp_code"],
                  "bgn_de": bgn.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
                  "sort": "date", "sort_mth": "desc", "page_count": 100}
        data = json.loads(http_get(DART_LIST_URL, params))
        status = data.get("status")
        if status == "013":  # 조회된 데이터 없음
            continue
        if status == "020":
            raise RateLimited(f"DART 하루 호출 한도 초과 (020): {data.get('message')}")
        if status != "000":
            raise RuntimeError(f"DART 오류 {status}: {data.get('message')}")
        for it in data.get("list", []):
            name = " ".join((it.get("report_nm") or "").split())
            rd = it.get("rcept_dt", "")
            items.append({
                "id": it["rcept_no"],
                "corp": c.get("name") or it.get("corp_name"),
                "stock_code": c.get("stock_code"),
                "title": name,
                "date": f"{rd[:4]}-{rd[4:6]}-{rd[6:8]}" if len(rd) == 8 else rd,
                "filer": it.get("flr_nm", ""),
                "url": DART_VIEW_URL + it["rcept_no"],
                "level": classify_report(name, dcfg),
            })
        time.sleep(0.12)
    return items, problems, cache_changed


# ---------------------------------------------------------------- 나라장터
def _g2b_items(text):
    t = text.strip()
    if t.startswith("<"):
        try:
            root = ET.fromstring(t)
        except ET.ParseError:
            raise RuntimeError("나라장터 응답 해석 실패: " + t[:200])
        msg = (root.findtext(".//returnAuthMsg") or root.findtext(".//resultMsg")
               or root.findtext(".//errMsg") or t[:200])
        code = root.findtext(".//returnReasonCode") or root.findtext(".//resultCode") or ""
        raise RuntimeError(f"나라장터 오류 {code}: {msg}")
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        raise RuntimeError("나라장터 응답 해석 실패: " + t[:200])
    resp = data.get("response", data)
    header = resp.get("header", {})
    code = str(header.get("resultCode", "00"))
    if code not in ("00", "0", "000"):
        raise RuntimeError(f"나라장터 오류 {code}: {header.get('resultMsg')}")
    body = resp.get("body", {}) or {}
    items = body.get("items") or []
    if isinstance(items, dict):
        items = items.get("item") or []
    if isinstance(items, dict):
        items = [items]
    try:
        total = int(body.get("totalCount") or 0)
    except (TypeError, ValueError):
        total = len(items)
    return items, total


def _won(v):
    try:
        n = float(str(v).replace(",", ""))
    except ValueError:
        return ""
    if n >= 1e8:
        return f"{n / 1e8:,.1f}억원"
    if n >= 1e4:
        return f"{n / 1e4:,.0f}만원"
    return f"{n:,.0f}원"


def _parse_corp(info):
    """opengCorpInfo 예: '업체명^사업자번호^대표자^투찰금액^투찰률'"""
    if not info:
        return ""
    parts = str(info).split("|")[0].split("^")
    name = parts[0].strip()
    extra = []
    if len(parts) > 3 and _won(parts[3]):
        extra.append(_won(parts[3]))
    if len(parts) > 4 and parts[4].strip():
        extra.append(f"{parts[4].strip()}%")
    return name + (f" ({', '.join(extra)})" if extra else "")


def fetch_g2b(cfg, key):
    gcfg = cfg.get("g2b", {})
    key = urllib.parse.unquote(key)  # Encoding/Decoding 키 둘 다 동작
    end = now()
    bgn = end - timedelta(hours=float(gcfg.get("lookback_hours", 3)))
    company_kw = gcfg.get("company_keywords", [])
    title_kw = gcfg.get("title_keywords", [])
    found = []
    for op in gcfg.get("operations", ["getOpengResultListInfoServc", "getOpengResultListInfoThng"]):
        kind = next((v for k, v in G2B_KIND.items() if op.endswith(k)), "")
        page = 1
        while True:
            params = {"serviceKey": key, "pageNo": page, "numOfRows": 999, "type": "json",
                      "inqryDiv": str(gcfg.get("inqry_div", "2")),
                      "inqryBgnDt": bgn.strftime("%Y%m%d%H%M"),
                      "inqryEndDt": end.strftime("%Y%m%d%H%M")}
            items, total = _g2b_items(http_get(f"{G2B_BASE}/{op}", params, timeout=60))
            for it in items:
                title = str(it.get("bidNtceNm") or "")
                blob = " ".join(str(v) for v in it.values() if isinstance(v, (str, int, float)))
                hits = [k for k in company_kw if k and k in blob]
                hits += [k for k in title_kw if k and k.lower() in title.lower()]
                if not hits:
                    continue
                uid = "-".join(str(it.get(k) or "") for k in ("bidNtceNo", "bidNtceOrd", "bidClsfcNo", "rbidNo"))
                found.append({
                    "id": uid, "kind": kind, "title": title,
                    "notice_no": f"{it.get('bidNtceNo', '')}-{it.get('bidNtceOrd', '')}".strip("-"),
                    "agency": it.get("dminsttNm") or it.get("ntceInsttNm") or "",
                    "opened_at": str(it.get("opengDt") or it.get("rlOpengDt") or ""),
                    "status": it.get("progrsDivCdNm") or "",
                    "participants": str(it.get("prtcptCnum") or ""),
                    "top": _parse_corp(it.get("opengCorpInfo")),
                    "matched": sorted(set(hits)),
                    "url": "https://www.g2b.go.kr",
                })
            if page * 999 >= total or not items:
                break
            page += 1
            time.sleep(0.3)
    return found


# ---------------------------------------------------------------- 일정
def due_reminders(cfg, state):
    t = now()
    if t.hour < int(cfg.get("event_remind_hour", 8)):
        return []
    today = t.date()
    sent = state.setdefault("event_sent", [])
    out = []
    for ev in cfg.get("events", []):
        try:
            start = date.fromisoformat(ev["start"])
        except (KeyError, ValueError, TypeError):
            continue
        for d in cfg.get("event_remind_days", [7, 1, 0]):
            if today == start - timedelta(days=int(d)):
                k = f"{ev.get('name')}|{ev['start']}|{d}"
                if k not in sent:
                    sent.append(k)
                    out.append((ev, int(d)))
    state["event_sent"] = sent[-500:]
    return out


# ---------------------------------------------------------------- 디스코드 메시지
def _trim(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def embed_dart(it):
    return {
        "title": _trim(f"{EMOJI[it['level']]} [{it['corp']}] {it['title']}", 256),
        "url": it["url"], "color": COLOR[it["level"]],
        "fields": [{"name": "제출인", "value": _trim(it.get("filer") or "-", 100), "inline": True},
                   {"name": "접수일", "value": it.get("date") or "-", "inline": True}],
        "footer": {"text": "DART 전자공시"},
    }


def embed_g2b(it):
    fields = [{"name": "수요기관", "value": _trim(it.get("agency") or "-", 100), "inline": True},
              {"name": "개찰일시", "value": it.get("opened_at") or "-", "inline": True}]
    if it.get("top"):
        fields.append({"name": "1순위", "value": _trim(it["top"], 200), "inline": False})
    if it.get("status"):
        fields.append({"name": "진행", "value": it["status"], "inline": True})
    fields.append({"name": "공고번호", "value": it.get("notice_no") or "-", "inline": True})
    return {
        "title": _trim(f"{EMOJI['g2b']} [개찰 {it.get('kind', '')}] {it['title']}", 256),
        "url": it["url"], "color": COLOR["g2b"],
        "description": "키워드: " + ", ".join(it.get("matched", [])),
        "fields": fields, "footer": {"text": "나라장터 개찰결과"},
    }


def embed_event(ev, d):
    when = "오늘 시작" if d == 0 else f"D-{d}"
    end = ev.get("end") or ev["start"]
    period = ev["start"] if end == ev["start"] else f"{ev['start']} ~ {end}"
    desc = period + (f" · {ev['place']}" if ev.get("place") else "")
    if ev.get("note"):
        desc += f"\n{ev['note']}"
    e = {"title": _trim(f"{EMOJI['event']} {when} · {ev.get('name')}", 256),
         "color": COLOR["event"], "description": _trim(desc, 2000), "footer": {"text": "일정 알림"}}
    if str(ev.get("url", "")).startswith("http"):
        e["url"] = ev["url"]
    return e


def system_embed(title, desc):
    return {"title": _trim(title, 256), "description": _trim(desc, 3500),
            "color": COLOR["system"], "footer": {"text": "투자 레이더 봇"}}


def send_embeds(webhook, cfg, embeds, content="", dry_run=False):
    """10개씩 나눠 보냅니다. 보낸 개수를 돌려주고, 실패하면 예외(보낸 개수 포함)."""
    dc = cfg.get("discord", {})
    sent = 0
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        payload = {"username": dc.get("username", "투자 레이더"), "embeds": chunk}
        if content and i == 0:
            payload["content"] = content
            payload["allowed_mentions"] = {"parse": ["everyone", "roles", "users"]}
        if dry_run or not webhook:
            print(json.dumps(payload, ensure_ascii=False, indent=1))
        else:
            try:
                discord_post(webhook, payload)
            except Exception as e:  # noqa: BLE001
                e.sent = sent
                raise
            time.sleep(1.1)
        sent += len(chunk)
    return sent


# ---------------------------------------------------------------- 상태 관리
def update_status(state, source, ok, message, alerts):
    """소스별 정상/오류 상태 기록. 오래 실패하면 경고, 회복되면 복구 알림. 바뀌었으면 True."""
    st = state.setdefault("status", {}).setdefault(source, {})
    t = now()
    label = LABEL.get(source, source)
    changed = False
    if ok is None:
        if st.get("state") != "off":
            st.clear()
            st["state"] = "off"
            changed = True
        st["message"] = message
    elif ok:
        if st.get("state") != "ok":
            if st.get("warned"):
                alerts.append(system_embed(f"✅ {label} 다시 정상", "확인이 다시 잘 되고 있어요."))
            st.clear()
            st["state"] = "ok"
            changed = True
        if st.get("message") != message:
            changed = True
        st["message"] = message
    else:
        if st.get("state") != "fail":
            st.clear()
            st["state"] = "fail"
            st["since"] = t.isoformat(timespec="minutes")
            changed = True
        st["message"] = message
        since = datetime.fromisoformat(st["since"])
        if not st.get("warned") and t - since >= timedelta(minutes=FAIL_WARN_MINUTES):
            st["warned"] = True
            changed = True
            hint = {
                "g2b": "\n\n키를 막 발급받았다면 몇 시간 기다려 보세요. 계속되면 README의 '문제 해결'을 확인하세요.",
                "github": "\n\nGitHub 토큰이 만료됐거나 권한이 바뀌었을 수 있어요. `radar setup` 으로 토큰을 다시 넣으세요.",
                "dart": "\n\n휴대폰의 인터넷 연결이나 OpenDART 인증키를 확인하세요.",
            }.get(source, "")
            alerts.append(system_embed(f"⚠️ {label} 실패가 계속되고 있어요", f"{message}{hint}"))
    return changed


def merge_items(old, new):
    ids = {x["id"] for x in new}
    return new + [x for x in old if x["id"] not in ids]


def schedule_text(s):
    a, b = s["fast_hours"]
    days = "평일 " if s.get("fast_weekdays_only", True) else ""
    def m(sec):
        return f"{sec // 60}분" if sec >= 60 else f"{sec}초"
    return (f"{days}{a:02d}~{b:02d}시: 공시 {m(s['dart_fast_seconds'])}·입찰 {m(s['g2b_fast_seconds'])}마다 / "
            f"그 외: 공시 {m(s['dart_slow_seconds'])}·입찰 {m(s['g2b_slow_seconds'])}마다")


# ---------------------------------------------------------------- 본체
class Radar:
    def __init__(self, args):
        self.args = args
        self.cfg = load_json(CONFIG_PATH)
        if self.cfg is None:
            sys.exit("config.json 이 없습니다.")
        err = validate_config(self.cfg)
        if err:
            sys.exit(f"config.json 오류: {err}")
        self.state = load_json(STATE_PATH, {}) or {}
        self.data = load_json(DATA_PATH, {}) or {}
        self.dart_key = os.environ.get("DART_API_KEY", "").strip()
        self.g2b_key = os.environ.get("DATA_GO_KR_KEY", "").strip()
        self.webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        repo = os.environ.get("GITHUB_REPO", "").strip()
        self.gh = GitHub(token, repo) if (token and repo and not args.dry_run) else None
        self.next = {"dart": 0.0, "g2b": 0.0, "cfg": 0.0}
        self.last_data_upload = 0.0
        self.last_alive_upload = 0.0
        self.upload_backoff_until = 0.0
        self.uploaded_data_text = None
        self.saved_state_text = None
        self.saved_data_text = None

    # --- 설정
    @property
    def sched(self):
        s = dict(DEFAULT_SCHEDULE)
        s.update(self.cfg.get("schedule", {}))
        return s

    def interval(self, src, t):
        s = self.sched
        a, b = s["fast_hours"]
        weekday_ok = (t.weekday() < 5) or not s.get("fast_weekdays_only", True)
        fast = weekday_ok and a <= t.hour < b
        return float(s[f"{src}_{'fast' if fast else 'slow'}_seconds"])

    def dart_daily_estimate(self):
        s = self.sched
        a, b = s["fast_hours"]
        fast_h = max(0, b - a)
        runs = fast_h * 3600 / s["dart_fast_seconds"] + (24 - fast_h) * 3600 / s["dart_slow_seconds"]
        return int(runs * len(self.cfg.get("dart", {}).get("companies", [])))

    def sync_config(self, alerts):
        try:
            text = self.gh.get("config.json")
        except Exception as e:  # noqa: BLE001
            log(f"설정 동기화 실패 (무시하고 계속): {e}")
            return
        if text is None:
            return
        try:
            remote = json.loads(text)
        except json.JSONDecodeError as e:
            alerts.append(system_embed("⚠️ GitHub의 config.json 형식 오류",
                                       f"고친 내용에 오류가 있어 이전 설정을 계속 써요.\n{e}"))
            return
        if remote == self.cfg:
            return
        err = validate_config(remote)
        if err:
            alerts.append(system_embed("⚠️ GitHub의 config.json 내용 오류", f"이전 설정을 계속 써요.\n{err}"))
            return
        old_n = len(self.cfg.get("dart", {}).get("companies", []))
        self.cfg = remote
        save_text(CONFIG_PATH, text if text.endswith("\n") else text + "\n")
        n = len(remote.get("dart", {}).get("companies", []))
        log(f"설정 변경 반영 (종목 {old_n} → {n}개)")
        alerts.append(system_embed("⚙️ 설정 변경을 반영했어요",
                                   f"관심 종목 {n}개 · 일정 {len(remote.get('events', []))}개"))
        self.next["dart"] = 0.0  # 바로 다시 확인

    # --- 한 번 확인
    def tick(self):
        ts = time.time()
        t = now()
        cfg, state, data = self.cfg, self.state, self.data
        alerts = []
        status_changed = False
        new_dart, new_g2b = [], []
        first_run = not state.get("initialized")

        if self.gh and ts >= self.next["cfg"]:
            self.sync_config(alerts)
            self.next["cfg"] = ts + self.sched["config_sync_minutes"] * 60
            cfg = self.cfg

        # 1) DART
        if ts >= self.next["dart"]:
            wait = self.interval("dart", t)
            if not cfg.get("dart", {}).get("enabled", True):
                status_changed |= update_status(state, "dart", None, "꺼짐 (config.json)", alerts)
            elif not self.dart_key:
                status_changed |= update_status(state, "dart", None, "DART_API_KEY 미설정", alerts)
            else:
                try:
                    items, problems, cache_changed = fetch_dart(cfg, state, self.dart_key)
                    status_changed |= cache_changed
                    seen = set(state.get("seen_dart", []))
                    for it in sorted(items, key=lambda x: x["id"]):
                        if it["id"] not in seen:
                            it["first_seen"] = t.isoformat(timespec="seconds")
                            new_dart.append(it)
                            seen.add(it["id"])
                    state["seen_dart"] = sorted(seen)[-4000:]
                    n_ok = len(cfg.get("dart", {}).get("companies", [])) - len(problems)
                    msg = f"정상 · 종목 {n_ok}개"
                    if problems:
                        msg += " · 못 찾은 종목코드: " + ", ".join(problems)
                    status_changed |= update_status(state, "dart", True, msg, alerts)
                except RateLimited as e:
                    log(str(e))
                    wait = 1800
                    status_changed |= update_status(state, "dart", False, str(e), alerts)
                except Exception as e:  # noqa: BLE001
                    log(f"DART 오류: {e}")
                    wait = max(wait, 120)
                    status_changed |= update_status(state, "dart", False, _trim(str(e), 300), alerts)
            self.next["dart"] = ts + wait

        # 2) 나라장터
        if ts >= self.next["g2b"]:
            wait = self.interval("g2b", t)
            if not cfg.get("g2b", {}).get("enabled", True):
                status_changed |= update_status(state, "g2b", None, "꺼짐 (config.json)", alerts)
            elif not self.g2b_key:
                status_changed |= update_status(state, "g2b", None, "DATA_GO_KR_KEY 미설정", alerts)
            else:
                try:
                    items = fetch_g2b(cfg, self.g2b_key)
                    seen = set(state.get("seen_g2b", []))
                    for it in items:
                        if it["id"] not in seen:
                            it["first_seen"] = t.isoformat(timespec="seconds")
                            new_g2b.append(it)
                            seen.add(it["id"])
                    state["seen_g2b"] = list(seen)[-4000:]
                    status_changed |= update_status(state, "g2b", True, "정상", alerts)
                except Exception as e:  # noqa: BLE001
                    log(f"나라장터 오류: {e}")
                    status_changed |= update_status(state, "g2b", False, _trim(str(e), 300), alerts)
            self.next["g2b"] = ts + wait

        # 3) 일정
        reminders = due_reminders(cfg, state)

        # 4) 알림 만들기 (소스가 처음 성공한 회차의 과거 항목은 대시보드에만)
        primed = state.setdefault("primed", {})
        newly = [s for s in ("dart", "g2b")
                 if state.get("status", {}).get(s, {}).get("state") == "ok" and not primed.get(s)]
        for s in newly:
            primed[s] = True
        alerts = [a for a in alerts if not any(a["title"] == f"✅ {LABEL[s]} 다시 정상" for s in newly)]
        alert_dart = [] if "dart" in newly else new_dart
        alert_g2b = [] if "g2b" in newly else new_g2b

        embeds = []
        if first_run:
            words = {"ok": "정상 ✅", "fail": "오류 ⚠️", "off": "미설정"}
            lines = [f"• {LABEL[s]}: {words.get(state.get('status', {}).get(s, {}).get('state'), '-')}"
                     for s in ("dart", "g2b")]
            embeds.append(system_embed(
                "✅ 투자 레이더 연결 완료",
                f"관심 종목 {len(cfg.get('dart', {}).get('companies', []))}개를 지켜보기 시작했어요.\n"
                + "\n".join(lines) +
                f"\n\n최근 공시 {len(new_dart)}건, 관련 개찰 {len(new_g2b)}건은 대시보드에만 담았어요. "
                "알림은 지금부터 새로 뜨는 것만 보낼게요."))
        else:
            for s in newly:
                n = len(new_dart) if s == "dart" else len(new_g2b)
                embeds.append(system_embed(f"✅ {LABEL[s]} 확인 시작",
                                           f"최근 {n}건은 대시보드에만 담았어요. 지금부터 새로 뜨는 것만 알려드릴게요."))
        embeds += [embed_dart(x) for x in alert_dart]
        embeds += [embed_g2b(x) for x in alert_g2b]
        embeds += [embed_event(ev, d) for ev, d in reminders]
        embeds += alerts
        if any(x["level"] == "danger" for x in alert_dart):
            state["pending_mention"] = cfg.get("discord", {}).get("mention_on_danger", "") or ""
        if embeds:
            state["pending"] = (state.get("pending", []) + embeds)[-PENDING_MAX:]
        state["initialized"] = True

        # 5) 대시보드 데이터
        dash = cfg.get("dashboard", {})
        if new_dart or new_g2b or status_changed or first_run or not data.get("title"):
            data["dart"] = sorted(merge_items(data.get("dart", []), new_dart),
                                  key=lambda x: x["id"], reverse=True)[:int(dash.get("max_dart", 200))]
            data["g2b"] = sorted(merge_items(data.get("g2b", []), new_g2b),
                                 key=lambda x: (x.get("opened_at", ""), x["id"]), reverse=True)[:int(dash.get("max_g2b", 100))]
            data["saved_at"] = t.isoformat(timespec="seconds")
        data["title"] = dash.get("title", "투자 레이더")
        data["events"] = cfg.get("events", [])
        data["watchlist"] = [{"name": c.get("name"), "stock_code": c.get("stock_code")}
                             for c in cfg.get("dart", {}).get("companies", [])]
        data["schedule_text"] = schedule_text(self.sched)
        data["device"] = cfg.get("device_name", "")
        data["status"] = {k: {"state": v.get("state"), "message": v.get("message", ""),
                              "since": v.get("since", "")}
                          for k, v in state.get("status", {}).items()}
        data_text = dumps(data)
        if data_text != self.saved_data_text:
            if not self.args.dry_run:
                save_text(DATA_PATH, data_text)
            self.saved_data_text = data_text

        # 6) GitHub 업로드
        self.upload(ts, t, data_text)

        # 7) 디스코드 전송 (실패하면 다음 회차에 다시)
        self.flush()

        # 8) 상태 저장
        state_text = dumps(state)
        if state_text != self.saved_state_text and not self.args.dry_run:
            save_text(STATE_PATH, state_text)
            self.saved_state_text = state_text

        if new_dart or new_g2b or reminders:
            log(f"새 공시 {len(new_dart)} · 새 개찰 {len(new_g2b)} · 일정 {len(reminders)}")

    def upload(self, ts, t, data_text):
        if not self.gh or ts < self.upload_backoff_until:
            return
        s = self.sched
        alerts = []
        try:
            if data_text != self.uploaded_data_text and ts - self.last_data_upload >= s["upload_min_seconds"]:
                self.gh.put("docs/data.json", data_text, f"데이터 갱신 {t:%m-%d %H:%M}")
                self.uploaded_data_text = data_text
                self.last_data_upload = ts
            if ts - self.last_alive_upload >= s["alive_minutes"] * 60:
                alive = {"alive_at": t.isoformat(timespec="seconds"), "device": self.cfg.get("device_name", ""),
                         "version": VERSION, "schedule_text": schedule_text(s)}
                self.gh.put("docs/alive.json", dumps(alive), f"생존 신호 {t:%m-%d %H:%M}")
                self.last_alive_upload = ts
            update_status(self.state, "github", True, "정상", alerts)
        except Exception as e:  # noqa: BLE001
            log(f"GitHub 업로드 오류 (2분 뒤 다시 시도): {e}")
            self.upload_backoff_until = ts + 120
            update_status(self.state, "github", False, _trim(str(e), 300), alerts)
        if alerts:
            self.state["pending"] = (self.state.get("pending", []) + alerts)[-PENDING_MAX:]

    def flush(self):
        pending = self.state.get("pending", [])
        if not pending:
            return
        content = self.state.get("pending_mention", "")
        try:
            send_embeds(self.webhook, self.cfg, pending, content, dry_run=self.args.dry_run)
            log(f"디스코드 알림 {len(pending)}건 전송")
            self.state["pending"] = []
            self.state["pending_mention"] = ""
        except Exception as e:  # noqa: BLE001
            sent = getattr(e, "sent", 0)
            self.state["pending"] = pending[sent:]
            if sent:
                self.state["pending_mention"] = ""
            log(f"디스코드 전송 실패, 다음 회차에 다시 보낼게요 (남은 {len(pending) - sent}건): {e}")

    def notify_now(self, title, desc):
        try:
            send_embeds(self.webhook, self.cfg, [system_embed(title, desc)], dry_run=self.args.dry_run)
        except Exception as e:  # noqa: BLE001
            log(f"디스코드 전송 실패: {e}")

    def seconds_until_next(self):
        ts = time.time()
        nxt = min(self.next["dart"], self.next["g2b"])
        if self.gh:
            up = self.last_alive_upload + self.sched["alive_minutes"] * 60
            if self.saved_data_text != self.uploaded_data_text:
                up = min(up, self.last_data_upload + self.sched["upload_min_seconds"])
            nxt = min(nxt, self.next["cfg"], max(up, self.upload_backoff_until))
        if self.state.get("pending"):
            nxt = min(nxt, ts + 60)
        return max(5.0, min(60.0, nxt - ts))


# ---------------------------------------------------------------- 실행 모드
def acquire_lock():
    if fcntl is None:
        return None
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def run_loop(args):
    global _log_to_file
    _log_to_file = True
    lock = acquire_lock()
    if lock is False:
        print("이미 실행 중이에요. (멈추려면: radar stop)")
        return 0
    radar = Radar(args)

    def on_term(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_term)

    est = radar.dart_daily_estimate()
    warn = ""
    if est > 19000:
        warn = (f"\n\n⚠️ 지금 설정이면 DART 호출이 하루 약 {est:,}회로 한도(2만 회)에 가까워요. "
                "종목 수를 줄이거나 config.json 의 schedule 간격을 늘리세요.")
    log(f"시작 (버전 {VERSION}) · {schedule_text(radar.sched)} · DART 예상 하루 {est:,}회")
    radar.notify_now("▶️ 투자 레이더 시작",
                     f"{radar.cfg.get('device_name') or '이 기기'}에서 실행을 시작했어요.\n"
                     f"{schedule_text(radar.sched)}\n대시보드 업로드: {'켜짐' if radar.gh else '꺼짐 (GitHub 설정 없음)'}"
                     + warn)
    try:
        while True:
            try:
                radar.tick()
            except Exception as e:  # noqa: BLE001 - 어떤 오류가 나도 루프는 계속
                log(f"예상치 못한 오류: {e!r}")
            time.sleep(radar.seconds_until_next())
    except (KeyboardInterrupt, SystemExit):
        log("종료")
        radar.notify_now("⏹️ 투자 레이더 종료", "직접 멈췄어요. 다시 켜려면 Termux에서 `radar start`")
    return 0


def setup_github():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    if not token or not repo:
        print("GITHUB_TOKEN / GITHUB_REPO 가 설정되지 않았어요.")
        return 1
    gh = GitHub(token, repo)
    err = gh.check()
    if err:
        print("❌ " + err)
        return 1
    ok = True
    for rel in SETUP_FILES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        try:
            gh.put(rel, text, f"투자 레이더 설치: {rel}")
            print(f"  ✅ {rel}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  ❌ {rel}: {e}")
            if rel.startswith(".github/"):
                print("     → 토큰 권한에 'Workflows: Read and write' 를 추가했는지 확인하세요. "
                      "(이 파일은 휴대폰 봇 감시용이라 없어도 봇은 동작해요)")
    # 예전 'GitHub에서 10분마다 실행' 버전이 있으면 알림이 두 번 오지 않게 지웁니다.
    try:
        if gh.delete(".github/workflows/monitor.yml", "휴대폰 버전으로 전환: GitHub 실행 끄기"):
            print("  🧹 예전 GitHub 실행 파일(monitor.yml)을 지웠어요 (알림 중복 방지)")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  ❌ 예전 monitor.yml 삭제 실패: {e}\n     → GitHub 저장소 Actions 탭에서 '투자 레이더' 워크플로를 Disable 해 주세요.")
    print("완료" if ok else "일부 파일을 올리지 못했어요. 위 메시지를 확인하세요.")
    return 0 if ok else 2


def main():
    p = argparse.ArgumentParser(description="투자 레이더 봇")
    p.add_argument("--loop", action="store_true", help="계속 실행 (휴대폰용)")
    p.add_argument("--once", action="store_true", help="한 번만 확인")
    p.add_argument("--dry-run", action="store_true", help="디스코드·GitHub 로 보내지 않고 파일도 저장하지 않음")
    p.add_argument("--test-discord", action="store_true", help="디스코드 테스트 메시지 전송")
    p.add_argument("--setup-github", action="store_true", help="저장소에 대시보드·설정·감시 파일 올리기")
    args = p.parse_args()
    load_dotenv()

    if args.test_discord:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if not webhook:
            print("DISCORD_WEBHOOK_URL 이 설정되지 않았어요.")
            return 1
        try:
            send_embeds(webhook, load_json(CONFIG_PATH, {}) or {},
                        [system_embed("🔔 테스트 알림", "디스코드 연결이 잘 되었어요!")])
        except Exception as e:  # noqa: BLE001
            print(f"❌ {e}")
            return 1
        print("✅ 디스코드로 테스트 메시지를 보냈어요.")
        return 0
    if args.setup_github:
        return setup_github()
    if args.loop:
        return run_loop(args)
    Radar(args).tick()
    return 0


if __name__ == "__main__":
    sys.exit(main())
