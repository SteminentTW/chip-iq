# -*- coding: utf-8 -*-
"""
海外授權夥伴的「最新消息」自動彙整 → data/partners_news.json

與 data/partners_profile.json 的分工：
  partners_profile.json 的 news[] 是**人工策展**（有判讀、有 fact／inference 標記、需覆核）。
  本檔只做**自動蒐集標題**：每天把各來源最新的標題、日期、連結帶進來，不改寫、不判讀，
  前端標示為「自動彙整、未經人工覆核」。本檔絕不寫入 partners_profile.json。

來源（皆為公開 RSS、免憑證，與本站零憑證原則一致）：
  REPROCELL  ① 日本版 IR news https://reprocell.co.jp/ir/news/（適時開示，HTML 解析）
             ② 日本版官網 WordPress RSS https://reprocell.co.jp/feed/（一般公告）
             ③ Google News RSS「リプロセル」（媒體報導）
             REPROCELL 官網分國際版（reprocell.com）與日本版（reprocell.co.jp），詳細資訊只在日本版。
             而且 IR news（TDnet 適時開示 PDF，託管在 eir-parts.net）**不在** WordPress RSS 裡，
             例如 2026-09-29 與東邦 HD 的 Stemchymal 國內流通基本合意書只出現在 IR news，
             所以 ① 必須另外抓，不能只靠 ②。
  풍전약품    ① Google News RSS「풍전약품 OR SCM생명과학」（媒體報導）
             官網 scmlifescience.com 的憑證與網域不符、DART 的 RSS 只有全市場最新 25 筆，
             兩者都不適合排程，故韓國這家只有媒體來源；公司公告仍以人工策展的 DART 連結為準。

韌性（最重要）：新聞是附加資訊，**任何失敗都不可以擋住每日發佈**。
  - 單一來源抓不到 → 保留該來源上一次的條目，在 meta.sources[] 記錄錯誤，繼續下一個來源
  - 整支腳本意外 crash → workflow 那一步設了 continue-on-error，不影響後續守門員與 commit
  - 絕不以空資料覆蓋好資料：本次抓到 0 則時沿用舊條目
  - 連結只收 http／https，擋掉 javascript: 之類的 URL（前端會把它當超連結）

用法：python fetch_partners_news.py [--dry-run]
"""
import hashlib, json, os, re, sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote

from _http import fetch_bytes

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(DATA, "partners_news.json")

TPE = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/126.0 Safari/537.36")

# 保留多久、每家最多幾則。畫面上只是「最近發生什麼事」，舊的留在人工策展區。
KEEP_DAYS = 120
MAX_PER_COMPANY = 30


def gnews(q, hl, gl):
    return ("https://news.google.com/rss/search?q=" + quote(q)
            + f"&hl={hl}&gl={gl}&ceid={gl}:{hl.split('-')[0]}")


# companyId 與 partners_profile.json 的 partners[].id 一致，前端靠它套用同一個公司篩選。
SOURCES = [
    {"id": "reprocell-ir", "companyId": "reprocell", "kind": "official", "parser": "ir_html",
     "label": "REPROCELL 日本版 IR news", "url": "https://reprocell.co.jp/ir/news/"},
    {"id": "reprocell-official", "companyId": "reprocell", "kind": "official",
     "label": "REPROCELL 日本版官網公告", "url": "https://reprocell.co.jp/feed/"},
    {"id": "reprocell-gnews", "companyId": "reprocell", "kind": "media",
     "label": "Google News（日文）", "url": gnews("リプロセル", "ja", "JP")},
    {"id": "scm-gnews", "companyId": "scm-lifescience", "kind": "media",
     "label": "Google News（韓文）", "url": gnews('"풍전약품" OR "SCM생명과학"', "ko", "KR")},
]


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clean(s):
    s = unescape(re.sub(r"<[^>]+>", "", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def safe_url(u):
    u = (u or "").strip()
    return u if re.match(r"^https?://", u, re.I) else None


JST = timezone(timedelta(hours=9))
_DATE = re.compile(r"(20\d\d)[./年-]\s*(\d{1,2})[./月-]\s*(\d{1,2})")
_A = re.compile(r"<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.S | re.I)


def item(src, day, dt, title, publisher, url):
    return {
        "id": hashlib.sha1((src["companyId"] + "|" + title).encode("utf-8")).hexdigest()[:12],
        "companyId": src["companyId"],
        "date": day,
        "published_at": dt.isoformat(),
        "title": title,
        "publisher": publisher,
        "url": url,
        "kind": src["kind"],
        "source": src["id"],
    }


def parse_ir_html(raw, src):
    """日本版 IR news 頁：每列是「YYYY.MM.DD ＋ 連到 eir-parts.net 開示 PDF 的標題連結」。

    不綁 class 名稱（改版就會失效），改成依文件順序掃描：每個指向 eir-parts.net／PDF 的
    連結，取它前方 400 字內最近的一個日期。頁面上其他導覽連結不是 PDF，自然不會被收進來。
    日期是日本時間的開示日，直接當 date，不做時區換算（換算會讓日期跑掉）。
    """
    html = raw.decode("utf-8", errors="replace")
    items, seen = [], set()
    for m in _A.finditer(html):
        href = unescape(m.group(1)).strip()
        if "eir-parts.net" not in href and not href.lower().endswith(".pdf"):
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://reprocell.co.jp" + href
        url = safe_url(href)
        title = clean(m.group(2))
        before = clean(html[max(0, m.start() - 400):m.start()])
        dates = _DATE.findall(before) or _DATE.findall(title)
        if not url or not title or not dates or url in seen:
            continue
        y, mo, d = (int(x) for x in dates[-1])
        try:
            dt = datetime(y, mo, d, 12, tzinfo=JST)
        except ValueError:
            continue
        title = _DATE.sub("", title).strip(" 　|｜-")
        if not title:
            continue
        seen.add(url)
        items.append(item(src, dt.strftime("%Y-%m-%d"), dt.astimezone(TPE), title,
                          "TDnet 適時開示" if "tdnet" in url else "REPROCELL IR", url))
    return items


def parse_rss(raw, src):
    """RSS 2.0 → 條目 list。Google News 的標題是「標題 - 媒體名」，媒體名另在 <source>。"""
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        title = clean(it.findtext("title"))
        url = safe_url(it.findtext("link"))
        pub = it.findtext("pubDate")
        if not title or not url or not pub:
            continue
        try:
            dt = parsedate_to_datetime(pub.strip())
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(TPE)
        publisher = clean(it.findtext("source")) if it.find("source") is not None else ""
        if src["kind"] == "official":
            publisher = publisher or src["label"].replace("公告", "").strip()
        elif publisher and title.endswith(" - " + publisher):
            title = title[: -len(" - " + publisher)].rstrip()
        items.append(item(src, dt.strftime("%Y-%m-%d"), dt, title, publisher, url))
    return items


def main():
    dry = "--dry-run" in sys.argv
    prev = load_json(OUT) or {}
    prev_items = [i for i in prev.get("items", []) if safe_url(i.get("url"))]
    now = datetime.now(TPE).replace(microsecond=0)
    today = now.date()

    fresh, status = [], []
    for src in SOURCES:
        try:
            html = src.get("parser") == "ir_html"
            raw = fetch_bytes(src["url"], {"User-Agent": UA,
                                           "Accept": "text/html" if html else
                                           "application/rss+xml, application/xml"},
                              timeout=30)
            got = (parse_ir_html if html else parse_rss)(raw, src)
            if not got:
                # IR 頁若改成 JS 動態載入，這裡會是 0 則 → 記為失敗並沿用上次，卡片上會顯示
                raise RuntimeError("解析後 0 則（頁面結構可能已改版）")
            fresh.extend(got)
            status.append({"id": src["id"], "companyId": src["companyId"], "label": src["label"],
                           "kind": src["kind"], "ok": True, "count": len(got)})
            print(f"  {src['id']:<20} {len(got):>3} 則")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:300]
            old_n = sum(1 for i in prev_items if i.get("source") == src["id"])
            status.append({"id": src["id"], "companyId": src["companyId"], "label": src["label"],
                           "kind": src["kind"], "ok": False, "error": err,
                           "kept_previous": old_n})
            print(f"  {src['id']:<20} 抓取失敗（{err}）→ 沿用上次 {old_n} 則")

    # 合併：新抓到的優先（標題／連結可能被更正），舊的補上本次沒出現的；以 id 去重，
    # 同一篇新聞被官方與媒體同時收錄時，官方那筆優先。
    first_seen = {i["id"]: i.get("first_seen") for i in prev_items}
    merged = {}
    for i in sorted(fresh + prev_items, key=lambda x: x.get("kind") != "official"):
        if i["id"] in merged:
            continue
        i = dict(i)
        i["first_seen"] = first_seen.get(i["id"]) or today.isoformat()
        merged[i["id"]] = i

    cut = (today - timedelta(days=KEEP_DAYS)).isoformat()
    items = sorted((i for i in merged.values() if i["date"] >= cut),
                   key=lambda x: x["published_at"], reverse=True)
    per, kept = {}, []
    for i in items:
        per[i["companyId"]] = per.get(i["companyId"], 0) + 1
        if per[i["companyId"]] <= MAX_PER_COMPANY:
            kept.append(i)

    ok_all = all(s["ok"] for s in status)
    out = {
        "schema_version": 1,
        "title": "海外授權夥伴 — 最新消息（自動彙整）",
        "meta": {
            "updated_at": today.isoformat(),
            "generated_at": now.isoformat(),
            "last_success_at": (now.isoformat() if any(s["ok"] for s in status)
                                else (prev.get("meta") or {}).get("last_success_at")),
            "ok": ok_all,
            "sources": status,
            "keep_days": KEEP_DAYS,
            "max_per_company": MAX_PER_COMPANY,
            "note": ("自動彙整各來源 RSS 的標題與連結，未經人工覆核、不代表與仲恩相關。"
                     "經判讀的重要事件請看人工策展的「夥伴動態」。"),
        },
        "items": kept,
    }

    if dry:
        print("\n--dry-run：不寫檔")
    else:
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, OUT)
    print(f"\n共 {len(kept)} 則（{', '.join(f'{k} {v}' for k, v in per.items())}）"
          f"　來源 {sum(s['ok'] for s in status)}/{len(status)} 成功")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 新聞是附加資訊，任何意外都不可以擋住每日發佈
        print(f"fetch_partners_news 發生未預期錯誤，本次不更新新聞：{type(e).__name__}: {e}")
