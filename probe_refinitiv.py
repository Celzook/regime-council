# -*- coding: utf-8 -*-
"""Refinitiv 권한·필드 점검 — 위탁운용사·마켓타이밍 카테고리 설계 전에 1회 실행한다.

Workspace 가 켜져 로그인된 Windows 파이썬에서 저장소 루트로:
    python probe_refinitiv.py            # Refinitiv 항목만
    python probe_refinitiv.py --kofia    # + 금투협 전자공시 일괄 1건(컴플라이언스 확인 후에만)

결과는 data/probe_result.json (Refinitiv 값이 들어가므로 .gitignore 로 저장소에서 뺀다 — 개발 담당에게만 직접 전달).
각 호출은 예외를 잡아 모양·첫/마지막 행·오류·소요시간만 기록한다. 실패도 결과다(권한 없음/필드 없음 판정에 쓴다).
필드 괄호식은 조용히 빈 값이 나올 수 있어 params 형태와 둘 다 시험한다(refinitiv_src.snapshot 주석).
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

import config
import refinitiv_src as R

OUT: dict = {}


def P(name: str, fn):
    t = time.time()
    try:
        r = fn()
        df, err = r if (isinstance(r, tuple) and len(r) == 2) else (r, None)
        info = {"ok": True, "err": str(err)[:800] if err is not None else None}
        if hasattr(df, "shape"):
            info["shape"] = list(df.shape)
            if len(df):
                info["head"] = df.head(6).astype(str).to_dict("records") if hasattr(df, "columns") else str(df.head(6))
                info["tail"] = df.tail(3).astype(str).to_dict("records") if hasattr(df, "columns") else str(df.tail(3))
                info["first_index"] = str(df.index[0])
                info["last_index"] = str(df.index[-1])
        else:
            info["value"] = str(r)[:3000]
    except Exception as e:  # noqa: BLE001
        info = {"ok": False, "err": repr(e)[:800]}
    info["sec"] = round(time.time() - t, 1)
    OUT[name] = info
    print(f"{name:34s} ok={info.get('ok')} shape={info.get('shape')} err={str(info.get('err'))[:100]}")


def _first_last_dates(name: str, df):
    """'Date' 열이 있으면 첫·마지막 날짜를 따로 남긴다(시작일 확인용)."""
    try:
        d = pd.to_datetime(df["Date"], errors="coerce").dropna()
        OUT[name + "_span"] = [str(d.min().date()), str(d.max().date()), int(len(d))]
    except Exception as e:  # noqa: BLE001
        OUT[name + "_span"] = repr(e)[:200]


def probe_funds():
    """A — 위탁운용사: 한국 Lipper 펀드의 TR.Fund* 채움률, 원시 NAV·총수익, 금투협 코드 연결."""
    lp = ["LP65024532"]  # Kyobo AXA Power Index Derivatives 1 Class A (LSEG 포럼 예시)
    fund_f = ["TR.FundName", "TR.FundType", "TR.FundCompany", "TR.FundDomicile", "TR.FundLaunchDate",
              "TR.FundLegalStructure", "TR.FundGeographicFocus", "TR.FundBenchmarkName", "TR.FundIndexTracking",
              "TR.FundTER", "TR.FundTERDate", "TR.FundTotalNetAssets", "TR.FundTotalNetAsset",
              "TR.FundTrackingError1Year", "TR.Fund1YearPerf", "TR.NETASSETVAL"]
    P("A1_fund_static", lambda: R.snapshot(lp, fund_f))
    P("A2_fund_nav_daily", lambda: R.snapshot(lp, ["TR.FundNAV.date", "TR.FundNAV"],
                                             {"SDate": "2023-10-01", "EDate": "2026-10-23", "Frq": "D"}))
    try:
        df, _ = R.snapshot(lp, ["TR.FundNAV.date", "TR.FundNAV"], {"SDate": "2023-10-01", "EDate": "2026-10-23", "Frq": "D"})
        v = pd.to_numeric(df.iloc[:, -1], errors="coerce")
        OUT["A2_drops_le_-5pct"] = df.loc[v.pct_change() <= -0.05].astype(str).to_dict("records")  # 결산 기준가 환원 후보
    except Exception as e:  # noqa: BLE001
        OUT["A2_drops_le_-5pct"] = repr(e)[:300]
    P("A3_fund_tr_daily", lambda: R.snapshot(lp, ["TR.TotalReturn.date", "TR.TotalReturn"],
                                            {"SDate": "2025-01-01", "EDate": "2026-10-23", "Frq": "D"}))
    P("A3_rollperf_paren", lambda: R.snapshot(lp, ["TR.FundRollingPerformance(RollTimeFrame=1Y Interval=W).date",
                                                   "TR.FundRollingPerformance(RollTimeFrame=1Y Interval=W)"]))
    P("A3_rollperf_params", lambda: R.snapshot(lp, ["TR.FundRollingPerformance.date", "TR.FundRollingPerformance"],
                                               {"RollTimeFrame": "1Y", "Interval": "W"}))
    kr = ["KR5105487700", "K55207CP5413", "K55206DW5141", "KR5101AH3807", "K55229EQ1231"]
    P("A4_lipper_map", lambda: R.snapshot(kr, ["TR.LipperRICCode", "TR.FundName", "TR.FundLaunchDate", "TR.FundCompany"]))
    P("A4_symbology", lambda: R._ek().get_symbology(["KR5101AH3807", "KR5105487700"], from_symbol_type="ISIN",
                                                     to_symbol_type=["RIC", "lipperID"]))
    P("A4_tna_hist", lambda: R.snapshot(lp, ["TR.FundTotalNetAssets.date", "TR.FundTotalNetAssets"],
                                        {"SDate": "2025-01-01", "EDate": "2026-10-23", "Frq": "M"}))
    P("A4_ter_hist", lambda: R.snapshot(lp, ["TR.FundTERDate", "TR.FundTER"], {"SDate": "2015-01-01", "EDate": "2026-10-23"}))
    P("A5_fund_screen", lambda: R.snapshot(['SCREEN(U(IN(FUNDS)),IN(TR.FundDomicile,"KR"))'], ["TR.FundName", "TR.FundCompany"]))


def probe_equities():
    """B — 페어: 보통·우선주 목록 SCREEN, 구분 필드, 거래대금·시총, 다중 RIC 일별 패널, 지분·지수 구성."""
    scr = {
        "mic_primary": 'SCREEN(U(IN(Equity(active,public,primary,countryprimaryquote))/*UNV:Public*/), IN(TR.ExchangeMarketIdCode,"XKRX","XKOS"))',
        "mic_noprimary": 'SCREEN(U(IN(Equity(active,public))/*UNV:Public*/), IN(TR.ExchangeMarketIdCode,"XKRX","XKOS"))',
        "ctry_primary": 'SCREEN(U(IN(Equity(active,public,primary))),IN(TR.ExchangeCountryCode,"KR"),CURN=KRW)',
    }
    for k, s in scr.items():
        P(f"B1_screen_{k}", lambda s=s: R.snapshot([s], ["TR.CommonName", "TR.ExchangeMarketIdCode", "TR.InstrumentTypeCode",
                                                         "TR.CompanyMarketCap", "TR.GICSSector", "TR.GICSIndustry"]))
        try:
            df, _ = R.snapshot([s], ["TR.CommonName", "TR.InstrumentTypeCode"])
            OUT[f"B1_{k}_rows"] = int(len(df))
            OUT[f"B1_{k}_has_pref"] = int(df["Instrument"].isin(["005935.KS", "005385.KS", "005387.KS"]).sum())
            OUT[f"B1_{k}_typecodes"] = df.iloc[:, -1].astype(str).value_counts().head(20).astype(int).to_dict()
        except Exception as e:  # noqa: BLE001
            OUT[f"B1_{k}_rows"] = repr(e)[:300]
    kr = ["005930.KS", "005935.KS", "005380.KS", "005385.KS", "005387.KS", "00088K.KS", "035720.KS", "247540.KQ"]
    P("B2_pref_fields", lambda: R.snapshot(kr, ["TR.CommonName", "TR.InstrumentTypeCode", "TR.InstrumentType", "TR.ShareClass",
                                                "TR.IssueType", "TR.OrganizationID", "TR.ExchangeMarketIdCode"]))
    P("B2_liq_cap", lambda: R.snapshot(kr, ["TR.AvgDailyValTraded20D", "TR.AvgDailyVolume20D", "TR.CompanyMarketCap",
                                            "TR.IssueMarketCap", "TR.GICSSector", "TR.GICSIndustry", "TR.TRBCIndustryGroup",
                                            "TR.PriceCloseCurrency"]))
    P("B3_panel_px", lambda: R.snapshot(["005930.KS", "005935.KS", "000660.KS", "035420.KS"],
                                        ["TR.PriceClose.date", "TR.PriceClose", "TR.TotalReturn", "TR.Volume"],
                                        {"SDate": "2025-10-23", "EDate": "2026-10-23", "Frq": "D", "Adjusted": 1}))
    P("B3_turnover_field", lambda: R.snapshot(["005935.KS"], ["TR.TurnoverValue.date", "TR.TurnoverValue", "TR.Volume"],
                                              {"SDate": "2026-09-01", "EDate": "2026-10-23", "Frq": "D"}))
    P("B3_avgval_hist", lambda: R.snapshot(["005930.KS"], ["TR.AvgDailyValTraded20D.date", "TR.AvgDailyValTraded20D"],
                                           {"SDate": "2026-06-01", "EDate": "2026-10-23", "Frq": "M"}))
    # 긴 배치 중 available() 가 _LOCK 때문에 거짓 실패하는지
    import threading
    big = ["005930.KS", "005935.KS", "000660.KS", "035420.KS", "005380.KS",
           "005385.KS", "051910.KS", "006400.KS", "035720.KS", "068270.KS"] * 10
    th = threading.Thread(target=lambda: R.snapshot(big, ["TR.PriceClose.date", "TR.PriceClose"],
                                                    {"SDate": "2016-01-01", "EDate": "2026-10-23", "Frq": "D"}))
    t0 = time.time()
    th.start()
    time.sleep(2)
    OUT["B4_available_during_batch"] = {"available": R.available(force=True), "status": R.status()}
    th.join(timeout=600)
    OUT["B4_batch_sec"] = round(time.time() - t0, 1)
    P("B5_parent", lambda: R.snapshot(["000660.KS", "402340.KS", "003550.KS", "051910.KS"],
                                      ["TR.CommonName", "TR.UltimateParentId", "TR.ParentId", "TR.OrganizationID"]))
    P("B5_ks200_chain", lambda: R.snapshot(["0#.KS200"], ["TR.CommonName"]))
    P("B5_ks200_const_hist", lambda: R.snapshot([".KS200"], ["TR.IndexConstituentRIC", "TR.IndexConstituentName"],
                                                {"SDate": "2022-06-30"}))


def probe_spikes():
    """C — 급등 매수·공통기간: VKOSPI 시작일, MOVE·VIX3M·NDX TR, KOSPI200 TR·국고10년 시작일."""
    P("C1_vkospi_ts", lambda: R._ek().get_timeseries(".KSVKOSPI", fields=["CLOSE"], start_date="2000-01-01",
                                                      end_date="2010-12-31", interval="daily"))
    try:
        df, err = R.snapshot([".KSVKOSPI"], ["TR.PriceClose.date", "TR.PriceClose"],
                             {"SDate": "2000-01-01", "EDate": "2010-12-31", "Frq": "D"})
        OUT["C1_vkospi_pc_err"] = str(err)[:300] if err is not None else None
        _first_last_dates("C1_vkospi_pc", df)
    except Exception as e:  # noqa: BLE001
        OUT["C1_vkospi_pc_err"] = repr(e)[:300]
    P("C1_vkospi_history", lambda: R.history(".KSVKOSPI", "2000-01-01", adjusted=False))
    for ric in [".MOVE", ".MOVEIDX", ".VIX3M", ".VXV", ".XNDX"]:
        P(f"C2_{ric}", lambda ric=ric: R.snapshot([ric], ["TR.CommonName", "TR.PriceClose.date", "TR.PriceClose"],
                                                  {"SDate": "1988-01-01", "EDate": "2026-10-23", "Frq": "Y"}))
    P("C2_tr_qqq_tlt", lambda: R.snapshot(["QQQ.OQ", "TLT.OQ", ".XNDX"], ["TR.TotalReturn.date", "TR.TotalReturn"],
                                          {"SDate": "1999-03-01", "EDate": "2003-01-31", "Frq": "M"}))
    P("C2_tlt_history", lambda: R.history("TLT", "2002-07-01"))
    P("C3_ks200tr", lambda: R.snapshot([".KS200TR"], ["TR.PriceClose.date", "TR.PriceClose"],
                                       {"SDate": "1990-01-01", "EDate": "2026-10-23", "Frq": "Y"}))
    P("C3_ktb", lambda: R.snapshot(["KRKTB10YAV=", "KRKTB3YAV="], ["TR.PriceClose.date", "TR.PriceClose"],
                                   {"SDate": "1995-01-01", "EDate": "2026-10-23", "Frq": "Y"}))
    for ric in ["KXc1", "LKTBc1", "KTBc1"]:
        P(f"C3_bondfut_{ric}", lambda ric=ric: R.snapshot([ric], ["TR.CommonName", "TR.PriceClose", "EXPIR_DATE"]))


def probe_timing():
    """D — 마켓타이밍 신호: KOSPI200 선물 미결제약정, 옵션 체인(풋콜), 기존 RIC 확인."""
    P("D1_ksc1_rt", lambda: R.snapshot(["KSc1"], ["OPINT_1", "TRDPRC_1", "EXPIR_DATE", "SETTLE"]))
    P("D1_ksc1_oi_hist", lambda: R.snapshot(["KSc1"], ["TR.OpenInterest.date", "TR.OpenInterest", "TR.SettlementPrice"],
                                            {"SDate": "2026-07-01", "EDate": "2026-10-23", "Frq": "D"}))
    P("D1_ksc1_ts_oi", lambda: R._ek().get_timeseries("KSc1", fields=["CLOSE", "OPINT_1"], start_date="2026-09-01",
                                                       end_date="2026-10-23", interval="daily"))
    P("D1_ks_chain", lambda: R.snapshot(["0#KS:"], ["OPINT_1", "EXPIR_DATE", "TRDPRC_1"]))
    P("D2_existing", lambda: R.snapshot([".KSVKOSPI", "KRW=KFTC", "AA-CNG3YAV=", "BBB-CNG3YAV=", "KRKTB3YAV="],
                                        ["TR.CommonName", "TR.PriceClose", "TR.PriceCloseDate"]))
    P("D2_opt_chain", lambda: R.snapshot(["0#KS200*.KS"], ["PUTCALLIND", "STRIKE_PRC", "ACVOL_1", "OPINT_1"]))


def probe_kofia():
    """금투협 전자공시 일괄(비공식 엔드포인트) — 컴플라이언스 확인 후에만 --kofia 로 실행."""
    import collections
    import re

    import requests
    body = ('<?xml version="1.0" encoding="utf-8"?><message><proframeHeader><pfmAppName>FS-DIS2</pfmAppName>'
            '<pfmSvcName>DISFundStdPriceSO</pfmSvcName><pfmFnName>select</pfmFnName></proframeHeader><systemHeader></systemHeader>'
            '<DISCondFuncDTO><tmpV30>20261023</tmpV30><tmpV11></tmpV11></DISCondFuncDTO></message>')

    def run():
        r = requests.post("https://dis.kofia.or.kr/proframeWeb/XMLSERVICES/", data=body.encode(),
                          headers={"Content-Type": "application/xml; charset=UTF-8", "Referer": "https://dis.kofia.or.kr/"}, timeout=180)
        t = r.text
        names = re.findall(r"<tmpV2>(.*?)</tmpV2>", t)
        types = re.findall(r"<tmpV3>(.*?)</tmpV3>", t)
        return {"status": r.status_code, "bytes": len(t), "rows": len(names),
                "tmpV3": collections.Counter(types).most_common(40),
                "mother": sum("모투자" in n for n in names), "unyong": sum("(운용)" in n for n in names),
                "samo": sum("사모" in n for n in names),
                "CF": sum(bool(re.search(r"C-?F", n)) for n in names), "CI": sum(bool(re.search(r"C-?I", n)) for n in names),
                "sample": names[:15], "first_row_xml": t[t.find("<tmpV1>"):t.find("<tmpV1>") + 1500]}
    P("A7_kofia_bulk", run)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kofia", action="store_true", help="금투협 전자공시 일괄 1건도 시험(컴플라이언스 확인 후)")
    a = ap.parse_args()
    if not R.available(force=True):
        sys.exit(f"Refinitiv 미연결 — Workspace 로그인 후 Windows 파이썬으로 다시 실행: {R.status()}")
    for fn in (probe_funds, probe_equities, probe_spikes, probe_timing):
        print(f"\n── {fn.__doc__.split('—')[0].strip()}")
        fn()
    if a.kofia:
        probe_kofia()
    out = Path(config.DATA_DIR) / "probe_result.json"
    out.write_text(json.dumps(OUT, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n저장: {out} ({len(OUT)}개 항목) — 이 파일을 그대로 전달하면 다음 단계(수집 설계)를 확정한다.")


if __name__ == "__main__":
    main()
