# -*- coding: utf-8 -*-
"""KOSPI200 옵션 풋/콜·선물 미결제약정 일별 수집 — 마켓타이밍 2차 수급 신호(timing.py put_call·put_call_oi·futures_oi)의 입력.

옵션 풋/콜(과거 이력 없음 → 매일 스냅샷 1회를 한 줄씩 쌓는다):
  체인    Refinitiv 0#KS200*.KS — 월물 행사가별 콜·풋(전 만기, 2026-10-26 실측 4,482종목·11개 만기, 위클리·미니는 체인에 없음).
          첫 줄은 기초지수(.KS200)라 PUTCALLIND 가 비어 있어 뺀다. PUTCALLIND 는 'CALL'·'PUT '(뒤 공백) — 공백을 지우고 본다.
  날짜    체인 TRADE_DATE 의 최댓값 = KRX 거래일. 거래량(ACVOL_1)은 TRADE_DATE 가 그날인 종목만 더한다(오래 거래 없던 행사가의
          옛 거래량이 섞이지 않게), 미결제약정(OPINT_1)은 OPINT_DATE 가 그날인 종목만 더한다(아니면 비워 둔다).
          그 거래일이 오늘이고 16시(KST) 전이면 장중 누적이라 쓰지 않는다(refinitiv_src._today_cut 와 같은 기준 — 작업 스케줄러는 16:30).
  저장    data/history/putcall_daily.csv — 날짜당 한 줄(같은 날 다시 받으면 덮어쓴다). 전 만기 합계 + 최근월(front_*) 합계를 함께 둔다.
선물 미결제약정(과거 이력 있음, 1996~):
  KSc1 + KSc2 TR.OpenInterest 일별 합계(total). 만기일엔 KSc1 이 0 으로 와서 그 날은 뺀다. 휴장일에 직전 값이 다시 오는 중복은 panel 이 없앤다.
  저장    data/history/futures_oi_daily.csv — 처음엔 전체, 그 뒤 최근 RECENT_DAYS 만 받아 날짜별로 덮어쓴다(레벨 조정 없음).

사용법: python options_pc.py      # Windows 파이썬(python.exe -X utf8) — Refinitiv Workspace 로그인 필요
"""
import datetime
import os
import sys

import pandas as pd

import config
import refinitiv_src as R

PC_HIST = config.DATA_DIR / "history" / "putcall_daily.csv"
OI_HIST = config.DATA_DIR / "history" / "futures_oi_daily.csv"
CHAIN = "0#KS200*.KS"
FIELDS = ["PUTCALLIND", "EXPIR_DATE", "ACVOL_1", "OPINT_1", "OPINT_DATE", "TRADE_DATE"]
PC_COLS = ["put_vol", "call_vol", "put_oi", "call_oi", "front_expiry", "front_put_vol", "front_call_vol",
           "front_put_oi", "front_call_oi", "n_series"]
OI_RICS = ["KSc1", "KSc2"]
OI_START = "1996-01-01"
RECENT_DAYS = 60
CLOSE_HOUR = 16


def _dates(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def summarize(df: pd.DataFrame, now: datetime.datetime | None = None) -> tuple[pd.Timestamp, dict] | None:
    """체인 스냅샷 → (거래일, 한 줄). 장중(거래일 = 오늘, 16시 전)이면 None. 순수 함수 — 테스트는 합성 표로 부른다."""
    now = now or datetime.datetime.now()
    side = df["PUTCALLIND"].astype("string").str.strip().str.upper()
    df = df.assign(_side=side, _td=_dates(df["TRADE_DATE"]), _oid=_dates(df["OPINT_DATE"]),
                   _exp=_dates(df["EXPIR_DATE"]),
                   _vol=pd.to_numeric(df["ACVOL_1"], errors="coerce").fillna(0),
                   _oi=pd.to_numeric(df["OPINT_1"], errors="coerce").fillna(0))
    df = df[df["_side"].isin(["PUT", "CALL"])]   # 기초지수 줄(PUTCALLIND 없음) 제외
    if df.empty or df["_td"].isna().all():
        raise ValueError(f"{CHAIN}: 옵션 종목·거래일 없음")
    td = df["_td"].max()
    if td.date() == now.date() and now.hour < CLOSE_HOUR:
        return None
    if td.date() > now.date():
        raise ValueError(f"{CHAIN}: 거래일 {td.date()} 이 오늘보다 뒤")
    front = df["_exp"][df["_exp"] >= td].min()
    oi_ok = (df["_oid"] == td).any()
    row = {"n_series": int(len(df)), "front_expiry": front.date().isoformat() if pd.notna(front) else None}
    for pre, part in (("", df), ("front_", df[df["_exp"] == front])):
        for s in ("put", "call"):
            g = part[part["_side"] == s.upper()]
            row[f"{pre}{s}_vol"] = int(g.loc[g["_td"] == td, "_vol"].sum())
            row[f"{pre}{s}_oi"] = int(g.loc[g["_oid"] == td, "_oi"].sum()) if oi_ok else None
    if not (row["put_vol"] or row["call_vol"]):
        raise ValueError(f"{CHAIN}: {td.date()} 풋·콜 거래량이 모두 0")
    return td, row


def upsert(path, date: pd.Timestamp, row: dict) -> pd.DataFrame:
    """날짜당 한 줄 — 같은 날이면 덮어쓴다(여러 번 돌려도 같은 결과)."""
    new = pd.DataFrame([row], index=pd.DatetimeIndex([date], name="date"))[PC_COLS]
    old = pd.read_csv(path, index_col=0, parse_dates=True) if path.exists() else pd.DataFrame()
    old = old[~old.index.isin(new.index)]
    df = pd.concat([old, new]).sort_index() if len(old) else new
    df.index.name = "date"
    _save(df, path)
    return df


def _save(df: pd.DataFrame, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, encoding="utf-8")
    os.replace(tmp, path)


def collect_putcall(now: datetime.datetime | None = None) -> str:
    df, _ = R.snapshot([CHAIN], FIELDS)   # 기초지수 줄의 '필드 없음' 경고는 정상
    if df is None or not len(df):
        raise RuntimeError(f"{CHAIN}: 응답 없음")
    r = summarize(df, now)
    if r is None:
        return "건너뜀(장중 — 16시 이후 다시)"
    td, row = r
    h = upsert(PC_HIST, td, row)
    pc = row["put_vol"] / row["call_vol"] if row["call_vol"] else float("nan")
    return f"{td.date()} 풋/콜 거래량 {pc:.2f} · 쌓인 {len(h)}일"


def oi_frame(p: pd.DataFrame) -> pd.DataFrame:
    """KSc1·KSc2 미결제약정 표 → total 추가. KSc1 이 0·빈 날(만기일)과 KSc2 가 빈 날은 뺀다."""
    p = p.reindex(columns=OI_RICS)
    p = p[(p["KSc1"] > 0) & p["KSc2"].notna()].copy()
    p["total"] = p["KSc1"] + p["KSc2"]
    p.index.name = "date"
    return p


def refresh_oi(full: bool = False) -> str:
    old = pd.read_csv(OI_HIST, index_col=0, parse_dates=True) if OI_HIST.exists() and not full else pd.DataFrame()
    start = (datetime.date.today() - datetime.timedelta(days=RECENT_DAYS)).isoformat() if len(old) else OI_START
    new = oi_frame(R.panel(OI_RICS, start, field="TR.OpenInterest"))
    if new.empty:
        raise RuntimeError("KSc1·KSc2 TR.OpenInterest 값 없음")
    df = pd.concat([old[~old.index.isin(new.index)], new]).sort_index() if len(old) else new
    df.index.name = "date"
    _save(df, OI_HIST)
    return f"선물 미결제약정 {df.index[-1].date()} {int(df['total'].iloc[-1]):,}계약 · {len(df)}일"


def run() -> str:
    """refresh_all 한 단계 — 풋/콜 스냅샷과 선물 미결제약정. 한쪽이 실패해도 다른 쪽은 받고, 둘 다 실패면 예외."""
    if not R.available(force=True):
        raise RuntimeError(R.status()["error"])
    out, errs = [], []
    for name, fn in (("풋/콜", collect_putcall), ("미결제약정", refresh_oi)):
        try:
            out.append(fn())
        except Exception as e:  # noqa: BLE001 — 한쪽 실패가 다른 쪽을 막지 않게
            errs.append(f"{name} 실패: {e}")
    if not out:
        raise RuntimeError("; ".join(errs))
    return " · ".join(out + errs)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(run())
    print(PC_HIST, OI_HIST)
