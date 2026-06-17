#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup


JST = timezone(timedelta(hours=9))
STATE_PATH = Path("state_policy24.json")

# ===== 設定 =====
MAX_POSTS_PER_STOCK = int(os.getenv("MAX_POSTS_PER_STOCK", "40"))

# エラーを減らすため、銘柄ごとに2秒あける
REQUEST_INTERVAL_SEC = float(os.getenv("REQUEST_INTERVAL_SEC", "2.0"))

# 新規投稿がこの件数以上 + 弱キーワードありなら通知
POST_SPIKE_THRESHOLD = int(os.getenv("POST_SPIKE_THRESHOLD", "5"))

# キーワードなしでも、新規投稿がこの件数以上なら通知
POST_SPIKE_ONLY_THRESHOLD = int(os.getenv("POST_SPIKE_ONLY_THRESHOLD", "8"))
ALERT_POST_SPIKE_ONLY = os.getenv("ALERT_POST_SPIKE_ONLY", "true").lower() == "true"

# 初回は大量通知を避けるため通知しない
ALERT_ON_FIRST_RUN = os.getenv("ALERT_ON_FIRST_RUN", "false").lower() == "true"

IFTTT_WEBHOOK_KEY = os.getenv("IFTTT_WEBHOOK_KEY") or ""
IFTTT_EVENT_NAME = os.getenv("IFTTT_EVENT_NAME") or "yahoo_board_spike"

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}


# ===== 監視銘柄 24銘柄 =====
# GX、医療機器、ゲーム株は除外。
# QPSは旧5595ではなく、464A QPSホールディングスを監視。
WATCHLIST: List[Dict[str, str]] = [
    # 国策AI・NEDO・GENIAC系
    {"code": "5574", "name": "ABEJA", "theme": "国策AI/NEDO"},
    {"code": "4488", "name": "AI inside", "theme": "国策AI/NEDO"},
    {"code": "5572", "name": "Ridge-i", "theme": "国策AI/NEDO"},
    {"code": "4418", "name": "JDSC", "theme": "国策AI/NEDO"},
    {"code": "4011", "name": "ヘッドウォータース", "theme": "国策AI/量子"},
    {"code": "3687", "name": "フィックスターズ", "theme": "国策AI/量子"},
    {"code": "3778", "name": "さくらインターネット", "theme": "AI計算資源"},

    # 防衛・ドローン・宇宙系
    {"code": "278A", "name": "Terra Drone", "theme": "防衛/ドローン"},
    {"code": "6232", "name": "ACSL", "theme": "防衛/ドローン"},
    {"code": "5597", "name": "ブルーイノベーション", "theme": "ドローン"},
    {"code": "218A", "name": "Liberaware", "theme": "ドローン/国交省"},
    {"code": "186A", "name": "アストロスケールHD", "theme": "宇宙/JAXA"},
    {"code": "9348", "name": "ispace", "theme": "宇宙/JAXA"},
    {"code": "464A", "name": "QPSホールディングス", "theme": "宇宙/衛星"},
    {"code": "7721", "name": "東京計器", "theme": "防衛/計測"},
    {"code": "6946", "name": "日本アビオニクス", "theme": "防衛/赤外線"},

    # 量子系
    {"code": "6864", "name": "エヌエフHD", "theme": "量子"},
    {"code": "6521", "name": "オキサイド", "theme": "量子"},

    # 官公庁サイバー系
    {"code": "3692", "name": "FFRIセキュリティ", "theme": "官公庁サイバー"},
    {"code": "4493", "name": "サイバーセキュリティクラウド", "theme": "サイバー"},
    {"code": "4417", "name": "グローバルセキュリティエキスパート", "theme": "サイバー"},
    {"code": "4398", "name": "ブロードバンドセキュリティ", "theme": "サイバー"},
    {"code": "3042", "name": "セキュアヴェイル", "theme": "サイバー"},
    {"code": "2326", "name": "デジタルアーツ", "theme": "官公庁サイバー"},
]


# 強キーワード：1件でも通知
STRONG_KEYWORDS = [
    "NEDO", "ＧＥＮＩＡＣ", "GENIAC", "SBIR", "ＳＢＩＲ",
    "SIP", "ＳＩＰ", "JST", "ＪＳＴ",
    "JAXA", "ＪＡＸＡ", "宇宙戦略基金",
    "防衛装備庁", "防衛省", "国交省", "経産省", "総務省", "デジタル庁",
    "採択", "交付決定", "受注", "落札", "契約", "委託", "選定", "公募結果",
    "官公庁", "自治体", "政府調達",
    "量子暗号", "ポスト量子暗号", "PQC", "ＰＱＣ",
    "能動的サイバー防御", "ゼロトラスト", "SBOM", "ＳＢＯＭ",
    "認定", "採用",
]

# 弱キーワード：投稿数急増とセットなら通知
WEAK_KEYWORDS = [
    "AI", "ＡＩ", "生成AI", "生成ＡＩ", "計算資源", "GPU", "ＧＰＵ",
    "ドローン", "無人機", "UAV", "ＵＡＶ",
    "衛星", "宇宙", "量子", "サイバー",
    "導入", "共同研究", "実証", "補助金", "大型案件",
    "ランサムウェア", "不正アクセス", "情報漏えい", "脆弱性",
]

IGNORE_TEXT_PATTERNS = [
    "Yahoo!ファイナンス", "Yahooファイナンス", "利用規約", "プライバシー",
    "ログイン", "新規登録", "掲示板", "株式ランキング", "みんなの評価",
    "投稿する", "返信する", "違反報告", "ポートフォリオ",
]


@dataclass
class Post:
    post_id: str
    text: str


def now_jst_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def board_url(code: str) -> str:
    return f"https://finance.yahoo.co.jp/quote/{code}.T/forum"


def normalize_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def looks_like_post_text(text: str) -> bool:
    if not text:
        return False
    if len(text) < 5 or len(text) > 1200:
        return False
    if any(p in text for p in IGNORE_TEXT_PATTERNS):
        return False
    if not re.search(r"[ぁ-んァ-ヶ一-龠ーA-Za-z0-9]", text):
        return False
    return True


def make_post_id(code: str, text: str, extra: str = "") -> str:
    src = f"{code}|{extra}|{normalize_text(text)[:600]}"
    return hashlib.sha1(src.encode("utf-8", errors="ignore")).hexdigest()[:16]


def iter_json_values(obj: Any) -> Iterable[Any]:
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from iter_json_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_json_values(v)


def extract_posts_from_json(code: str, data: Any) -> List[Post]:
    posts: List[Post] = []
    seen = set()

    text_keys = ("body", "text", "message", "comment", "content", "description")
    id_keys = ("id", "commentId", "postId", "no", "index", "createdAt", "updatedAt", "date", "time")

    for item in iter_json_values(data):
        if not isinstance(item, dict):
            continue

        text = None
        for k in text_keys:
            v = item.get(k)
            if isinstance(v, str):
                candidate = normalize_text(v)
                if looks_like_post_text(candidate):
                    text = candidate
                    break

        if not text:
            continue

        extra_parts = []
        for k in id_keys:
            v = item.get(k)
            if isinstance(v, (str, int, float)):
                extra_parts.append(str(v))

        pid = make_post_id(code, text, "|".join(extra_parts))

        if pid in seen:
            continue

        seen.add(pid)
        posts.append(Post(post_id=pid, text=text))

    return posts


def extract_posts_from_html_fallback(code: str, soup: BeautifulSoup) -> List[Post]:
    posts: List[Post] = []
    seen = set()

    for selector in ["article", "li", "section", "div"]:
        for node in soup.select(selector):
            text = normalize_text(node.get_text(" "))

            if not looks_like_post_text(text):
                continue

            if len(text) > 600:
                continue

            pid = make_post_id(code, text)

            if pid in seen:
                continue

            seen.add(pid)
            posts.append(Post(post_id=pid, text=text))

            if len(posts) >= MAX_POSTS_PER_STOCK * 2:
                break

        if posts:
            break

    return posts


def fetch_posts(code: str) -> Tuple[List[Post], Optional[str]]:
    url = board_url(code)
    last_error = None

    # 一時的なエラー対策で2回試す
    for _ in range(2):
        try:
            res = requests.get(url, headers=HEADERS, timeout=25)

            if res.status_code != 200:
                last_error = f"HTTP {res.status_code}"
                time.sleep(2)
                continue

            soup = BeautifulSoup(res.text, "html.parser")

            posts: List[Post] = []
            seen_ids = set()

            # Next.js等のJSONから投稿を拾う
            for script in soup.find_all("script"):
                raw = script.string or script.get_text() or ""
                raw = raw.strip()

                if not raw:
                    continue

                if not (raw.startswith("{") or raw.startswith("[")):
                    continue

                try:
                    data = json.loads(raw)
                except Exception:
                    continue

                for p in extract_posts_from_json(code, data):
                    if p.post_id not in seen_ids:
                        seen_ids.add(p.post_id)
                        posts.append(p)

            # JSONで取れない時だけHTMLから拾う
            if not posts:
                posts = extract_posts_from_html_fallback(code, soup)

            unique: List[Post] = []
            seen_text = set()

            for p in posts:
                key = normalize_text(p.text)

                if key in seen_text:
                    continue

                seen_text.add(key)
                unique.append(p)

                if len(unique) >= MAX_POSTS_PER_STOCK:
                    break

            return unique, None

        except Exception as e:
            last_error = repr(e)
            time.sleep(2)

    return [], last_error


def keyword_hits(text: str, keywords: Sequence[str]) -> List[str]:
    hits = []
    upper_text = text.upper()

    for kw in keywords:
        if kw.upper() in upper_text:
            hits.append(kw)

    return hits


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"stocks": {}, "updated_at": None}

    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"stocks": {}, "updated_at": None}


def save_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = now_jst_iso()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_ifttt(value1: str, value2: str, value3: str) -> bool:
    if not IFTTT_WEBHOOK_KEY:
        print("[WARN] IFTTT_WEBHOOK_KEY が未設定のため通知をスキップしました。")
        print(f"       {value1} | {value2} | {value3}")
        return False

    url = f"https://maker.ifttt.com/trigger/{IFTTT_EVENT_NAME}/with/key/{IFTTT_WEBHOOK_KEY}"

    payload = {
        "value1": value1[:1000],
        "value2": value2[:1000],
        "value3": value3[:1000],
    }

    try:
        res = requests.post(url, json=payload, timeout=15)

        if 200 <= res.status_code < 300:
            return True

        print(f"[WARN] IFTTT通知失敗 HTTP {res.status_code}: {res.text[:200]}")
        return False

    except Exception as e:
        print(f"[WARN] IFTTT通知例外: {e!r}")
        return False


def make_snippet(posts: Sequence[Post], max_len: int = 180) -> str:
    if not posts:
        return ""

    text = re.sub(r"\s+", " ", posts[0].text).strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


def main() -> int:
    print(f"=== policy24 board monitor start: {now_jst_iso()} ===")

    state = load_state()
    stocks_state: Dict[str, Any] = state.setdefault("stocks", {})

    total_alerts = 0
    total_errors = 0

    for stock in WATCHLIST:
        code = stock["code"]
        name = stock["name"]
        theme = stock["theme"]
        url = board_url(code)

        print(f"\n[{code} {name}] fetch {url}")

        posts, error = fetch_posts(code)

        if error:
            total_errors += 1
            print(f"  ERROR: {error}")

            prev = stocks_state.get(code, {})
            stocks_state[code] = {
                **prev,
                "name": name,
                "theme": theme,
                "url": url,
                "last_error": error,
                "updated_at": now_jst_iso(),
            }

            time.sleep(REQUEST_INTERVAL_SEC)
            continue

        prev = stocks_state.get(code, {})
        prev_seen = set(prev.get("seen_post_ids", []))
        first_run_for_stock = code not in stocks_state

        current_ids = [p.post_id for p in posts]
        new_posts = [p for p in posts if p.post_id not in prev_seen]

        strong_posts: List[Tuple[Post, List[str]]] = []
        weak_posts: List[Tuple[Post, List[str]]] = []

        for p in new_posts:
            sh = keyword_hits(p.text, STRONG_KEYWORDS)
            wh = keyword_hits(p.text, WEAK_KEYWORDS)

            if sh:
                strong_posts.append((p, sh))

            if wh:
                weak_posts.append((p, wh))

        print(
            f"  posts={len(posts)} "
            f"new={len(new_posts)} "
            f"strong={len(strong_posts)} "
            f"weak={len(weak_posts)}"
        )

        should_alert = False
        alert_type = ""
        hit_words: List[str] = []
        sample_posts: List[Post] = []

        if strong_posts:
            should_alert = True
            alert_type = "強キーワード検知"
            hit_words = sorted({kw for _, kws in strong_posts for kw in kws})[:10]
            sample_posts = [p for p, _ in strong_posts]

        elif len(new_posts) >= POST_SPIKE_THRESHOLD and weak_posts:
            should_alert = True
            alert_type = "投稿急増＋弱キーワード"
            hit_words = sorted({kw for _, kws in weak_posts for kw in kws})[:10]
            sample_posts = [p for p, _ in weak_posts]

        elif ALERT_POST_SPIKE_ONLY and len(new_posts) >= POST_SPIKE_ONLY_THRESHOLD:
            should_alert = True
            alert_type = "投稿数急増"
            sample_posts = new_posts

        # 初回は既存投稿を保存するだけ
        if first_run_for_stock and not ALERT_ON_FIRST_RUN:
            print("  first run baseline only: no alert")
            should_alert = False

        if should_alert:
            total_alerts += 1

            title = f"政策24｜{code} {name}｜{alert_type}"
            value2 = (
                f"テーマ:{theme} / 新規{len(new_posts)}件"
                + (f" / 検出:{', '.join(hit_words)}" if hit_words else "")
                + f" / 抜粋:{make_snippet(sample_posts)}"
            )
            value3 = url

            ok = send_ifttt(title, value2, value3)
            print(f"  ALERT sent={ok}: {title}")

        # 最新分＋過去分を残し、再通知を抑える
        merged_seen = list(dict.fromkeys(current_ids + list(prev_seen)))[:400]

        stocks_state[code] = {
            "name": name,
            "theme": theme,
            "url": url,
            "seen_post_ids": merged_seen,
            "last_seen_count_on_page": len(posts),
            "last_new_count": len(new_posts),
            "last_error": None,
            "updated_at": now_jst_iso(),
        }

        time.sleep(REQUEST_INTERVAL_SEC)

    save_state(state)

    print(
        f"\n=== done: alerts={total_alerts}, "
        f"errors={total_errors}, "
        f"updated={now_jst_iso()} ==="
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
