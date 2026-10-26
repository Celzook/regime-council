# -*- coding: utf-8 -*-
"""웹 공유본(GitHub Pages) — 지금 화면 전체를 '보기 전용' 정적 사이트로 docs/ 에 만든다.

서버(web.py)가 켜져 있어야 한다. 화면이 부르는 GET 주소의 응답을 모두 받아 docs/snap.json 에 담고,
백테스트는 전략별 기본 설정 × 최근 기간(1·3·5·10년·올해·전체)과 기간별 전체 비교를 미리 계산해 docs/bt.json 에 담는다.
docs/index.html = static/index.html + 응답 가로채기(서버 대신 snap.json·bt.json) + '공유 보기' 안내 한 줄.
점검 실행·저장·대화처럼 서버가 필요한 버튼은 '원본 앱에서 실행' 안내만 한다. 회사 이름 표시는 뺀다(공개 페이지).

⚠ 2026-10-23 부터 시장 데이터가 Refinitiv 라이선스 — 준법 확인 전에는 실행하지 않는다(--force 없이는 멈춤).
사용법: python share_site.py --force  → git add docs share_site.py && git commit && git push  (Pages 소스 = main 브랜치 /docs)
       python share_site.py --html  → 화면(index.html)만 다시 만든다(숫자는 그대로, 서버 불필요)
       python share_site.py --file [--out 경로] [--no-org]  → 사내 전달용 HTML 한 파일(더블클릭으로 열림, 서버·인터넷 불필요).
           docs/ 는 건드리지 않는다. 기본 저장 위치 data/share/(git 제외). 회사 이름 표시는 남긴다(--no-org 로 뺌).
공유본(docs/·한 파일)은 둘 다 이 PC 폴더 경로를 파일 이름만 남기고(scrub), 사내 공유 보기에서 숨기는 화면(config.LAN_HIDE)은 담지 않는다.
"""
import base64
import datetime
import json
import re
import math
import shutil
import sys
import urllib.parse
from pathlib import Path

import requests

import config

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs"
BASE = "http://127.0.0.1:5101"
PRESETS = ["1y", "3y", "5y", "10y", "ytd", "all"]

SHIM = """<script>
// 공유 보기(GitHub Pages) — 서버 대신 저장된 응답(snap.json · bt.json)으로 화면을 그린다. 서버가 필요한 버튼은 안내만.
(function(){
  const realFetch = window.fetch.bind(window);
  let snap = null, bt = null;
  const load = async f => (await realFetch(f, {cache: 'no-cache'})).json();
  const reply = (obj, status = 200) => new Response(JSON.stringify(obj), {status, headers: {'Content-Type': 'application/json'}});
  const RO = '공유 보기(읽기 전용)라 이 버튼은 동작하지 않습니다 — 원본 앱에서 실행하세요.';
  const BT_NOTE = '공유 보기에서는 각 전략의 기본 설정 + 최근 기간(1·3·5·10년·올해·전체)만 볼 수 있습니다. 다른 설정·과거 국면·날짜 지정은 원본 앱에서 돌려 주세요.';
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    if(!url.startsWith('/api/')) return realFetch(input, init);
    const method = ((init && init.method) || 'GET').toUpperCase();
    snap = snap || await load('snap.json');
    if(method === 'GET') return url in snap.get ? reply(snap.get[url]) : reply({error: '공유 보기에 담기지 않은 데이터입니다.'}, 404);
    let body = {}; try { body = JSON.parse((init && init.body) || '{}'); } catch(e){}
    if(url === '/api/backtest/run' || url === '/api/backtest/all'){
      bt = bt || await load('bt.json');
      if(url === '/api/backtest/all') return bt.all[body.period] ? reply(bt.all[body.period]) : reply({error: BT_NOTE}, 400);
      const s = snap.get['/api/backtest/catalog'].strategies.find(x => x.id === body.id);
      const same = (v, d) => String(v) === String(d) || (v !== '' && v !== null && Number(v) === Number(d));
      const dflt = s && s.params.every(p => same((body.params || {})[p.key], p.default));
      const r = dflt && bt.run[body.id + '|' + body.period];
      return r ? reply(r) : reply({error: BT_NOTE}, 400);
    }
    if(url === '/api/monthly'){ const m = snap.monthly[body.month || Object.keys(snap.monthly)[0]]; if(m) return reply(m); }
    return reply({error: RO}, 403);
  };
})();
</script>"""

# 한 파일본: 응답을 <script type="application/json" id="snap.json"> 블록에서 읽는다(file:// 에선 fetch 로 파일을 못 읽는다)
FILE_LOAD = "const load = async f => JSON.parse(document.getElementById(f).textContent);"

NOTE = """<div style="font-size:12.5px;color:var(--muted);margin:-6px 0 16px">공유 보기 · {at} 기준 화면 — 숫자는 이 시점에 고정돼 있고, 점검 실행·저장·대화처럼 서버가 필요한 버튼은 원본 앱에서만 동작합니다.</div>"""


FILE_NOTE = """<div style="font-size:12.5px;color:var(--muted);margin:-6px 0 16px">보기 전용 스냅샷 · {at} 생성 — 숫자는 이 시점에 고정돼 있고, 점검 실행·저장·대화처럼 서버가 필요한 버튼은 원본 앱에서만 동작합니다. · Refinitiv(LSEG) 라이선스 데이터 포함 — 사내 라이선스 범위 안에서만 전달하세요.</div>"""


def q(s) -> str:
    """JS encodeURIComponent 와 같은 인코딩 — 화면이 부르는 주소 그대로 키로 쓴다."""
    return urllib.parse.quote(str(s), safe="-_.!~*'()")


def clean(x):
    """NaN·무한대는 JSON 에서 null 로(브라우저 JSON.parse 가 NaN 을 못 읽는다)."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [clean(v) for v in x]
    return x


def get(path):
    r = requests.get(BASE + path, timeout=180)
    r.raise_for_status()
    return r.json()


def post(path, body):
    r = requests.post(BASE + path, json=body, timeout=900)
    r.raise_for_status()
    return r.json()


def rep(s: str, old: str, new: str) -> str:
    assert s.count(old) == 1, old[:60]
    return s.replace(old, new)


def collect():
    """화면이 부르는 GET 응답 + 월간 보고 + 백테스트(기본 설정 × 최근 기간)를 서버에서 받는다."""
    snap = {}

    def take(path):
        if any(path.startswith(x) for x in config.LAN_HIDE):   # 사내 공유 보기에서 숨기는 화면은 공유본에도 넣지 않는다
            return None
        try:
            snap[path] = get(path)
        except Exception as e:  # noqa: BLE001 — 한 주소 실패가 전체를 막지 않게
            print(f"  건너뜀 {path}: {e}")
        return snap.get(path)

    for p in ["/api/overview", "/api/room", "/api/alerts", "/api/log?limit=150", "/api/book", "/api/quant", "/api/strategy",
              "/api/equity", "/api/equity/fund", "/api/equity/valhist", "/api/universe/spark", "/api/backtest/catalog", "/api/macro",
              "/api/timing", "/api/timing/spikes", "/api/funds"]:
        take(p)
    for a in ("bond", "equity", "fx"):
        take(f"/api/analog?asset={a}")
    for d in take("/api/discussions") or []:
        take("/api/discussions/" + q(d["name"]))
    for c in take("/api/checks") or []:
        take("/api/checks/" + q(c["report_id"]))
    for m in take("/api/mp") or []:
        take("/api/mp/" + q(m["mp_id"]))
    for n in (take("/api/state") or {}).get("reports") or []:
        take("/api/report/" + q(n))
    for p in ("/api/equity", "/api/equity/fund", "/api/macro"):   # 공유본은 고정 — '받는 중' 표시·다시 부르기를 끈다
        if isinstance(snap.get(p), dict):
            snap[p]["refreshing"] = False
            snap[p].pop("job_id", None)
    month = datetime.date.today().strftime("%Y-%m")
    mrep = ROOT / "reports" / f"투자위보고_{month}.md"   # /api/monthly 는 이 파일을 다시 쓴다 — 공유본을 만들 때는 원래대로 되돌린다
    before = mrep.read_bytes() if mrep.exists() else None
    try:
        monthly = {month: post("/api/monthly", {"month": month})}
    finally:
        if before is None:
            mrep.unlink(missing_ok=True)
        elif mrep.read_bytes() != before:
            mrep.write_bytes(before)

    cat = snap["/api/backtest/catalog"]
    presets = {p["value"]: p for p in cat["presets"]}
    runs = {}
    for s in (x for x in cat["strategies"] if x["available"]):
        params = {p["key"]: p["default"] for p in s["params"]}
        for key in PRESETS:
            p = presets[key]
            try:
                runs[f"{s['id']}|{key}"] = post("/api/backtest/run", {"id": s["id"], "params": params, "period": key, "start": p["start"], "end": p["end"]})
            except Exception as e:  # noqa: BLE001 — 데이터가 짧아 못 도는 기간은 빼고 간다
                print(f"  백테스트 건너뜀 {s['id']} {key}: {e}")
        print(f"  백테스트 {s['id']} 끝")
    alls = {}
    for key in [k for k in PRESETS if k != "10y"] + ["10y"]:   # 10년을 마지막에 — 서버의 저장 요약이 기본(10년)으로 남게
        p = presets[key]
        alls[key] = post("/api/backtest/all", {"period": key, "start": p["start"], "end": p["end"]})
    cat["summary"] = alls["10y"]
    if isinstance(snap.get("/api/equity"), dict):
        snap["/api/equity"]["bt_summary"] = alls["10y"]

    at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return {"at": at, "get": snap, "monthly": monthly}, {"run": runs, "all": alls}


def dump(o) -> str:
    return json.dumps(clean(o), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def build():
    snap, bt = collect()
    snap, bt = scrub(snap), scrub(bt)   # 공개 페이지에도 이 PC 폴더 경로가 남지 않게(build_file 과 같게)
    at, runs = snap["at"], bt["run"]
    OUT.mkdir(exist_ok=True)
    (OUT / "snap.json").write_text(dump(snap), encoding="utf-8")
    (OUT / "bt.json").write_text(dump(bt), encoding="utf-8")
    for sub in ("vendor", "avatars"):
        shutil.copytree(ROOT / "static" / sub, OUT / "static" / sub, dirs_exist_ok=True)
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    write_html(at)
    print(f"주소 {len(snap['get'])}개 · 백테스트 {len(runs)}개 · 전체 비교 {len(bt['all'])}개 → {OUT} ({at})")


def write_html(at: str):
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    html = rep(html, '<span class="org">한화생명</span>', "")
    html = rep(html, '<script src="static/vendor/purify.min.js"></script>', '<script src="static/vendor/purify.min.js"></script>\n' + SHIM)
    html = rep(html, "<main>", "<main>\n" + NOTE.format(at=at))
    (OUT / "index.html").write_text(html, encoding="utf-8")


def json_block(id_: str, o) -> str:
    """JSON 을 <script> 안에 그대로 넣을 수 있게 — '<' 를 \\u003c 로(</script>·<!-- 가 블록을 끊지 못한다, JSON 값은 같다)."""
    return f'<script type="application/json" id="{id_}">' + dump(o).replace("<", "\\u003c") + "</script>"


def file_html(snap: dict, bt: dict, keep_org: bool = True) -> str:
    """한 파일본 — 응답 JSON·외부 스크립트·아바타 그림을 모두 페이지 안에 넣는다(요청은 하나도 나가지 않는다)."""
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    if not keep_org:
        html = rep(html, '<span class="org">한화생명</span>', "")
    shim = rep(SHIM, "const load = async f => (await realFetch(f, {cache: 'no-cache'})).json();", FILE_LOAD)
    tag = '<script src="static/vendor/purify.min.js"></script>'
    html = rep(html, tag, tag + "\n" + json_block("snap.json", snap) + "\n" + json_block("bt.json", bt) + "\n" + shim)
    html = rep(html, "<main>", "<main>\n" + FILE_NOTE.format(at=snap["at"]))
    for f in sorted((ROOT / "static" / "avatars").glob("*.svg")):   # 같은 그림이 여러 곳(태그·JS 상수)에 있어 전부 바꾼다
        html = html.replace(f"static/avatars/{f.name}", "data:image/svg+xml;base64," + base64.b64encode(f.read_bytes()).decode())
    for name in ("marked.min.js", "purify.min.js"):   # 마지막에 — 라이브러리 본문 안 글자가 위 치환에 걸리지 않게
        js = (ROOT / "static" / "vendor" / name).read_text(encoding="utf-8")
        assert "</script" not in js.lower(), name
        html = rep(html, f'<script src="static/vendor/{name}"></script>', "<script>" + js + "</script>")
    return html


# 드라이브 경로(C:\, D:/ …)·\\wsl 등 네트워크 경로·/home/·/mnt/ — 앞이 글자·':'·'/' 면 주소(https://…)의 일부라 건너뛴다.
# 중간 폴더 이름의 공백은 다음 구분자까지 허용(따옴표·줄바꿈 전까지), 마지막 이름은 공백 전까지.
LOCAL_PATH = re.compile(r"""(?<![\w:/.\\])(?:[A-Za-z]:[\\/]|\\\\|/home/|/mnt/)(?:[^\\/\n"'<>|]*[\\/])*[^\s\\/"'<>|]*""")


def scrub(x):
    """전달용 파일에 이 PC 의 폴더 경로(사용자 이름·폴더 구조)가 남지 않게 — 경로 문자열은 파일 이름만 남긴다."""
    if isinstance(x, dict):
        return {k: scrub(v) for k, v in x.items()}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    if isinstance(x, str):   # 문장 속에 섞인 경로도 그 부분만 파일 이름으로
        return LOCAL_PATH.sub(lambda m: re.split(r"[\\/]", m.group(0))[-1], x)
    return x


def build_file(out=None, keep_org: bool = True):
    snap, bt = collect()
    snap, bt = scrub(snap), scrub(bt)
    out = Path(out) if out else ROOT / "data" / "share" / f"대시보드_스냅샷_{snap['at'][:10].replace('-', '')}_{snap['at'][11:].replace(':', '')}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(file_html(snap, bt, keep_org), encoding="utf-8")
    print(f"주소 {len(snap['get'])}개 · 백테스트 {len(bt['run'])}개 · 전체 비교 {len(bt['all'])}개 → {out} ({out.stat().st_size / 1e6:.1f} MB)")
    print("⚠ Refinitiv 라이선스 데이터 포함 — 사내 라이선스 범위 안에서만 전달하세요.")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--file" in sys.argv:   # 사내 전달용 한 파일 — docs/(공개 페이지)는 건드리지 않으므로 --force 불필요
        a = sys.argv
        build_file(a[a.index("--out") + 1] if "--out" in a else None, keep_org="--no-org" not in a)
        raise SystemExit
    # 시장 데이터가 Refinitiv(LSEG) 라이선스로 바뀌어(2026-10-23) 공개 페이지에 숫자를 담으면 재배포가 된다.
    # 준법 확인 전에는 멈춘다. 확인을 받았으면 --force 로 실행한다.
    if "--force" not in sys.argv:
        raise SystemExit("중단: 공유본(docs/)에 Refinitiv 시세가 담긴다 — 라이선스(재배포) 확인 후 --force 로 실행하세요.")
    if "--html" in sys.argv:   # 화면만 — 기준 시각은 이미 만든 snap.json 그대로
        write_html(json.loads((OUT / "snap.json").read_text(encoding="utf-8"))["at"])
        print("화면만 다시 만듦 →", OUT / "index.html")
    else:
        build()
