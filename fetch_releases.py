#!/usr/bin/env python3
"""
국내 극장 개봉 예정작 + OTT(넷플릭스/디즈니+/쿠팡플레이/애플TV+) 공개 예정 '영화'를
TMDB에서 가져와 애플 캘린더 구독용 .ics 파일로 만든다.

필요: 환경변수 TMDB_API_KEY (TMDB v3 API 키 또는 v4 Read Access Token)
외부 패키지 없이 파이썬 표준 라이브러리만 사용.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ───────────────────────── 설정 ─────────────────────────
PAST_DAYS = 14          # 이미 개봉/공개된 작품도 며칠간은 캘린더에 남겨둠
FUTURE_DAYS = 120       # 앞으로 몇 일치 일정을 가져올지
MIN_POPULARITY = 1.0    # 극장 개봉작 인기도 하한 (0이면 전부, 올리면 주요작 위주)
MAX_PAGES = 10          # 카테고리별 TMDB 결과 페이지 수 상한 (페이지당 20개)
STREAM_SCAN_PAGES = 15  # OTT 영화 후보를 훑을 페이지 수 (인기순)
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")

# OTT 서비스 정의 (영화만 수집)
#   match : TMDB 제공처 이름 / 개봉일 메모(note)에 포함되는 문자열 (소문자)
SERVICES = [
    {"key": "netflix", "label": "넷플릭스", "emoji": "🟥", "match": ["netflix", "넷플릭스"]},
    {"key": "disney", "label": "디즈니+", "emoji": "🟦", "match": ["disney+", "disney plus", "디즈니"]},
    {"key": "coupang", "label": "쿠팡플레이", "emoji": "🟧", "match": ["coupang", "쿠팡"]},
    {"key": "appletv", "label": "애플TV+", "emoji": "⬛", "match": ["apple tv", "애플tv", "애플 tv"]},
]
THEATER = {"key": "movies", "label": "극장 개봉", "emoji": "🎬"}

# 극장 개봉작에서 공연 실황(콘서트 필름, 오페라·연극 중계 등) 제외
EXCLUDE_LIVE = True
#   ① 장르가 '다큐멘터리 + 음악' 둘 다인 작품
#   ② TMDB 키워드 (실행 시 이름으로 ID를 찾아 discover에서 제외)
LIVE_KEYWORDS = ["concert film", "concert", "filmed concert", "live concert", "concert tour",
                 "live performance", "filmed theater", "filmed play", "opera performance"]
#   ③ 제목(한글/원제)에 들어가는 단어 — 대소문자 무시
LIVE_TITLE_WORDS = ["콘서트", "실황", "투어", "라이브 인 시네마", "앙코르 상영", "메트 오페라",
                    "메트로폴리탄 오페라", "NT Live", "NT라이브", "concert", "in cinemas", "Met Opera"]
#   ③-2 대문자로 쓸 때만 공연으로 보는 단어 (Live Free 같은 일반 제목 오탐 방지)
LIVE_TITLE_UPPER = ["LIVE", "TOUR"]

# OTT 신규 입고 (판권 작품 등이 OTT에 새로 들어온 날을 기록)
ARRIVALS = {"key": "arrivals", "label": "OTT 신규 입고"}
ARRIVAL_KEEP_DAYS = 60     # 신규 입고 일정을 며칠간 캘린더에 남겨둘지
ARRIVAL_SURGE_LIMIT = 150  # 하루에 이보다 많이 '새로' 잡히면 데이터 오류로 보고 등록하지 않음
CATALOG_MAX_PAGES = 500    # 서비스별 한국 카탈로그를 훑는 페이지 상한 (TMDB 최대 500)
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "_arrivals_state.json")
# ────────────────────────────────────────────────────────

API = "https://api.themoviedb.org/3"
KEY = os.environ.get("TMDB_API_KEY", "").strip()
TODAY = dt.date.today()
START = TODAY - dt.timedelta(days=PAST_DAYS)
END = TODAY + dt.timedelta(days=FUTURE_DAYS)


def api(path, **params):
    """TMDB GET 요청 (재시도 포함)."""
    headers = {"Accept": "application/json"}
    if KEY.startswith("eyJ"):            # v4 Read Access Token
        headers["Authorization"] = f"Bearer {KEY}"
    else:                                 # v3 API key
        params["api_key"] = KEY
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            if e.code == 404:
                return None
            raise
        except urllib.error.URLError:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"TMDB 요청 실패: {path}")


def paged(path, max_pages=None, **params):
    """discover 류 결과를 여러 페이지에 걸쳐 모은다."""
    page, out = 1, []
    while page <= (max_pages or MAX_PAGES):
        data = api(path, page=page, **params) or {}
        out += data.get("results", [])
        if page >= data.get("total_pages", 0):
            break
        page += 1
    return out


def in_window(date_str):
    try:
        d = dt.date.fromisoformat(date_str[:10])
    except (TypeError, ValueError):
        return None
    return d if START <= d <= END else None


def kr_release(movie_id, types):
    """한국 개봉일 중 기간 안에 드는 가장 이른 날짜 (types: 2=제한상영 3=극장 4=디지털)."""
    data = api(f"/movie/{movie_id}/release_dates") or {}
    dates = []
    for c in data.get("results", []):
        if c.get("iso_3166_1") != "KR":
            continue
        for r in c.get("release_dates", []):
            d = in_window(r.get("release_date", ""))
            if r.get("type") in types and d:
                dates.append(d)
    return min(dates) if dates else None


# ───────────────────── 서비스 판별 ─────────────────────
def matches(svc, text):
    t = (text or "").lower()
    return any(m in t for m in svc["match"])


def resolve_provider(svc, providers):
    for p in providers:
        name = p.get("provider_name", "").lower()
        if matches(svc, name) and "basic" not in name and "ads" not in name:
            return p["provider_id"]
    return None


# ───────────────────── 데이터 수집 ─────────────────────
def live_keyword_ids():
    ids = set()
    for name in LIVE_KEYWORDS:
        for k in (api("/search/keyword", query=name) or {}).get("results", []):
            if k.get("name", "").lower() == name:
                ids.add(k["id"])
    return ids


_LIVE_RE = re.compile("|".join(re.escape(w) for w in LIVE_TITLE_WORDS), re.IGNORECASE)
_LIVE_UPPER_RE = re.compile(r"\b(" + "|".join(LIVE_TITLE_UPPER) + r")\b")


def is_live_show(m):
    g = set(m.get("genre_ids") or [])
    if {99, 10402} <= g:                       # 다큐멘터리 + 음악
        return True
    for t in (m.get("title"), m.get("original_title")):
        if t and (_LIVE_RE.search(t) or _LIVE_UPPER_RE.search(t)):
            return True
    return False


def collect_theater():
    print("극장 개봉작 수집…")
    events = []
    extra = {}
    if EXCLUDE_LIVE:
        kw = live_keyword_ids()
        if kw:
            extra["without_keywords"] = "|".join(map(str, sorted(kw)))
    movies = paged("/discover/movie", region="KR", language="ko-KR",
                   with_release_type="2|3", sort_by="popularity.desc",
                   **{"release_date.gte": START.isoformat(),
                      "release_date.lte": END.isoformat()}, **extra)
    seen, skipped = set(), []
    for m in movies:
        if m["id"] in seen or m.get("popularity", 0) < MIN_POPULARITY:
            continue
        seen.add(m["id"])
        if EXCLUDE_LIVE and is_live_show(m):
            skipped.append(m.get("title"))
            continue
        d = kr_release(m["id"], {2, 3})
        if not d:
            continue
        events.append(make_event("movie", m["id"], d, THEATER, m.get("title"),
                                 m.get("original_title"), m.get("overview"), kind="영화"))
    if skipped:
        print(f"  공연 실황으로 제외 {len(skipped)}편: " + ", ".join(map(str, skipped)))
    return events


def collect_streaming_movies(providers):
    """OTT 공개 영화 수집.
    후보 ① 전 세계 디지털(타입 4) 공개 예정작을 인기순으로 훑고
    후보 ② 서비스별 한국 제공처 필터 결과를 더한 뒤,
    각 영화의 개봉일 메모(note, 예: "Netflix")나 제공처로 서비스를 판별한다."""
    print("OTT 영화 수집…")
    window = {"release_date.gte": START.isoformat(), "release_date.lte": END.isoformat()}

    cand = {}   # movie_id -> (movie dict, set of service keys from provider filter)
    for m in paged("/discover/movie", max_pages=STREAM_SCAN_PAGES, with_release_type="4",
                   language="ko-KR", sort_by="popularity.desc", **window):
        cand.setdefault(m["id"], (m, set()))

    for svc in SERVICES:
        pid = resolve_provider(svc, providers)
        if not pid:
            continue
        for m in paged("/discover/movie", watch_region="KR", with_watch_providers=pid,
                       region="KR", with_release_type="4", language="ko-KR",
                       sort_by="popularity.desc", **window):
            cand.setdefault(m["id"], (m, set()))[1].add(svc["key"])

    events = []
    for mid, (m, via_provider) in cand.items():
        rd = (api(f"/movie/{mid}/release_dates") or {}).get("results", [])
        for svc in SERVICES:
            kr, other = [], []
            for c in rd:
                for r in c.get("release_dates", []):
                    if r.get("type") != 4 or not matches(svc, r.get("note")):
                        continue
                    d = in_window(r.get("release_date", ""))
                    if d:
                        (kr if c.get("iso_3166_1") == "KR" else other).append(d)
            date = min(kr) if kr else (min(other) if other else None)
            if not date and svc["key"] in via_provider:
                date = kr_release(mid, {4}) or in_window(m.get("release_date") or "")
            if not date:
                continue
            events.append(make_event("movie", f"{mid}-{svc['key']}", date, svc,
                                     m.get("title"), m.get("original_title"),
                                     m.get("overview"), kind="영화", link_id=mid))
    for svc in SERVICES:
        print(f"  {svc['label']}: {sum(e['feed'] == svc['key'] for e in events)}편")
    return events


def fetch_catalog(pid):
    """서비스의 한국 정액제(flatrate) 영화 카탈로그 전체. (영화 dict 목록, 전부 받았는지)"""
    page, out, total = 1, {}, None
    while page <= CATALOG_MAX_PAGES:
        data = api("/discover/movie", page=page, watch_region="KR", with_watch_providers=pid,
                   with_watch_monetization_types="flatrate", include_adult="false",
                   language="ko-KR", sort_by="primary_release_date.desc") or {}
        if total is None:
            total = data.get("total_results", 0)
        for m in data.get("results", []):
            out[m["id"]] = m
        if page >= data.get("total_pages", 0):
            break
        page += 1
    complete = bool(total) and len(out) >= total * 0.98
    return out, complete


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"services": {}, "arrivals": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))


def collect_arrivals(providers, originals):
    """어제까지 없던 영화가 OTT 한국 카탈로그에 새로 보이면 오늘 날짜로 '신규 입고' 기록.
    서비스별 첫 실행은 현재 카탈로그를 기준선으로 저장만 하고 등록하지 않는다."""
    print("OTT 신규 입고 확인…")
    state = load_state()
    today = TODAY.isoformat()

    for svc in SERVICES:
        pid = resolve_provider(svc, providers)
        if not pid:
            print(f"  ! {svc['label']} 제공처 ID 없음")
            continue
        catalog, complete = fetch_catalog(pid)
        st = state["services"].setdefault(svc["key"], {"baselined": False, "seen": []})
        seen = set(st["seen"])

        if not st["baselined"]:
            if complete:
                st["baselined"] = True
                print(f"  {svc['label']}: 기준선 저장 ({len(catalog)}편) — 내일부터 신규 입고 등록")
            else:
                print(f"  {svc['label']}: 카탈로그를 다 못 받아서 기준선 보류")
            st["seen"] = sorted(seen | set(catalog))
            continue

        new = [catalog[i] for i in catalog if i not in seen]
        if len(new) > ARRIVAL_SURGE_LIMIT:
            print(f"  ! {svc['label']}: 신규 {len(new)}편은 비정상적으로 많아 등록하지 않음")
        else:
            for m in new:
                state["arrivals"].append({
                    "id": m["id"], "svc": svc["key"], "date": today,
                    "title": m.get("title"), "original_title": m.get("original_title"),
                    "overview": m.get("overview"), "release_date": m.get("release_date"),
                })
            print(f"  {svc['label']}: 신규 {len(new)}편")
        st["seen"] = sorted(seen | set(catalog))

    cutoff = (TODAY - dt.timedelta(days=ARRIVAL_KEEP_DAYS)).isoformat()
    state["arrivals"] = [a for a in state["arrivals"] if a["date"] >= cutoff]
    save_state(state)

    by_key = {s["key"]: s for s in SERVICES}
    events = []
    for a in state["arrivals"]:
        svc = by_key.get(a["svc"])
        if not svc or (a["id"], a["svc"]) in originals:
            continue    # 오리지널로 이미 잡힌 작품은 중복 등록하지 않음
        e = make_event("movie", a["id"], dt.date.fromisoformat(a["date"]), svc,
                       a["title"], a["original_title"], a["overview"], kind="신규 입고")
        if a.get("release_date"):
            e["description"] = e["description"].replace(
                "신규 입고", f"신규 입고 (최초 개봉 {a['release_date']})", 1)
        e["uid"] = f"arrivals-{a['svc']}-{a['id']}@release-calendar"
        e["feed"] = ARRIVALS["key"]
        e["category"] = ARRIVALS["label"]
        events.append(e)
    return events


def make_event(media, uid, date, svc, title, original, overview, kind, link_id=None):
    link_id = link_id or uid
    url = f"https://www.themoviedb.org/{media}/{link_id}?language=ko-KR"
    desc_parts = []
    if original and original != title:
        desc_parts.append(f"원제: {original}")
    desc_parts.append(f"{svc['label']} · {kind}")
    if overview:
        desc_parts.append("")
        desc_parts.append(overview)
    desc_parts += ["", url]
    return {
        "uid": f"{svc['key']}-{media}-{uid}@release-calendar",
        "date": date,
        "summary": f"{svc['emoji']} {title}" + ("" if svc is THEATER else f" ({svc['label']})"),
        "description": "\n".join(desc_parts),
        "url": url,
        "category": svc["label"],
        "feed": svc["key"],
        "mid": link_id,
    }


# ───────────────────── ICS 출력 ─────────────────────
def esc(text):
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n"))


def fold(line):
    """RFC 5545: 75옥텟마다 줄바꿈 (UTF-8 문자 중간에서 자르지 않음)."""
    out, cur, size = [], "", 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        limit = 75 if not out else 74   # 이어지는 줄은 앞의 공백 1바이트
        if size + b > limit:
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += b
    out.append(cur)
    return "\r\n ".join(out)


def build_ics(name, events):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//release-calendar//KO", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(name)}", "X-WR-TIMEZONE:Asia/Seoul",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
    ]
    for e in sorted(events, key=lambda x: (x["date"], x["summary"])):
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e['uid']}",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{e['date']:%Y%m%d}",
            f"DTEND;VALUE=DATE:{e['date'] + dt.timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{esc(e['summary'])}",
            f"DESCRIPTION:{esc(e['description'])}",
            f"URL:{e['url']}",
            f"CATEGORIES:{esc(e['category'])}",
            "TRANSP:TRANSPARENT",       # '바쁨'으로 표시되지 않게
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def build_index(feeds):
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        base = f"{owner.lower()}.github.io/{name}"
    else:
        base = None
    rows = []
    for key, label, count in feeds:
        if base:
            href = f"webcal://{base}/{key}.ics"
            link = f'<a class="btn" href="{href}">구독</a>'
            addr = f"<code>https://{base}/{key}.ics</code>"
        else:
            link, addr = "", f"<code>{key}.ics</code>"
        rows.append(f"<li><div><b>{html.escape(label)}</b> <span>{count}건</span><br>{addr}</div>{link}</li>")
    updated = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST")
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>개봉·공개 캘린더</title>
<style>
:root{{--bg:#fafafa;--fg:#111;--muted:#666;--card:#fff;--line:#e5e5e5;--accent:#e50914}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--muted:#999;--card:#1b1b1b;--line:#2a2a2a}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,sans-serif}}
main{{max-width:560px;margin:0 auto;padding:24px 16px}}
h1{{font-size:22px;margin:0 0 4px}} p{{color:var(--muted);margin:0 0 20px}}
ul{{list-style:none;padding:0;margin:0}}
li{{display:flex;align-items:center;justify-content:space-between;gap:12px;background:var(--card);
border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px}}
li span{{color:var(--muted);font-size:13px}} code{{font-size:11px;color:var(--muted);word-break:break-all}}
.btn{{background:var(--accent);color:#fff;text-decoration:none;padding:8px 14px;border-radius:8px;font-weight:600;white-space:nowrap}}
</style></head><body><main>
<h1>개봉·공개 예정 캘린더</h1>
<p>아이폰에서 '구독'을 누르면 캘린더 앱에 추가돼요. 마지막 갱신: {updated}</p>
<ul>{''.join(rows)}</ul>
<p style="margin-top:20px;font-size:12px">데이터: TMDB. This product uses the TMDB API but is not endorsed or certified by TMDB.</p>
</main></body></html>"""


def main():
    if not KEY:
        sys.exit("TMDB_API_KEY 환경변수가 없습니다.")
    os.makedirs(OUT_DIR, exist_ok=True)

    providers = (api("/watch/providers/movie", watch_region="KR", language="ko-KR") or {}).get("results", [])

    streaming = collect_streaming_movies(providers)
    originals = {(e["mid"], e["feed"]) for e in streaming}
    all_events = collect_theater() + streaming + collect_arrivals(providers, originals)

    # 같은 UID 중복 제거
    uniq = {}
    for e in all_events:
        uniq.setdefault(e["uid"], e)
    all_events = list(uniq.values())

    def pick(key):
        return [e for e in all_events if e["feed"] == key]

    feeds = [("all", "전체", all_events), (THEATER["key"], THEATER["label"], pick(THEATER["key"]))]
    feeds += [(s["key"], s["label"], pick(s["key"])) for s in SERVICES]
    feeds.append((ARRIVALS["key"], ARRIVALS["label"], pick(ARRIVALS["key"])))

    names = {"all": "개봉·공개 예정 (전체)", ARRIVALS["key"]: ARRIVALS["label"]}
    for key, label, evs in feeds:
        cal_name = names.get(key, f"{label} 예정작")
        with open(os.path.join(OUT_DIR, f"{key}.ics"), "w", encoding="utf-8", newline="") as f:
            f.write(build_ics(cal_name, evs))
        print(f"  {key}.ics: {len(evs)}건")

    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_index([(k, l, len(e)) for k, l, e in feeds]))
    print("완료")


if __name__ == "__main__":
    main()
