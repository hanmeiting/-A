#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分时三龙 · 盘中 A 档扫描（新浪列表 + 腾讯分钟线）"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

UA = {"User-Agent": "Mozilla/5.0"}


def get_bytes(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_json(url: str, timeout: int = 12) -> Any:
    return json.loads(get_bytes(url, timeout).decode("utf-8", errors="ignore"))


def is_main_board(code: str) -> bool:
    if code.startswith(("688", "300", "301", "8", "4", "9")):
        return False
    return code.startswith(("60", "00"))


def fetch_market() -> list[dict]:
    out: list[dict] = []
    for node in ("sh_a", "sz_a"):
        page = 1
        while True:
            url = (
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "Market_Center.getHQNodeData?"
                + urllib.parse.urlencode(
                    {
                        "page": page,
                        "num": 80,
                        "sort": "symbol",
                        "asc": 1,
                        "node": node,
                        "symbol": "",
                        "_s_r_a": "page",
                    }
                )
            )
            try:
                data = get_json(url)
            except Exception as e:
                print("list fail", node, page, e)
                time.sleep(0.5)
                try:
                    data = get_json(url)
                except Exception:
                    break
            if not data:
                break
            for row in data:
                code = str(row.get("code") or "")
                name = str(row.get("name") or "")
                if not is_main_board(code):
                    continue
                if "ST" in name.upper():
                    continue
                try:
                    price = float(row["trade"])
                    pct = float(row["changepercent"])
                    pre = float(row["settlement"])
                    mktcap = float(row["mktcap"])  # 万元
                    turn = float(row.get("turnoverratio") or 0)
                except (TypeError, ValueError, KeyError):
                    continue
                if price <= 0 or pre <= 0 or mktcap <= 0:
                    continue
                limit = round(pre * 1.1 + 1e-8, 2)
                dist = (limit - price) / price * 100
                out.append(
                    {
                        "code": code,
                        "name": name,
                        "price": price,
                        "pct": pct,
                        "pre": pre,
                        "limit": limit,
                        "dist": dist,
                        "mcap_yi": mktcap / 10000.0,
                        "turn": turn,
                        "sym": ("sh" if code.startswith("6") else "sz") + code,
                    }
                )
            if len(data) < 80:
                break
            page += 1
            if page > 60:
                break
            time.sleep(0.08)
    # 去重
    uniq = {s["code"]: s for s in out}
    return list(uniq.values())


def fetch_minutes(sym: str) -> list[dict] | None:
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    try:
        data = get_json(url, timeout=10)
    except Exception:
        return None
    try:
        rows = data["data"][sym]["data"]["data"]
    except (KeyError, TypeError):
        return None
    bars = []
    cum_v = 0.0  # 手累计
    cum_a = 0.0  # 金额累计
    for row in rows:
        parts = row.split()
        if len(parts) < 4:
            continue
        try:
            hhmm = parts[0]
            if len(hhmm) == 4:
                hhmm = hhmm[:2] + ":" + hhmm[2:]
            price = float(parts[1])
            # 腾讯累计成交量（手）、累计金额
            vol_cum = float(parts[2])
            amt_cum = float(parts[3])
        except ValueError:
            continue
        if price <= 0:
            continue
        vol = vol_cum - cum_v
        amt = amt_cum - cum_a
        cum_v, cum_a = vol_cum, amt_cum
        if vol < 0:
            vol = 0
        if amt < 0:
            amt = 0
        # VWAP 黄线
        avg = (cum_a / (cum_v * 100)) if cum_v > 0 else price
        # 无高低时用收盘近似
        bars.append({"t": hhmm, "c": price, "h": price, "l": price, "v": vol, "avg": avg})
    return bars


def classify_attach(mean_dev: float, max_dev: float, amp: float, mode: str) -> str | None:
    if mode == "ge":
        if amp > 1.2:
            return None
        if mean_dev <= 0.25 and max_dev <= 0.7:
            return "★"
        if mean_dev <= 0.45 and max_dev <= 1.2:
            return "○"
        if mean_dev <= 0.8 and max_dev <= 1.5:
            return "△"
        if mean_dev <= 1.5 and max_dev <= 1.8:
            return "弱"
        return None
    if amp > 2.5:
        return None
    if mean_dev > 0.8 or max_dev > 1.8:
        return None
    if mean_dev <= 0.25 and max_dev <= 0.7:
        return "★"
    if mean_dev <= 0.45 and max_dev <= 1.2:
        return "○"
    if mean_dev <= 0.8 and max_dev <= 1.5:
        return "△"
    return None


def dragon_type(above_ratio: float) -> str:
    if above_ratio >= 0.75:
        return "卧龙"
    if above_ratio <= 0.35:
        return "潜龙"
    return "蛟龙"


def analyze(bars: list[dict], mode: str) -> dict | None:
    n = len(bars)
    if n < 30:
        return None
    open30_vol = sum(b["v"] for b in bars[:30]) / 30.0
    if open30_vol <= 0:
        return None

    min_len = 45 if mode == "ge" else 25
    max_len = 150 if mode == "ge" else 90
    best = None
    end = n - 1

    for length in range(min_len, min(max_len, n) + 1):
        start = end - length + 1
        if start < 0:
            break
        win = bars[start : end + 1]
        # 振幅：分钟收盘高低近似（文档注明略低估）
        cs = [b["c"] for b in win]
        hi, lo = max(cs), min(cs)
        mid_ref = sum(b["avg"] for b in win) / length
        if mid_ref <= 0:
            continue
        amp = (hi - lo) / mid_ref * 100
        if mode == "ge" and amp > 1.2:
            continue
        if mode == "tb" and amp > 2.5:
            continue

        abs_devs = [abs(b["c"] - b["avg"]) / b["avg"] * 100 for b in win]
        mean_dev = sum(abs_devs) / length
        max_dev = max(abs_devs)
        grade = classify_attach(mean_dev, max_dev, amp, mode)
        if not grade:
            continue

        above = sum(1 for b in win if b["c"] >= b["avg"]) / length
        cur = bars[end]["c"]
        win_avg = sum(cs) / length
        rel = (cur - win_avg) / win_avg * 100
        if rel < -2 or rel > 3.5:
            continue
        cur_dev = (cur - bars[end]["avg"]) / bars[end]["avg"] * 100
        if abs(cur_dev) > 2.5:
            continue

        win_vol = sum(b["v"] for b in win) / length
        vol_ratio = win_vol / open30_vol
        if vol_ratio >= 0.6:
            continue

        score = (
            {"★": 4, "○": 3, "△": 2, "弱": 1}[grade] * 10
            - mean_dev
            - amp * 0.2
            + min(length, 120) * 0.01
            - vol_ratio
        )
        cand = {
            "mode": mode,
            "grade": grade,
            "dragon": dragon_type(above),
            "start": win[0]["t"],
            "end": win[-1]["t"],
            "len": length,
            "amp": amp,
            "mean_dev": mean_dev,
            "max_dev": max_dev,
            "cur_dev": cur_dev,
            "rel": rel,
            "vol_ratio": vol_ratio,
            "score": score,
        }
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best


def main() -> None:
    now = datetime.now().strftime("%H:%M")
    print(f"[{now}] fetch market...")
    stocks = fetch_market()
    print(f"主板非ST: {len(stocks)}")

    hard = [
        s
        for s in stocks
        if s["mcap_yi"] < 1000
        and s["pct"] < 4
        and s["dist"] > 3
        and s["price"] > 0
        and s["turn"] >= 1.0
        and s["pct"] > -9
    ]
    print(f"硬门槛+换手过滤: {len(hard)}")

    results: list[tuple] = []
    done = 0

    def work(s: dict):
        bars = fetch_minutes(s["sym"])
        if not bars:
            return []
        out = []
        ge = analyze(bars, "ge")
        tb = analyze(bars, "tb")
        if ge:
            out.append((ge, s))
        if tb:
            if ge is None or abs(tb["len"] - ge["len"]) > 5 or tb["start"] != ge["start"]:
                out.append((tb, s))
        return out

    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = [ex.submit(work, s) for s in hard]
        for fut in as_completed(futs):
            done += 1
            if done % 150 == 0:
                print(f"progress {done}/{len(hard)} hits={len(results)}")
            try:
                picks = fut.result()
            except Exception:
                continue
            if picks:
                results.extend(picks)

    uniq: dict[tuple[str, str], tuple] = {}
    for meta, s in results:
        key = (s["code"], meta["mode"])
        if key not in uniq or meta["score"] > uniq[key][0]["score"]:
            uniq[key] = (meta, s)
    rows = list(uniq.values())

    def sort_key(item):
        meta, _ = item
        g = {"★": 0, "○": 1, "△": 2, "弱": 3}[meta["grade"]]
        return (g, meta["mean_dev"], -meta["len"])

    ge_rows = sorted([x for x in rows if x[0]["mode"] == "ge"], key=sort_key)
    tb_rows = sorted([x for x in rows if x[0]["mode"] == "tb"], key=sort_key)
    print(f"隔段命中 {len(ge_rows)} | 贴板命中 {len(tb_rows)}")

    out_path = "/Users/hanting/Documents/板块/盘面分析/20261029/分时三龙_20261029.md"
    lines = [
        "# 2026-10-29 分时三龙 · 盘中扫描（A 档·趴着观察）\n",
        f"> 规则：`模型条件/分时三龙.md` 第 5 节。扫描时点 **{now}**（盘中，未收盘）。\n"
        f"> 数据：新浪全市场列表 + 腾讯分钟线；黄线 = 成交额加权均价（VWAP）。振幅用分钟收盘高低近似（略低估）。仅研究参考，非投资建议。\n",
        "## 扫描口径\n",
        "| 步骤 | 数量 |",
        "|---|---|",
        f"| 沪深主板非ST | {len(stocks)} |",
        f"| 硬门槛+换手≥1%+剔大跌 | {len(hard)} |",
        f"| 隔段趴命中 | {len(ge_rows)} |",
        f"| 贴板趴命中 | {len(tb_rows)} |",
        "",
        "> 附加过滤（对齐 10/26）：缩量（窗均量 < 开盘30m均量×0.6）、换手≥1%、剔跌幅≥9%。按贴黄紧密度截断展示。\n",
    ]

    def emit(title: str, subset: list, limit: int):
        lines.append(f"## {title}\n")
        lines.append(
            "| 代码 | 名称 | 龙型 | 档 | 趴着窗口 | 振幅 | 均偏黄 | 最大偏 | 现偏黄 | 今日涨幅 | 量比(窗/开盘30m) | 市值(亿) |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for meta, s in subset[:limit]:
            lines.append(
                f"| {s['code']} | **{s['name']}** | {meta['dragon']} | {meta['grade']} | "
                f"{meta['start']}–{meta['end']}（{meta['len']}m） | {meta['amp']:.2f}% | "
                f"{meta['mean_dev']:.2f}% | {meta['max_dev']:.2f}% | {meta['cur_dev']:+.2f}% | "
                f"{s['pct']:+.2f}% | {meta['vol_ratio']:.2f} | {s['mcap_yi']:.0f} |"
            )
        lines.append("")

    ge_star = [x for x in ge_rows if x[0]["grade"] in ("★", "○")]
    ge_tri = [x for x in ge_rows if x[0]["grade"] == "△"]
    ge_weak = [x for x in ge_rows if x[0]["grade"] == "弱"]
    tb_good = [x for x in tb_rows if x[0]["grade"] in ("★", "○")]

    emit("一、隔段趴（主）· 优质 ★/○ · 前 20", ge_star, 20)
    emit("二、隔段趴 · 次优 △ · 前 10", ge_tri, 10)
    emit("三、隔段趴 · 弱 · 前 8", ge_weak, 8)
    emit("四、贴板趴（次）· ★/○ · 前 10", tb_good, 10)

    lines.append("## 使用提示\n")
    lines.append(
        "- 以上均为 **A 档**（还在趴、未抬头）。13:00–14:30 隔段二次抬头高发，破整理上沿+放量才升 **B 档**。\n"
        "- 14:30 后不再广筛新趴，只盯已有 B 档。\n"
        "- 禁止按主线缩池；本池按全市场硬门槛扫描。\n"
        "- 优先看：窗口起点较晚、振幅更窄、★/○ 档；全天从 09:30 起趴着的弹性不一定大。\n"
    )

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("wrote", out_path)

    print("\n=== 隔段★/○ Top15 ===")
    for meta, s in ge_star[:15]:
        print(
            f"{s['code']} {s['name']} {meta['grade']} {meta['dragon']} "
            f"{meta['start']}-{meta['end']}({meta['len']}m) amp={meta['amp']:.2f} "
            f"mean={meta['mean_dev']:.2f} pct={s['pct']:+.2f} turn={s['turn']:.1f}"
        )


if __name__ == "__main__":
    main()
