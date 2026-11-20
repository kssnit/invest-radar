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
import email.utils
import hashlib
import io
import json
import re
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

VERSION = "2.7-phone"
KST = timezone(timedelta(hours=9))
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state", "state.json")
LOCK_PATH = os.path.join(ROOT, "state", "bot.lock")
DATA_PATH = os.path.join(ROOT, "docs", "data.json")
ALIVE_PATH = os.path.join(ROOT, "docs", "alive.json")
LOG_PATH = os.path.join(ROOT, "logs", "bot.log")
ENV_PATH = os.path.join(ROOT, ".env")
MARKET_CACHE_PATH = os.path.join(ROOT, "state", "market_cache.json")
KAKAO_TOKEN_PATH = os.path.join(ROOT, "state", "kakao_token.json")  # 휴대폰에만 저장 (GitHub 에 안 올려요)
KAKAO_AUTH = "https://kauth.kakao.com/oauth"
KAKAO_MEMO = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
KAKAO_SEND = ("all", "alerts", "important", "none")
KAKAO_DOMAINS = ["https://dart.fss.or.kr", "https://news.google.com", "https://www.g2b.go.kr"]
KAKAO_PENDING_MAX = 40

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
LABEL = {"dart": "DART 공시", "g2b": "나라장터", "github": "대시보드 업로드", "market": "시세"}

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
    "dart_unheld_every": 3,
}
ALERT_LEVELS = {"all": {"danger", "good", "info"}, "keywords": {"danger", "good"}, "none": set()}


STOCK_TYPES = ("stock", "etf", "us")   # stock: 국내 상장사(DART 공시 확인) / etf: 국내 ETF / us: 미국 주식 (둘 다 시세만)


def is_dart(c):
    return c.get("type", "stock") == "stock"


def is_held(c):
    """config 의 종목이 보유 종목인지. held 가 없으면 보유로 봅니다 (예전 설정 호환)."""
    return bool(c.get("held", True))


def held_word(held):
    return "보유" if held else "미보유"

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
        if not isinstance(c, dict) or c.get("type", "stock") not in STOCK_TYPES:
            return f"종목 항목 오류 (type 은 stock / etf / us): {c}"
        code = str(c.get("stock_code", ""))
        if c.get("type", "stock") in ("stock", "etf") and not (len(code) == 6 and code.isalnum()):
            return f"국내 종목코드는 6자리여야 해요: {c}"
        if c.get("type") == "us" and (not code or " " in code):
            return f"미국 주식은 stock_code 에 티커를 적어요 (예: NVDA.O): {c}"
        if "held" in c and not isinstance(c["held"], bool):
            return f"held 는 true 또는 false 로 적어야 해요: {c}"
    for k in ("alert_held", "alert_unheld"):
        if k in cfg.get("dart", {}) and cfg["dart"][k] not in ALERT_LEVELS:
            return f"dart.{k} 는 all / keywords / none 중 하나여야 해요: {cfg['dart'][k]}"
    mk = cfg.get("market", {})
    if not isinstance(mk, dict) or not isinstance(mk.get("indices", []), list):
        return "market.indices 가 목록이 아님"
    kk = cfg.get("kakao", {})
    if not isinstance(kk, dict) or kk.get("send", "alerts") not in KAKAO_SEND:
        return f"kakao.send 는 {' / '.join(KAKAO_SEND)} 중 하나여야 해요: {kk.get('send') if isinstance(kk, dict) else kk}"
    if mk.get("news_alert", "keywords") not in ("keywords", "none"):
        return f"market.news_alert 는 keywords / none 중 하나여야 해요: {mk.get('news_alert')}"
    for ix in mk.get("indices", []):
        if not isinstance(ix, dict) or not (ix.get("code") or ix.get("symbol")) or not ix.get("name"):
            return f"market.indices 항목에는 name 과 symbol 이 있어야 해요: {ix}"
        if ix.get("kind", "world_index") not in MARKET_KINDS:
            return f"market.indices 의 kind 는 {', '.join(MARKET_KINDS)} 중 하나여야 해요: {ix}"
        if ix.get("kind") == "marketindex" and not ix.get("category"):
            return f"환율·원자재(marketindex)에는 category 가 있어야 해요: {ix}"
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

    def publish_branch(self, branch, files, message):
        """files 만 담은 '기록 없는' 커밋 하나로 branch 를 덮어씁니다.
        시세처럼 자주 바뀌는 파일용: 저장소 기록이 쌓이지 않고, main 이 아니라 Pages 재배포도 일어나지 않아요."""
        api = f"{GITHUB_API}/repos/{self.repo}/git"
        tree = [{"path": p, "mode": "100644", "type": "blob", "content": t} for p, t in files.items()]
        code, obj = github_request("POST", f"{api}/trees", self.token, {"tree": tree})
        if code != 201:
            raise RuntimeError(f"GitHub 트리 생성 실패 HTTP {code}: {obj.get('message')}")
        code, obj = github_request("POST", f"{api}/commits", self.token,
                                   {"message": message, "tree": obj["sha"], "parents": []})
        if code != 201:
            raise RuntimeError(f"GitHub 커밋 생성 실패 HTTP {code}: {obj.get('message')}")
        sha = obj["sha"]
        code, obj = github_request("PATCH", f"{api}/refs/heads/{branch}", self.token, {"sha": sha, "force": True})
        if code == 200:
            return
        if code in (404, 422):  # 브랜치가 아직 없음 → 새로 만들기
            code, obj = github_request("POST", f"{api}/refs", self.token,
                                       {"ref": f"refs/heads/{branch}", "sha": sha})
            if code == 201:
                return
        raise RuntimeError(f"GitHub 브랜치 갱신 실패 {branch} HTTP {code}: {obj.get('message')}")

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
    companies = [c for c in dcfg.get("companies", []) if is_dart(c)]
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


def fetch_dart(cfg, state, key, include_unheld=True):
    dcfg = cfg.get("dart", {})
    companies, problems, cache_changed = resolve_corp_codes(dcfg, state, key)
    if not include_unheld:
        companies = [c for c in companies if is_held(c)]
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
                "held": is_held(c),
            })
        time.sleep(0.12)
    return items, problems, cache_changed, [c["stock_code"] for c in companies]


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
    held = it.get("held", True)
    return {
        "title": _trim(f"{EMOJI[it['level']]} [{it['corp']} · {held_word(held)}] {it['title']}", 256),
        "url": it["url"], "color": COLOR[it["level"]],
        "fields": [{"name": "제출인", "value": _trim(it.get("filer") or "-", 100), "inline": True},
                   {"name": "접수일", "value": it.get("date") or "-", "inline": True}],
        "footer": {"text": "DART 전자공시 · " + ("보유 종목" if held else "미보유 관심 종목")},
    }


def count_held(cfg, dart_only=False):
    comps = [c for c in cfg.get("dart", {}).get("companies", []) if is_dart(c) or not dart_only]
    h = sum(1 for c in comps if is_held(c))
    return h, len(comps) - h


def config_diff(old, new):
    """종목 추가·삭제·보유 변경을 사람이 읽을 문장으로."""
    o = {c["stock_code"]: c for c in old.get("dart", {}).get("companies", [])}
    n = {c["stock_code"]: c for c in new.get("dart", {}).get("companies", [])}
    lines = []
    for code, c in n.items():
        if code not in o:
            lines.append(f"➕ 추가: {c.get('name')} ({held_word(is_held(c))})")
        elif is_held(o[code]) != is_held(c):
            lines.append(f"🔁 {c.get('name')}: {held_word(is_held(o[code]))} → {held_word(is_held(c))}")
    for code, c in o.items():
        if code not in n:
            lines.append(f"➖ 삭제: {c.get('name')}")
    return lines


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


# ---------------------------------------------------------------- 카카오톡 (나에게 보내기)
# 카카오 디벨로퍼스 앱의 REST API 키로 '나와의 채팅'에 알림을 남겨요. 키·토큰은 휴대폰 .env / state 에만 있어요.
class KakaoAuthError(RuntimeError):
    """다시 연결(radar kakao)해야 하는 오류"""


def _plain(text):
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", str(text or ""))
    return re.sub(r"[*`_]{1,2}", "", text).strip()


def dashboard_url(repo):
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


def embed_to_kakao(e, dash, domains):
    lines = [_plain(e.get("title"))]
    if e.get("description"):
        lines.append(_plain(e["description"]))
    for f in (e.get("fields") or [])[:3]:
        lines.append(f"{_plain(f.get('name'))}: {_plain(f.get('value'))}")
    text = _trim("\n".join(x for x in lines if x), 200)
    url = str(e.get("url") or "")
    ok = url.startswith("https://") and any(url == d or url.startswith(d.rstrip("/") + "/") for d in domains)
    return {"text": text, "url": url if ok else dash, "button": "원문 보기" if ok else "대시보드 열기"}


def kakao_wanted(e, mode):
    """보낼 알림 고르기: all 전부 / alerts 시스템 메시지 빼고 / important 주의·호재·입찰만"""
    if mode == "all":
        return True
    if mode == "none":
        return False
    color = e.get("color")
    if mode == "important":
        return color in (COLOR["danger"], COLOR["good"], COLOR["g2b"])
    return color != COLOR["system"]


class Kakao:
    def __init__(self, rest_key, secret="", redirect=""):
        self.key, self.secret, self.redirect = rest_key, secret, redirect

    @staticmethod
    def linked():
        data = load_json(KAKAO_TOKEN_PATH, {}) or {}
        return bool(data.get("refresh_token")) and not data.get("broken")

    @staticmethod
    def mark_broken(reason):
        data = load_json(KAKAO_TOKEN_PATH, {}) or {}
        if data:
            data["broken"] = _trim(reason, 300)
            save_text(KAKAO_TOKEN_PATH, dumps(data))

    def _post(self, url, fields, token=None, timeout=20):
        body = urllib.parse.urlencode(fields).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8", "User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()[:400].decode("utf-8", "replace")
            try:
                info = json.loads(raw)
            except ValueError:
                info = {"msg": raw}
            err = RuntimeError(f"카카오 HTTP {e.code}: {info.get('error_description') or info.get('msg') or raw}")
            err.code, err.info = e.code, info
            raise err

    def auth_url(self):
        q = {"client_id": self.key, "redirect_uri": self.redirect, "response_type": "code", "scope": "talk_message"}
        return f"{KAKAO_AUTH}/authorize?" + urllib.parse.urlencode(q)

    def _save(self, tok, old=None):
        t = time.time()
        data = dict(old or {})
        data["access_token"] = tok["access_token"]
        data["expires_at"] = int(t + int(tok.get("expires_in", 21599)) - 120)
        if tok.get("refresh_token"):
            data["refresh_token"] = tok["refresh_token"]
            data["refresh_expires_at"] = int(t + int(tok.get("refresh_token_expires_in", 5183999)))
        save_text(KAKAO_TOKEN_PATH, dumps(data))
        return data

    def exchange(self, code):
        f = {"grant_type": "authorization_code", "client_id": self.key, "redirect_uri": self.redirect, "code": code}
        if self.secret:
            f["client_secret"] = self.secret
        return self._save(self._post(f"{KAKAO_AUTH}/token", f))

    def token(self, force=False):
        data = load_json(KAKAO_TOKEN_PATH, {}) or {}
        if not data.get("refresh_token"):
            raise KakaoAuthError("카카오 연결 정보가 없어요")
        if not force and data.get("access_token") and time.time() < data.get("expires_at", 0):
            return data["access_token"]
        f = {"grant_type": "refresh_token", "client_id": self.key, "refresh_token": data["refresh_token"]}
        if self.secret:
            f["client_secret"] = self.secret
        try:
            return self._save(self._post(f"{KAKAO_AUTH}/token", f), data)["access_token"]
        except RuntimeError as e:
            if getattr(e, "code", 0) in (400, 401):
                raise KakaoAuthError(f"카카오 로그인이 만료됐어요 ({e})") from e
            raise

    def send(self, text, url, button="대시보드 열기"):
        tpl = {"object_type": "text", "text": _trim(text, 200),
               "link": {"web_url": url, "mobile_web_url": url}, "button_title": button}
        fields = {"template_object": json.dumps(tpl, ensure_ascii=False)}
        try:
            return self._post(KAKAO_MEMO, fields, self.token())
        except RuntimeError as e:
            code = getattr(e, "code", 0)
            if code == 401:  # 액세스 토큰 만료 → 한 번 새로 받아서 다시
                return self._post(KAKAO_MEMO, fields, self.token(force=True))
            if code == 403 or (getattr(e, "info", {}) or {}).get("code") in (-3, -402):
                raise KakaoAuthError(f"메시지 전송 권한이 없어요. 동의항목(카카오톡 메시지 전송)을 확인하세요 ({e})") from e
            raise


# ---------------------------------------------------------------- 시세 (네이버 증권, 키 필요 없음)
# 국내 종목·지수 / 미국 지수 / 환율·유가·금·국채를 네이버 증권의 공개 JSON 에서 받아요.
# (Yahoo 는 2025년부터 브라우저가 아닌 프로그램 요청을 429 로 막아서 쓰지 않아요.)
NAVER_POLL = "https://polling.finance.naver.com/api/realtime"
NAVER_MI = "https://m.stock.naver.com/front-api/marketIndex"
NAVER_CHART = "https://api.stock.naver.com/chart"
NAVER_SISE = "https://api.finance.naver.com/siseJson.naver"
BROWSER_UA = ("Mozilla/5.0 (Linux; Android 13; SM-G981N) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36")
MARKET_BRANCH = "market"
MARKET_FILE = "market.json"
MARKET_KINDS = ("kr_index", "world_index", "marketindex")
NEWS_RSS = "https://news.google.com/rss/search"
DEFAULT_NEWS_GOOD = ["급등", "상승", "신고가", "최고가", "호실적", "어닝 서프라이즈", "깜짝 실적", "목표가 상향", "목표주가 상향",
                     "수주", "계약 체결", "승인", "자사주 매입", "배당 인상", "증액"]
DEFAULT_NEWS_BAD = ["급락", "하락", "신저가", "폭락", "소송", "리콜", "조사", "제재", "적자", "어닝 쇼크", "목표가 하향",
                    "목표주가 하향", "감원", "해고", "규제", "벌금", "배당 삭감"]
DEFAULT_MARKET = {"enabled": True, "open_seconds": 120, "closed_minutes": 30, "history_hours": 6,
                  "news_minutes": 30, "news_types": ["stock", "etf", "us"], "news_alert": "keywords",
                  "news_alert_pct": 5, "news_alert_cooldown_hours": 3, "news_alert_daily_max": 4,
                  "intraday_minutes": 15, "upload_seconds": 120, "indices": []}


def naver_get(url, referer=None):
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def _n(v):
    """'1,234.5' / '-0.81' / '+3' / 숫자 → float"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).replace(",", "").replace("+", "").replace("−", "-").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _signed(v, direction):
    x = _n(v)
    name = direction.get("name") if isinstance(direction, dict) else direction
    if x is not None and x > 0 and name in ("FALLING", "LOWER_LIMIT"):
        return -x
    return x


def _epoch(iso):
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=KST)).timestamp())


def _quote(price, chg, pct, high=None, low=None, vol=None, when=None, status=""):
    prev = round(price - chg, 4) if price is not None and chg is not None else None
    if pct is None and prev:
        pct = round(chg / prev * 100, 2)
    return {"price": price, "chg": chg, "pct": pct, "prev": prev, "high": high, "low": low,
            "vol": int(vol) if vol else None, "time": when, "status": status or ""}


def parse_poll_row(d):
    """polling.finance.naver.com 의 datas[] 한 줄 (국내 종목·국내 지수·해외 지수 공통)"""
    def g(k):
        return d.get(k + "Raw", d.get(k))
    dirn = d.get("compareToPreviousPrice")
    return _quote(_n(g("closePrice")), _signed(g("compareToPreviousClosePrice"), dirn),
                  _signed(g("fluctuationsRatio"), dirn), _n(g("highPrice")), _n(g("lowPrice")),
                  _n(g("accumulatedTradingVolume")), _epoch(d.get("localTradedAt") or ""), d.get("marketStatus"))


def parse_marketindex(obj):
    r = obj.get("result") if isinstance(obj.get("result"), dict) else obj
    dirn = r.get("fluctuationsType")
    return _quote(_n(r.get("calcPrice")) or _n(r.get("closePrice")), _signed(r.get("fluctuations"), dirn),
                  _signed(r.get("fluctuationsRatio"), dirn), _n(r.get("highPrice")), _n(r.get("lowPrice")),
                  None, _epoch(r.get("localTradedAt") or ""), "OPEN" if now().weekday() < 5 else "CLOSE")


def _code_of(d):
    return str(d.get("itemCode") or d.get("reutersCode") or d.get("symbolCode") or d.get("code") or "")


def _digits(v):
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def parse_rows(obj):
    """여러 모양의 일봉 응답 → {yyyymmdd: (종가, 고가, 저가, 거래량)}"""
    if isinstance(obj, dict):
        rows = obj.get("priceInfos") or obj.get("result") or obj.get("datas") or []
    else:
        rows = obj or []
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        day = _digits(r.get("localDate") or r.get("localTradedAt") or r.get("localDateTime") or r.get("bizdate"))[:8]
        c = _n(r.get("closePrice")) or _n(r.get("currentPrice"))
        if len(day) == 8 and c:
            out[int(day)] = (c, _n(r.get("highPrice")) or c, _n(r.get("lowPrice")) or c,
                             int(_n(r.get("accumulatedTradingVolume")) or 0))
    return out


def parse_sise_json(text):
    """siseJson.naver 응답(JS 배열 글자) → {yyyymmdd: (종가, 고가, 저가, 거래량)}"""
    import re
    out = {}
    for m in re.finditer(r'\["(\d{8})",\s*([\d.\-]+),\s*([\d.\-]+),\s*([\d.\-]+),\s*([\d.\-]+),\s*([\d.\-]+)', text):
        c = float(m.group(5))
        if not c:
            continue
        out[int(m.group(1))] = (c, float(m.group(3)) or c, float(m.group(4)) or c, int(float(m.group(6))))
    return out


def daily_summary(rows):
    days = sorted(rows)[-260:]
    full = days[:-1]
    vols = [rows[x][3] for x in full[-20:] if rows[x][3]]
    return {"d": days, "c": [rows[x][0] for x in days],
            "hi52": max((rows[x][1] for x in days[-252:]), default=None),
            "lo52": min((rows[x][2] for x in days[-252:]), default=None),
            "vol20": int(sum(vols) / len(vols)) if vols else None}


def fetch_daily(tgt):
    kind, code = tgt["kind"], tgt["code"]
    if kind in ("kr_stock", "kr_index"):
        end = now().date()
        q = urllib.parse.urlencode({"symbol": code, "requestType": 1, "timeframe": "day",
                                    "startTime": (end - timedelta(days=380)).strftime("%Y%m%d"),
                                    "endTime": end.strftime("%Y%m%d")})
        rows = parse_sise_json(naver_get(f"{NAVER_SISE}?{q}", referer="https://finance.naver.com/"))
    elif kind == "world_index":
        rows = parse_rows(json.loads(naver_get(
            f"{NAVER_CHART}/foreign/index/{urllib.parse.quote(code, safe='')}?periodType=dayCandle")))
    elif kind == "us_stock":
        base = f"{NAVER_CHART}/foreign/item/{urllib.parse.quote(tgt.get('rcode') or code, safe='')}?periodType=dayCandle"
        exch = tgt.get("exch") or ""
        rows = parse_rows(json.loads(naver_get(base + (f"&stockExchangeType={exch}" if exch else ""))))
        if not rows and exch:
            rows = parse_rows(json.loads(naver_get(base)))
    else:
        rows = {}
        for page in range(1, 5):
            got = parse_rows(json.loads(naver_get(f"{NAVER_MI}/prices?category={tgt.get('category', '')}"
                                                  f"&reutersCode={urllib.parse.quote(code, safe='')}&page={page}&pageSize=60")))
            if not got:
                break
            rows.update(got)
    if not rows:
        raise RuntimeError("일봉 없음")
    return daily_summary(rows)


def fetch_minutes(tgt):
    """국내 종목·지수 당일 1분 시세 → [(epoch, 가격)]"""
    kind = "index" if tgt["kind"] == "kr_index" else "item"
    obj = json.loads(naver_get(f"{NAVER_CHART}/domestic/{kind}/{tgt['code']}/minute?count=500"))
    rows = obj if isinstance(obj, list) else (obj.get("priceInfos") or [])
    pts = []
    for r in rows:
        dg = _digits(r.get("localDateTime") or r.get("localDate"))
        c = _n(r.get("currentPrice")) or _n(r.get("closePrice"))
        if len(dg) >= 12 and c:
            t = datetime(int(dg[:4]), int(dg[4:6]), int(dg[6:8]), int(dg[8:10]), int(dg[10:12]), tzinfo=KST)
            pts.append((int(t.timestamp()), c))
    return sorted(pts)


def _us_dst(d):
    """미국 서머타임: 3월 둘째 일요일 ~ 11월 첫째 일요일"""
    if d.month in (4, 5, 6, 7, 8, 9, 10):
        return True
    if d.month == 3:
        second_sun = 8 + (6 - date(d.year, 3, 8).weekday()) % 7
        return d.day >= second_sun
    if d.month == 11:
        first_sun = 1 + (6 - date(d.year, 11, 1).weekday()) % 7
        return d.day < first_sun
    return False


def session_window(kind, ts):
    """지금 진행 중이거나 가장 최근의 정규장 [시작, 끝] (epoch). 공휴일은 따지지 않아요."""
    if kind in ("kr_stock", "kr_index"):
        off, hm, length = 9 * 3600, (9, 0), 6.5 * 3600
    elif kind in ("world_index", "us_stock"):
        utc = datetime.fromtimestamp(ts, timezone.utc)
        off = (-4 if _us_dst((utc - timedelta(hours=5)).date()) else -5) * 3600
        hm, length = (9, 30), 6.5 * 3600
    else:
        return None
    local = datetime.fromtimestamp(ts + off, timezone.utc)
    for back in range(0, 7):
        d = (local - timedelta(days=back)).date()
        if d.weekday() >= 5:
            continue
        start = int(datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=timezone.utc).timestamp()) - off
        if start <= ts:
            return [start, int(start + length)]
    return None


def _thin_idx(n, keep):
    """0..n-1 중 keep 개 안팎을 고르되 처음과 마지막은 꼭 포함."""
    if n <= keep:
        return list(range(n))
    step = (n - 1) / (keep - 1)
    return sorted({round(i * step) for i in range(keep)})


def _kst_day(epoch):
    return int(datetime.fromtimestamp(epoch, KST).strftime("%Y%m%d"))


def merged_closes(h, q):
    """일봉 종가에 오늘 시세를 붙이거나(새 날) 바꿔 끼운(같은 날) 날짜·종가 목록."""
    days, closes = list(h.get("d", [])), list(h.get("c", []))
    if q and q.get("price") is not None and q.get("time"):
        today = _kst_day(q["time"] - 14 * 3600) if q.get("kind") in ("world_index", "us_stock") else _kst_day(q["time"])
        if days and days[-1] == today:
            closes[-1] = q["price"]
        elif not days or today > days[-1]:
            days.append(today)
            closes.append(q["price"])
    return days, closes


def _sma(xs, n):
    return round(sum(xs[-n:]) / n, 4) if len(xs) >= n else None


def _ema(xs, n):
    k, e, out = 2 / (n + 1), None, []
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def _rsi(xs, n=14):
    if len(xs) <= n:
        return None
    d = [b - a for a, b in zip(xs, xs[1:])]
    ag = sum(max(x, 0) for x in d[:n]) / n
    al = sum(max(-x, 0) for x in d[:n]) / n
    for x in d[n:]:
        ag = (ag * (n - 1) + max(x, 0)) / n
        al = (al * (n - 1) + max(-x, 0)) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return round(100 - 100 / (1 + ag / al), 1)


def indicators(closes):
    """이동평균·RSI·MACD 와 이를 묶은 흐름 점수(-100~100). 매매 권유가 아니라 지표 요약이에요."""
    if len(closes) < 2:
        return {}
    price = closes[-1]
    out = {"ma5": _sma(closes, 5), "ma20": _sma(closes, 20), "ma60": _sma(closes, 60),
           "rsi": _rsi(closes)}
    if len(closes) >= 35:
        line = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
        sig = _ema(line, 9)
        out.update(macd=round(line[-1], 4), macd_sig=round(sig[-1], 4), macd_hist=round(line[-1] - sig[-1], 4))
    parts, tags = [], []
    ma5, ma20, ma60, rsi, hist = out["ma5"], out["ma20"], out["ma60"], out["rsi"], out.get("macd_hist")
    if ma20:
        parts.append(1 if price >= ma20 else -1)
        tags.append("20일선 위" if price >= ma20 else "20일선 아래")
    if ma20 and ma60:
        parts.append(1 if ma20 >= ma60 else -1)
    if ma5 and ma20 and ma60:
        if ma5 > ma20 > ma60:
            tags.append("정배열")
        elif ma5 < ma20 < ma60:
            tags.append("역배열")
    if hist is not None:
        parts.append(1 if hist > 0 else -1)
        tags.append("MACD 상승 흐름" if hist > 0 else "MACD 하락 흐름")
    if rsi is not None:
        parts.append(max(-1.0, min(1.0, (rsi - 50) / 20)))
        if rsi >= 70:
            tags.append("RSI 과열권")
        elif rsi <= 30:
            tags.append("RSI 침체권")
    if parts:
        out["score"] = int(round(sum(parts) / 4 * 100))
    out["tags"] = tags
    return out


def fetch_news(query, limit=8):
    """Google 뉴스 RSS (한국어) → [{id, title, source, url, time}] 최신순"""
    url = NEWS_RSS + "?" + urllib.parse.urlencode({"q": f"{query} when:7d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    root = ET.fromstring(naver_get(url))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].strip()
        link = (it.findtext("link") or "").strip()
        try:
            when = int(email.utils.parsedate_to_datetime(it.findtext("pubDate") or "").timestamp())
        except (TypeError, ValueError, IndexError):
            when = None
        if title and link:
            out.append({"id": hashlib.sha1(link.encode()).hexdigest()[:12], "title": title, "source": source,
                        "url": link, "time": when})
    out.sort(key=lambda x: x["time"] or 0, reverse=True)
    return out[:limit]


def news_level(title, mk):
    if any(k in title for k in mk.get("news_bad") or DEFAULT_NEWS_BAD):
        return "danger"
    if any(k in title for k in mk.get("news_good") or DEFAULT_NEWS_GOOD):
        return "good"
    return "info"


# 디스코드 알림은 대시보드 분류보다 엄격하게: '상승/하락' 같은 흔한 말 대신 강한 표현이나 5% 이상 움직임만
NEWS_ALERT_GOOD = ["급등", "신고가", "최고가", "호실적", "어닝 서프라이즈", "깜짝 실적", "흑자 전환", "흑자전환",
                   "목표가 상향", "목표주가 상향", "수주", "공급계약", "계약 체결", "자사주 매입", "자사주 소각",
                   "배당 인상", "상한가"]
NEWS_ALERT_BAD = ["급락", "폭락", "신저가", "하한가", "어닝 쇼크", "적자 전환", "적자전환", "목표가 하향",
                  "목표주가 하향", "소송", "리콜", "제재", "벌금", "압수수색", "횡령", "배임", "거래정지",
                  "상장폐지", "유상증자", "감원", "해고", "사임", "배당 삭감"]
_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_ASCII_TICKER = re.compile(r"^[A-Z0-9&]{1,5}$")


def news_alert_level(title, mk):
    """알림을 보낼 만한 제목이면 danger/good, 아니면 None"""
    if any(k in title for k in mk.get("news_alert_bad") or NEWS_ALERT_BAD):
        return "danger"
    if any(k in title for k in mk.get("news_alert_good") or NEWS_ALERT_GOOD):
        return "good"
    lim = float(mk.get("news_alert_pct", 5))
    if any(float(x) >= lim for x in _PCT.findall(title)):
        if any(k in title for k in ("하락", "내림", "약세", "빠져", "↓")):
            return "danger"
        if any(k in title for k in ("상승", "오름", "강세", "뛰", "↑")):
            return "good"
    return None


def news_aliases(c):
    name = str(c.get("name") or "")
    out = [name, re.sub(r"\(.*?\)", "", name)]
    if c.get("type") == "us":
        out.append(str(c.get("stock_code", "")).split(".")[0])
    out += [str(a) for a in c.get("alias", [])]
    return [a.strip() for a in out if len(a.replace(" ", "")) >= 2]


def news_mentions(title, aliases, exclude=()):
    """제목에 종목 이름(또는 별칭·티커)이 들어 있나. exclude 단어는 먼저 지우고 봐요 (예: 카카오뱅크)."""
    t = title
    for x in exclude:
        t = t.replace(x, " ")
    flat = t.replace(" ", "").lower()
    for a in aliases:
        if _ASCII_TICKER.match(a):
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])", t):
                return True
        elif a.replace(" ", "").lower() in flat:
            return True
    return False


def embed_news_group(corp, held, level, items):
    head = items[0]
    e = embed_news(dict(head, corp=corp, held=held, level=level))
    if len(items) > 1:
        more = "\n".join(f"· [{_trim(x['title'], 90)}]({x['url']})" for x in items[1:4])
        e["description"] = f"비슷한 소식 {len(items) - 1}건 더\n{more}"
    return e


def news_query(c):
    if c.get("news"):
        return c["news"]
    return f"{c.get('name')} 주가" if c.get("type") == "us" else str(c.get("name"))


def embed_news(it):
    held = it.get("held", True)
    return {
        "title": _trim(f"{EMOJI[it['level']]} [{it['corp']} · {held_word(held)}] {it['title']}", 256),
        "url": it["url"], "color": COLOR[it["level"]],
        "fields": [{"name": "출처", "value": _trim(it.get("source") or "-", 100), "inline": True}],
        "footer": {"text": "종목 뉴스 · 제목 키워드로 분류"},
    }


def market_targets(cfg):
    out = []
    for ix in cfg.get("market", {}).get("indices", []):
        out.append({"kind": ix.get("kind", "world_index"), "code": ix.get("code") or ix["symbol"], "name": ix["name"],
                    "group": ix.get("group", ""), "category": ix.get("category", "")})
    for c in cfg.get("dart", {}).get("companies", []):
        kind = "us_stock" if c.get("type") == "us" else "kr_stock"
        out.append({"kind": kind, "code": c["stock_code"], "name": c.get("name"), "held": is_held(c),
                    "type": c.get("type", "stock"), "exchange": c.get("exchange", ""), "news_q": news_query(c),
                    "aliases": news_aliases(c), "news_exclude": list(c.get("news_exclude", []))})
    return out


def _rounder(e):
    """원화 종목은 정수, 그 밖에는 소수 둘째(10 미만은 셋째) 자리까지."""
    nd = 0 if e.get("currency") == "KRW" else (2 if abs(e.get("price") or 0) >= 10 else 3)

    def r(v):
        if v is None:
            return None
        v = round(v, nd)
        return int(v) if nd == 0 else v
    return r


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _money(v, cur):
    if v is None:
        return "-"
    if cur == "USD":
        return f"${v:,.2f}"
    return f"{v:,.0f}" if cur == "KRW" or abs(v) >= 1000 else f"{v:,.2f}"


def outlook(e, closes, kind, ctx):
    """1~2일 단기 전망: 추세·모멘텀·과열·거래량·뉴스·공시·시장 분위기를 점수로 합친 규칙 기반 추정."""
    ind, p, cur, code = e.get("ind") or {}, e.get("price"), e.get("currency"), e.get("code")
    F = []

    def add(pts, t, why):
        pts = int(round(pts))
        if pts:
            F.append({"p": pts, "t": t, "why": why})

    ma5, ma20, ma60 = ind.get("ma5"), ind.get("ma20"), ind.get("ma60")
    if ma5 and ma20 and ma60:
        if ma5 > ma20 > ma60:
            add(15, "정배열", "5일·20일·60일 평균선이 위에서부터 차례로 놓여 있어 상승 추세가 이어지는 중이에요.")
        elif ma5 < ma20 < ma60:
            add(-15, "역배열", "5일·20일·60일 평균선이 아래에서부터 차례로 놓여 있어 하락 추세가 이어지는 중이에요.")
    if ma20 and p:
        gap = (p - ma20) / ma20 * 100
        add(10 if p >= ma20 else -10, "20일선 위" if p >= ma20 else "20일선 아래",
            f"현재가 {_money(p, cur)}가 20일 평균 {_money(ma20, cur)}보다 {abs(gap):.1f}% {'높아' if p >= ma20 else '낮아'}요.")
    if len(closes) >= 36:
        line = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
        sig = _ema(line, 9)
        h0, h1 = line[-1] - sig[-1], line[-2] - sig[-2]
        add(10 if h0 > 0 else -10, "MACD 상승 흐름" if h0 > 0 else "MACD 하락 흐름",
            f"MACD가 신호선보다 {'위' if h0 > 0 else '아래'}에 있어 {'오르는' if h0 > 0 else '내리는'} 쪽 힘이 우세해요.")
        if abs(h0 - h1) > abs(h0) * 0.02:
            add(5 if h0 > h1 else -5, "모멘텀 강해짐" if h0 > h1 else "모멘텀 약해짐",
                f"MACD와 신호선의 차이가 어제보다 {'커졌어요(위쪽으로)' if h0 > h1 else '줄었어요(아래쪽으로)'}. "
                f"{'오르는 속도가 붙고' if h0 > h1 else '오르는 힘이 빠지고'} 있다는 뜻이에요.")
    rsi = ind.get("rsi")
    if rsi is not None:
        if rsi >= 75:
            add(-15, "RSI 과열", f"RSI {rsi:.0f}: 단기간 많이 올라 하루 이틀 쉬어 가는(되돌림) 경우가 많은 구간이에요.")
        elif rsi >= 70:
            add(-8, "RSI 과열 근접", f"RSI {rsi:.0f}: 과열권(70 이상)이라 상승 속도가 둔해지기 쉬워요.")
        elif rsi <= 25:
            add(15, "RSI 침체", f"RSI {rsi:.0f}: 많이 팔려 기술적 반등이 나오기 쉬운 구간이에요.")
        elif rsi <= 30:
            add(8, "RSI 침체 근접", f"RSI {rsi:.0f}: 침체권(30 이하)이라 하락 속도가 둔해지기 쉬워요.")
    pct = e.get("pct")
    if pct is not None and abs(pct) >= 3:
        add(6 if pct > 0 else -6, "오늘 큰 상승" if pct > 0 else "오늘 큰 하락",
            f"오늘 {pct:+.1f}% 움직였어요. 큰 움직임은 하루 이틀 더 이어지는 경우가 많아요.")
    vol, vol20 = e.get("vol"), e.get("vol20")
    if vol and vol20 and pct and vol / vol20 >= 1.8:
        add(8 if pct > 0 else -8, "거래량 실린 상승" if pct > 0 else "거래량 실린 하락",
            f"평소의 {vol / vol20:.1f}배 거래량과 함께 {'올라' if pct > 0 else '내려'} 그 방향에 힘이 실렸어요.")
    since = time.time() - 48 * 3600
    nws = [x for x in ctx.get("news", {}).get(code, []) if (x.get("time") or 0) >= since]
    good = sum(1 for x in nws if x.get("level") == "good")
    bad = sum(1 for x in nws if x.get("level") == "danger")
    if good != bad:
        add(_clip((good - bad) * 6, -18, 18), "뉴스 분위기", f"최근 이틀 뉴스 {len(nws)}건 중 호재 {good}건 · 주의 {bad}건이에요.")
    dz = [x for x in ctx.get("dart", {}).get(code, []) if x.get("level") == "danger"]
    gd = [x for x in ctx.get("dart", {}).get(code, []) if x.get("level") == "good"]
    if dz:
        add(-min(24, 12 * len(dz)), "주의 공시", f"최근 이틀 주의 공시 {len(dz)}건: " + _trim(dz[0].get("title", ""), 40))
    if gd:
        add(min(16, 8 * len(gd)), "호재 공시", f"최근 이틀 호재 공시 {len(gd)}건: " + _trim(gd[0].get("title", ""), 40))
    kr, us = ctx.get("kr"), ctx.get("us")
    if kind == "kr_stock":
        if kr is not None:
            add(_clip(kr * 3, -8, 8), "국내 시장 분위기", f"코스피·코스닥 평균 {kr:+.2f}%예요.")
        if us is not None:
            add(_clip(us * 2, -6, 6), "미국 증시", f"S&P 500·나스닥 평균 {us:+.2f}%예요. 다음 날 국내 장에 영향을 주곤 해요.")
    elif us is not None:
        add(_clip(us * 3, -8, 8), "미국 시장 분위기", f"S&P 500·나스닥 평균 {us:+.2f}%예요.")
    total = int(_clip(sum(f["p"] for f in F), -100, 100))
    label = ("상승 우세" if total >= 30 else "약한 상승" if total >= 10 else
             "하락 우세" if total <= -30 else "약한 하락" if total <= -10 else "중립")
    d = "up" if total >= 10 else "down" if total <= -10 else "flat"
    same = sum(1 for f in F if (f["p"] > 0) == (total > 0)) if total else 0
    F.sort(key=lambda f: -abs(f["p"]))
    return {"score": total, "label": label, "dir": d, "agree": [same, len(F)], "factors": F}


def outlook_ctx(indices, news, dart_items):
    def avg(codes):
        v = [x["pct"] for x in indices if x.get("symbol") in codes and x.get("pct") is not None]
        return sum(v) / len(v) if v else None
    since = datetime.now(KST) - timedelta(hours=48)
    dart = {}
    for x in dart_items or []:
        try:
            seen = datetime.fromisoformat(x.get("first_seen") or "")
        except ValueError:
            continue
        if seen >= since:
            dart.setdefault(x.get("stock_code"), []).append(x)
    return {"kr": avg(("KOSPI", "KOSDAQ")), "us": avg((".INX", ".IXIC")), "news": news, "dart": dart}


def track_outlooks(cache, stocks, targets, ts):
    """장 마감 뒤 그날의 전망을 기록하고, 2거래일 뒤 종가로 맞았는지 채점해요."""
    log = cache.setdefault("olog", [])
    kinds = {t["code"]: t["kind"] for t in targets}
    have = {(r["c"], r["d"]) for r in log}
    for e in stocks:
        ol, code = e.get("outlook"), e.get("code")
        if not ol or e.get("missing") or code not in kinds:
            continue
        ses = session_window(kinds[code], ts)
        if not ses or ts < ses[1] + 600:
            continue
        d = _kst_day(ses[0])
        if (code, d) not in have:
            log.append({"c": code, "d": d, "s": ol["score"]})
            have.add((code, d))
    for r in log:
        if "r" in r or r["c"] not in kinds:
            continue
        key = kinds[r["c"]] + ":" + r["c"]
        h = cache.get("h", {}).get(key)
        if not h:
            continue
        days, closes = merged_closes(h, dict(cache.get("q", {}).get(key) or {}, kind=kinds[r["c"]]))
        if r["d"] in days:
            i = days.index(r["d"])
            if i + 2 < len(days) and closes[i]:
                r["r"] = round(closes[i + 2] / closes[i] - 1, 4)
    del log[:-3000]
    stats, per = {"all": [0, 0]}, {}
    for r in reversed(log):
        if "r" not in r or abs(r["s"]) < 10:
            continue
        hit = int((r["s"] > 0) == (r["r"] > 0))
        a = per.setdefault(r["c"], [0, 0])
        if a[1] < 60:
            a[0] += hit
            a[1] += 1
        if stats["all"][1] < 300:
            stats["all"][0] += hit
            stats["all"][1] += 1
    stats.update(per)
    stats["pending"] = sum(1 for r in log if "r" not in r)
    return stats


def market_entry(tgt, cache, ts, ctx=None):
    key = tgt["kind"] + ":" + tgt["code"]
    e = {k: tgt[k] for k in ("name", "group", "held") if tgt.get(k) not in (None, "")}
    e["symbol"] = tgt["code"]
    e["kind"] = tgt["kind"]
    if tgt["kind"] in ("kr_stock", "us_stock"):
        e["code"] = tgt["code"]
        e["type"] = tgt.get("type", "stock")
        e["currency"] = "USD" if tgt["kind"] == "us_stock" else "KRW"
    q, h = cache.get("q", {}).get(key), cache.get("h", {}).get(key)
    if not q or q.get("price") is None:
        e["missing"] = True
        return e
    for k in ("price", "pct", "vol", "time", "status"):
        e[k] = q.get(k)
    r = _rounder(e)
    for k in ("price", "prev", "chg", "high", "low"):
        e[k] = r(q.get(k))
    e["st"] = q.get("status", "")
    ses = session_window(tgt["kind"], ts)
    if ses:
        e["ses"] = ses
    pts = [p for p in (cache.get("i", {}).get(key) or []) if not ses or p[0] >= ses[0] - 600]
    if tgt["kind"] == "marketindex":
        pts = [p for p in pts if p[0] >= ts - 24 * 3600]
    if len(pts) >= 2:
        idx = _thin_idx(len(pts), 80 if tgt["kind"] in ("kr_stock", "us_stock") else 120)
        e["t0"] = pts[idx[0]][0]
        e["tm"] = [(pts[i][0] - e["t0"]) // 60 for i in idx]
        e["iv"] = [r(pts[i][1]) for i in idx]
    if h:
        days, closes = merged_closes(h, dict(q, kind=tgt["kind"]))
        ind = indicators(closes)
        if ind:
            for k in ("ma5", "ma20", "ma60", "macd", "macd_sig", "macd_hist"):
                if ind.get(k) is not None:
                    ind[k] = r(ind[k]) if k in ("ma5", "ma20", "ma60") else round(ind[k], 3)
            e["ind"] = ind
        hi = [x for x in (h.get("hi52"), q.get("high"), q.get("price")) if x is not None]
        lo = [x for x in (h.get("lo52"), q.get("low"), q.get("price")) if x is not None]
        e["hi52"], e["lo52"], e["vol20"] = r(max(hi)), r(min(lo)), h.get("vol20")
        if ctx is not None and tgt["kind"] in ("kr_stock", "us_stock"):
            e["outlook"] = outlook(e, closes, tgt["kind"], ctx)
        cut = max(0, len(days) - 63)
        sel = list(range(0, cut, 3)) + list(range(cut, len(days)))
        if sel:
            d0 = datetime.strptime(str(days[sel[0]]), "%Y%m%d")
            e["d0"] = days[sel[0]]
            e["dd"] = [(datetime.strptime(str(days[i]), "%Y%m%d") - d0).days for i in sel]
            e["dv"] = [r(closes[i]) for i in sel]
    return e

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
                "market": "\n\n네이버 증권 응답이 막혔거나 휴대폰 인터넷 문제일 수 있어요. 공시·입찰 알림은 그대로 동작해요.",
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
    text = (f"{days}{a:02d}~{b:02d}시: 공시 {m(s['dart_fast_seconds'])}·입찰 {m(s['g2b_fast_seconds'])}마다 / "
            f"그 외: 공시 {m(s['dart_slow_seconds'])}·입찰 {m(s['g2b_slow_seconds'])}마다")
    k = int(s.get("dart_unheld_every", 1))
    if k > 1:
        text += f" (미보유 종목 공시는 {k}배 간격)"
    return text


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
        self.dash = dashboard_url(repo)
        kkey = os.environ.get("KAKAO_REST_KEY", "").strip()
        self.kakao = Kakao(kkey, os.environ.get("KAKAO_CLIENT_SECRET", "").strip()) if (kkey and not args.dry_run) else None
        self.next = {"dart": 0.0, "g2b": 0.0, "cfg": 0.0, "market": 0.0}
        self.mcache = load_json(MARKET_CACHE_PATH, {}) or {}
        self.saved_mcache_text = None
        self.market_text = None
        self.uploaded_market_text = None
        self.last_market_upload = 0.0
        self.market_upload_err = None
        self.last_data_upload = 0.0
        self.last_alive_upload = 0.0
        self.upload_backoff_until = 0.0
        self.dart_round = 0
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
        held, unheld = count_held(self.cfg, dart_only=True)
        return int(runs * held + runs * unheld / max(1, int(s.get("dart_unheld_every", 1))))

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
        changes = config_diff(self.cfg, remote)
        self.cfg = remote
        self.dart_round = 0  # 다음 확인에 미보유 종목까지 바로 포함
        save_text(CONFIG_PATH, text if text.endswith("\n") else text + "\n")
        held, unheld = count_held(remote)
        log(f"설정 변경 반영: 보유 {held} · 미보유 {unheld} · " + " / ".join(changes))
        alerts.append(system_embed("⚙️ 설정 변경을 반영했어요",
                                   "\n".join(changes + [""] if changes else []) +
                                   f"보유 {held}개 · 미보유 {unheld}개 · 일정 {len(remote.get('events', []))}개"))
        self.next["dart"] = 0.0  # 바로 다시 확인
        self.next["market"] = 0.0

    def news_alert_ok(self, code, level, ts, m):
        """같은 종목·같은 방향 뉴스 알림은 몇 시간에 한 번, 하루 최대 몇 건까지만"""
        log_ = self.state.setdefault("news_alerts", {})
        hist = [x for x in log_.get(code, []) if ts - x[0] < 86400]
        cool = float(m.get("news_alert_cooldown_hours", 3)) * 3600
        if any(x[1] == level and ts - x[0] < cool for x in hist) or len(hist) >= int(m.get("news_alert_daily_max", 4)):
            log_[code] = hist
            return False
        log_[code] = hist + [[int(ts), level]]
        return True

    @property
    def mcfg(self):
        m = dict(DEFAULT_MARKET)
        m.update(self.cfg.get("market", {}))
        return m

    # --- 시세
    def market_tick(self, ts, alerts):
        """지수·종목 시세를 받아 market.json 내용을 만듭니다. 상태가 바뀌었으면 True."""
        m = self.mcfg
        if not m.get("enabled", True):
            self.market_text = None
            return update_status(self.state, "market", None, "꺼짐 (config.json)", alerts)
        if ts < self.next["market"]:
            return False
        cache = self.mcache
        if cache.get("src") != "naver":  # 예전(Yahoo) 캐시는 버려요
            cache.clear()
            cache["src"] = "naver"
        for k in ("q", "h", "i", "hfail", "ifetch"):
            cache.setdefault(k, {})
        targets = market_targets(self.cfg)
        errors, ok = {}, 0

        def store(tgt, q):
            nonlocal ok
            key = tgt["kind"] + ":" + tgt["code"]
            if q.get("price") is None:
                errors[tgt["name"]] = "값 없음"
                return
            q["at"] = ts
            q["time"] = q.get("time") or int(ts)
            cache["q"][key] = q
            ok += 1
            # 장중이면 받은 값을 당일 흐름에 이어 붙여요 (4시간 넘게 비면 새 장으로 봐요)
            if q.get("status") == "OPEN":
                pts = cache["i"].setdefault(key, [])
                if pts and q["time"] - pts[-1][0] > 4 * 3600:
                    pts.clear()
                if not pts or q["time"] > pts[-1][0]:
                    pts.append([q["time"], q["price"]])
                del pts[:-800]

        def by_kind(kind):
            return [t for t in targets if t["kind"] == kind]

        # 1) 한 번에 여러 개 받는 것들
        def rows_of(obj):
            out = {}
            for d in (obj.get("datas") or []) if isinstance(obj, dict) else []:
                if isinstance(d, dict):
                    for k in ("itemCode", "reutersCode", "symbolCode", "code"):
                        if d.get(k):
                            out[str(d[k])] = d
            return out

        usmap = cache.setdefault("usmap", {})
        usfail = cache.setdefault("usfail", {})
        usx = cache.setdefault("usx", {})
        for t in targets:
            if t["kind"] == "us_stock":
                t["rcode"] = usmap.get(t["code"]) or t["code"]

        def qcode(t):
            return t.get("rcode") or t["code"]

        for kind, path in (("kr_stock", "domestic/stock"), ("kr_index", "domestic/index"),
                           ("world_index", "worldstock/index"), ("us_stock", "worldstock/stock")):
            group = by_kind(kind)
            for i in range(0, len(group), 20):
                part = group[i:i + 20]
                try:
                    rows = rows_of(json.loads(naver_get(f"{NAVER_POLL}/{path}/" +
                                                        ",".join(urllib.parse.quote(qcode(t), safe="") for t in part))))
                    if not rows:
                        raise RuntimeError("빈 응답")
                except Exception as e:  # noqa: BLE001
                    if len(part) == 1:
                        errors[part[0]["name"]] = _trim(str(e), 80)
                        continue
                    rows = {}
                    for j, t in enumerate(part):  # 묶음이 실패하면 하나씩 다시 (코드 하나가 틀린 경우 대비)
                        try:
                            rows.update(rows_of(json.loads(naver_get(f"{NAVER_POLL}/{path}/" +
                                                                     urllib.parse.quote(qcode(t), safe="")))))
                        except Exception as e2:  # noqa: BLE001
                            errors[t["name"]] = _trim(str(e2), 80)
                            if j == 0 and len(part) > 1:  # 첫 개도 안 되면 서버·인터넷 문제 → 나머지는 건너뜀
                                for rest in part[1:]:
                                    errors[rest["name"]] = errors[t["name"]]
                                break
                        time.sleep(0.2)
                for t in part:
                    d = rows.get(qcode(t))
                    # 미국 주식 코드를 못 찾으면 거래소 꼬리표(.O 나스닥 / .N·없음 뉴욕 / .K 아멕스)를 붙여 한 번 찾아봐요
                    if d is None and kind == "us_stock" and "." not in t["code"] and ts - usfail.get(t["code"], 0) > 6 * 3600:
                        for cand in (t["code"] + ".O", t["code"] + ".N", t["code"] + ".K"):
                            try:
                                got = rows_of(json.loads(naver_get(f"{NAVER_POLL}/{path}/" + urllib.parse.quote(cand, safe=""))))
                            except Exception:  # noqa: BLE001
                                got = {}
                            time.sleep(0.2)
                            if cand in got:
                                d, usmap[t["code"]], t["rcode"] = got[cand], cand, cand
                                break
                        else:
                            usfail[t["code"]] = ts
                    if d is None:
                        errors.setdefault(t["name"], "응답에 없음")
                        continue
                    errors.pop(t["name"], None)
                    if kind == "us_stock":
                        ex = d.get("stockExchangeType")
                        name = (ex.get("name") or ex.get("code")) if isinstance(ex, dict) else (ex or d.get("stockExchangeName"))
                        if name:
                            usx[t["code"]] = str(name).upper()
                    store(t, parse_poll_row(d))
                time.sleep(0.2)
        # 2) 환율·유가·금·국채
        for t in by_kind("marketindex"):
            try:
                store(t, parse_marketindex(json.loads(naver_get(
                    f"{NAVER_MI}/productDetail?category={t['category']}&reutersCode={urllib.parse.quote(t['code'], safe='')}"))))
            except Exception as e:  # noqa: BLE001
                errors[t["name"]] = _trim(str(e), 80)
            time.sleep(0.2)
        # 3) 국내 1분 시세로 당일 흐름 채우기 (장중 15분마다, 장 끝나고 한 번)
        imin = float(m["intraday_minutes"]) * 60
        for t in targets:
            if t["kind"] not in ("kr_stock", "kr_index"):
                continue
            key = t["kind"] + ":" + t["code"]
            q = cache["q"].get(key) or {}
            last = cache["ifetch"].get(key, 0)
            ses = session_window(t["kind"], ts)
            need = (q.get("status") == "OPEN" and ts - last >= imin) or (ses and last < ses[1] <= ts)
            if not need or ts - cache["hfail"].get("i:" + key, 0) < 1800:
                continue
            try:
                pts = fetch_minutes(t)
                if pts:
                    cache["i"][key] = [list(p) for p in pts]
                cache["ifetch"][key] = ts
            except Exception:  # noqa: BLE001 - 다음에 다시
                cache["hfail"]["i:" + key] = ts
            time.sleep(0.2)
        # 4) 일봉 (6시간마다)
        hist_s = float(m["history_hours"]) * 3600
        for t in targets:
            key = t["kind"] + ":" + t["code"]
            h = cache["h"].get(key)
            if (h and ts - h.get("at", 0) < hist_s) or ts - cache["hfail"].get(key, 0) < 1800:
                continue
            if t["kind"] == "us_stock":
                rc = qcode(t)
                t["exch"] = usx.get(t["code"]) or t.get("exchange") or ("NASDAQ" if rc.endswith(".O") else "NYSE")
            try:
                cache["h"][key] = dict(fetch_daily(t), at=ts)
            except Exception as e:  # noqa: BLE001
                cache["hfail"][key] = ts
                log(f"일봉 못 받음 ({t['name']}): {e}")
            time.sleep(0.2)

        # 5) 종목 뉴스 (30분마다). 알림은 제목에 종목명이 있고 강한 표현일 때만, 종목·방향별 쿨다운
        news = cache.setdefault("news", {})
        nat = cache.setdefault("news_at", {})
        nseen = cache.setdefault("news_seen", {})
        ntypes = m.get("news_types") or []
        npolicy = ALERT_LEVELS.get(m.get("news_alert", "keywords"), set())  # 예전 news_discord 는 v2.7부터 안 써요
        nretry = cache.setdefault("news_retry", {})
        news_err = 0
        for t in targets:
            if (t.get("type") not in ntypes or ts - nat.get(t["code"], 0) < float(m["news_minutes"]) * 60
                    or ts < nretry.get(t["code"], 0)):
                continue
            first = t["code"] not in nat  # 처음 받는 종목은 기준만 잡고 알림 없음
            if news_err >= 3:  # 뉴스 서버가 막혔으면 나머지는 10분 뒤에
                nretry[t["code"]] = ts + 600
                continue
            try:
                items = fetch_news(t["news_q"])
                nat[t["code"]] = ts
                nretry.pop(t["code"], None)
            except Exception as e:  # noqa: BLE001
                nretry[t["code"]] = ts + 600  # 10분 뒤 다시
                news_err += 1
                log(f"뉴스 못 받음 ({t['name']}): {e}")
                continue
            fresh = {"danger": [], "good": []}
            for it in items:
                it["level"] = news_level(it["title"], m)
                it["first_seen"] = nseen.setdefault(it["id"], int(ts))
                if first or it["first_seen"] != int(ts) or (it.get("time") and ts - it["time"] > 2 * 86400):
                    continue
                lv = news_alert_level(it["title"], m)
                if lv and lv in npolicy and news_mentions(it["title"], t.get("aliases", []), t.get("news_exclude", [])):
                    fresh[lv].append(it)
            for lv, group in fresh.items():
                if group and self.news_alert_ok(t["code"], lv, ts, m):
                    alerts.append(embed_news_group(t["name"], t.get("held", True), lv, group))
            news[t["code"]] = items
            time.sleep(0.3)
        for k in [k for k, v in nseen.items() if ts - v > 14 * 86400]:
            del nseen[k]

        # 다음 확인: 열린 시장이 있으면 짧게
        any_open = any((q or {}).get("status") == "OPEN" for q in cache["q"].values())
        weekday = now().weekday() < 5
        busy = any_open or (weekday and any(t["kind"] == "marketindex" for t in targets))
        if not ok and errors:
            self.next["market"] = ts + 300
        else:
            self.next["market"] = ts + (float(m["open_seconds"]) if busy else float(m["closed_minutes"]) * 60)

        stamps = [q.get("at", 0) for q in cache["q"].values()]
        idx_entries = [market_entry(x, cache, ts) for x in targets if x["kind"] not in ("kr_stock", "us_stock")]
        dart_items = (load_json(DATA_PATH, {}) or {}).get("dart", [])
        ctx = outlook_ctx(idx_entries, news, dart_items)
        stk_entries = [market_entry(x, cache, ts, ctx) for x in targets if x["kind"] in ("kr_stock", "us_stock")]
        payload = {"updated_at": datetime.fromtimestamp(max(stamps), KST).isoformat(timespec="seconds") if stamps else "",
                   "source": "네이버 증권",
                   "news": {code: items for code, items in news.items()
                            if any(t["code"] == code and t.get("type") in ntypes for t in targets)},
                   "indices": idx_entries, "stocks": stk_entries,
                   "outlook_stats": track_outlooks(cache, stk_entries, targets, ts)}
        self.market_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))  # 자주 올리니 공백 없이

        mtext = json.dumps(cache, ensure_ascii=False, separators=(",", ":"))
        if mtext != self.saved_mcache_text and not self.args.dry_run:
            save_text(MARKET_CACHE_PATH, mtext)
        self.saved_mcache_text = mtext

        if self.market_upload_err:
            return update_status(self.state, "market", False, "시세 업로드 실패: " + self.market_upload_err, alerts)
        if not ok and errors:
            return update_status(self.state, "market", False, "시세를 받지 못했어요: " +
                                 _trim("; ".join(f"{k} {v}" for k, v in errors.items()), 250), alerts)
        n_ix = sum(1 for x in payload["indices"] if not x.get("missing"))
        n_st = sum(1 for x in payload["stocks"] if not x.get("missing"))
        msg = f"정상 · 지수 {n_ix}개 · 종목 {n_st}개"
        if news_err:
            msg += f" · 뉴스 {news_err}곳 못 받음"
        if errors:
            msg += " · 못 받은 것: " + ", ".join(errors)
        return update_status(self.state, "market", True, msg, alerts)

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
                    every = max(1, int(self.sched.get("dart_unheld_every", 1)))
                    include_unheld = self.dart_round % every == 0
                    self.dart_round += 1
                    if "primed_corps" not in state:  # 예전 버전에서 넘어온 경우: 이미 확인하던 종목은 그대로
                        state["primed_corps"] = sorted(k for k, v in state.get("corp_codes", {}).items() if v)
                    items, problems, cache_changed, queried = fetch_dart(
                        cfg, state, self.dart_key, include_unheld=include_unheld)
                    status_changed |= cache_changed
                    primed_corps = set(state["primed_corps"])
                    seen = set(state.get("seen_dart", []))
                    for it in sorted(items, key=lambda x: x["id"]):
                        if it["id"] not in seen:
                            it["first_seen"] = t.isoformat(timespec="seconds")
                            if it["stock_code"] not in primed_corps:
                                it["quiet"] = True  # 새로 추가한 종목의 지난 공시: 대시보드에만
                            new_dart.append(it)
                            seen.add(it["id"])
                    newly_added = sorted(set(queried) - primed_corps)
                    if newly_added:
                        state["primed_corps"] = sorted(primed_corps | set(queried))
                        status_changed = True
                    state["seen_dart"] = sorted(seen)[-4000:]
                    n_held, n_unheld = count_held(cfg, dart_only=True)
                    msg = f"정상 · 보유 {n_held}개 · 미보유 {n_unheld}개"
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

        # 2.5) 시세
        try:
            status_changed |= self.market_tick(ts, alerts)
        except Exception as e:  # noqa: BLE001 - 시세 문제로 공시 알림이 멈추면 안 돼요
            log(f"시세 오류: {e!r}")
            self.next["market"] = ts + 300

        # 3) 일정
        reminders = due_reminders(cfg, state)

        # 4) 알림 만들기 (소스가 처음 성공한 회차의 과거 항목은 대시보드에만)
        primed = state.setdefault("primed", {})
        newly = [s for s in ("dart", "g2b")
                 if state.get("status", {}).get(s, {}).get("state") == "ok" and not primed.get(s)]
        for s in newly:
            primed[s] = True
        alerts = [a for a in alerts if not any(a["title"] == f"✅ {LABEL[s]} 다시 정상" for s in newly)]
        dcfg = cfg.get("dart", {})
        lv_held = ALERT_LEVELS.get(dcfg.get("alert_held", "all"), ALERT_LEVELS["all"])
        lv_unheld = ALERT_LEVELS.get(dcfg.get("alert_unheld", "keywords"), ALERT_LEVELS["keywords"])
        alert_dart = [] if "dart" in newly else [
            x for x in new_dart
            if not x.get("quiet") and x["level"] in (lv_held if x.get("held", True) else lv_unheld)]
        for x in new_dart:
            x.pop("quiet", None)
        alert_g2b = [] if "g2b" in newly else new_g2b

        embeds = []
        if first_run:
            words = {"ok": "정상 ✅", "fail": "오류 ⚠️", "off": "미설정"}
            lines = [f"• {LABEL[s]}: {words.get(state.get('status', {}).get(s, {}).get('state'), '-')}"
                     for s in ("dart", "g2b")]
            n_held, n_unheld = count_held(cfg)
            embeds.append(system_embed(
                "✅ 투자 레이더 연결 완료",
                f"보유 {n_held}개 · 미보유 {n_unheld}개 종목을 지켜보기 시작했어요.\n"
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
        if any(x["level"] == "danger" and x.get("held", True) for x in alert_dart):
            state["pending_mention"] = cfg.get("discord", {}).get("mention_on_danger", "") or ""
        if embeds:
            self.enqueue(embeds)
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
        data["watchlist"] = [{"name": c.get("name"), "stock_code": c.get("stock_code"), "held": is_held(c),
                              "type": c.get("type", "stock")}
                             for c in cfg.get("dart", {}).get("companies", [])]
        data["schedule_text"] = schedule_text(self.sched)
        data["device"] = cfg.get("device_name", "")
        if self.gh and self.mcfg.get("enabled", True):
            data["market_url"] = f"https://raw.githubusercontent.com/{self.gh.repo}/{MARKET_BRANCH}/{MARKET_FILE}"
        else:
            data.pop("market_url", None)
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
            self.enqueue(alerts)
        # 시세는 따로: 실패해도 공시 데이터·생존 신호 업로드는 막지 않아요
        if (self.market_text and self.market_text != self.uploaded_market_text and ts >= self.upload_backoff_until
                and ts - self.last_market_upload >= float(self.mcfg["upload_seconds"])):
            try:
                self.gh.publish_branch(MARKET_BRANCH, {MARKET_FILE: self.market_text}, f"시세 {t:%m-%d %H:%M}")
                self.uploaded_market_text = self.market_text
                self.market_upload_err = None
            except Exception as e:  # noqa: BLE001
                log(f"시세 업로드 오류 (5분 뒤 다시 시도): {e}")
                self.market_upload_err = _trim(str(e), 200)
            self.last_market_upload = ts if not self.market_upload_err else ts + 180

    def enqueue(self, embeds):
        self.state["pending"] = (self.state.get("pending", []) + embeds)[-PENDING_MAX:]
        if not self.kakao or not Kakao.linked():
            return
        kc = self.cfg.get("kakao", {})
        mode = kc.get("send", "alerts")
        domains = list(kc.get("domains") or KAKAO_DOMAINS) + ([self.dash] if self.dash else [])
        msgs = [embed_to_kakao(e, self.dash, domains) for e in embeds if kakao_wanted(e, mode)]
        if msgs:
            self.state["kakao_pending"] = (self.state.get("kakao_pending", []) + msgs)[-KAKAO_PENDING_MAX:]

    def kakao_keepalive(self):
        """리프레시 토큰(약 2달)이 한 달 안쪽으로 남으면 하루 한 번 새로 받아 연장해요."""
        data = load_json(KAKAO_TOKEN_PATH, {}) or {}
        ts = time.time()
        if data.get("refresh_expires_at", 0) - ts > 25 * 86400 or ts - self.state.get("kakao_keepalive", 0) < 86400:
            return
        self.state["kakao_keepalive"] = int(ts)
        self.kakao.token(force=True)

    def kakao_broken(self, e):
        Kakao.mark_broken(str(e))
        log(f"카카오톡 연결 끊김: {e}")
        self.state["kakao_pending"] = []
        self.state["pending"] = (self.state.get("pending", []) + [system_embed(
            "⚠️ 카카오톡 전달이 멈췄어요",
            f"{e}\nTermux에서 `radar kakao` 로 다시 연결하면 돼요. 디스코드 알림은 그대로 와요.")])[-PENDING_MAX:]

    def flush_kakao(self):
        if not self.kakao or not Kakao.linked():
            return
        try:
            self.kakao_keepalive()
        except KakaoAuthError as e:
            self.kakao_broken(e)
            return
        except Exception as e:  # noqa: BLE001
            log(f"카카오 토큰 연장 실패 (내일 다시): {e}")
        q = self.state.get("kakao_pending", [])
        if not q:
            return
        if len(q) > 25:  # 한꺼번에 너무 많으면 앞쪽은 요약 한 줄로
            q = [{"text": f"알림 {len(q) - 10}건이 한꺼번에 와서 앞의 것은 대시보드·디스코드에서 확인해 주세요.",
                  "url": self.dash, "button": "대시보드 열기"}] + q[-10:]
        sent = 0
        try:
            for msg in q[:15]:
                self.kakao.send(msg["text"], msg["url"] or self.dash, msg.get("button") or "대시보드 열기")
                sent += 1
                time.sleep(0.4)
        except KakaoAuthError as e:
            self.kakao_broken(e)
            return
        except Exception as e:  # noqa: BLE001 - 네트워크 오류 등은 다음에 다시
            log(f"카카오톡 전송 실패, 다음에 다시 보낼게요 (남은 {len(q) - sent}건): {e}")
        if sent:
            log(f"카카오톡 알림 {sent}건 전송")
        self.state["kakao_pending"] = q[sent:]

    def flush(self):
        try:
            self.flush_kakao()
        except Exception as e:  # noqa: BLE001
            log(f"카카오톡 처리 오류: {e!r}")
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
        if self.mcfg.get("enabled", True):
            nxt = min(nxt, self.next["market"])
        if self.gh:
            up = self.last_alive_upload + self.sched["alive_minutes"] * 60
            if self.saved_data_text != self.uploaded_data_text:
                up = min(up, self.last_data_upload + self.sched["upload_min_seconds"])
            if self.market_text and self.market_text != self.uploaded_market_text:
                up = min(up, self.last_market_upload + float(self.mcfg["upload_seconds"]))
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


def set_env_values(values):
    """.env 의 KEY=VALUE 를 바꾸거나 덧붙여요 (휴대폰에만 있는 파일)."""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    for k, v in values.items():
        row = f"{k}={v}"
        for i, ln in enumerate(lines):
            if ln.split("=", 1)[0].strip() == k:
                lines[i] = row
                break
        else:
            lines.append(row)
        os.environ[k] = v
    save_text(ENV_PATH, "\n".join(lines) + "\n")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


KOE_HINT = {
    "KOE006": "리다이렉트 URI가 등록되지 않았어요. [앱] > [플랫폼 키] > [REST API 키]에 아래 주소를 그대로 등록하세요",
    "KOE004": "카카오 로그인이 꺼져 있어요. [카카오 로그인] > [사용 설정]에서 상태를 ON으로 바꾸세요",
    "KOE010": "클라이언트 시크릿이 틀렸어요. `radar kakao reset` 으로 키를 다시 넣으세요",
    "KOE101": "REST API 키가 틀렸어요. `radar kakao reset` 으로 키를 다시 넣으세요",
    "KOE320": "코드가 만료됐거나 이미 썼어요. `radar kakao` 를 처음부터 다시 해 주세요 (코드는 한 번만, 몇 분 안에 써야 해요)",
}


def setup_kakao(reset=False):
    key = "" if reset else os.environ.get("KAKAO_REST_KEY", "").strip()
    secret = "" if reset else os.environ.get("KAKAO_CLIENT_SECRET", "").strip()
    if not key:
        print("카카오 디벨로퍼스 > 내 앱 > [앱] > [플랫폼 키] > [REST API 키] 화면의 값을 붙여 넣으세요.")
        key = input("REST API 키: ").strip()
        if not key:
            print("취소했어요.")
            return 1
        secret = input("클라이언트 시크릿 (같은 화면, 없으면 그냥 Enter): ").strip()
        set_env_values({"KAKAO_REST_KEY": key, "KAKAO_CLIENT_SECRET": secret})
    repo = os.environ.get("GITHUB_REPO", "").strip()
    redirect = os.environ.get("KAKAO_REDIRECT_URI", "").strip() or (dashboard_url(repo) + "kakao.html" if repo else "")
    if not redirect.startswith("https://"):
        print(".env 에 GITHUB_REPO 가 없어서 돌아올 주소를 만들 수 없어요. KAKAO_REDIRECT_URI 를 넣어 주세요.")
        return 1
    k = Kakao(key, secret, redirect)
    url = k.auth_url()
    print("\n1) 브라우저가 열리면 카카오 로그인 → '카카오톡 메시지 전송'에 동의하세요.")
    print("   (안 열리면 아래 주소를 길게 눌러 복사해 브라우저에 붙여 넣으세요)\n")
    print(url + "\n")
    import shutil
    import subprocess
    opener = shutil.which("termux-open-url")
    if opener:
        subprocess.run([opener, url], check=False)
    print("2) 동의하면 '카카오 연결 코드' 화면이 나와요. [코드 복사]를 누르고 여기로 돌아와 붙여 넣으세요.")
    raw = input("코드: ").strip()
    if "code=" in raw:
        raw = (urllib.parse.parse_qs(urllib.parse.urlparse(raw).query).get("code") or [""])[0]
    if not raw:
        print("코드가 비어 있어요. 다시 해 주세요.")
        return 1
    try:
        tok = k.exchange(raw)
    except RuntimeError as e:
        info = getattr(e, "info", {}) or {}
        hint = KOE_HINT.get(info.get("error_code", ""), "")
        print(f"❌ 토큰을 받지 못했어요: {e}" + (f"\n   → {hint}" if hint else ""))
        if info.get("error_code") == "KOE006":
            print("   " + redirect)
        return 1
    try:
        k.send("✅ 투자 레이더와 카카오톡을 연결했어요.\n앞으로 디스코드 알림이 '나와의 채팅'에도 남아요.",
               dashboard_url(repo), "대시보드 열기")
    except Exception as e:  # noqa: BLE001
        print(f"❌ 테스트 메시지를 보내지 못했어요: {e}")
        print("   → [카카오 로그인] > [동의항목]에서 '카카오톡 메시지 전송'을 켜고, `radar kakao` 를 다시 해 주세요.")
        return 1
    days = max(0, int((tok.get("refresh_expires_at", 0) - time.time()) / 86400))
    print(f"✅ 연결 완료! 카카오톡 '나와의 채팅'을 확인하세요. (봇이 쓰는 동안 알아서 연장돼요 · 지금 기준 {days}일)")
    return 0


def main():
    p = argparse.ArgumentParser(description="투자 레이더 봇")
    p.add_argument("--loop", action="store_true", help="계속 실행 (휴대폰용)")
    p.add_argument("--once", action="store_true", help="한 번만 확인")
    p.add_argument("--dry-run", action="store_true", help="디스코드·GitHub 로 보내지 않고 파일도 저장하지 않음")
    p.add_argument("--test-discord", action="store_true", help="디스코드 테스트 메시지 전송")
    p.add_argument("--setup-github", action="store_true", help="저장소에 대시보드·설정·감시 파일 올리기")
    p.add_argument("--kakao-setup", action="store_true", help="카카오톡 '나에게 보내기' 연결")
    p.add_argument("--kakao-reset", action="store_true", help="카카오 키부터 다시 넣고 연결")
    p.add_argument("--kakao-test", action="store_true", help="카카오톡 테스트 메시지 보내기")
    args = p.parse_args()
    load_dotenv()

    if args.kakao_setup or args.kakao_reset:
        return setup_kakao(reset=args.kakao_reset)
    if args.kakao_test:
        key = os.environ.get("KAKAO_REST_KEY", "").strip()
        if not key or not Kakao.linked():
            print("카카오톡이 아직 연결되지 않았어요. `radar kakao` 로 연결하세요.")
            return 1
        try:
            Kakao(key, os.environ.get("KAKAO_CLIENT_SECRET", "").strip()).send(
                "🔔 테스트 알림\n카카오톡 연결이 잘 되어 있어요!", dashboard_url(os.environ.get("GITHUB_REPO", "")))
        except Exception as e:  # noqa: BLE001
            print(f"❌ {e}")
            return 1
        print("✅ 카카오톡 '나와의 채팅'으로 테스트 메시지를 보냈어요.")
        return 0

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
