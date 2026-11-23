#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分时三龙 · 盘后确认（已涨停）扫描 + 对照今早 A 档漏检反思"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

UA = {"User-Agent": "Mozilla/5.0"}
DATE = "20261112"
EARLY_SEAL = "09:52"
GE_AMP_BASE = 1.5
GE_AMP_STAR_CIRCLE = 1.8
TB_AMP = 2.5
GE_LIFT = 5.0
TB_LIFT = 1.8
MORNING_A_MD = f"/Users/hanting/Documents/板块/盘面分析/{DATE}/分时三龙_{DATE}.md"
OUT_MD = f"/Users/hanting/Documents/板块/盘面分析/{DATE}/分时三龙_涨停确认_{DATE}.md"


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


def fbt_to_hhmm(fbt: int | str) -> str:
    s = str(int(fbt)).zfill(6)  # HHMMSS
    return f"{s[:2]}:{s[2:4]}"


def fetch_zt_pool() -> list[dict]:
    url = (
        "https://push2ex.eastmoney.com/getTopicZTPool?"
        + urllib.parse.urlencode(
            {
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "dpt": "wz.ztzt",
                "Pageindex": 0,
                "pagesize": 200,
                "sort": "fbt:asc",
                "date": DATE,
            }
        )
    )
    data = get_json(url)
    pool = (data.get("data") or {}).get("pool") or []
    out = []
    for row in pool:
        code = str(row.get("c") or "")
        name = str(row.get("n") or "")
        if not is_main_board(code):
            continue
        if "ST" in name.upper():
            continue
        # ltsz / tshare 东财单位：元
        ltsz = float(row.get("ltsz") or 0)
        tshare = float(row.get("tshare") or 0)
        mcap_yi = (tshare or ltsz) / 1e8
        if mcap_yi <= 0 and ltsz > 0:
            mcap_yi = ltsz / 1e8
        fbt = int(row.get("fbt") or 0)
        out.append(
            {
                "code": code,
                "name": name,
                "pct": float(row.get("zdp") or 0),
                "mcap_yi": mcap_yi,
                "fbt": fbt,
                "fbt_hm": fbt_to_hhmm(fbt) if fbt else "",
                "lbt": int(row.get("lbt") or 0),
                "lbc": int(row.get("lbc") or 0),
                "hybk": str(row.get("hybk") or ""),
                "sym": ("sh" if code.startswith("6") else "sz") + code,
            }
        )
    return out


def fetch_minutes(sym: str) -> list[dict] | None:
    url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
    try:
        data = get_json(url, timeout=10)
    except Exception:
        return None
    try:
        rows = data["data"][sym]["data"]["data"]
    except (KeyError, TypeError):
        return None
    bars = []
    cum_v = 0.0
    cum_a = 0.0
    for row in rows:
        parts = row.split()
        if len(parts) < 4:
            continue
        try:
            hhmm = parts[0]
            if len(hhmm) == 4:
                hhmm = hhmm[:2] + ":" + hhmm[2:]
            price = float(parts[1])
            vol_cum = float(parts[2])
            amt_cum = float(parts[3])
        except ValueError:
            continue
        if price <= 0:
            continue
        vol = max(0.0, vol_cum - cum_v)
        amt = max(0.0, amt_cum - cum_a)
        cum_v, cum_a = vol_cum, amt_cum
        avg = (cum_a / (cum_v * 100)) if cum_v > 0 else price
        bars.append({"t": hhmm, "c": price, "v": vol, "avg": avg})
    return bars


def ge_amp_limit(mean_dev: float) -> float:
    return GE_AMP_STAR_CIRCLE if mean_dev <= 0.45 else GE_AMP_BASE


def classify_attach(mean_dev: float, max_dev: float, amp: float, mode: str) -> str | None:
    if mode == "ge":
        if amp > ge_amp_limit(mean_dev):
            return None
        if mean_dev <= 0.25 and max_dev <= 0.7:
            return "★"
        if mean_dev <= 0.45 and max_dev <= 1.2:
            return "○"
        if mean_dev <= 0.8 and max_dev <= 1.6:
            return "△"
        if mean_dev <= 1.5 and max_dev <= 1.8 and amp <= GE_AMP_BASE:
            return "弱"
        return None
    if amp > TB_AMP:
        return None
    if mean_dev > 0.8 or max_dev > 1.8:
        return None
    if mean_dev <= 0.25 and max_dev <= 0.7:
        return "★"
    if mean_dev <= 0.45 and max_dev <= 1.2:
        return "○"
    if mean_dev <= 0.8 and max_dev <= 1.6:
        return "△"
    return None


def dragon_type(above_ratio: float) -> str:
    if above_ratio >= 0.75:
        return "卧龙"
    if above_ratio <= 0.35:
        return "潜龙"
    return "蛟龙"


def find_seal_idx(bars: list[dict], limit: float) -> int | None:
    for i, b in enumerate(bars):
        if b["c"] + 1e-6 >= limit:
            return i
    return None


def win_stats(win: list[dict]) -> dict | None:
    length = len(win)
    if length < 2:
        return None
    cs = [b["c"] for b in win]
    avgs = [b["avg"] for b in win]
    mid_ref = sum(avgs) / length
    if mid_ref <= 0:
        return None
    hi, lo = max(cs), min(cs)
    amp = (hi - lo) / mid_ref * 100
    abs_devs = [abs(b["c"] - b["avg"]) / b["avg"] * 100 for b in win]
    mean_dev = sum(abs_devs) / length
    max_dev = max(abs_devs)
    above = sum(1 for b in win if b["c"] >= b["avg"]) / length
    win_avg = sum(cs) / length
    return {
        "amp": amp,
        "mean_dev": mean_dev,
        "max_dev": max_dev,
        "above": above,
        "win_avg": win_avg,
        "hi": hi,
        "lo": lo,
        "len": length,
    }


def analyze_ge(bars: list[dict], seal_i: int, limit: float) -> dict | None:
    """隔段：横盘在封板前结束，与封板可隔开；抬头≥5%；结束后至封板不破均价98%。"""
    best = None
    # 横盘结束点至少早于封板若干分钟（隔开）；允许 5～90 分钟间隔，也允许更久
    for end in range(44, seal_i):  # end inclusive index, length至少45 → end-start+1>=45
        gap = seal_i - end
        if gap < 3:  # 至少隔开约3分钟，否则更像贴板
            continue
        for length in range(45, min(150, end + 1) + 1):
            start = end - length + 1
            if start < 0:
                break
            win = bars[start : end + 1]
            st = win_stats(win)
            if not st:
                continue
            if st["amp"] > GE_AMP_STAR_CIRCLE:
                continue
            if st["amp"] > ge_amp_limit(st["mean_dev"]):
                continue
            grade = classify_attach(st["mean_dev"], st["max_dev"], st["amp"], "ge")
            if not grade:
                continue
            lift = (limit - st["win_avg"]) / st["win_avg"] * 100
            if lift < GE_LIFT:
                continue
            # 横盘结束后至封板不破横盘均价 98%
            after = bars[end + 1 : seal_i + 1]
            floor = st["win_avg"] * 0.98
            if any(b["c"] < floor for b in after):
                continue
            score = (
                {"★": 4, "○": 3, "△": 2, "弱": 1}[grade] * 10
                - st["mean_dev"]
                - st["amp"] * 0.2
                + min(length, 120) * 0.01
                + min(gap, 60) * 0.02
                + lift * 0.05
            )
            cand = {
                "mode": "隔段",
                "grade": grade,
                "dragon": dragon_type(st["above"]),
                "start": win[0]["t"],
                "end": win[-1]["t"],
                "len": length,
                "amp": st["amp"],
                "mean_dev": st["mean_dev"],
                "max_dev": st["max_dev"],
                "lift": lift,
                "gap": gap,
                "score": score,
            }
            if best is None or cand["score"] > best["score"]:
                best = cand
    return best


def analyze_tb(bars: list[dict], seal_i: int, limit: float) -> dict | None:
    """贴板：封板前末端 25～90 分钟窄横，随后直接抬头封板。"""
    best = None
    end = seal_i - 1  # 横盘结束于封板前一分钟
    if end < 24:
        return None
    for length in range(25, min(90, end + 1) + 1):
        start = end - length + 1
        if start < 0:
            break
        win = bars[start : end + 1]
        st = win_stats(win)
        if not st:
            continue
        if st["amp"] > TB_AMP:
            continue
        grade = classify_attach(st["mean_dev"], st["max_dev"], st["amp"], "tb")
        if not grade:
            continue
        lift = (limit - st["win_avg"]) / st["win_avg"] * 100
        if lift < TB_LIFT:
            continue
        score = (
            {"★": 4, "○": 3, "△": 2, "弱": 1}[grade] * 10
            - st["mean_dev"]
            - st["amp"] * 0.2
            + min(length, 90) * 0.01
            + lift * 0.1
        )
        cand = {
            "mode": "贴板",
            "grade": grade,
            "dragon": dragon_type(st["above"]),
            "start": win[0]["t"],
            "end": win[-1]["t"],
            "len": length,
            "amp": st["amp"],
            "mean_dev": st["mean_dev"],
            "max_dev": st["max_dev"],
            "lift": lift,
            "gap": 1,
            "score": score,
        }
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best


def estimate_pre_limit(bars: list[dict], seal_i: int) -> tuple[float, float]:
    """用封板价反推昨收与涨停价；若首封前已有价，取接近封板价。"""
    seal_px = bars[seal_i]["c"]
    # 主板约 10%
    pre = round(seal_px / 1.1, 2)
    limit = round(pre * 1.1 + 1e-8, 2)
    # 微调：若 seal_px 就是涨停价
    if abs(seal_px - limit) > 0.02:
        limit = seal_px
        pre = round(limit / 1.1, 2)
    return pre, limit


def load_morning_a_codes() -> set[str]:
    try:
        text = open(MORNING_A_MD, encoding="utf-8").read()
    except OSError:
        return set()
    return set(re.findall(r"\|\s*(\d{6})\s*\|", text))


def why_missed(s: dict, meta: dict, morning: set[str], bars: list[dict], seal_i: int) -> list[str]:
    reasons = []
    code = s["code"]
    if code in morning:
        reasons.append("今早 A 档池已收录（展示截断前/后需对照全文；若仅在命中集合未进 Top 展示，属截断问题）")
        return reasons

    # 模拟 10:20 时点硬门槛与趴着条件
    # 取 10:22 左右 bar（与今早扫描 end 对齐）
    cut_t = "10:22"
    cut_i = None
    for i, b in enumerate(bars):
        if b["t"] <= cut_t:
            cut_i = i
    if cut_i is None or cut_i < 30:
        reasons.append(f"10:20 前后分钟线不足（cut_i={cut_i}），当时无法识别≥45m 隔段趴")
        return reasons

    # 封板是否发生在扫描前
    if seal_i <= cut_i:
        reasons.append(f"首封约 {bars[seal_i]['t']}，早于/等于扫描时点 10:22，盘中 A 档规则「距板>3% / 未封」会剔除")
        return reasons

    pre, limit = estimate_pre_limit(bars, seal_i)
    px = bars[cut_i]["c"]
    pct = (px / pre - 1) * 100
    dist = (limit - px) / px * 100
    if pct >= 4:
        reasons.append(f"10:22 时涨幅约 {pct:.2f}% ≥4%，硬门槛剔除")
    if dist <= 3:
        reasons.append(f"10:22 时距板约 {dist:.2f}% ≤3%，硬门槛剔除")

    # 用扫描时点之前的 bars 做隔段/贴板趴检测（对齐盘中 analyze，但简化）
    sub = bars[: cut_i + 1]
    open30 = sum(b["v"] for b in sub[:30]) / 30.0 if len(sub) >= 30 else 0
    ge_ok = False
    tb_ok = False
    detail = []
    if open30 > 0 and len(sub) >= 45:
        end = len(sub) - 1
        for length in range(45, min(150, len(sub)) + 1):
            start = end - length + 1
            win = sub[start : end + 1]
            st = win_stats(win)
            if not st:
                continue
            if st["amp"] > GE_AMP_STAR_CIRCLE:
                continue
            if st["amp"] > ge_amp_limit(st["mean_dev"]):
                continue
            grade = classify_attach(st["mean_dev"], st["max_dev"], st["amp"], "ge")
            if not grade:
                continue
            rel = (sub[end]["c"] - st["win_avg"]) / st["win_avg"] * 100
            if rel < -2 or rel > 3.5:
                continue
            vol_ratio = (sum(b["v"] for b in win) / length) / open30
            if vol_ratio >= 0.90:
                detail.append(f"隔段窗{win[0]['t']}-{win[-1]['t']}量比{vol_ratio:.2f}≥0.9")
                continue
            ge_ok = True
            detail.append(
                f"10:22回放隔段可命中 {grade} {win[0]['t']}-{win[-1]['t']} amp={st['amp']:.2f} mean={st['mean_dev']:.2f} vol={vol_ratio:.2f}"
            )
            break
    if open30 > 0 and len(sub) >= 25:
        end = len(sub) - 1
        for length in range(25, min(90, len(sub)) + 1):
            start = end - length + 1
            win = sub[start : end + 1]
            st = win_stats(win)
            if not st or st["amp"] > TB_AMP:
                continue
            grade = classify_attach(st["mean_dev"], st["max_dev"], st["amp"], "tb")
            if not grade:
                continue
            rel = (sub[end]["c"] - st["win_avg"]) / st["win_avg"] * 100
            if rel < -2 or rel > 3.5:
                continue
            vol_ratio = (sum(b["v"] for b in win) / length) / open30
            if vol_ratio >= 0.90:
                continue
            tb_ok = True
            break

    if ge_ok or tb_ok:
        reasons.append(
            "10:22 回放形态可过盘中门槛，但未进今早展示名单 → 更可能是：换手<1%被附加过滤、"
            "列表截断未展示、或当时分钟/行情源瞬时失败"
        )
        reasons.extend(detail[:2])
    else:
        reasons.append("10:22 回放：当时尚无合格「趴着段」（振幅/贴黄/缩量/相对均价未达标）")
        # 对照盘后横盘窗是否在扫描后才成型
        if meta["start"] > cut_t:
            reasons.append(f"盘后认定横盘起点 {meta['start']} 晚于扫描时点，属「扫描后才趴出」")
        elif meta["end"] > cut_t:
            reasons.append(f"盘后横盘窗延伸至 {meta['end']}，10:22 时窗口未走完/未收窄到位")
        if detail:
            reasons.extend(detail[:3])
        else:
            # 给盘后窗在 cut 时的状态
            reasons.append(
                f"盘后横盘为 {meta['start']}–{meta['end']}（{meta['len']}m，{meta['mode']}）；"
                f"首封 {bars[seal_i]['t']}；10:22涨幅≈{pct:.2f}% 距板≈{dist:.2f}%"
            )
    return reasons


def main() -> None:
    now = datetime.now().strftime("%H:%M")
    print(f"[{now}] fetch zt pool {DATE}...")
    pool = fetch_zt_pool()
    print(f"主板非ST涨停: {len(pool)}")
    big = [s for s in pool if s["mcap_yi"] >= 1000]
    cands = [s for s in pool if s["mcap_yi"] < 1000]
    early = [s for s in cands if s["fbt_hm"] and s["fbt_hm"] <= EARLY_SEAL]
    late = [s for s in cands if not s["fbt_hm"] or s["fbt_hm"] > EARLY_SEAL]
    print(f"市值≥1000亿剔: {len(big)} | 早封≤{EARLY_SEAL}: {len(early)} | 进入形态: {len(late)}")

    morning = load_morning_a_codes()
    print(f"今早A档展示代码数: {len(morning)}")

    hits: list[tuple[dict, dict, int, list]] = []
    fails: list[tuple[dict, str]] = []

    def work(s: dict):
        bars = fetch_minutes(s["sym"])
        if not bars or len(bars) < 40:
            return ("fail", s, "分钟线不足", None, None)
        # 用东财首封时间定位；再用价格确认
        seal_i = None
        for i, b in enumerate(bars):
            if b["t"] >= s["fbt_hm"]:
                seal_i = i
                break
        if seal_i is None:
            seal_i = find_seal_idx(bars, bars[-1]["c"])  # fallback
        if seal_i is None:
            return ("fail", s, "未定位封板分钟", None, None)
        _, limit = estimate_pre_limit(bars, seal_i)
        # 若该分钟尚未到涨停价，向后找真正封板
        real = None
        for i in range(seal_i, len(bars)):
            if bars[i]["c"] + 1e-6 >= limit:
                real = i
                break
        if real is None:
            # 可能午休前已封，用最高价
            hi_i = max(range(len(bars)), key=lambda i: bars[i]["c"])
            limit = bars[hi_i]["c"]
            real = hi_i
        seal_i = real
        if bars[seal_i]["t"] <= EARLY_SEAL:
            return ("fail", s, f"分钟确认早封 {bars[seal_i]['t']}", None, None)

        ge = analyze_ge(bars, seal_i, limit)
        tb = analyze_tb(bars, seal_i, limit)
        best = None
        if ge and tb:
            best = ge if ge["score"] >= tb["score"] else tb
        else:
            best = ge or tb
        if not best:
            return ("fail", s, "硬条件未命中", seal_i, bars)
        best["seal"] = bars[seal_i]["t"]
        best["limit"] = limit
        return ("hit", s, best, seal_i, bars)

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(work, s) for s in late]
        for fut in as_completed(futs):
            try:
                tag, s, payload, seal_i, bars = fut.result()
            except Exception as e:
                continue
            if tag == "hit":
                hits.append((s, payload, seal_i, bars))
            else:
                fails.append((s, str(payload)))

    def sort_key(item):
        _, meta, _, _ = item
        g = {"★": 0, "○": 1, "△": 2, "弱": 3}[meta["grade"]]
        mode = 0 if meta["mode"] == "隔段" else 1
        return (g, mode, meta["mean_dev"])

    hits.sort(key=sort_key)

    # 反思
    reflections = []
    for s, meta, seal_i, bars in hits:
        rs = why_missed(s, meta, morning, bars, seal_i)
        reflections.append((s, meta, rs, s["code"] in morning))

    # 写报告
    lines = [
        f"# 2026-11-12 分时三龙 · 涨停确认（截至 {now}）\n",
        "> 规则：`模型条件/分时三龙.md` 第 1～4 节盘后确认。池：东财涨停池主板；剔 ST/科创/创业/北交/市值≥1000亿/≤09:52早封。\n"
        "> 分时：腾讯分钟线；黄线=VWAP。振幅用分钟收盘高低（略低估）。未收盘样本以当前已封为准。仅研究参考。\n",
        "## 扫描口径\n",
        "| 项 | 数量 |",
        "|----|------|",
        f"| 主板非ST涨停 | {len(pool)} |",
        f"| 市值≥1000亿剔除 | {len(big)} |",
        f"| 早封≤09:52剔除 | {len(early)} |",
        f"| 进入形态识别 | {len(late)} |",
        f"| **三龙涨停合计（计入）** | **{len(hits)}** |",
        f"| 其中优质 ★/○ | {sum(1 for _,m,_,_ in hits if m['grade'] in ('★','○'))} |",
        f"| 其中次优 △ | {sum(1 for _,m,_,_ in hits if m['grade']=='△')} |",
        f"| 其中弱贴黄·隔段 | {sum(1 for _,m,_,_ in hits if m['grade']=='弱')} |",
        "",
    ]

    def emit(title: str, subset: list):
        lines.append(f"## {title}\n")
        if not subset:
            lines.append("（无）\n")
            return
        lines.append(
            "| 代码 | 名称 | 类型 | 模式 | 横盘时段 | 振幅 | 抬头 | 封板约 | 均偏黄 | 最大偏 | 今早A档 |"
        )
        lines.append("|------|------|------|------|----------|------|------|--------|--------|--------|--------|")
        for s, meta, seal_i, bars in subset:
            in_a = "是" if s["code"] in morning else "否"
            lines.append(
                f"| {s['code']} | **{s['name']}** | {meta['dragon']} | **{meta['mode']}** | "
                f"{meta['start']}–{meta['end']}（{meta['len']}m） | {meta['amp']:.2f}% | "
                f"{meta['lift']:.2f}% | {meta['seal']} | {meta['mean_dev']:.2f}%{meta['grade']} | "
                f"{meta['max_dev']:.2f}% | {in_a} |"
            )
        lines.append("")

    emit("一、优质（★ / ○）", [x for x in hits if x[1]["grade"] in ("★", "○")])
    emit("二、次优（△）", [x for x in hits if x[1]["grade"] == "△"])
    emit("三、弱贴黄·隔段（弱）", [x for x in hits if x[1]["grade"] == "弱"])

    lines.append("## 四、漏检反思（盘后命中 vs 今早 10:20 A 档）\n")
    if not hits:
        lines.append("今日截至目前无三龙涨停计入，无需漏检反思。\n")
    else:
        missed = [x for x in reflections if not x[3]]
        caught = [x for x in reflections if x[3]]
        lines.append(f"- 盘后命中 {len(hits)} 只；今早展示名单已覆盖 {len(caught)} 只；**未覆盖 {len(missed)} 只**。\n")
        for s, meta, rs, in_a in reflections:
            flag = "已覆盖" if in_a else "**漏检**"
            lines.append(f"### {s['code']} {s['name']}（{meta['mode']} {meta['grade']} · {flag}）\n")
            for r in rs:
                lines.append(f"- {r}")
            lines.append("")

    lines.append("## 五、早封摘要\n")
    early_names = "、".join(f"{s['name']}({s['fbt_hm']})" for s in early[:40])
    lines.append(f"- 早封≤09:52共 {len(early)}：{early_names}{'…' if len(early)>40 else ''}。\n")

    lines.append("## 六、硬条件未命中（非早封）\n")
    if fails:
        lines.append("、".join(f"{s['name']}({reason})" for s, reason in fails[:50]))
        lines.append("")
    else:
        lines.append("（无）\n")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("wrote", OUT_MD)
    print(f"\n=== 三龙涨停计入 {len(hits)} ===")
    for s, meta, _, _ in hits:
        print(
            f"{s['code']} {s['name']} {meta['mode']} {meta['grade']} {meta['dragon']} "
            f"{meta['start']}-{meta['end']}({meta['len']}m) amp={meta['amp']:.2f} "
            f"lift={meta['lift']:.2f} seal={meta['seal']} morning={'Y' if s['code'] in morning else 'N'}"
        )


if __name__ == "__main__":
    main()
