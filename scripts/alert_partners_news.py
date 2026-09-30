# -*- coding: utf-8 -*-
"""
海外夥伴出現「與仲恩相關」的新消息時，自動開 GitHub Issue 通知 → repo 擁有者收到 Email／App 推播

由 daily.yml 在 fetch_partners_news.py 之後執行（跟每日更新同一班，台北 18:17／20:47／隔天 06:00）。
不另外抓來源：直接讀剛產出的 data/partners_news.json（已去重、已翻譯），不多打請求。

本腳本不寫任何資料檔。「通知過哪些」不存在 repo 裡，而是存在 Issue 本身：每張 Issue 內文
帶一行 <!-- partner-alert-id: xxx -->，下次執行先列出已開過的 Issue、比對 id，同一則只通知一次。

判斷「與仲恩相關」：標題（原文或中文譯文）含 KEYWORDS 任一個。寧可少報不要亂報，
關鍵字只放 Stemchymal 與仲恩本身的各語言寫法、Stemchymal 目前的適應症，以及日本再生醫療監管用語。

失敗處理：GitHub API 失敗會 exit 1，但 daily.yml 該步驟設了 continue-on-error，
通知壞掉不會擋住當晚發佈（log 裡看得到錯誤）。

用法：python alert_partners_news.py [--dry-run]
  需要環境變數 GITHUB_TOKEN、GITHUB_REPOSITORY（Actions 內建）；--dry-run 只印出會通知的條目。
"""
import json, os, re, sys, urllib.request
from datetime import datetime, timedelta

import fetch_partners_news as news

KEYWORDS = [
    "Stemchymal", "ステムカイマル", "스템카이말",
    "Steminent", "ステミネント", "스테미넌트", "仲恩",
    "脊髄小脳変性症", "척수소뇌변성증", "脊髓小腦",
    # REPROCELL 在日本唯一的再生醫療等製品就是 Stemchymal，這類監管公告標題常不寫產品名
    # （例：2026-09-24「再生医療等製品製造販売業許可の取得に関するお知らせ」）
    "再生医療等製品", "製造販売承認", "製造販売業許可",
]
# 只看最近幾天發布的消息：第一次上線時不會把幾個月前的舊聞全部通知一遍
LOOKBACK_DAYS = 3
LABEL = "partner-alert"
MENTION = "@SteminentTW"
API = "https://api.github.com"


def gh(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def alerted_ids(repo):
    ids = set()
    for page in range(1, 11):
        issues = gh("GET", f"/repos/{repo}/issues?labels={LABEL}&state=all&per_page=100&page={page}")
        for i in issues:
            ids.update(re.findall(r"partner-alert-id:\s*([0-9a-f]+)", i.get("body") or ""))
        if len(issues) < 100:
            break
    return ids


def relevant(i):
    text = i["title"] + " " + (i.get("title_zh") or "")
    return [k for k in KEYWORDS if k.lower() in text.lower()]


def main():
    dry = "--dry-run" in sys.argv
    cut = (datetime.now(news.TPE).date() - timedelta(days=LOOKBACK_DAYS)).isoformat()

    doc = news.load_json(news.OUT) or {}
    hits = []
    for i in doc.get("items", []):
        if i["date"] >= cut:
            kw = relevant(i)
            if kw:
                hits.append((i, kw))
    print(f"\n符合關鍵字的新消息 {len(hits)} 則")
    if not hits:
        return

    repo = os.environ.get("GITHUB_REPOSITORY", "SteminentTW/chip-iq")
    done = set() if dry else alerted_ids(repo)
    names = {"reprocell": "REPROCELL", "scm-lifescience": "풍전약품"}
    for i, kw in hits:
        if i["id"] in done:
            continue
        zh = i.get("title_zh")
        if not zh:
            try:
                zh = news.translate(i["title"])
            except Exception:
                zh = None
        co = names.get(i["companyId"], i["companyId"])
        kind = "公司公告" if i["kind"] == "official" else "媒體報導"
        title = f"【夥伴消息】{co}：{(zh or i['title'])[:80]}"
        body = "\n".join([
            f"{MENTION} 海外夥伴出現與仲恩相關的新消息（{kind}）。",
            "",
            f"- **日期**：{i['date']}",
            f"- **公司**：{co}",
            f"- **標題（機器翻譯）**：{zh or '（翻譯失敗，請看原文）'}",
            f"- **原文**：{i['title']}",
            f"- **來源**：{i.get('publisher') or ''} {i['url']}",
            f"- **命中關鍵字**：{'、'.join(kw)}",
            "",
            "此通知由 `scripts/alert_partners_news.py` 自動產生，未經人工判讀。"
            "確認與仲恩的關聯後，可請 Claude 起草成「夥伴動態」策展條目；處理完關閉此 Issue 即可。",
            "",
            f"<!-- partner-alert-id: {i['id']} -->",
        ])
        if dry:
            print(f"  [dry-run] {title}")
            continue
        r = gh("POST", f"/repos/{repo}/issues", {"title": title, "body": body, "labels": [LABEL]})
        print(f"  已開 Issue #{r['number']}：{title}")


if __name__ == "__main__":
    main()
