# -*- coding: utf-8 -*-
"""金/银/汉堡包 · 2026-11-27 盘中。条件见 模型条件/金银汉堡包.md。
题材过滤：科创/创业板与半导体算力链 · 国产替代（主板可交易标的）。
"""
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

TODAY = "20261127"
TOL = 0.02
PCT_MAX = None  # 不按涨幅%一刀切；改为剔除今日涨停
EXCLUDE_TODAY_ZT = True  # 今日涨停不要
MV_MAX_YI = 1000.0
HIGH_DAYS_OK = {2, 3, 5, 8}
OUT = Path("/Users/hanting/Documents/板块/盘面分析/20261127")
OUT.mkdir(parents=True, exist_ok=True)
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"

# 今日主线：半导体算力链 / 国产替代。命中按顺序归入第一条（强相关优先）。
HOT_RULES = [
    ("半导体设备", ("半导体设备", "刻蚀", "光刻", "薄膜沉积", "量测", "清洗设备", "CMP", "涂胶显影")),
    ("先进封装存储", ("先进封装", "HBM", "存储芯片", "封测", "玻璃基", "Chiplet", "Chiplet封装")),
    ("国产算力芯片", ("昇腾", "算力芯片", "GPU", "AI芯片", "国产算力", "寒武纪", "海光", "推理芯片", "训练芯片")),
    ("算力数据中心", ("算力", "算力租赁", "数据中心", "东数西算", "液冷", "服务器", "IDC")),
    ("CPO光模块", ("CPO", "光模块", "光通信", "共封装", "硅光")),
    ("半导体材料", ("半导体材料", "光刻胶", "电子特气", "靶材", "硅片", "抛光液", "湿电子化学品")),
    ("国产替代芯片", ("国产替代", "芯片国产化", "集成电路", "半导体概念", "芯片概念", "功率半导体", "MCU", "模拟芯片")),
    ("华为产业链", ("华为概念", "华为昇腾", "鸿蒙", "华为链")),
    ("PCB算力板", ("PCB", "印制电路", "覆铜板", "HDI", "高多层", "算力板")),
]


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
    "https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=sh000001,day,,,40,",
    headers={"Referer": "https://finance.qq.com/"},
)
r.encoding = "utf-8"
days = sorted({d[0].replace("-", "") for d in r.json()["data"]["sh000001"]["day"]})
if TODAY not in days:
    raise SystemExit(f"TODAY {TODAY} 不在上证指数交易日里，最近交易日 {days[-1]}")
idx = days.index(TODAY)
lookback8 = days[max(0, idx - 8) : idx]
print("TODAY", TODAY, "lookback8", lookback8)


def fetch_zt_pool(date_ymd):
    out = []
    page = 1
    while page <= 6:
        url = (
            "http://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
            f"?filter=HS,GEM2STAR&order_field=first_limit_up_time&order_type=0"
            f"&date={date_ymd}&page={page}&limit=200"
        )
        rr = get(url)
        rr.encoding = "utf-8"
        j = rr.json()
        info = j.get("data") or {}
        items = info.get("info") or info.get("list") or []
        for it in items:
            code = str(it.get("code") or it.get("stock_code") or "").zfill(6)
            name = it.get("name") or it.get("stock_name") or ""
            if code:
                out.append({"code": code, "name": name, "date": date_ymd})
        if len(items) < 200:
            break
        page += 1
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


def is_eligible(code, name):
    """沪深主板 + 科创板(688)；排除创业板/北交所/ST。"""
    if "ST" in (name or "").upper():
        return False
    if code.startswith(("300", "301", "8", "4", "920")):
        return False
    return code.startswith(("60", "00", "688"))


def limit_ratio(code):
    """主板 10%；科创板 20%。"""
    return 1.2 if code.startswith("688") else 1.1


def zt_pct_floor(code):
    """接近涨停的涨幅阈值（用于今日涨停剔除）。"""
    return 19.9 if code.startswith("688") else 9.9


cand_dates = defaultdict(list)
cand_name = {}
for d in reversed(lookback8):
    for it in zt_by_date.get(d, []):
        if not is_eligible(it["code"], it["name"]):
            continue
        if d not in cand_dates[it["code"]]:
            cand_dates[it["code"]].append(d)
        cand_name[it["code"]] = it["name"]
print("candidates", len(cand_dates))

today_zt_codes = {
    it["code"]
    for it in zt_by_date.get(TODAY, [])
    if is_eligible(it["code"], it["name"])
}
print("today_zt_eligible", len(today_zt_codes))


def is_limit_up(price, pre_close, code=""):
    """涨停价判定；允许 1 分钱误差。科创 20%，其余主板 10%。"""
    if pre_close <= 0 or price <= 0:
        return False
    limit = round(pre_close * limit_ratio(code), 2)
    return price + 1e-9 >= limit - 0.01


def fetch_kline(code, n=60):
    pref = "sh" if code.startswith("6") else "sz"
    url = (
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        f"?param={pref}{code},day,,,{n},qfq"
    )
    rr = get(url, headers={"Referer": "https://finance.qq.com/"})
    rr.encoding = "utf-8"
    node = (rr.json().get("data") or {}).get(f"{pref}{code}") or {}
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    for x in rows:
        out.append(
            {
                "date": str(x[0]).replace("-", ""),
                "open": float(x[1]),
                "close": float(x[2]),
                "high": float(x[3]),
                "low": float(x[4]),
                "vol": float(x[5]) if len(x) > 5 and str(x[5]) not in ("", "{}") else 0,
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


def eval_anchor(by, dates, zt_date, today, today_pre, mv_yi, code=""):
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
    limit = round(today_pre * limit_ratio(code), 2)
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
        if PCT_MAX is not None and pct >= PCT_MAX:
            return {"skip": "pct", "code": code, "pct": round(pct, 2)}
        name = (q or {}).get("name") or cand_name.get(code, "")
        if EXCLUDE_TODAY_ZT and (
            code in today_zt_codes
            or is_limit_up(today["close"], today_pre, code)
            or pct + 1e-9 >= zt_pct_floor(code)
        ):
            return {"skip": "today_zt", "code": code, "pct": round(pct, 2)}
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
            ev = eval_anchor(by, dates, zt_date, today, today_pre, mv_yi, code=code)
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


def fetch_concepts(code):
    mkt = "SH" if code.startswith("6") else "SZ"
    url = (
        "https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax"
        f"?code={mkt}{code}"
    )
    rr = get(url, headers={"Referer": "https://emweb.securities.eastmoney.com/"})
    rr.encoding = "utf-8"
    text = rr.text.strip()
    if text.startswith("("):
        text = text[text.find("{") : text.rfind("}") + 1]
    j = json.loads(text)
    names = []
    for key in ("ssbk", "hxtc"):
        for it in j.get(key) or []:
            if not isinstance(it, dict):
                continue
            name = it.get("BOARD_NAME") or ""
            point = it.get("MAINPOINT_CONTENT") or it.get("KEY_CLASSIF") or ""
            if name:
                names.append(name)
            if point and len(point) < 80:
                names.append(point)
    # 去重保序
    seen = []
    for n in names:
        if n and n not in seen:
            seen.append(n)
    return seen


def match_hot(concepts):
    blob = " ".join(concepts)
    hits = []
    for label, keys in HOT_RULES:
        if any(k.lower() in blob.lower() for k in keys):
            hits.append(label)
    if not hits:
        return "", []
    # 人形优先于泛机器人；影视优先于泛传媒已由规则顺序处理
    primary = hits[0]
    if "人形机器人" in hits and "机器人" in hits:
        hits = [h for h in hits if h != "机器人"]
        primary = "人形机器人"
    return primary, hits


hits = gold + silver + burger
concept_map = {}
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(fetch_concepts, row["code"]): row["code"] for row in hits}
    for fut in as_completed(futs):
        code = futs[fut]
        try:
            concept_map[code] = fut.result()
        except Exception as e:
            concept_map[code] = []
            print("concept fail", code, e)

for row in hits:
    concepts = concept_map.get(row["code"]) or []
    primary, labels = match_hot(concepts)
    row["concepts"] = concepts
    row["hot"] = primary
    row["hot_labels"] = labels

raw = {
    "gold": gold,
    "silver": silver,
    "burger": burger,
    "lookback": lookback8,
    "today": TODAY,
    "ts": datetime.now().isoformat(timespec="seconds"),
    "note": "11/27盘中。主板+科创板。条件=金银汉堡包.md（剔除今日涨停）。题材=半导体算力链/国产替代。",
    "skips": dict(Counter(x.get("skip") for x in rows if x and "skip" in x)),
}
(OUT / "金银汉堡包_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
print("saved", OUT / "金银汉堡包_raw.json")
print("HOT", Counter(x["hot"] or "-" for x in hits))
for label in [h[0] for h in HOT_RULES]:
    subset = [x for x in hits if x.get("hot") == label]
    if subset:
        print(f"\n=== HOT {label} ({len(subset)}) ===")
        for x in subset:
            model = "金" if x["gold_ok"] else ("银" if x["silver_ok"] else "汉")
            dist = x["dist_gold_pct"] if x["gold_ok"] else (x["dist_mid_pct"] if x["silver_ok"] else x["dist_foot_pct"])
            print(f"{x['code']} {x['name']} {model} pct={x['pct']} dist={dist} n={x['n_day']} elas={x['elas']}")
