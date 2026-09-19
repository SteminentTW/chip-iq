# -*- coding: utf-8 -*-
"""
海外授權夥伴資料回歸測試 —— data/partners.json 的守門員

跟 verify_broker.py 一樣的角色：測試沒過就不發佈。但這裡多了一層分級，因為
Yahoo 是非官方端點、抓不到是正常的營運狀況，不該讓整個排程停擺：

  ❌ FAIL（exit 1，擋下發佈）—— 資料是「錯的」
     算錯、對不上外部來源、日期倒退、市值與股數不一致、停牌列混進序列……
     這種東西推上去會讓人看到錯的數字，寧可讓網頁停在昨天。

  ⚠️ WARN（exit 0，照常發佈）—— 資料是「舊的」或「缺的」
     本次抓取失敗但沿用了上一次的良好資料、資料超過 N 天沒更新、某家從未抓到過。
     這些狀態 JSON 裡都有標記，前端會顯示「資料最後成功更新於 X」或「無資料」。
     加 --strict 可把 WARN 也當成失敗（人工驗收時用）。

最重要的一組測試是 GOLDEN —— 用**外部主要來源**（韓媒報導的實際成交價、Naver 수정주가、
決算短信）交叉驗證。這組存在的理由：2026-08 初版簡報從「close == adjclose」誤推出
「Yahoo 沒還原分割」，把 298060 的 2 年報酬寫成 −95.7%（正確 −78.3%）。純內部一致性
檢查抓不到這種錯，只有對外部真值比對才抓得到。

用法：
    python verify_partners.py              # 正常回歸測試
    python verify_partners.py --strict     # WARN 也視為失敗
    python verify_partners.py --self-test  # 突變測試：證明上面那些檢查真的抓得到錯
"""
import copy, json, os, subprocess, sys
from datetime import date, timedelta

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA = os.path.join(ROOT, "data")
OUT_REL = "data/partners.json"

MAX_GAP_DAYS = 12          # 超過此天數的交易日缺口必須被已宣告的停牌涵蓋
STALE_WARN_DAYS = 10
CAP_TOL = 1.0              # 市值容忍（貨幣單位）：只吸收 round 的尾差
PCT_TOL = 0.011            # 百分比容忍（與 verify_broker.py 同一慣例）

# ── 外部主要來源交叉驗證的錨點 ────────────────────────────────────────
# c  = Yahoo v8 chart 的 close（已含分割還原）
# rc = 當時實際成交的收盤價（本站由 close 回推）
# 只要錨點還在 2 年窗格內就必須命中；滾出窗格則降級為 WARN。
GOLDEN = {
    "298060.KQ": [
        # 뉴스워커 2026-03-17：관리종목 해제 번복 당일 종가 773원 (−5.73%)
        # → 這一筆同時釘住了「還原方向」與「還原倍率」，是最強的一個錨點
        {"d": "2026-03-17", "c": 3865.0, "rc": 773.0,
         "src": "뉴스워커：2026-03-17 종가 773원"},
        {"d": "2026-03-16", "c": 4100.0, "rc": 820.0,
         "src": "與前一日搭配可還原出報導的 −5.73%"},
        {"d": "2026-03-17", "chg_from_prev_pct": -5.73,
         "src": "뉴스워커：당일 −5.73%"},
        # 병합 전 마지막 거래일，실제 체결가 927원（停牌期間 Yahoo 凍結在還原後 4,635）
        {"d": "2026-04-22", "c": 4635.0, "rc": 927.0,
         "src": "액면병합 전 마지막 거래일 실제가 927원"},
        # Naver 금융 수정주가 52주 최고／최저
        {"d": "2025-09-04", "h": 6900.0, "src": "Naver 수정주가 52주 최고 6,900"},
        {"d": "2026-07-30", "l": 2300.0, "src": "Naver 수정주가 52주 최저 2,300"},
        # 恢復交易首日：3,705 是新基準下的真實成交，不是相對 4,635 的單日暴跌
        {"d": "2026-05-19", "c": 3705.0, "rc": 3705.0,
         "src": "재상장 첫날 종가（분할 이후이므로 rc == c）"},
        {"d": "2026-08-14", "c": 2605.0, "rc": 2605.0,
         "src": "Naver 현재가 2,605（시총 251억의 분모）"},
    ],
    "4978.T": [
        # PMDA 申請前後的量價（事實；歸因於消息面屬推論，不在測試範圍）
        {"d": "2026-06-24", "c": 120.0, "v": 687100, "src": "PMDA 申請當日"},
        {"d": "2026-06-25", "c": 132.0, "v": 4316000, "src": "翌日量能 6.3 倍"},
        {"d": "2026-08-14", "c": 148.0, "rc": 148.0,
         "src": "決算短信期後基準日股價；無分割故 rc == c"},
    ],
}

# 市值的外部交叉驗證。
# 鐵則：value 必須是「外部網站實際顯示的數字」，不能是本專案自己的推導結果——
# 否則這條檢查就是拿自己的答案對自己的答案，改壞了也不會 fail。
# value 是「as_of 那天」的市值，不是今天的：檢查會用序列裡 as_of 當天的收盤價
# 重算來比對。不要拿它去對最新市值——股價每天在動，那樣會天天 fail。
# 韓國：Naver 금융 시총（外部真值）。
# 日本：發行済株式総数 101,011,891（＝決算短信 98,079,891 ＋ 2026-07-16 第三者割當増資 2,932,000）
#       × 終値 148（2026-08-14）＝ 14,949,759,868，與日本各報價站顯示的時価総額 149.5 億円同級。
GOLDEN_CAP = {
    "298060.KQ": {"value": 25105549435.0, "shares": 9637447, "as_of": "2026-08-14",
                  "src": "Naver 금융 시총 251억 = 9,637,447 × 2,605"},
    "4978.T": {"value": 14949759868.0, "shares": 101011891, "as_of": "2026-08-14",
               "src": "發行済株式総数 101,011,891 × 終値 148（日本各報價站時価総額 ≈149.5 億円）"},
}


class Report:
    def __init__(self):
        self.fails, self.warns, self.lines = [], [], []

    def fail(self, cid, msg):
        self.fails.append(f"[{cid}] {msg}")

    def warn(self, cid, msg):
        self.warns.append(f"[{cid}] {msg}")

    def ok(self, cid, msg):
        self.lines.append(f"  ✅ [{cid}] {msg}")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def git_prev(rel):
    """上一次已提交的版本。CI 上跑在 fetch 之後、commit 之前，HEAD 正好是前一版。"""
    try:
        r = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT,
                           capture_output=True, timeout=30)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout.decode("utf-8"))
    except Exception:
        return None


def raw_factor(day, splits):
    """由 splits_detected 獨立重算「還原後 → 當時實際價」的因子。

    刻意不從 JSON 讀現成的因子欄位 —— 那樣等於拿被驗證的東西驗證自己。
    """
    f = 1.0
    for s in splits:
        if day < s["date"]:
            f *= float(s["numerator"]) / float(s["denominator"])
    return f


# ── 各項檢查 ─────────────────────────────────────────────────────────

def check_structure(doc, ref_doc, R):
    want = {p["id"]: p["symbol"] for p in ref_doc["partners"]}
    got = {p.get("id"): p.get("symbol") for p in doc.get("partners", [])}
    for pid, sym in want.items():
        if pid not in got:
            R.fail("C1", f"參考資料有 {pid}（{sym}）但 partners.json 找不到")
        elif got[pid] != sym:
            R.fail("C1", f"{pid} 的 symbol 不符：{got[pid]} != {sym}")
    if not R.fails:
        R.ok("C1", f"參考資料的 {len(want)} 家夥伴都在輸出中")


def check_partner(p, R):
    sym = p.get("symbol", "?")
    series = p.get("series") or []
    status = p.get("data_status")

    if not series:
        if status == "unavailable":
            R.warn("W3", f"{sym} 從未成功取得行情（{p.get('last_error')}）→ 前端顯示「無資料」")
        else:
            R.fail("C1", f"{sym} 沒有序列，但 data_status={status}（應為 unavailable）")
        return
    if not p.get("fetch_ok"):
        R.warn("W1", f"{sym} 本次抓取失敗，沿用 {p.get('as_of')} 的資料"
                     f"（{p.get('stale_days')} 天前）：{p.get('last_error')}")

    # C2 日期
    days = [r["d"] for r in series]
    bad = [d for d in days if len(d) != 10 or d[4] != "-" or d[7] != "-"]
    if bad:
        R.fail("C2", f"{sym} 日期格式不合法：{bad[:3]}")
    if len(set(days)) != len(days):
        dup = sorted({d for d in days if days.count(d) > 1})
        R.fail("C2", f"{sym} 日期重複：{dup[:5]}")
    if days != sorted(days):
        R.fail("C2", f"{sym} 日期未嚴格遞增")
    if p.get("as_of") != days[-1]:
        R.fail("C2", f"{sym} as_of {p.get('as_of')} 與序列末日 {days[-1]} 不一致")
    if not [f for f in R.fails if f.startswith("[C2]")]:
        R.ok("C2", f"{sym} {len(series)} 筆日期遞增無重複（{days[0]} ~ {days[-1]}）")

    # C3 價格為正、OHLC 關係成立
    px_bad = []
    for r in series:
        vals = [r.get(k) for k in ("o", "h", "l", "c")]
        if any(v is None or v <= 0 for v in vals):
            px_bad.append((r["d"], "非正值或缺值"))
            continue
        o, h, l, c = vals
        if not (l <= min(o, c) and max(o, c) <= h and l <= h):
            px_bad.append((r["d"], f"OHLC 關係不成立 {o}/{h}/{l}/{c}"))
        if r.get("v") is None or r["v"] <= 0:
            px_bad.append((r["d"], f"成交量非正 {r.get('v')}"))
    if px_bad:
        R.fail("C3", f"{sym} 價量不合理 {len(px_bad)} 筆，前 3 筆：{px_bad[:3]}")
    else:
        R.ok("C3", f"{sym} 全數 OHLC 為正且 low ≤ open/close ≤ high、成交量 > 0")

    # C10 停牌／缺值列不得留在序列裡（會污染波動度與最大回撤）
    zero = [r["d"] for r in series if not r.get("v")]
    if zero:
        R.fail("C10", f"{sym} 序列含 volume=0 的列 {len(zero)} 筆（停牌前值填補，應剔除）：{zero[:3]}")
    else:
        R.ok("C10", f"{sym} 已剔除 {p.get('dropped_rows', {}).get('count', 0)} 筆停牌／缺值列")

    # C4 交易日缺口必須被已宣告的停牌涵蓋
    halts = ((p.get("corporate_actions") or {}).get("trading_halts") or [])
    for a, b in zip(series, series[1:]):
        gap = (date.fromisoformat(b["d"]) - date.fromisoformat(a["d"])).days
        if gap <= MAX_GAP_DAYS:
            continue
        covered = any(h["from"] <= b["d"] and a["d"] <= h["to"] for h in halts)
        if covered:
            R.ok("C4", f"{sym} {a['d']} → {b['d']} 缺口 {gap} 天，已由宣告的停牌涵蓋")
        else:
            R.fail("C4", f"{sym} {a['d']} → {b['d']} 缺口 {gap} 天，無對應的停牌宣告")

    # C5 還原因子：rc 必須等於 c × 由 splits 重算的因子；分割日之後因子必為 1
    splits = p.get("splits_detected") or []
    f_bad, post_bad = [], []
    for r in series:
        f = raw_factor(r["d"], splits)
        want = round(r["c"] * f, 2)
        if abs(r.get("rc", 0) - want) > 0.01:
            f_bad.append((r["d"], r.get("rc"), want, f))
        if splits and r["d"] >= splits[-1]["date"] and abs(f - 1.0) > 1e-12:
            post_bad.append(r["d"])
    if f_bad:
        R.fail("C5", f"{sym} rc 與由分割事件重算的因子不符 {len(f_bad)} 筆，"
                     f"前 3 筆（日期, rc, 應為, 因子）：{f_bad[:3]}")
    else:
        R.ok("C5", f"{sym} rc = close × Π(num/den) 全數吻合"
                   f"（分割 {len(splits)} 筆{'：' + splits[0]['ratio'] + ' @ ' + splits[0]['date'] if splits else ''}）")
    if post_bad:
        R.fail("C5", f"{sym} 分割事件日之後仍套用了還原因子：{post_bad[:3]}")

    # C6 外部主要來源錨點
    idx = {r["d"]: r for r in series}
    hit = skip = 0
    for g in GOLDEN.get(sym, []):
        d = g["d"]
        if d < days[0]:
            skip += 1
            R.warn("W4", f"{sym} 錨點 {d} 已滾出 2 年窗格，本次未驗（{g['src']}）")
            continue
        r = idx.get(d)
        if not r:
            R.fail("C6", f"{sym} 錨點 {d} 落在窗格內卻查無此交易日（{g['src']}）")
            continue
        for k in ("c", "rc", "h", "l"):
            if k in g and abs(r.get(k, 0) - g[k]) > 0.01:
                R.fail("C6", f"{sym} {d} 的 {k} = {r.get(k)}，外部來源為 {g[k]}（{g['src']}）")
            elif k in g:
                hit += 1
        if "v" in g and r.get("v") != g["v"]:
            R.fail("C6", f"{sym} {d} 的成交量 = {r.get('v')}，外部來源為 {g['v']}（{g['src']}）")
        elif "v" in g:
            hit += 1
        if "chg_from_prev_pct" in g:
            i = days.index(d)
            if i == 0:
                R.warn("W4", f"{sym} 錨點 {d} 無前一交易日可比")
            else:
                got = (r["c"] / series[i - 1]["c"] - 1) * 100
                if abs(got - g["chg_from_prev_pct"]) > PCT_TOL:
                    R.fail("C6", f"{sym} {d} 日變動 {got:+.2f}%，外部來源為 "
                                 f"{g['chg_from_prev_pct']:+.2f}%（{g['src']}）")
                else:
                    hit += 1
    if hit:
        R.ok("C6", f"{sym} 外部來源錨點命中 {hit} 項"
                   f"{'（另 ' + str(skip) + ' 項已滾出窗格）' if skip else ''}")

    # C7 市值＝股價 × 股數，且股數三欄自洽
    sh = p.get("shares") or {}
    if sh.get("issued") is not None and sh.get("treasury") is not None:
        if sh["issued"] - sh["treasury"] != sh.get("outstanding"):
            R.fail("C7", f"{sym} 股數不自洽：issued {sh['issued']} − treasury "
                         f"{sh['treasury']} != outstanding {sh.get('outstanding')}")
    mc = p.get("market_cap")
    if not mc:
        R.warn("W1", f"{sym} 無市值（行情缺）")
    else:
        basis = mc.get("shares_basis")
        if sh.get(basis) != mc.get("shares_used"):
            R.fail("C7", f"{sym} market_cap.shares_used {mc.get('shares_used')} "
                         f"與 shares.{basis} {sh.get(basis)} 不一致")
        want = (mc.get("shares_used") or 0) * (mc.get("price") or 0)
        if abs((mc.get("value") or 0) - want) > CAP_TOL:
            R.fail("C7", f"{sym} 市值 {mc.get('value'):,.0f} != 股數 × 股價 {want:,.0f}")
        if mc.get("price") != series[-1]["c"] or mc.get("price_date") != days[-1]:
            R.fail("C7", f"{sym} 市值用的股價／日期（{mc.get('price')} @ {mc.get('price_date')}）"
                         f"不是序列最新值（{series[-1]['c']} @ {days[-1]}）")
        g = GOLDEN_CAP.get(sym)
        if g:
            # 錨點是「基準日當天」的外部市值，不是今天的：股價每天在動，拿今天的
            # 市值去對一個固定數字必然天天失敗（2026-08-17 就是這樣掛的）。
            # 正確作法是用我們自己序列裡「基準日」的收盤價重算，等於同時驗了
            # 股數與那天的收盤價都跟外部網站對得上。
            # 股數一旦改變就靜默跳過的話，等於把這條防線整條拔掉——改成明確警告。
            anchor = next((r for r in series if r["d"] == g["as_of"]), None)
            if mc.get("shares_used") != g["shares"]:
                R.warn("C7", f"{sym} 股數已從外部錨點的 {g['shares']:,} 改為 "
                             f"{mc.get('shares_used'):,}，外部市值交叉驗證這次沒有執行。"
                             f"請同步更新 GOLDEN_CAP（錨點基準日 {g.get('as_of')}）。")
            elif anchor is None:
                R.warn("C7", f"{sym} 外部市值錨點基準日 {g['as_of']} 不在序列內"
                             f"（已滾出窗格或當天無交易），交叉驗證這次沒有執行。"
                             f"請更新 GOLDEN_CAP 到較近的基準日。")
            elif abs(g["shares"] * anchor["c"] - g["value"]) > CAP_TOL:
                R.fail("C7", f"{sym} {g['as_of']} 市值 {g['shares'] * anchor['c']:,.0f}"
                             f"（{g['shares']:,} × {anchor['c']:,.2f}）與外部交叉驗證值 "
                             f"{g['value']:,.0f} 不符（{g['src']}）")
        if not [f for f in R.fails if f.startswith("[C7]")]:
            R.ok("C7", f"{sym} 市值 {mc['display']}（{mc['display_local']}）"
                       f"= {mc['shares_used']:,} × {mc['price']:,.2f} ✓")

        # C8 歷史市值：依查證結論不可回推
        h = mc.get("history") or {}
        if h.get("available"):
            if not h.get("series"):
                R.fail("C8", f"{sym} market_cap.history.available=true 卻沒有序列")
        elif not h.get("reason"):
            R.fail("C8", f"{sym} 歷史市值標記為不可得，卻沒有寫原因")
        else:
            R.ok("C8", f"{sym} 歷史市值已標記為不可回推並附原因（前端顯示「無法回推」）")

    # C11 績效數字與序列自洽，52 週高低與 Yahoo meta 交叉核對
    perf = p.get("performance")
    if perf:
        want = round((series[-1]["c"] / series[0]["c"] - 1) * 100, 2)
        if abs(perf.get("return_pct", 0) - want) > PCT_TOL:
            R.fail("C11", f"{sym} 期間報酬 {perf.get('return_pct')}% 與序列首末算出的 {want}% 不符")
        # 52 週高低：Yahoo 的視窗邊界跟我們可能差一天，那不是資料有問題。
        # 2026-09-09 的排程就因此擋掉一整天：我們的視窗含 2025-09-09（盤中高 6900），
        # Yahoo 的視窗已把那天滾出去、取到次高的 6790，兩邊都沒算錯。
        # 所以改成比對「合理視窗帶」（365±7 天各算一次，取兩者構成的區間）：
        # 自算值與 Yahoo 都必須落在帶內。序列真被動過手腳時整條帶會跟著偏，
        # 對方的值就會掉出去，一樣擋得下來——而且這比原本只比單一數字更嚴，
        # 因為現在連我們自己 perf 裡的數字都要能被序列重算出來。
        last_d = date.fromisoformat(series[-1]["d"])

        def window_band(field, agg):
            out = []
            for days in (372, 358):
                cut = (last_d - timedelta(days=days)).isoformat()
                vals = [r[field] for r in series
                        if r.get(field) is not None and r["d"] >= cut]
                if not vals:
                    return None
                out.append(agg(vals))
            return min(out), max(out)

        # Yahoo 的 meta 有時比它同一份回應裡的 chart 序列快一個交易日。新的那天
        # 剛創新高／新低時，meta 已經反映、我們的序列還沒收到，值就會往「更極端」
        # 的方向掉出帶外——2026-09-09、09-11、09-14、09-17 四次停發都是這樣，事後
        # 都證實 Yahoo 是對的（例：9/17 的 2240.0 就是當天最低）。用 meta 自己的
        # session 日期當證據放行這一種，且只放行更極端的方向；往「更不極端」跑出
        # 帶外仍照擋（那是視窗舊端的差異，帶本來就涵蓋）。沒有這個日期時行為不變。
        meta_as_of = perf.get("yahoo_meta_as_of")
        meta_ahead = bool(meta_as_of and meta_as_of > series[-1]["d"])

        crossed, ahead = [], []
        for key, ykey, field, agg, label in (
                ("high_52w", "high_52w_yahoo", "h", max, "52 週高"),
                ("low_52w", "low_52w_yahoo", "l", min, "52 週低")):
            band = window_band(field, agg)
            if band is None:
                continue
            lo_b, hi_b = band
            ours = perf.get(key)
            if ours is not None and not (lo_b - 0.01 <= ours <= hi_b + 0.01):
                R.fail("C11", f"{sym} 自算{label} {ours} 不在序列重算得出的範圍 "
                              f"{lo_b}～{hi_b} 內")
            y = perf.get(ykey)
            # Yahoo 偶爾整欄回 0（8/27 就因此擋掉一整天）。價格不可能 ≤ 0，
            # 那是「這次沒給」而不是「跟我們算的不一樣」，跳過不當成失敗。
            if not (y or 0) > 0:
                continue
            if lo_b - 0.01 <= y <= hi_b + 0.01:
                crossed.append(label)
                continue
            more_extreme = y > hi_b if agg is max else y < lo_b
            if meta_ahead and more_extreme:
                ahead.append(f"{label} Yahoo meta {y} 更極端但 meta 已含 "
                             f"{meta_as_of}、本站序列到 {series[-1]['d']}，放行")
            else:
                R.fail("C11", f"{sym} Yahoo meta {label} {y} 不在序列重算得出的範圍 "
                              f"{lo_b}～{hi_b} 內")
        if not [f for f in R.fails if f.startswith("[C11]")]:
            parts = []
            if crossed:
                parts.append("／".join(crossed) + " 與 Yahoo meta 一致")
            parts.extend(ahead)
            cross = ("、".join(parts) if parts
                     else "Yahoo meta 未提供 52 週高低，本次未交叉核對")
            R.ok("C11", f"{sym} 期間報酬 {perf['return_pct']:+.2f}%"
                        f"（{perf['first']['d']} {perf['first']['c']:,.2f}"
                        f" → {perf['last']['d']} {perf['last']['c']:,.2f}）"
                        f"、{cross}")

    # C12 溯源：股數每一筆變動都要有來源與基準日；註記必須分清事實與推論
    for comp in (sh.get("components") or []):
        if not comp.get("source_url") or not comp.get("as_of"):
            R.fail("C12", f"{sym} 股數來源缺 source_url 或 as_of：{comp.get('label')}")
    if sh and not sh.get("basis_date"):
        R.fail("C12", f"{sym} 股數缺 basis_date")
    for n in (p.get("notes") or []):
        if n.get("type") not in ("fact", "inference"):
            R.fail("C12", f"{sym} 註記的 type 必須是 fact 或 inference：{n.get('type')}")
        if n.get("type") == "fact" and not n.get("source_url"):
            R.fail("C12", f"{sym} 標為 fact 的註記沒有來源：{str(n.get('text'))[:40]}")
    if not [f for f in R.fails if f.startswith("[C12]")]:
        n_inf = sum(1 for n in (p.get("notes") or []) if n.get("type") == "inference")
        R.ok("C12", f"{sym} 股數 {len(sh.get('components') or [])} 筆變動皆有來源；"
                    f"註記 {len(p.get('notes') or [])} 則（其中 {n_inf} 則標為推論）")


def check_monotonic(doc, prev, R):
    """資料日期只能往前 —— 抓失敗時沿用舊資料是可以的，但不能比舊資料還舊。"""
    if not prev:
        R.warn("W5", "找不到上一版 partners.json（首次執行或不在 git 中），略過時間單調檢查")
        return
    a = ((doc.get("meta") or {}).get("last_success") or {}).get("data_date")
    b = ((prev.get("meta") or {}).get("last_success") or {}).get("data_date")
    if a and b and a < b:
        R.fail("C9", f"meta.last_success.data_date 倒退：本次 {a} < 上一版 {b}")
    prev_p = {p.get("id"): p for p in prev.get("partners", [])}
    for p in doc.get("partners", []):
        q = prev_p.get(p.get("id"))
        if q and q.get("as_of") and p.get("as_of") and p["as_of"] < q["as_of"]:
            R.fail("C9", f"{p.get('symbol')} 的 as_of 倒退：本次 {p['as_of']} < 上一版 {q['as_of']}")
    if not [f for f in R.fails if f.startswith("[C9]")]:
        R.ok("C9", f"資料日期未倒退（上一版 {b} → 本次 {a}）")


def check_freshness(doc, R):
    m = doc.get("meta") or {}
    if m.get("stale"):
        R.warn("W2", f"資料已 {m.get('stale_days')} 天未成功更新"
                     f"（門檻 {m.get('stale_after_days')} 天），最後成功 "
                     f"{(m.get('last_success') or {}).get('data_date')}")
    # 個別失敗已由 check_partner 逐家報出（W1／W3），這裡只補一行總計
    run = m.get("run") or {}
    if run.get("failures"):
        R.warn("W1", f"本次 {run.get('partners_total')} 家中有 "
                     f"{len(run['failures'])} 家抓取失敗，"
                     f"已成功 {run.get('partners_fetched')} 家（詳見上方逐家說明）")


def run_checks(doc, ref_doc, prev):
    R = Report()
    check_structure(doc, ref_doc, R)
    for p in doc.get("partners", []):
        check_partner(p, R)
    check_monotonic(doc, prev, R)
    check_freshness(doc, R)
    return R


# ── 突變測試：證明上面那些檢查真的抓得到錯 ──────────────────────────────

def _p(doc, pid="poongjeon"):
    return next(p for p in doc["partners"] if p["id"] == pid)


def mut_factor_one(doc):
    """把還原因子改成 1（rc = c）—— 這正是 2026-08 初版簡報犯的錯的鏡像。"""
    p = _p(doc)
    for r in p["series"]:
        r["rc"] = r["c"]
    return doc


def mut_double_adjust(doc):
    """對已還原的序列再乘一次 5 —— 會得出錯誤的 −95.7%。"""
    p = _p(doc)
    for r in p["series"]:
        if r["d"] < "2026-05-19":
            for k in ("o", "h", "l", "c"):
                r[k] *= 5
    p["performance"]["return_pct"] = round(
        (p["series"][-1]["c"] / p["series"][0]["c"] - 1) * 100, 2)
    return doc


def mut_negative_price(doc):
    _p(doc)["series"][100]["c"] = -1.0
    return doc


def mut_duplicate_date(doc):
    p = _p(doc)
    p["series"].insert(50, dict(p["series"][49]))
    return doc


def mut_meta_date_regression(doc):
    doc["meta"]["last_success"]["data_date"] = "2000-01-01"
    return doc


def mut_partner_date_regression(doc):
    """某家的資料整個倒退一天（序列本身仍自洽，只有 C9 抓得到）。"""
    p = _p(doc)
    p["series"] = p["series"][:-1]
    p["as_of"] = p["series"][-1]["d"]
    p["latest"] = p["series"][-1]
    p["prev"] = p["series"][-2]
    p["market_cap"]["price"] = p["latest"]["c"]
    p["market_cap"]["price_date"] = p["latest"]["d"]
    p["market_cap"]["value"] = p["market_cap"]["shares_used"] * p["latest"]["c"]
    p["performance"]["last"] = {"d": p["latest"]["d"], "c": p["latest"]["c"]}
    p["performance"]["return_pct"] = round(
        (p["series"][-1]["c"] / p["series"][0]["c"] - 1) * 100, 2)
    return doc


def mut_market_cap(doc):
    _p(doc)["market_cap"]["value"] *= 1.1
    return doc


def mut_shares_mismatch(doc):
    _p(doc)["shares"]["outstanding"] = 12345678
    return doc


def mut_halt_rows_back(doc):
    """把停牌那段前值填補的列塞回序列（會讓走勢圖出現假的水平線）。"""
    p = _p(doc)
    i = next(i for i, r in enumerate(p["series"]) if r["d"] == "2026-04-22")
    p["series"][i + 1:i + 1] = [
        {"d": d, "o": 4635.0, "h": 4635.0, "l": 4635.0, "c": 4635.0, "v": 0, "rc": 927.0}
        for d in ("2026-04-23", "2026-04-24")]
    return doc


def mut_drop_partner(doc):
    doc["partners"] = [p for p in doc["partners"] if p["id"] != "poongjeon"]
    return doc


def mut_fake_cap_history(doc):
    _p(doc)["market_cap"]["history"] = {"available": True}
    return doc


def mut_unsourced_fact(doc):
    _p(doc)["notes"].append({"type": "fact", "text": "授權案已停滯", "source_url": None})
    return doc


def mut_undeclared_gap(doc):
    """製造一個沒有停牌宣告可解釋的缺口。"""
    p = _p(doc)
    p["corporate_actions"]["trading_halts"] = []
    return doc


def mut_perf_mismatch(doc):
    _p(doc)["performance"]["return_pct"] = -95.7
    return doc


def mut_yahoo_less_extreme_while_ahead(doc):
    """meta 確實比序列新，但 Yahoo 的 52 週低往『更不極端』的方向跑出帶外。

    放行條款只認「更極端」那一邊；這一種是真的對不起來，必須照擋。
    """
    perf = _p(doc)["performance"]
    perf["yahoo_meta_as_of"] = "2099-01-01"
    perf["low_52w_yahoo"] = (perf["low_52w"] or 0) * 2 + 1000
    return doc


def mut_yahoo_extreme_without_ahead(doc):
    """meta 跟序列同一天，Yahoo 的 52 週低卻低得離譜 —— 沒有時間差可解釋，照擋。"""
    p = _p(doc)
    p["performance"]["yahoo_meta_as_of"] = p["series"][-1]["d"]
    p["performance"]["low_52w_yahoo"] = 1.0
    return doc


MUTATIONS = [
    ("還原因子改成 1（rc = c）", mut_factor_one, "C5"),
    ("重複套用 5 倍還原（→ 錯誤的 −95.7%）", mut_double_adjust, "C6"),
    ("某日收盤價改成負數", mut_negative_price, "C3"),
    ("插入重複日期", mut_duplicate_date, "C2"),
    ("meta 的資料日期往回調", mut_meta_date_regression, "C9"),
    ("某家的 as_of 比上一版舊", mut_partner_date_regression, "C9"),
    ("市值乘以 1.1", mut_market_cap, "C7"),
    ("股數三欄不自洽", mut_shares_mismatch, "C7"),
    ("把停牌的零成交量列塞回序列", mut_halt_rows_back, "C10"),
    ("刪掉一家夥伴", mut_drop_partner, "C1"),
    ("宣稱歷史市值可得卻無序列", mut_fake_cap_history, "C8"),
    ("加一則沒有來源的『事實』", mut_unsourced_fact, "C12"),
    ("移除停牌宣告（缺口變成無法解釋）", mut_undeclared_gap, "C4"),
    ("期間報酬改成 −95.7%", mut_perf_mismatch, "C11"),
    ("meta 較新、但 Yahoo 52 週低往更不極端的方向出帶",
     mut_yahoo_less_extreme_while_ahead, "C11"),
    ("meta 與序列同日、Yahoo 52 週低卻低得離譜",
     mut_yahoo_extreme_without_ahead, "C11"),
]


def self_test(doc, ref_doc):
    print("═══ 突變測試：每個突變都必須被對應的檢查抓到 ═══\n")
    # C9 的突變是「相對於上一版倒退」，基準固定用本次的乾淨資料，不讀 git。
    # 拿 git 裡的上一版當基準並不可靠：中間只要有幾天沒成功發佈，上一版就會
    # 落後本次好幾天，退一天的突變仍然比上一版新，C9 就抓不到，測試假性失敗
    # ——反而擋住整站發佈（2026-08-18、08-19 兩次排程就是這樣掛的）。
    prev = copy.deepcopy(doc)
    bad = 0
    for name, fn, expect in MUTATIONS:
        R = run_checks(fn(copy.deepcopy(doc)), ref_doc, prev)
        caught = [f for f in R.fails if f.startswith(f"[{expect}]")]
        if caught:
            print(f"  ✅ {name}")
            print(f"       → {expect} 抓到：{caught[0][len(expect) + 3:][:88]}")
        else:
            bad += 1
            other = R.fails[0] if R.fails else "（完全沒有任何檢查失敗）"
            print(f"  ❌ {name}")
            print(f"       → 預期 {expect} 失敗，實際：{other[:88]}")
    print()
    if bad:
        print(f"❌ 突變測試失敗：{bad}/{len(MUTATIONS)} 個突變沒被抓到 —— 測試本身失效了")
        return 1
    print(f"✅ 突變測試通過：{len(MUTATIONS)} 個突變全部被抓到")
    return 0


def main():
    strict = "--strict" in sys.argv
    doc = load(os.path.join(DATA, "partners.json"))
    ref_doc = load(os.path.join(DATA, "partners_ref.json"))
    prev = git_prev(OUT_REL)

    if "--self-test" in sys.argv:
        sys.exit(self_test(doc, ref_doc))

    m = doc.get("meta") or {}
    print(f"═══ 海外授權夥伴回歸測試 ═══")
    print(f"資料日期 {m.get('data_date')}　最後成功更新 "
          f"{(m.get('last_success') or {}).get('data_date')} "
          f"（{(m.get('last_success') or {}).get('at')}）\n")

    R = run_checks(doc, ref_doc, prev)
    for line in R.lines:
        print(line)

    if R.warns:
        print(f"\n⚠️  警告 {len(R.warns)} 項（資料舊或缺，照常發佈；前端會標示狀態）：")
        for w in R.warns:
            print(f"   - {w}")
    if R.fails:
        print(f"\n❌ 回歸測試失敗 {len(R.fails)} 項 —— 不發佈：")
        for f in R.fails:
            print(f"   - {f}")
        sys.exit(1)
    if strict and R.warns:
        print("\n❌ --strict：警告視為失敗")
        sys.exit(1)
    print(f"\n✅ 回歸測試通過（{len(R.lines)} 項檢查，{len(R.warns)} 項警告）")


if __name__ == "__main__":
    main()
