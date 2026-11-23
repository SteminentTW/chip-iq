# -*- coding: utf-8 -*-
"""
台灣再生醫療產業群組 12 檔 — 市值與相對表現

12 檔橫跨三個市場，取價與單位規則都不同，這是本檔最容易出錯的地方：

  市場   價格來源                         取價欄位   量/額單位
  上市   TWSE  STOCK_DAY                  收盤價     股 / 元
  上櫃   TPEx  afterTrading/tradingStock  收盤價     仟股 / 仟元  ← 需 ×1000
  興櫃   TPEx  emerging/historical        成交均價   股 / 元

興櫃採議價交易，最後成交價常是離群值，故市值一律以**成交均價**計算
（與公司現行報表一致，7729 已對帳吻合）。

⚠️ 股數一定要用 MOPS「已發行普通股數」，不可用「實收資本額 ÷ 10」推算：
   群組外的沛爾生醫(6949)、康霈(6919) 面額是 0.5 元不是 10 元，推算會差 20 倍。

驗證（2026-07-21 對《生技總體戰報》黃金樣本）：
   上市/上櫃 5 檔誤差 0.00%；興櫃 7 檔中 5 檔誤差 <0.25%。
   樂迦(-1.20%)、永笙(+1.05%) 的落差來自**股數**而非價格——
   現行報表的股數是靜態清單會過期，本腳本每次執行都重抓 MOPS 最新值。

用法：python fetch_peers.py [月數，預設 12]
"""
import json, os, sys, time, re
from datetime import date

from _http import fetch_json

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA = os.path.join(ROOT, "data")
FOCUS = "7729"

# 群組成員原沿用公司現行報表的 12 檔定義，順序不拘（依市值排序輸出）
# 2026-11-23 依使用者要求：全福生技(6885) 關聯度低，改為 育世博-KY(6976，興櫃)
PEERS = ["6712", "6891", "7729", "6794", "4178", "1784",
         "6976", "6892", "6704", "6973", "4724", "4186"]

# 《生技總體戰報》2026-07-21 那期 → 回歸測試黃金樣本
# （6976 育世博-KY 不在該期報表內，沒有黃金值，對帳表只列其餘 11 檔；
#   6885 已移出群組，留著無作用，故一併移除）
GOLD_20260721 = {
    "6712": 150.61, "6891": 83.86, "7729": 56.89, "6794": 49.32,
    "4178": 41.23, "1784": 31.98, "6892": 24.98,
    "6704": 18.32, "6973": 14.93, "4724": 9.03, "4186": 8.67,
}

TW = "https://www.twse.com.tw/"
TP = "https://www.tpex.org.tw/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/126.0 Safari/537.36")


def get(url, referer):
    return fetch_json(url, {"User-Agent": UA, "Referer": referer})


def num(s):
    s = re.sub(r"<[^>]+>", "", str(s)).replace(",", "").strip()
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def roc_to_iso(s):
    y, m, d = s.split("/")
    return f"{int(y) + 1911:04d}-{int(m):02d}-{int(d):02d}"


def load_profiles():
    """三個市場的公司基本資料，用來判斷市場別並取得已發行普通股數與掛牌日"""
    L = get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", TW)
    O = get(TP + "openapi/v1/mopsfin_t187ap03_O", TP)
    R = get(TP + "openapi/v1/mopsfin_t187ap03_R", TP)
    out = {}
    for r in L:
        out[r["公司代號"]] = ("上市", r["公司簡稱"],
                              num(r["已發行普通股數或TDR原股發行股數"]),
                              str(r.get("上市日期") or ""))
    for src, mkt in ((O, "上櫃"), (R, "興櫃")):
        for r in src:
            out.setdefault(r["SecuritiesCompanyCode"],
                           (mkt, r["CompanyAbbreviation"], num(r["IssueShares"]),
                            str(r.get("DateOfListing") or "")))
    return out


def months_back(n):
    y, m = date.today().year, date.today().month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def hist_listed(code, y, m):
    j = get(TW + f"rwd/zh/afterTrading/STOCK_DAY?date={y}{m:02d}01&stockNo={code}"
                 f"&response=json", TW)
    if j.get("stat") != "OK":
        return {}
    return {roc_to_iso(r[0]): num(r[6]) for r in j["data"] if num(r[6])}


def hist_otc(code, y, m):
    j = get(TP + f"www/zh-tw/afterTrading/tradingStock?code={code}"
                 f"&date={y}/{m:02d}/01&response=json", TP)
    if not j.get("tables") or not j["tables"][0].get("data"):
        return {}
    return {roc_to_iso(r[0]): num(r[6]) for r in j["tables"][0]["data"] if num(r[6])}


def hist_esb(code, y, m):
    """興櫃取成交均價（欄位 5）；無成交日（欄位 1 為 0）跳過"""
    j = get(TP + f"www/zh-tw/emerging/historical?date={y}/{m:02d}/01"
                 f"&code={code}&response=json", TP)
    if j.get("stat") != "ok" or not j.get("tables"):
        return {}
    return {roc_to_iso(r[0]): num(r[5])
            for r in j["tables"][0]["data"] if num(r[1]) and num(r[5])}


FETCHER = {"上市": hist_listed, "上櫃": hist_otc, "興櫃": hist_esb}


def main():
    n_months = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    prof = load_profiles()
    period = months_back(n_months)

    peers = []
    for code in PEERS:
        mkt, name, shares, listed_on = prof.get(code, (None, None, None, ""))
        if not shares:
            print(f"  {code}: 查無基本資料，略過")
            continue

        # 期間內才轉上市/上櫃的個股，掛牌前那段要回興櫃補（如永笙-KY 2026-04-30 轉上市）
        stitch_from = ""
        if mkt != "興櫃" and len(listed_on) == 8:
            ly, lm = int(listed_on[:4]), int(listed_on[4:6])
            if (ly, lm) > period[0]:
                stitch_from = f"{ly}-{listed_on[4:6]}-{listed_on[6:]}"

        px = {}
        for y, m in period:
            # 掛牌前用興櫃均價；轉換當月兩邊都抓，掛牌後的收盤價覆蓋同日興櫃值
            steps = [FETCHER[mkt]]
            if stitch_from and (y, m) < (ly, lm):
                steps = [hist_esb]
            elif stitch_from and (y, m) == (ly, lm):
                steps = [hist_esb, FETCHER[mkt]]
            for fetch in steps:
                try:
                    px.update(fetch(code, y, m))
                except Exception as e:
                    print(f"  {code} {y}/{m:02d}: {type(e).__name__}")
                time.sleep(0.35)

        series = [{"d": d, "p": px[d], "cap": round(px[d] * shares / 1e8, 2)}
                  for d in sorted(px)]
        p = {"code": code, "name": name, "market": mkt,
             "shares": int(shares), "series": series}
        if stitch_from:
            p["stitched_from_emerging"] = stitch_from
        peers.append(p)
        note = f"（{stitch_from} 前為興櫃，已接軌）" if stitch_from else ""
        print(f"  {code} {name} ({mkt}) {len(series)} 天 {note}")

    # 以最新共同交易日排名；各市場休市日可能不同，故取各自最後一筆
    all_days = sorted({p["series"][-1]["d"] for p in peers if p["series"]})
    as_of = all_days[-1] if all_days else None

    for p in peers:
        s = p["series"]
        p["latest"] = s[-1] if s else None
        p["prev"] = s[-2] if len(s) > 1 else None
        if p["latest"] and p["prev"]:
            p["chg_pct"] = round((p["latest"]["cap"] / p["prev"]["cap"] - 1) * 100, 2)
        else:
            p["chg_pct"] = None

    ranked = sorted([p for p in peers if p["latest"]],
                    key=lambda p: -p["latest"]["cap"])
    for i, p in enumerate(ranked, 1):
        p["group_rank"] = i
    prev_ranked = sorted([p for p in peers if p["prev"]],
                         key=lambda p: -p["prev"]["cap"])
    for i, p in enumerate(prev_ranked, 1):
        p["prev_group_rank"] = i

    # 回歸測試：2026-07-21 對黃金樣本
    checks = []
    for p in peers:
        hit = next((x for x in p["series"] if x["d"] == "2026-07-21"), None)
        gold = GOLD_20260721.get(p["code"])
        if hit and gold:
            checks.append({"code": p["code"], "name": p["name"],
                           "computed": hit["cap"], "reported": gold,
                           "diff_pct": round((hit["cap"] / gold - 1) * 100, 2)})

    out = {
        "group": "台灣再生醫療產業群組",
        "as_of": as_of,
        "updated_at": date.today().isoformat(),
        "focus": FOCUS,
        "method": {
            "market_cap": "已發行普通股數 × 當日價格 ÷ 1e8（單位：億元）",
            "price_listed": "上市/上櫃取收盤價",
            "price_emerging": "興櫃取成交均價（議價交易，最後成交價易失真）",
            "shares": "MOPS 公開資訊觀測站 已發行普通股數，每次執行重抓",
            "caveat": "不可用實收資本額÷10 推股數，部分公司面額非 10 元",
        },
        "validation": {
            "sample": "《生技總體戰報》2026-07-21",
            "checks": sorted(checks, key=lambda c: -abs(c["diff_pct"])),
        },
        "peers": sorted(peers, key=lambda p: -(p["latest"]["cap"] if p["latest"] else 0)),
    }

    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "peers.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print(f"\n基準日 {as_of}　群組 {len(peers)} 檔")
    print(f"{'代碼':<6}{'名稱':<11}{'市場':<5}{'市值(億)':>10}{'日變動':>9}{'群內':>5}")
    for p in out["peers"]:
        mark = " ←" if p["code"] == FOCUS else ""
        print(f"{p['code']:<6}{p['name']:<11}{p['market']:<5}"
              f"{p['latest']['cap']:>10.2f}{(p['chg_pct'] or 0):>+8.2f}%"
              f"{p.get('group_rank', 0):>5}{mark}")
    print("\n回歸測試 vs 2026-07-21 黃金樣本（誤差由大到小）：")
    for c in out["validation"]["checks"]:
        flag = "OK" if abs(c["diff_pct"]) < 0.5 else "查"
        print(f"  [{flag}] {c['code']} {c['name']:<10} 算出 {c['computed']:>8.2f} "
              f"報表 {c['reported']:>8.2f}  {c['diff_pct']:>+6.2f}%")


if __name__ == "__main__":
    main()
