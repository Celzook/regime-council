# -*- coding: utf-8 -*-
"""Refinitiv(LSEG) Workspace 시세 접근 — 시장 데이터 수집은 이 모듈 하나를 거친다.

- 데스크톱 세션: Workspace 가 켜져 로그인돼 있어야 하고, Windows 파이썬(python.exe)에서만 붙는다.
  프록시가 Windows loopback 127.0.0.1:9000 에만 열려 WSL 리눅스 파이썬에서는 닿지 않는다.
- App Key: 환경변수 EIKON_APP_KEY → config.REFINITIV_APPKEY_FILES 순서로 찾는다. 코드에 박지 않는다.
- 키 체계: 호출하는 쪽은 기존 yfinance 표기(^KS11, 069500.KS, SPY, KRW=X …)를 그대로 쓰고
  여기서 RIC 으로 바꾼다. 그래서 CSV 열 이름·backtest 의 UNDER 표는 바뀌지 않는다.
- 이 계정에서 값이 전혀 안 나오는 VIX 만 YF_ONLY 로 yfinance 에 남긴다(2026-10-23 실측).
  .SPX·.VXN·.N225 처럼 get_timeseries 가 'no permission' 인 지수는 get_data TR.PriceClose 일별로 받는다.
- 주식·ETF 는 TR.TotalReturn 일별 수익률을 누적해 '배당 재투자 가격'을 만든다(yfinance auto_adjust 와 같은 뜻).
  지수·환율·금리·원자재는 CLOSE 그대로.
"""
import datetime
import logging
import os
import socket
import threading
import urllib.error
import urllib.request
import warnings
from pathlib import Path

import config

# yfinance 표기 → RIC. 여기 없는 6자리.KS / .SS 코드는 그대로 RIC 이고, 미국 티커는 _us_ric 으로 찾는다.
TICKER_RIC = {
    "^KS11": ".KS11", "^KQ11": ".KQ11", "^KS200": ".KS200",
    "^IXIC": ".IXIC", "^NDX": ".NDX", "^N225": ".N225", "^STOXX50E": ".STOXX50E",
    "^HSI": ".HSI", "^NSEI": ".NSEI", "^TWII": ".TWII", "000300.SS": ".CSI300",
    "^GSPC": ".SPX", "^VXN": ".VXN",
    "^IRX": "US3MT=RR", "^TNX": "US10YT=RR",
    "KRW=X": "KRW=KFTC",   # 서울외환중개 현물 — KOSPI 와 같은 세션 마감(KRW= 은 뉴욕 마감이라 하루 어긋난다)
    "JPY=X": "JPY=", "EURUSD=X": "EUR=", "CNY=X": "CNY=",
    "JPYKRW=X": "JPYKRW=R", "DX-Y.NYB": ".DXY",
    "CL=F": "CLc1", "GC=F": "XAU=",
}
# 미국 ETF 주 상장 RIC — TR.PrimaryQuote 로 확인한 값(2026-10-23). Arca(.P) 시세로 받으면 나스닥 상장 ETF 종가가 틀린다.
US_RIC = {
    "SPY": "SPY.P", "EEM": "EEM.P", "SCHD": "SCHD.P", "USMV": "USMV.Z", "QUAL": "QUAL.Z",
    "VLUE": "VLUE.Z", "IWF": "IWF.P", "MTUM": "MTUM.Z", "XLF": "XLF.P", "XLV": "XLV.P",
    "XLK": "XLK.P", "SOXX": "SOXX.OQ", "XLE": "XLE.P", "ICLN": "ICLN.OQ", "PAVE": "PAVE.Z",
    "PFF": "PFF.OQ", "AAXJ": "AAXJ.OQ", "IEF": "IEF.OQ", "LQD": "LQD.P", "CWB": "CWB.P",
    "EMLC": "EMLC.P", "EWY": "EWY.P",
}
# Refinitiv 에서 어느 경로로도 값이 안 나오는 것 → yfinance 유지(사용자 결정 2026-10-23).
# .SPX·.VXN·.N225 는 get_timeseries 권한은 없지만 get_data TR.PriceClose 일별로는 나온다(_close_series 폴백).
YF_ONLY = {"^VIX"}

_LOCK = threading.Lock()
_EK = None
_STATE = {"ok": None, "checked": None, "error": ""}
_US_CACHE: dict[str, str] = {}


def _appkey() -> str:
    key = os.environ.get("EIKON_APP_KEY", "").strip()
    if key:
        return key
    for p in getattr(config, "REFINITIV_APPKEY_FILES", []):
        try:
            key = Path(p).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if key:
            return key
    raise RuntimeError("Refinitiv App Key 없음 — 환경변수 EIKON_APP_KEY 또는 config.REFINITIV_APPKEY_FILES")


def _ek():
    """eikon 을 pandas 3.0 shim 뒤에 불러온다(eikon 1.1.18 이 삭제된 to_numeric(errors='ignore') 를 호출)."""
    global _EK
    if _EK is not None:
        return _EK
    import pandas as pd

    if not getattr(pd.to_numeric, "_ek_shim", False):
        orig = pd.to_numeric

        def compat(arg, errors="raise", **kw):
            if errors == "ignore":
                try:
                    return orig(arg, errors="raise", **kw)
                except (ValueError, TypeError):
                    return arg
            return orig(arg, errors=errors, **kw)
        compat._ek_shim = True
        pd.to_numeric = compat
    warnings.filterwarnings("ignore", module="eikon")
    import eikon as ek

    logging.getLogger("pyeikon").setLevel(logging.CRITICAL)
    ek.set_app_key(_appkey())
    _EK = ek
    return ek


def _call(fn, *args, **kw):
    """ek 호출 직렬화 + 일시 오류 재시도. 'Backend error. 400'·타임아웃이 간헐적으로 난다(2026-10-23 실측)."""
    import time

    last = None
    for i in range(3):
        try:
            with _LOCK:
                return fn(*args, **kw)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "Invalid RIC" in msg or "permission" in msg or "No data available" in msg:
                raise
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def available(force: bool = False) -> bool:
    """프록시 포트가 열려 있고 세션이 붙는지. 5분 캐시."""
    now = datetime.datetime.now()
    if not force and _STATE["checked"] and (now - _STATE["checked"]).seconds < 300:
        return bool(_STATE["ok"])
    ok, err = False, ""
    s = socket.socket()
    s.settimeout(2)
    try:
        if s.connect_ex(("127.0.0.1", 9000)) != 0:
            err = "Workspace 프록시(127.0.0.1:9000) 닫힘 — Workspace 실행·로그인 확인, Windows 파이썬으로 실행"
        else:
            # 포트는 열려 있는데 프록시가 멈춘 경우(2026-10-24 실측)에도 화면이 매달리지 않게 — 응답·잠금 모두 시간 제한
            try:
                urllib.request.urlopen("http://127.0.0.1:9000/api/status", timeout=3).close()
            except urllib.error.HTTPError:
                pass  # 상태 코드가 오면 살아 있음
            except OSError as e:
                raise RuntimeError(f"Workspace 프록시 응답 없음({e}) — Workspace 재시작·재로그인") from None
            if not _LOCK.acquire(timeout=3):
                raise RuntimeError("이전 Refinitiv 요청이 끝나지 않음 — Workspace 응답 확인")
            try:
                _ek()
            finally:
                _LOCK.release()
            ok = True
    except Exception as e:  # noqa: BLE001
        err = f"Refinitiv 연결 실패: {e}"
    finally:
        s.close()
    _STATE.update(ok=ok, checked=now, error=err)
    return ok


def status() -> dict:
    return {"ok": _STATE["ok"], "checked": _STATE["checked"].isoformat(timespec="seconds") if _STATE["checked"] else None,
            "error": _STATE["error"]}


def is_level(ric: str) -> bool:
    """지수(.xxx)·환율/금리/원자재(=, c1) 는 CLOSE 레벨, 그 밖(주식·ETF)은 총수익 가격."""
    return ric.startswith(".") or "=" in ric or ric.endswith(("c1", "c2"))


def _us_ric(ticker: str) -> str:
    """처음 보는 미국 티커 → 주 상장 RIC. 접미어 후보로 존재를 확인하고 TR.PrimaryQuote 로 바꾼다."""
    if ticker in US_RIC:
        return US_RIC[ticker]
    if ticker in _US_CACHE:
        return _US_CACHE[ticker]
    ek = _ek()
    cands = [f"{ticker}.{s}" for s in ("P", "O", "N", "Z", "K")]
    df, _ = _call(ek.get_data, cands, ["TR.CommonName", "TR.PrimaryQuote"])
    ric = ""
    for _, row in df.iterrows():
        pq = str(row.iloc[2] or "").strip()
        if pq and pq not in ("<NA>", "nan"):
            ric = pq
            break
    if not ric:
        raise RuntimeError(f"{ticker}: Refinitiv RIC 을 찾지 못함")
    _US_CACHE[ticker] = ric
    return ric


_KNOWN_RICS = set(TICKER_RIC.values()) | set(US_RIC.values())


def ric_for(ticker: str) -> str:
    """yfinance 표기 → RIC. 이미 RIC 이면(알려진 값·거래소 접미어·=·선물 연속물) 그대로."""
    if ticker in TICKER_RIC:
        return TICKER_RIC[ticker]
    if ticker.endswith(("=X", "=F")) or ticker.startswith("^"):
        # yfinance 식 환율·선물·지수 표기는 표에서만 바꾼다 — 그대로 넘기면 엉뚱한 RIC 이 조용히 값을 줄 수 있다(JPY=X 실측)
        raise KeyError(f"{ticker}: TICKER_RIC 에 RIC 매핑이 없다")
    if (ticker in _KNOWN_RICS or "=" in ticker or ticker.startswith(".")
            or ticker.endswith((".KS", ".KQ", ".SS", ".HK", ".T", ".P", ".O", ".OQ", ".N", ".Z", ".K"))
            or (len(ticker) > 2 and ticker[-2] == "c" and ticker[-1].isdigit())):
        return ticker
    return _us_ric(ticker)


def _today_cut(s, ric: str):
    """장중이면 당일 미완성 봉을 뺀다. 한국물은 16시(KST) 이후면 당일 종가로 본다."""
    now = datetime.datetime.now()
    today = now.date()
    korean = ric.endswith((".KS", ".KQ", "=KQ", "=KFTC")) or ric in (".KS11", ".KQ11", ".KS200", ".KS200TR", ".KSVKOSPI")
    if korean and now.hour >= 16:
        return s
    return s[s.index.date < today]


def _empty():
    import pandas as pd
    return pd.Series(dtype=float, index=pd.DatetimeIndex([]))


def _chunks(start: datetime.date, end: datetime.date, years: int = 5):
    cur = start
    while cur <= end:
        nxt = min(datetime.date(cur.year + years, 1, 1) - datetime.timedelta(days=1), end)
        yield cur, nxt
        cur = nxt + datetime.timedelta(days=1)


def _close_series(ric: str, start: datetime.date, end: datetime.date):
    """일별 종가. get_timeseries 가 권한 오류면 get_data TR.PriceClose 일별로 다시 받는다."""
    import pandas as pd

    ek = _ek()
    parts = []
    try:
        for a, b in _chunks(start, end):
            try:
                t = _call(ek.get_timeseries, ric, fields=["CLOSE"], start_date=a.isoformat(),
                          end_date=b.isoformat(), interval="daily")
            except Exception as e:  # noqa: BLE001 — 구간에 데이터가 없으면 예외로 온다
                if "No data available" in str(e):
                    continue
                raise
            if t is None:
                raise RuntimeError(f"{ric}: 응답 없음 — Workspace 연결 확인({status()['error'] or '프록시'})")
            if len(t):
                parts.append(t["CLOSE"] if "CLOSE" in t else t.iloc[:, 0])
    except Exception as e:  # noqa: BLE001
        if "permission" not in str(e):
            raise
        return _priceclose_series(ric, start, end)
    if not parts:
        return _empty()
    s = pd.concat(parts)
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = pd.to_numeric(s, errors="coerce").dropna()
    return s[~s.index.duplicated(keep="last")].sort_index()


def _priceclose_series(ric: str, start: datetime.date, end: datetime.date):
    """get_data TR.PriceClose 일별. 휴장일엔 직전 거래일 값이 그 날짜로 다시 오므로 반환 날짜로 중복을 없앤다."""
    import pandas as pd

    ek = _ek()
    parts = []
    for a, b in _chunks(start, end):
        d, _ = _call(ek.get_data, ric, ["TR.PriceClose.date", "TR.PriceClose"],
                     {"SDate": a.isoformat(), "EDate": b.isoformat(), "Frq": "D"})
        if d is None or not len(d):
            continue
        r = pd.Series(pd.to_numeric(d.iloc[:, 2], errors="coerce").values,
                      index=pd.to_datetime(d.iloc[:, 1], errors="coerce", utc=True))
        parts.append(r[r.index.notna()].dropna())
    if not parts:
        raise RuntimeError(f"{ric}: 시계열·TR.PriceClose 모두 값 없음(권한 없음)")
    s = pd.concat(parts)
    s.index = s.index.tz_localize(None).normalize()
    return s[~s.index.duplicated(keep="last")].sort_index()


def _tr_series(ric: str, start: datetime.date, end: datetime.date, anchor_ric: str | None = None):
    """TR.TotalReturn 일별 % → 누적 지수. 마지막 값을 최근 종가에 맞춰 '배당 재투자 가격'으로 만든다.
    TR 값이 빈 구간은 종가 수익률로 메운다(그 구간 배당만 빠진다). anchor_ric 을 주면 레벨은 그 RIC 의 종가에 맞춘다
    (미국 ETF: 수익률은 이력이 긴 통합 RIC, 레벨은 주 상장 RIC 의 공식 종가)."""
    import pandas as pd

    ek = _ek()
    parts = []
    for a, b in _chunks(start, end):
        d, _ = _call(ek.get_data, ric, ["TR.TotalReturn.date", "TR.TotalReturn"],
                     {"SDate": a.isoformat(), "EDate": b.isoformat(), "Frq": "D"})
        if d is None or not len(d):
            continue
        r = pd.Series(pd.to_numeric(d.iloc[:, 2], errors="coerce").values,
                      index=pd.to_datetime(d.iloc[:, 1], errors="coerce", utc=True))
        parts.append(r[r.index.notna()].dropna())
    close = _today_cut(_close_series(ric, start, end), ric)
    rt = pd.Series(dtype=float)
    if parts:
        rt = pd.concat(parts)
        rt.index = rt.index.tz_localize(None).normalize()
        rt = rt[~rt.index.duplicated(keep="last")].sort_index() / 100
    rc = close.pct_change()
    idx = rt.index.union(rc.index)
    r = rt.reindex(idx).fillna(rc.reindex(idx)).dropna()
    r = _today_cut(r, ric)
    if r.empty:
        return _empty()
    level = (1 + r).cumprod()
    # 기준 종가도 당일 미완성 봉을 뺀 뒤에 맞춘다 — 장중 지연가에 맞추면 전 구간 레벨이 어긋난다
    if anchor_ric and anchor_ric != ric:
        close = _today_cut(_close_series(anchor_ric, max(start, end - datetime.timedelta(days=20)), end), anchor_ric)
    if len(close):
        base = level[level.index <= close.index[-1]]
        if len(base):
            level = level * float(close.iloc[-1]) / float(base.iloc[-1])
    return level


_COMPOSITE: dict[str, str] = {}


def _composite(ticker: str) -> str | None:
    """미국 ETF 의 통합(composite) RIC — 주 상장 RIC(.P·.Z·.OQ)은 이력이 짧거나 구멍이 있다
    (SPY.P 1996~·2002-03~08 공백, IEF.OQ·LQD.P 2002~03 313일 공백 실측). 통합 RIC 은 상장일부터 빈틈이 없다."""
    if ticker in _COMPOSITE:
        return _COMPOSITE[ticker] or None
    ek = _ek()
    cands = [ticker, f"{ticker}.O"]
    df, _ = _call(ek.get_data, cands, ["TR.PriceClose"])
    ric = ""
    for row in df.itertuples(index=False):
        v = row[1]
        if v is not None and str(v) not in ("", "<NA>", "nan"):
            ric = str(row[0])
            break
    _COMPOSITE[ticker] = ric
    return ric or None


def history(ticker: str, start: datetime.date | str, end: datetime.date | str | None = None,
            adjusted: bool = True):
    """yfinance 표기 티커 하나의 일별 시계열(pd.Series, 이름=ticker). adjusted 면 주식·ETF 는 총수익 가격."""
    if isinstance(start, str):
        start = datetime.date.fromisoformat(start)
    if isinstance(end, str):
        end = datetime.date.fromisoformat(end)
    end = end or datetime.date.today()
    ric = ric_for(ticker)
    if adjusted and not is_level(ric):
        us = "." not in ticker and "=" not in ticker and not ticker.startswith("^")
        comp = _composite(ticker) if us else None
        s = _tr_series(comp, start, end, anchor_ric=ric) if comp else _tr_series(ric, start, end)
    else:
        s = _close_series(ric, start, end)
    s = _today_cut(s, ric)
    return s.rename(ticker)


def snapshot(rics: list[str], fields: list[str], params: dict | None = None):
    """ek.get_data 래퍼. 날짜는 params={"SDate": …} 로 넘긴다(필드 괄호식은 여러 필드에서 조용히 빈 값)."""
    ek = _ek()
    df, err = _call(ek.get_data, rics, fields, params or None)
    return df, err


def last_close(ticker: str, days: int = 14):
    """(값, 날짜) — 최근 종가. 실패 시 예외."""
    s = history(ticker, datetime.date.today() - datetime.timedelta(days=days), adjusted=False)
    if s.empty:
        raise ValueError(f"{ticker}: 최근 {days}일 값 없음")
    return float(s.iloc[-1]), s.index[-1].date()


def source_label(ticker: str) -> str:
    if ticker in YF_ONLY:
        return f"yfinance {ticker}"
    try:
        return f"Refinitiv {ric_for(ticker)}"
    except Exception:  # noqa: BLE001
        return f"Refinitiv {ticker}"


def _yf_history(ticker: str, start: datetime.date | None):
    import pandas as pd
    import yfinance as yf

    tk = yf.Ticker(ticker)
    h = (tk.history(start=start.isoformat(), auto_adjust=True) if start else tk.history(period="max", auto_adjust=True))["Close"]
    h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
    return h[h.notna()].rename(ticker)


def splice(old, new):
    """기존 이력 뒤에 새 구간을 잇는다. 총수익 가격은 받을 때마다 최근 종가에 맞춰 다시 스케일되므로
    (배당이 지나가면 과거 레벨이 바뀐다) 겹치는 첫 날의 비율로 기존 구간을 맞춘 뒤 잇는다."""
    old, new = old.dropna(), new.dropna()
    if old.empty:
        return new
    if new.empty:
        return old
    common = old.index.intersection(new.index)
    if len(common):
        d = common[0]
        if float(old[d]):
            old = old * (float(new[d]) / float(old[d]))
    import pandas as pd

    return pd.concat([old[old.index < new.index[0]], new]).rename(new.name)


def _fill_vix_holes(df, limit: int = 2):
    """yfinance ^VIX 는 원천에 하루씩 빠지는 날이 있다(2026-10-21 실측). S&P500(^GSPC) 이 거래한 날인데 VIX 가 비면
    직전 값으로 최대 limit 일 채운다 — 비워 두면 백테스트가 그 날짜를 통째로 버려 벤치마크 수익이 빠진다."""
    if "^VIX" not in df.columns or "^GSPC" not in df.columns:
        return df
    filled = df["^VIX"].ffill(limit=limit)
    traded = df["^GSPC"].notna() & df["^VIX"].isna() & (df.index >= df["^VIX"].first_valid_index())
    df.loc[traded, "^VIX"] = filled[traded]
    return df


def refresh_frame(path: Path, tickers, start: str, full: bool = False, recent_days: int = 92):
    """가격 이력 CSV(열=yfinance 표기 티커)를 갱신해 돌려준다.
    full 이거나 처음 보는 열은 start 부터 전부, 그 밖엔 최근 recent_days 만 받아 splice 로 잇는다.
    한 종목 실패는 기존 이력으로 둔다. 권한 없는 티커(YF_ONLY)는 yfinance."""
    import pandas as pd

    old = pd.read_csv(path, index_col=0, parse_dates=True) if path.exists() else pd.DataFrame()
    if not available(force=True):
        print(f"  Refinitiv 미연결 — 기존 이력 유지: {status()['error']}")
        return old
    today = pd.Timestamp(datetime.date.today())
    s0 = datetime.date.fromisoformat(start)
    cols, changed = {}, False
    for t in sorted(set(tickers)):
        have = (not full) and t in old.columns and old[t].notna().any()
        if have and old[t].last_valid_index() >= today - pd.Timedelta(days=1):
            continue
        since = (datetime.date.today() - datetime.timedelta(days=recent_days)) if have else s0
        try:
            s = _yf_history(t, since if have else None) if t in YF_ONLY else history(t, since)
            if s.empty:
                raise ValueError("빈 응답")
            cols[t] = splice(old[t], s) if have else s[s.index >= pd.Timestamp(s0)]
            changed = True
        except Exception as e:  # noqa: BLE001 — 한 종목 실패는 기존 이력으로
            print(f"  경고: {t} 가격 실패 — {e}")
    if not changed:
        return old
    df = old.copy() if len(old) else pd.DataFrame()
    if full:   # 전체 재구성 — 요청하지 않은 옛 열은 버리고, 이번에 실패한 열만 옛 값을 남긴다
        df = df[[t for t in df.columns if t in set(tickers) and t not in cols]]
    for t, s in cols.items():
        df = df.drop(columns=[t], errors="ignore").join(s.rename(t), how="outer")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = _fill_vix_holes(df)
    df.index.name = "date"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, encoding="utf-8")
    os.replace(tmp, path)
    return df
