# -*- coding: utf-8 -*-
"""金元宝/银元宝/汉堡包 · 2026-11-18 收盘口径。剔除今日涨停/触及涨停。"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

S = requests.Session()
S.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://data.10jqka.com.cn/",
    }
)

TODAY = "20261118"
TOL = 0.02
PCT_MAX = 3.5
MV_MAX_YI = 1000.0
HIGH_DAYS_OK = {2, 3, 5, 8}
OUT = Path("/Users/hanting/Documents/板块/盘面分析/20261118")
OUT.mkdir(parents=True, exist_ok=True)
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"


def get(url, headers=None, retries=5):
    last = None
    h = dict(S.headers)
    if headers:
        h.update(headers)
    for i in range(retries):
        try:
            r = S.get(url, headers=h, timeout=25)
            if r.status_code != 200:
                raise RuntimeError(r.status_code)
            return r
        except Exception as e:
            last = e
            time.sleep(0.35 * (i + 1))
    raise last


r = get(
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid=1.000001&klt=101&fqt=1&lmt=40&end=20500101"
    "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56",
    headers={"Referer": "https://quote.eastmoney.com/"},
)
r.encoding = "utf-8"
days = sorted({x.split(",")[0].replace("-", "") for x in r.json()["data"]["klines"]})
if TODAY not in days:
    raise SystemExit(f"TODAY {TODAY} 不在上证指数交易日里，最近交易日 {days[-1]}")
idx = days.index(TODAY)
lookback8 = days[max(0, idx - 8) : idx]
print("TODAY", TODAY, "lookback8", lookback8)


def fetch_zt_pool(date_ymd):
    url = (
        "http://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
        f"?filter=HS,GEM2STAR&order_field=first_limit_up_time&order_type=0"
        f"&date={date_ymd}&page=1&limit=200"
    )
    rr = get(url)
    rr.encoding = "utf-8"
    j = rr.json()
    info = j.get("data") or {}
    items = info.get("info") or info.get("list") or []
    out = []
    for it in items:
        code = str(it.get("code") or it.get("stock_code") or "").zfill(6)
        name = it.get("name") or it.get("stock_name") or ""
        if code:
            out.append({"code": code, "name": name, "date": date_ymd})
    return out


def fetch_zt_em(date_ymd):
    out = []
    page = 0
    while page < 8:
        url = (
            f"https://push2ex.eastmoney.com/getTopicZTPool?ut={EM_UT}"
            f"&dpt=wz.ztzt&Pageindex={page}&pagesize=200&sort=fbt%3Aasc&date={date_ymd}"
        )
        rr = get(url, headers={"Referer": "https://quote.eastmoney.com/"})
        j = rr.json()
        data = j.get("data") or {}
        pool = data.get("pool") or []
        for it in pool:
            if it.get("c"):
                out.append(
                    {
                        "code": str(it.get("c")).zfill(6),
                        "name": it.get("n") or "",
                        "date": date_ymd,
                    }
                )
        total = int(data.get("tc") or 0)
        if not pool or len(out) >= total:
            break
        page += 1
    return out


zt_by_date = {}
for d in lookback8 + [TODAY]:
    try:
        items = fetch_zt_pool(d)
        if len(items) < 5:
            items = fetch_zt_em(d)
        zt_by_date[d] = items
        print(d, "zt", len(items))
    except Exception as e:
        try:
            items = fetch_zt_em(d)
            zt_by_date[d] = items
            print(d, "em", len(items))
        except Exception as e2:
            zt_by_date[d] = []
            print("fail", d, e, e2)

today_zt = {it["code"] for it in zt_by_date.get(TODAY, [])}
print("today_zt", len(today_zt))


def is_mainboard(code, name):
    if code.startswith(("688", "300", "301", "8", "4", "920")):
        return False
    if "ST" in (name or "").upper():
        return False
    return code.startswith(("60", "00"))


cand_dates = defaultdict(list)
cand_name = {}
for d in reversed(lookback8):
    for it in zt_by_date.get(d, []):
        if not is_mainboard(it["code"], it["name"]):
            continue
        if d not in cand_dates[it["code"]]:
            cand_dates[it["code"]].append(d)
        cand_name[it["code"]] = it["name"]
print("candidates", len(cand_dates))


def fetch_kline(code, n=60):
    market = "1" if code.startswith("6") else "0"
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={market}.{code}&klt=101&fqt=1&lmt={n}&end=20500101"
        "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56"
    )
    rr = get(url, headers={"Referer": "https://quote.eastmoney.com/"})
    rr.encoding = "utf-8"
    klines = ((rr.json().get("data") or {}).get("klines")) or []
    out = []
    for line in klines:
        x = line.split(",")
        out.append(
            {
                "date": x[0].replace("-", ""),
                "open": float(x[1]),
                "close": float(x[2]),
                "high": float(x[3]),
                "low": float(x[4]),
                "vol": float(x[5]) if len(x) > 5 else 0,
            }
        )
    return out


def fetch_quote(code):
    pref = "sh" if code.startswith("6") else "sz"
    rr = get(f"https://qt.gtimg.cn/q={pref}{code}", headers={"Referer": "https://finance.qq.com/"})
    rr.encoding = "gbk"
    m = re.search(r'="([^"]*)"', rr.text)
    if not m:
        return None
    p = m.group(1).split("~")
    try:
        return {
            "name": p[1],
            "price": float(p[3]),
            "pre": float(p[4]),
            "open": float(p[5]),
            "high": float(p[33]) if p[33] else float(p[3]),
            "low": float(p[34]) if p[34] else float(p[3]),
            "pct": float(p[32]),
            "mv_yi": float(p[45]),
        }
    except Exception:
        return None


def elasticity(mv_yi, amp10, price, room):
    s = 0.0
    if mv_yi and mv_yi > 0:
        s += max(0, 80 - mv_yi) / 6
    s += amp10 * 1.8
    if price > 0:
        s += max(0, 25 - price) / 5
    s += room * 0.4
    return round(s, 2)


def eval_anchor(by, dates, zt_date, today, today_pre, mv_yi):
    if zt_date not in by:
        return None
    zi = dates.index(zt_date)
    if zi == 0:
        return None
    prev = by[dates[zi - 1]]
    zt = by[zt_date]
    zt_close = zt["close"]
    foot = prev["close"]
    mid = (zt_close + foot) / 2
    seg = [by[d] for d in dates if zt_date <= d <= TODAY]
    if not seg:
        return None
    max_high = max(x["high"] for x in seg)
    high_day = [x["date"] for x in seg if abs(x["high"] - max_high) < 1e-6][-1]
    n_day = len([d for d in dates if high_day <= d <= TODAY])
    if n_day not in HIGH_DAYS_OK:
        return {"fail": "nday", "n": n_day, "high_day": high_day}
    after = [by[d] for d in dates if zt_date < d <= TODAY]
    gold_ok = all(x["close"] + 1e-9 >= zt_close - TOL for x in after) if after else False
    silver_ok = today["close"] + 1e-9 >= mid - TOL
    burger_ok = False
    post = [d for d in dates if zt_date < d <= TODAY]
    for i, d in enumerate(post):
        bar = by[d]
        if bar["close"] + 1e-9 >= foot - TOL:
            burger_ok = True
            break
        if bar["low"] < foot - TOL and bar["close"] + 1e-9 >= foot - TOL:
            burger_ok = True
            break
        if i + 1 < len(post):
            nxt = by[post[i + 1]]
            if bar["low"] < foot - TOL and nxt["open"] + 1e-9 >= foot - TOL:
                burger_ok = True
                break
    if not (gold_ok or silver_ok or burger_ok):
        return {"fail": "support"}
    last10 = [by[d] for d in dates if d <= TODAY][-10:]
    amp10 = (
        (max(x["high"] for x in last10) / min(x["low"] for x in last10) - 1) * 100 if last10 else 0
    )
    limit = round(today_pre * 1.1, 2)
    room = max(0, (limit / today["close"] - 1) * 100) if today["close"] else 0
    elas = elasticity(mv_yi, amp10, today["close"], room)
    return {
        "zt_date": zt_date,
        "zt_close": zt_close,
        "mid": mid,
        "foot": foot,
        "high_day": high_day,
        "n_day": n_day,
        "gold_ok": gold_ok,
        "silver_ok": silver_ok,
        "burger_ok": burger_ok,
        "amp10": amp10,
        "elas": elas,
        "room": room,
        "limit": limit,
    }


def process(code, zt_dates):
    try:
        if code in today_zt:
            return {"skip": "today_zt", "code": code}
        kl = fetch_kline(code, 50)
        by = {x["date"]: x for x in kl}
        q = fetch_quote(code)
        if TODAY not in by:
            if not q:
                return {"skip": "no_today", "code": code}
            by[TODAY] = {
                "date": TODAY,
                "open": q["open"],
                "close": q["price"],
                "high": q["high"],
                "low": q["low"],
                "vol": 0,
            }
        dates = sorted(by.keys())
        if TODAY not in dates:
            return {"skip": "no_today", "code": code}
        today = by[TODAY]
        ti = dates.index(TODAY)
        if ti == 0:
            return {"skip": "no_prev", "code": code}
        today_pre = by[dates[ti - 1]]["close"]
        pct = (today["close"] / today_pre - 1) * 100
        limit = round(today_pre * 1.1, 2)
        # 收盘涨停或盘中触及涨停价，一律剔除
        if today["close"] + 1e-9 >= limit - 0.001 or today["high"] + 1e-9 >= limit - 0.001:
            return {"skip": "today_touch_zt", "code": code, "pct": pct}
        if pct >= PCT_MAX:
            return {"skip": "pct", "code": code, "pct": pct}
        name = (q or {}).get("name") or cand_name.get(code, "")
        mv_yi = (q or {}).get("mv_yi")
        if mv_yi is None:
            return {"skip": "no_mv", "code": code}
        if mv_yi >= MV_MAX_YI:
            return {"skip": "mv", "code": code, "mv": mv_yi}
        if "ST" in (name or "").upper():
            return {"skip": "st", "code": code}
        hit = None
        fails = []
        for zt_date in zt_dates:
            ev = eval_anchor(by, dates, zt_date, today, today_pre, mv_yi)
            if ev is None:
                fails.append((zt_date, "missing"))
                continue
            if "fail" in ev:
                fails.append((zt_date, ev["fail"]))
                continue
            hit = ev
            break
        if not hit:
            reason = fails[0][1] if fails else "no_anchor"
            return {"skip": reason, "code": code, "fails": fails}
        zt_date = hit["zt_date"]
        return {
            "code": code,
            "name": name,
            "mv_yi": round(mv_yi, 1),
            "pct": round(pct, 2),
            "zt_date": f"{zt_date[:4]}-{zt_date[4:6]}-{zt_date[6:]}",
            "zt_close": round(hit["zt_close"], 3),
            "mid": round(hit["mid"], 3),
            "foot": round(hit["foot"], 3),
            "high_day": f"{hit['high_day'][:4]}-{hit['high_day'][4:6]}-{hit['high_day'][6:]}",
            "n_day": hit["n_day"],
            "dist_gold_pct": round((today["close"] / hit["zt_close"] - 1) * 100, 2),
            "dist_mid_pct": round((today["close"] / hit["mid"] - 1) * 100, 2),
            "dist_foot_pct": round((today["close"] / hit["foot"] - 1) * 100, 2),
            "elas": hit["elas"],
            "gold_ok": hit["gold_ok"],
            "silver_ok": hit["silver_ok"],
            "burger_ok": hit["burger_ok"],
            "close": today["close"],
            "amp10": round(hit["amp10"], 2),
        }
    except Exception as e:
        return {"skip": "err", "code": code, "err": str(e)}


rows = []
with ThreadPoolExecutor(max_workers=12) as ex:
    futs = {ex.submit(process, c, ds): c for c, ds in cand_dates.items()}
    for fut in as_completed(futs):
        rows.append(fut.result())

gold, silver, burger = [], [], []
for row in rows:
    if not row or "gold_ok" not in row:
        continue
    if row["gold_ok"]:
        gold.append(row)
    elif row["silver_ok"]:
        silver.append(row)
    elif row["burger_ok"]:
        burger.append(row)

gold.sort(key=lambda x: -x["elas"])
silver.sort(key=lambda x: -x["elas"])
burger.sort(key=lambda x: -x["elas"])
print("GOLD", len(gold), "SILVER", len(silver), "BURGER", len(burger))
print("skips", dict(Counter(x.get("skip") for x in rows if x and "skip" in x)))
print("---GOLD---")
for x in gold:
    print(x["code"], x["name"], x["mv_yi"], x["pct"], x["zt_date"], x["n_day"], x["dist_gold_pct"], x["elas"])
print("---SILVER---")
for x in silver:
    print(x["code"], x["name"], x["mv_yi"], x["pct"], x["zt_date"], x["n_day"], x["dist_mid_pct"], x["elas"])
print("---BURGER---")
for x in burger:
    print(x["code"], x["name"], x["mv_yi"], x["pct"], x["zt_date"], x["n_day"], x["dist_foot_pct"], x["elas"])

raw = {
    "gold": gold,
    "silver": silver,
    "burger": burger,
    "lookback": lookback8,
    "today": TODAY,
    "ts": datetime.now().isoformat(timespec="seconds"),
    "note": "11/18收盘。剔除今日涨停池及盘中触及涨停价。涨幅<3.5%。",
    "skips": dict(Counter(x.get("skip") for x in rows if x and "skip" in x)),
}
(OUT / "金银汉堡包_raw.json").write_text(
    json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("saved", OUT / "金银汉堡包_raw.json")
