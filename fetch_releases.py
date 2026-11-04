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
def collect_theater():
    print("극장 개봉작 수집…")
    events = []
    movies = paged("/discover/movie", region="KR", language="ko-KR",
                   with_release_type="2|3", sort_by="popularity.desc",
                   **{"release_date.gte": START.isoformat(),
                      "release_date.lte": END.isoformat()})
    seen = set()
    for m in movies:
        if m["id"] in seen or m.get("popularity", 0) < MIN_POPULARITY:
            continue
        seen.add(m["id"])
        d = kr_release(m["id"], {2, 3})
        if not d:
            continue
        events.append(make_event("movie", m["id"], d, THEATER, m.get("title"),
                                 m.get("original_title"), m.get("overview"), kind="영화"))
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

    all_events = collect_theater() + collect_streaming_movies(providers)

    # 같은 UID 중복 제거
    uniq = {}
    for e in all_events:
        uniq.setdefault(e["uid"], e)
    all_events = list(uniq.values())

    feeds = [("all", "전체", all_events), (THEATER["key"], THEATER["label"],
             [e for e in all_events if e["feed"] == THEATER["key"]])]
    feeds += [(s["key"], s["label"], [e for e in all_events if e["feed"] == s["key"]]) for s in SERVICES]

    for key, label, evs in feeds:
        cal_name = "개봉·공개 예정 (전체)" if key == "all" else f"{label} 예정작"
        with open(os.path.join(OUT_DIR, f"{key}.ics"), "w", encoding="utf-8", newline="") as f:
            f.write(build_ics(cal_name, evs))
        print(f"  {key}.ics: {len(evs)}건")

    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_index([(k, l, len(e)) for k, l, e in feeds]))
    print("완료")


if __name__ == "__main__":
    main()
