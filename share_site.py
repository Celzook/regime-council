# -*- coding: utf-8 -*-
"""웹 공유본(GitHub Pages) — 지금 화면 전체를 '보기 전용' 정적 사이트로 docs/ 에 만든다.

서버(web.py)가 켜져 있어야 한다. 화면이 부르는 GET 주소의 응답을 모두 받아 docs/snap.json 에 담고,
백테스트는 전략별 기본 설정 × 최근 기간(1·3·5·10년·올해·전체)과 기간별 전체 비교를 미리 계산해 docs/bt.json 에 담는다.
docs/index.html = static/index.html + 응답 가로채기(서버 대신 snap.json·bt.json) + '공유 보기' 안내 한 줄.
점검 실행·저장·대화처럼 서버가 필요한 버튼은 '원본 앱에서 실행' 안내만 한다. 회사 이름 표시는 뺀다(공개 페이지).

사용법: python share_site.py  → git add docs share_site.py && git commit && git push  (Pages 소스 = main 브랜치 /docs)
       python share_site.py --html  → 화면(index.html)만 다시 만든다(숫자는 그대로, 서버 불필요)
"""
import datetime
import json
import math
import shutil
import sys
import urllib.parse
from pathlib import Path

import requests

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

NOTE = """<div style="font-size:12.5px;color:var(--muted);margin:-6px 0 16px">공유 보기 · {at} 기준 화면 — 숫자는 이 시점에 고정돼 있고, 점검 실행·저장·대화처럼 서버가 필요한 버튼은 원본 앱에서만 동작합니다.</div>"""


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


def build():
    snap = {}

    def take(path):
        try:
            snap[path] = get(path)
        except Exception as e:  # noqa: BLE001 — 한 주소 실패가 전체를 막지 않게
            print(f"  건너뜀 {path}: {e}")
        return snap.get(path)

    for p in ["/api/overview", "/api/room", "/api/alerts", "/api/log?limit=150", "/api/book", "/api/quant", "/api/strategy",
              "/api/equity", "/api/equity/fund", "/api/backtest/catalog", "/api/macro"]:
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
    monthly = {month: post("/api/monthly", {"month": month})}

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
    OUT.mkdir(exist_ok=True)
    dump = lambda o: json.dumps(clean(o), ensure_ascii=False, separators=(",", ":"), allow_nan=False)  # noqa: E731
    (OUT / "snap.json").write_text(dump({"at": at, "get": snap, "monthly": monthly}), encoding="utf-8")
    (OUT / "bt.json").write_text(dump({"run": runs, "all": alls}), encoding="utf-8")
    for sub in ("vendor", "avatars"):
        shutil.copytree(ROOT / "static" / sub, OUT / "static" / sub, dirs_exist_ok=True)
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    write_html(at)
    print(f"주소 {len(snap)}개 · 백테스트 {len(runs)}개 · 전체 비교 {len(alls)}개 → {OUT} ({at})")


def write_html(at: str):
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    html = rep(html, '<span class="org">한화생명</span>', "")
    html = rep(html, '<script src="static/vendor/purify.min.js"></script>', '<script src="static/vendor/purify.min.js"></script>\n' + SHIM)
    html = rep(html, "<main>", "<main>\n" + NOTE.format(at=at))
    (OUT / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if "--html" in sys.argv:   # 화면만 — 기준 시각은 이미 만든 snap.json 그대로
        write_html(json.loads((OUT / "snap.json").read_text(encoding="utf-8"))["at"])
        print("화면만 다시 만듦 →", OUT / "index.html")
    else:
        build()
