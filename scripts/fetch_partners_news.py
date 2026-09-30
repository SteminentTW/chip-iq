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

中文翻譯（title_zh）：來源標題都是日文／韓文，面板使用者看中文，所以每則標題翻成繁體中文。
  - 用 Google 翻譯的免憑證端點 translate.googleapis.com（client=gtx）。它和 Yahoo v8 一樣是
    非官方端點、沒有帳密，符合本站零憑證原則；代價是可能被限流或哪天改掉。
  - 每則只翻一次：譯文跟著條目存進 JSON，下次沿用，不重打。
  - 翻譯失敗絕不擋發佈：第一次失敗就停止本次所有翻譯（避免每則都各燒一輪逾時），
    該則沒有 title_zh，前端顯示原文；下次排程會再補翻。
  - 機器翻譯可能不精確，前端標示「機器翻譯」並在下方保留原文標題。

用法：python fetch_partners_news.py [--dry-run]
"""
import hashlib, json, os, re, sys, time, unicodedata
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

TRANSLATE = ("https://translate.googleapis.com/translate_a/single"
             "?client=gtx&sl=auto&tl=zh-TW&dt=t&q=")
MAX_TRANSLATE_PER_RUN = 60
TRANSLATION_VERSION = 2  # 1：初版；2：GLOSSARY 加入 Stemchymal 片假名／韓文
# 公司名先換成英文再送翻譯：機器翻譯會把「풍전약품」逐字譯成「豐田製藥」這類錯名。
GLOSSARY = {"풍전약품": "Poongjeon", "SCM생명과학": "SCM Lifescience",
            "에스씨엠생명과학": "SCM Lifescience", "リプロセル": "REPROCELL",
            # 產品名：日文片假名會被音譯成「Stem Kaimal」
            "ステムカイマル": "Stemchymal", "스템카이말": "Stemchymal"}

# 保留多久、每家最多幾則。畫面上只是「最近發生什麼事」，舊的留在人工策展區。
KEEP_DAYS = 120
# 官方與媒體分開計額度：2026-09-30 首次上線時，Google News 單日就有 25 則 REPROCELL
# 報導（多是同一件事的轉載），共用 30 則額度會把官方 IR 擠到只剩 5 則。
MAX_OFFICIAL_PER_COMPANY = 30
MAX_MEDIA_PER_COMPANY = 5
# 媒體標題相似度（字元二元組 Jaccard）超過這個值、且日期相差 3 天內，視為同一事件的轉載，只留最早一則
SIMILAR_TITLE = 0.45


def gnews(q, hl, gl):
    return ("https://news.google.com/rss/search?q=" + quote(q)
            + f"&hl={hl}&gl={gl}&ceid={gl}:{hl.split('-')[0]}")


# companyId 與 partners_profile.json 的 partners[].id 一致，前端靠它套用同一個公司篩選。
SOURCES = [
    {"id": "reprocell-ir", "companyId": "reprocell", "kind": "official", "parser": "ir_html",
     "label": "REPROCELL 日本版 IR news", "url": "https://reprocell.co.jp/ir/news/"},
    {"id": "reprocell-official", "companyId": "reprocell", "kind": "official",
     "label": "REPROCELL 日本版官網公告", "url": "https://reprocell.co.jp/feed/"},
    # REPROCELL 不收 Google News（2026-09-30 使用者回饋「太多太雜、很多重複」）：
    # 官方 IR news＋官網公告已涵蓋所有公司事件，媒體多是同一公告的轉載或「3日ぶり反発」這類
    # 股價短評。풍전약품沒有可用的官方來源，只能靠媒體，所以保留並加上過濾。
    {"id": "scm-gnews", "companyId": "scm-lifescience", "kind": "media",
     "label": "Google News（韓文）", "url": gnews('"풍전약품" OR "SCM생명과학"', "ko", "KR"),
     # 例行、無事件內容的標題：股票網站每週自動產生的「투자분석」、盤勢短評等
     "exclude": r"투자분석|특징주|주가\s*(급등|급락|상승|하락)|오늘의\s*(종목|주식)|前場コメント|本日のおすすめ銘柄"},
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


def norm_title(t):
    t = unicodedata.normalize("NFKC", t or "")
    return re.sub(r"[\W_]+", "", t).lower()


def _bigrams(t):
    t = norm_title(t)
    return {t[k:k + 2] for k in range(len(t) - 1)}


def drop_reposts(items):
    """同一事件被多家媒體轉載時只留最早的一則；轉載官方公告的媒體條目也丟掉。"""
    kept = []
    # 官方公告先放進比對清單：媒體轉載官方公告（標題略改）也算重複
    seen = [(i["companyId"], date.fromisoformat(i["date"]), _bigrams(i["title"]))
            for i in items if i["kind"] != "media"]
    for i in reversed(items):  # 由舊到新，保留最早報導
        if i["kind"] != "media":
            kept.append(i)
            continue
        bg, d = _bigrams(i["title"]), date.fromisoformat(i["date"])
        dup = any(c == i["companyId"] and abs((d - d2).days) <= 3 and bg and b2
                  and len(bg & b2) / len(bg | b2) >= SIMILAR_TITLE
                  for c, d2, b2 in seen)
        if not dup:
            seen.append((i["companyId"], d, bg))
            kept.append(i)
    return list(reversed(kept))


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
    連結，日期優先取連結**內部**的日期；連結內沒有日期時，才取它前方 400 字內最近的一個。
    （2026-09-30 首次上線時先取前方日期，而實際頁面的日期在連結內，前方那個是**上一則**的
    日期，結果整串錯位一則：9/24 的業許可取得被標成 9/28。）
    頁面上其他導覽連結不是 PDF，自然不會被收進來。
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
        dates = _DATE.findall(title) or _DATE.findall(before)
        if not url or not title or not dates or url in seen:
            continue
        y, mo, d = (int(x) for x in dates[-1])
        try:
            dt = datetime(y, mo, d, 12, tzinfo=JST)
        except ValueError:
            continue
        # 連結內除了日期，還有分類與「›」箭頭圖示文字，一併去掉
        title = _DATE.sub("", title, count=1).strip(" 　|｜-›»>")
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


def translate(text):
    """單次呼叫、不走 _http 的重試：翻譯是附加資訊，失敗就等下次排程。"""
    import urllib.request
    for k, v in GLOSSARY.items():
        text = text.replace(k, v)
    req = urllib.request.Request(TRANSLATE + quote(text), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.load(r)
    zh = "".join(seg[0] for seg in (j[0] or []) if seg and seg[0]).strip()
    if not zh:
        raise RuntimeError("翻譯結果為空")
    return zh


def add_translations(items, prev_zh):
    """補上 title_zh。回傳狀態 dict 給 meta。"""
    done = failed = 0
    err = None
    for i in items:
        if prev_zh.get(i["id"]):
            i["title_zh"] = prev_zh[i["id"]]
            i["title_zh_v"] = TRANSLATION_VERSION
            continue
        if err or done >= MAX_TRANSLATE_PER_RUN:
            failed += 1
            continue
        # 這個端點偶爾回 429／500（限流），隔一下重試一次通常就過；再失敗才熔斷
        for attempt in (0, 1):
            try:
                time.sleep(0.5 if attempt == 0 else 3)
                i["title_zh"] = translate(i["title"])
                i["title_zh_v"] = TRANSLATION_VERSION
                done += 1
                break
            except Exception as e:
                if attempt:
                    err = f"{type(e).__name__}: {e}"[:300]
                    failed += 1
    return {"engine": "Google 翻譯（translate.googleapis.com，免憑證非官方端點）",
            "translated_this_run": done, "untranslated": failed, "error": err}


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
            if src.get("exclude"):
                got = [i for i in got if not re.search(src["exclude"], i["title"])]
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

    # 合併：本次抓成功的來源，以新資料**整批取代**該來源的舊條目（舊條目的日期或標題若是
    # 解析錯誤產生的，這樣下一次就會自動修正，不會殘留 120 天）；抓失敗的來源才沿用舊條目。
    # 以 id 去重，同一篇新聞被官方與媒體同時收錄時，官方那筆優先。
    ok_sources = {s["id"] for s in status if s["ok"]}
    # 已從 SOURCES 移除的來源，其舊條目一併丟掉
    live = {s["id"] for s in SOURCES}
    carried = [i for i in prev_items
               if i.get("source") in live and i.get("source") not in ok_sources]
    excl = {s["id"]: s.get("exclude") for s in SOURCES}
    carried = [i for i in carried
               if not (excl.get(i["source"]) and re.search(excl[i["source"]], i["title"]))]
    first_seen = {i["id"]: i.get("first_seen") for i in prev_items}
    merged = {}
    for i in sorted(fresh + carried, key=lambda x: x.get("kind") != "official"):
        if i["id"] in merged:
            continue
        i = dict(i)
        i["first_seen"] = first_seen.get(i["id"]) or today.isoformat()
        merged[i["id"]] = i

    cut = (today - timedelta(days=KEEP_DAYS)).isoformat()
    items = sorted((i for i in merged.values() if i["date"] >= cut),
                   key=lambda x: x["published_at"], reverse=True)
    # 媒體轉貼官方公告時標題常一字不差（只差空白或全形），這種媒體條目直接丟掉，留官方那筆
    official_titles = {norm_title(i["title"]) for i in items if i["kind"] == "official"}
    items = [i for i in items
             if i["kind"] == "official" or norm_title(i["title"]) not in official_titles]
    items = drop_reposts(items)
    per, kept = {}, []
    for i in items:
        key = (i["companyId"], i["kind"])
        per[key] = per.get(key, 0) + 1
        cap = MAX_OFFICIAL_PER_COMPANY if i["kind"] == "official" else MAX_MEDIA_PER_COMPANY
        if per[key] <= cap:
            kept.append(i)
    per = {}
    for i in kept:
        per[i["companyId"]] = per.get(i["companyId"], 0) + 1

    # GLOSSARY 改了就把 TRANSLATION_VERSION 加一，舊譯文會在下次排程重翻
    prev_zh = {i["id"]: i.get("title_zh") for i in prev_items
               if i.get("title_zh") and i.get("title_zh_v") == TRANSLATION_VERSION}
    tr = add_translations(kept, prev_zh)
    print(f"  翻譯：本次新翻 {tr['translated_this_run']} 則、未翻 {tr['untranslated']} 則"
          + (f"（{tr['error']}）" if tr["error"] else ""))

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
            "translation": tr,
            "keep_days": KEEP_DAYS,
            "max_per_company": {"official": MAX_OFFICIAL_PER_COMPANY,
                                "media": MAX_MEDIA_PER_COMPANY},
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
