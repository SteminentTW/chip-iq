# -*- coding: utf-8 -*-
"""
海外授權夥伴（REPROCELL 4978.T／풍전약품 298060.KQ）價量與市值 → data/partners.json

兩家都是 Stemchymal 的授權夥伴，2026-08-14 會議上列為追蹤標的。

┌── 這支腳本最容易被改錯的三件事，先講 ────────────────────────────────┐

① Yahoo v8 chart 的 `close` **已經還原分割**，不要再乘任何因子。
   `adjclose` 是在 close 之上再還原**股利**。這兩家都不配息，所以 close 與 adjclose
   逐筆相同 —— 那只證明沒有配息，**不能拿來推論「沒還原分割」**。
   2026-08 的初版簡報就是這樣推錯，把 298060 的 2 年報酬寫成 −95.7%（正確是 −78.3%），
   誇大 17 個百分點。三組獨立驗證見 verify_partners.py 的 GOLDEN 常數。

② 所以本檔做的是**反方向**：把已還原的序列**回推**成「當時實際成交的價格」（rc 欄位），
   讓前端能同時顯示「還原後 ₩4,635」與「當時實際成交 ₩927」並解釋差異。
       rc = round(c × Π(numerator/denominator))，連乘範圍＝事件日晚於該列日期的所有分割
   298060 只有一筆 1:5（事件日 2026-05-19），故 2026-05-19 之前 rc = c × 0.2。

③ 歷史市值**不做**。兩家在 2 年窗格內股數變動劇烈（298060 光 2026-03 的 17 天就 +12.0%，
   07-21 再 +9.0%；4978 三個半月 +6.2%），用現在的股數回推當時市值會失真到沒有意義。
   市值只出一個最新值，旁邊標股數、基準日、來源。歷史那段在 JSON 裡明確標記 available=false
   並附 reason，讓前端顯示「無法回推」而不是畫一條錯的線。

└──────────────────────────────────────────────────────────────────┘

股數沒有免憑證的公開 API（日本要解析決算短信 PDF、韓國要解析 DART 揭露文件），
故落地在 data/partners_ref.json 人工維護，本檔只負責原封不動帶進輸出。

韌性：Yahoo 是非官方端點，GitHub Actions 的 IP 有可能被擋（429／403／HTML 擋頁）。
抓不到時**保留上一次的良好資料**、在 meta 記錄失敗原因與資料實際日期，然後 exit 0 ——
排程不會整個掛掉，前端會顯示「資料最後成功更新於 X」。絕不寫入空資料覆蓋好資料。

用法：python fetch_partners.py [--dry-run]
"""
import json, os, sys
from datetime import date, datetime, timedelta, timezone

from _http import fetch_json

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA = os.path.join(ROOT, "data")
REF = os.path.join(DATA, "partners_ref.json")
OUT = os.path.join(DATA, "partners.json")

CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
         "?range=2y&interval=1d&events=split")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/126.0 Safari/537.36")

# 前端把資料視為「舊到該提醒使用者」的天數。兩家都是週一至週五交易，
# 連假最長約 8 天（韓國中秋），故 10 天以上才算異常。
STALE_AFTER_DAYS = 10


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def iso(ts, gmtoffset):
    """Yahoo 的 timestamp 是 UTC 秒；加上交易所時區位移才是當地交易日。"""
    return datetime.fromtimestamp(ts + gmtoffset, timezone.utc).strftime("%Y-%m-%d")


def fetch_chart(symbol):
    j = fetch_json(CHART.format(sym=symbol), {"User-Agent": UA,
                                              "Accept": "application/json"})
    chart = (j or {}).get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"Yahoo 回報錯誤：{chart['error']}")
    result = (chart.get("result") or [None])[0]
    if not result or not result.get("timestamp"):
        raise RuntimeError("Yahoo 回傳空 result 或無 timestamp")
    return result


def parse_splits(result, gmtoffset):
    """把 events.splits 解析成依日期排序的清單。無分割時回傳空 list。"""
    raw = ((result.get("events") or {}).get("splits") or {})
    out = []
    for s in raw.values():
        num, den = float(s["numerator"]), float(s["denominator"])
        if num <= 0 or den <= 0:
            continue
        out.append({
            "date": iso(int(s["date"]), gmtoffset),
            "numerator": num,
            "denominator": den,
            "ratio": s.get("splitRatio"),
            "kind": "reverse" if den > num else "forward",
            # 還原後 → 當時實際價 的換算因子（見檔頭 ②）
            "raw_factor": num / den,
        })
    out.sort(key=lambda s: s["date"])
    return out


def raw_factor(day, splits):
    """該交易日的「還原後 → 當時實際成交價」因子。分割事件日當天及之後為 1。"""
    f = 1.0
    for s in splits:
        if day < s["date"]:
            f *= s["raw_factor"]
    return f


def build_series(result, splits, gmtoffset):
    """回傳 (series, dropped)。

    剔除兩種列，兩者都不是真實成交：
      - close 為 null（Yahoo 偶發缺值）
      - volume 為 0 或 null（停牌期間 Yahoo 用前值填補，價格會凍結成一條水平線）
    停牌那段若保留，會污染波動度與最大回撤，恢復首日相對凍結值的跌幅也會被誤讀成單日暴跌。
    """
    ts = result["timestamp"]
    q = result["indicators"]["quote"][0]
    series, dropped = [], []
    for i, t in enumerate(ts):
        day = iso(t, gmtoffset)
        o, h, l = q["open"][i], q["high"][i], q["low"][i]
        c, v = q["close"][i], q["volume"][i]
        if c is None:
            dropped.append({"d": day, "reason": "close 為 null"})
            continue
        if not v:
            dropped.append({"d": day, "reason": "volume 為 0（停牌或無成交，價格為前值填補）",
                            "frozen_close": round(c, 4)})
            continue
        f = raw_factor(day, splits)
        series.append({
            "d": day,
            "o": round(o, 4) if o is not None else None,
            "h": round(h, 4) if h is not None else None,
            "l": round(l, 4) if l is not None else None,
            "c": round(c, 4),
            "v": int(v),
            # rc = 當時實際成交的收盤價（未還原）。無分割時等於 c。
            "rc": round(c * f, 2),
        })
    return series, dropped


def gaps_over(series, days):
    out = []
    for a, b in zip(series, series[1:]):
        d0, d1 = date.fromisoformat(a["d"]), date.fromisoformat(b["d"])
        if (d1 - d0).days > days:
            out.append({"from": a["d"], "to": b["d"], "days": (d1 - d0).days})
    return out


def build_partner(ref, result):
    gmt = int(result["meta"].get("gmtoffset") or 0)
    meta = result["meta"]
    splits = parse_splits(result, gmt)
    series, dropped = build_series(result, splits, gmt)
    if not series:
        raise RuntimeError("剔除無效列後序列為空")

    last, first = series[-1], series[0]
    prev = series[-2] if len(series) > 1 else None

    basis = ref.get("cap_basis", "outstanding")
    shares_used = ref["shares"][basis]
    cap = shares_used * last["c"]
    unit = ref.get("local_unit") or {"name": "", "divisor": 1}

    # 52 週高低：用 Yahoo meta 的值當交叉核對，同時自己從序列算一份。
    # 兩者不一致代表序列被動過手腳，verify_partners.py 會擋下來。
    cut = (date.fromisoformat(last["d"]) - timedelta(days=365)).isoformat()
    win = [r for r in series if r["d"] >= cut]
    hi = max((r["h"] for r in win if r["h"] is not None), default=None)
    lo = min((r["l"] for r in win if r["l"] is not None), default=None)

    out = dict(ref)
    out.pop("_readme", None)
    out.update({
        "as_of": last["d"],
        "fetch_ok": True,
        "data_status": "live",
        "yahoo_name": meta.get("longName") or meta.get("shortName"),
        "series": series,
        "series_note": ("c = 還原後收盤價（Yahoo v8 close，已含分割還原，不得再乘因子）；"
                        "rc = 當時實際成交的收盤價（未還原）。無分割期間兩者相同。"),
        "dropped_rows": {
            "count": len(dropped),
            "reason": "close 為 null 或 volume 為 0 的列不是真實成交，已剔除",
            "rows": dropped,
        },
        "splits_detected": splits,
        "date_gaps_over_12d": gaps_over(series, 12),
        "latest": last,
        "prev": prev,
        "day_change_pct": (round((last["c"] / prev["c"] - 1) * 100, 2)
                           if prev and prev["c"] else None),
        "performance": {
            "window": "range=2y（Yahoo v8 chart）",
            "first": {"d": first["d"], "c": first["c"]},
            "last": {"d": last["d"], "c": last["c"]},
            "return_pct": round((last["c"] / first["c"] - 1) * 100, 2),
            "basis": "還原後收盤價。298060 期間內的 5:1 額面併合已由 Yahoo 還原，本站未再加工。",
            "high_52w": hi,
            "low_52w": lo,
            "high_52w_yahoo": meta.get("fiftyTwoWeekHigh"),
            "low_52w_yahoo": meta.get("fiftyTwoWeekLow"),
            # meta 可能比同一份回應裡的 chart 序列快一個交易日（收盤後、當天那根
            # K 棒還沒進序列時）。存下 meta 自己的 session 日期，verify 的 C11 才
            # 分得出「Yahoo 比我們新」與「Yahoo 跟我們不一致」。
            "yahoo_meta_as_of": (iso(int(meta["regularMarketTime"]), gmt)
                                 if meta.get("regularMarketTime") else None),
        },
        "market_cap": {
            "value": round(cap, 2),
            "currency": ref["currency"],
            "display": f"{ref['currency_symbol']}{cap / 1e9:,.2f}B",
            "display_local": f"{cap / unit['divisor']:,.2f} {unit['name']}",
            "price": last["c"],
            "price_date": last["d"],
            "shares_used": shares_used,
            "shares_basis": basis,
            "shares_basis_date": ref["shares"]["basis_date"],
            "formula": f"市值 = {basis} 股數 × 最新收盤價",
            "history": {
                "available": False,
                "reason": ("2 年窗格內兩家的發行股數變動劇烈（298060：2026-03 的 17 天內 +12.0%、"
                           "07-21 再 +9.0%；4978：三個半月 +6.2%），且雙方都有已簽約但尚未執行完的"
                           "稀釋。用現行股數 × 當時股價回推歷史市值會系統性失真，故不產生此序列。"),
                "alternative": "歷史走勢僅提供股價（還原後），標題請寫「股價走勢」而非「市值走勢」。",
            },
        },
    })
    return out


def carry_forward(ref, prev_partner, err, today):
    """抓取失敗時沿用上一次的良好資料，並如實標示它有多舊。"""
    if prev_partner and prev_partner.get("series"):
        out = dict(prev_partner)
        # ref 是人工維護的，即使行情抓不到也要用最新版（股數／註記可能剛更新過）
        for k, v in ref.items():
            if k not in ("_readme",):
                out[k] = v
        # 但市值與績效是舊行情算的，要重算 shares 相關欄位就會不一致 —— 一律保留舊值並標示
        for k in ("as_of", "series", "latest", "prev", "performance", "market_cap",
                  "splits_detected", "dropped_rows", "date_gaps_over_12d",
                  "day_change_pct", "series_note", "yahoo_name"):
            if k in prev_partner:
                out[k] = prev_partner[k]
        age = (date.fromisoformat(today) - date.fromisoformat(out["as_of"])).days
        out.update({
            "fetch_ok": False,
            "data_status": "stale",
            "stale_days": age,
            "last_error": err,
            "last_error_at": today,
            "stale_note": (f"本次更新失敗（{err}），畫面顯示的是 {out['as_of']} 的資料，"
                           f"距今 {age} 天。市值亦為該日股價計算。"),
        })
        return out

    out = dict(ref)
    out.pop("_readme", None)
    out.update({
        "as_of": None,
        "fetch_ok": False,
        "data_status": "unavailable",
        "series": [],
        "latest": None,
        "prev": None,
        "day_change_pct": None,
        "performance": None,
        "market_cap": None,
        "last_error": err,
        "last_error_at": today,
        "stale_note": f"從未成功取得行情資料（最近一次失敗：{err}）。前端請顯示「無資料」。",
    })
    return out


def main():
    dry = "--dry-run" in sys.argv
    ref_doc = load_json(REF)
    if not ref_doc or not ref_doc.get("partners"):
        sys.exit(f"讀不到參考資料 {REF}，無法產生 partners.json")

    prev_doc = load_json(OUT) or {}
    prev_partners = {p.get("id"): p for p in prev_doc.get("partners", [])}
    today = date.today().isoformat()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    partners, failures = [], []
    for ref in ref_doc["partners"]:
        sym = ref["symbol"]
        try:
            result = fetch_chart(sym)
            p = build_partner(ref, result)
            # Yahoo 偶爾回一份比上一版還舊的序列（2026-09-11 台北 00:41 那班，兩家都少了
            # 9/10 最後一根，疑似 Yahoo 盤後批次重算中）。這不算抓取失敗，但照寫會讓資料
            # 倒退、被 verify_partners 的 C9 擋下——而 C9 一擋就是整個 workflow 不 commit，
            # 連分點與台股行情都跟著停。所以跟抓取失敗同樣處理：沿用上一版、標 stale、只警告。
            # C9 本身不放寬：真的有人把資料改舊，照擋。
            old = prev_partners.get(ref["id"])
            if old and old.get("as_of") and p["as_of"] < old["as_of"]:
                raise RuntimeError(f"Yahoo 回傳的資料只到 {p['as_of']}，比上一版 {old['as_of']} 舊")
            partners.append(p)
            print(f"  {sym:<12} {p['as_of']}  {len(p['series'])} 筆  "
                  f"收盤 {p['latest']['c']:,.2f}  剔除 {p['dropped_rows']['count']} 筆  "
                  f"分割 {len(p['splits_detected'])} 筆")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            failures.append({"symbol": sym, "id": ref["id"], "error": err, "at": now})
            p = carry_forward(ref, prev_partners.get(ref["id"]), err, today)
            partners.append(p)
            print(f"  {sym:<12} 抓取失敗（{err}）→ "
                  f"{'沿用 ' + str(p['as_of']) + ' 的資料' if p['series'] else '無舊資料可沿用，標記為無資料'}")

    dates = [p["as_of"] for p in partners if p.get("as_of")]
    data_date = max(dates) if dates else None
    ok_any = any(p["fetch_ok"] for p in partners)

    prev_meta = prev_doc.get("meta") or {}
    last_success = prev_meta.get("last_success") or {}
    if ok_any:
        ok_dates = [p["as_of"] for p in partners if p["fetch_ok"]]
        last_success = {"at": now, "data_date": max(ok_dates)}
    # 保險：資料日期只能往前，不可因為部分失敗而倒退
    if prev_meta.get("last_success", {}).get("data_date") and last_success.get("data_date"):
        last_success["data_date"] = max(last_success["data_date"],
                                        prev_meta["last_success"]["data_date"])

    stale_days = None
    if last_success.get("data_date"):
        stale_days = (date.fromisoformat(today)
                      - date.fromisoformat(last_success["data_date"])).days

    out = {
        "schema_version": 1,
        "title": "海外授權夥伴",
        "meta": {
            "updated_at": today,
            "generated_at": now,
            "data_date": data_date,
            "last_success": last_success,
            "stale": bool(stale_days is not None and stale_days > STALE_AFTER_DAYS),
            "stale_days": stale_days,
            "stale_after_days": STALE_AFTER_DAYS,
            "run": {
                "ok": not failures,
                "partners_fetched": sum(1 for p in partners if p["fetch_ok"]),
                "partners_total": len(partners),
                "failures": failures,
                "note": ("Yahoo v8 chart 是非官方端點，GitHub Actions 的 IP 有可能被擋。"
                         "抓不到時保留上一次的良好資料並在此記錄原因，排程不視為失敗。"),
            },
            "sources": {
                "price_volume": "Yahoo Finance v8 chart API（免憑證）"
                                " https://query1.finance.yahoo.com/v8/finance/chart/",
                "shares": "人工維護於 data/partners_ref.json，每筆附來源 URL 與基準日"
                          "（日本：決算短信；韓國：DART 揭露文件）",
                "not_used": ("Yahoo quoteSummary 與 v7/finance/quote 需 cookie＋crumb（回 Unauthorized），"
                             "stooq.com 已改 JS proof-of-work —— 兩者都不符合本站零憑證原則，未採用。"),
            },
            "method": {
                "price": "Yahoo v8 chart 的 close 已含分割還原，本站不再套任何還原因子。",
                "raw_price": "rc 欄位＝當時實際成交價，由 close × Π(numerator/denominator) 回推。",
                "adjclose_caveat": ("close == adjclose 只代表沒有配息，不代表沒還原分割。"
                                    "兩家皆不配息，故兩欄逐筆相同。"),
                "market_cap": "最新收盤價 × 現行股數（基準日見各 partner）。歷史市值不回推，原因見 market_cap.history。",
                "excluded_rows": "close 為 null 或 volume 為 0 的列已剔除（停牌期間為前值填補，非真實成交）。",
            },
            "disclaimers": [
                "本頁數值皆可追溯至來源；抓不到的欄位顯示「無資料」，不以 0 或估算值填補。",
                "標記為「推論」的敘述非公司說法，請與事實區分。",
                "股數為人工維護，基準日見各 partner 的 shares.basis_date；兩家皆有尚未執行完畢的稀釋。",
            ],
        },
        "partners": partners,
    }

    if dry:
        print("\n--dry-run：不寫檔")
    else:
        os.makedirs(DATA, exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n資料日期 {data_date}　最後成功 {last_success.get('data_date')} "
          f"（{last_success.get('at')}）　失敗 {len(failures)} 家")
    for p in partners:
        if p.get("market_cap"):
            mc = p["market_cap"]
            print(f"  {p['symbol']:<12}{p['name_zh'][:22]:<24}"
                  f"{mc['display']:>12}  {mc['display_local']:>18}"
                  f"  2年 {p['performance']['return_pct']:>+7.2f}%  [{p['data_status']}]")
        else:
            print(f"  {p['symbol']:<12}{p['name_zh'][:22]:<24}{'無資料':>12}  [{p['data_status']}]")


if __name__ == "__main__":
    main()
