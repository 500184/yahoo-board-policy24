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

# 前回取得した投稿IDを保存するファイル
STATE_PATH = Path(
    os.getenv(
        "STATE_PATH",
        "state_yahoo_board_5min.json",
    )
)


# ============================================================
# 設定
# ============================================================

# 1銘柄につき取得する最大投稿数
MAX_POSTS_PER_STOCK = int(
    os.getenv(
        "MAX_POSTS_PER_STOCK",
        "60",
    )
)

# 銘柄ごとのアクセス間隔
REQUEST_INTERVAL_SEC = float(
    os.getenv(
        "REQUEST_INTERVAL_SEC",
        "2.0",
    )
)

# 前回実行時より新しい投稿が5件以上なら通知
NEW_POST_THRESHOLD = int(
    os.getenv(
        "NEW_POST_THRESHOLD",
        "5",
    )
)

# 初回実行では通知せず、現在の投稿を基準として保存する
ALERT_ON_FIRST_RUN = (
    os.getenv(
        "ALERT_ON_FIRST_RUN",
        "false",
    ).strip().lower()
    == "true"
)

# IFTTT設定
# IFTTT_WEBHOOK_KEY、IFTTT_KEYのどちらでも動作
IFTTT_WEBHOOK_KEY = (
    os.getenv("IFTTT_WEBHOOK_KEY")
    or os.getenv("IFTTT_KEY")
    or ""
)

# IFTTT_EVENT_NAME、IFTTT_EVENTのどちらでも動作
IFTTT_EVENT_NAME = (
    os.getenv("IFTTT_EVENT_NAME")
    or os.getenv("IFTTT_EVENT")
    or "yahoo_board_spike"
)

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 "
    "(iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1",
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# ============================================================
# 監視銘柄
# ============================================================

WATCHLIST: List[Dict[str, str]] = [
    {
        "code": "6209",
        "name": "リケンNPR",
    },
    {
        "code": "464A",
        "name": "QPSホールディングス",
    },
    {
        "code": "186A",
        "name": "アストロスケールHD",
    },
    {
        "code": "278A",
        "name": "Terra Drone",
    },
    {
        "code": "3656",
        "name": "KLab",
    },
    {
        "code": "3903",
        "name": "gumi",
    },
    {
        "code": "4199",
        "name": "ワンダープラネット",
    },
    {
        "code": "6016",
        "name": "ジャパンエンジンコーポレーション",
    },
    {
        "code": "5246",
        "name": "ELEMENTS",
    },
    {
        "code": "3877",
        "name": "中越パルプ工業",
    },
    {
        "code": "6508",
        "name": "明電舎",
    },
    {
        "code": "7004",
        "name": "カナデビア",
    },
    {
        "code": "4114",
        "name": "日本触媒",
    },
    {
        "code": "5915",
        "name": "駒井ハルテック",
    },
    {
        "code": "6232",
        "name": "ACSL",
    },
    {
        "code": "6769",
        "name": "ザインエレクトロニクス",
    },
]


# Yahoo掲示板の投稿本文ではない可能性が高い文章
IGNORE_TEXT_PATTERNS = [
    "Yahoo!ファイナンス",
    "Yahooファイナンス",
    "利用規約",
    "プライバシー",
    "ログイン",
    "新規登録",
    "株式ランキング",
    "みんなの評価",
    "投稿する",
    "返信する",
    "違反報告",
    "ポートフォリオ",
]


@dataclass(frozen=True)
class Post:
    post_id: str
    text: str


def now_jst_iso() -> str:
    """現在の日本時間をISO形式で返す。"""
    return datetime.now(JST).isoformat(timespec="seconds")


def board_url(code: str) -> str:
    """Yahooファイナンス掲示板のURLを作る。"""
    return f"https://finance.yahoo.co.jp/quote/{code}.T/forum"


def normalize_text(text: str) -> str:
    """HTMLエスケープや余分な空白を整理する。"""
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def looks_like_post_text(text: str) -> bool:
    """掲示板の投稿本文らしい文章か判定する。"""
    if not text:
        return False

    if len(text) < 5:
        return False

    if len(text) > 1200:
        return False

    if any(
        pattern in text
        for pattern in IGNORE_TEXT_PATTERNS
    ):
        return False

    if not re.search(
        r"[ぁ-んァ-ヶ一-龠ーA-Za-z0-9]",
        text,
    ):
        return False

    return True


def sha1_short(value: str) -> str:
    """短いハッシュ値を作る。"""
    return hashlib.sha1(
        value.encode(
            "utf-8",
            errors="ignore",
        )
    ).hexdigest()[:20]


def make_post_id(
    code: str,
    text: str,
    stable_id: str = "",
) -> str:
    """
    投稿IDを作る。

    Yahoo側の投稿IDが取得できた場合はそのIDを使用。
    取得できなかった場合は投稿本文からIDを作る。
    """
    if stable_id:
        return sha1_short(
            f"{code}|stable-id|{stable_id}"
        )

    normalized = normalize_text(text)

    return sha1_short(
        f"{code}|text|{normalized[:800]}"
    )


def iter_json_values(
    obj: Any,
) -> Iterable[Any]:
    """JSON内のすべての値を再帰的に取得する。"""
    yield obj

    if isinstance(obj, dict):
        for value in obj.values():
            yield from iter_json_values(value)

    elif isinstance(obj, list):
        for value in obj:
            yield from iter_json_values(value)


def get_first_scalar(
    item: Dict[str, Any],
    keys: Sequence[str],
) -> str:
    """指定したキーのうち、最初に見つかった値を返す。"""
    for key in keys:
        value = item.get(key)

        if isinstance(
            value,
            (str, int, float),
        ):
            return str(value)

    return ""


def extract_posts_from_json(
    code: str,
    data: Any,
) -> List[Post]:
    """ページ内のJSONから投稿を抽出する。"""
    posts: List[Post] = []
    seen_ids = set()

    text_keys = (
        "body",
        "text",
        "message",
        "comment",
        "content",
        "description",
    )

    stable_id_keys = (
        "commentId",
        "postId",
        "messageId",
        "threadCommentId",
        "id",
        "no",
        "index",
    )

    for item in iter_json_values(data):
        if not isinstance(item, dict):
            continue

        post_text = ""

        for key in text_keys:
            value = item.get(key)

            if not isinstance(value, str):
                continue

            candidate = normalize_text(value)

            if looks_like_post_text(candidate):
                post_text = candidate
                break

        if not post_text:
            continue

        stable_id = get_first_scalar(
            item,
            stable_id_keys,
        )

        post_id = make_post_id(
            code=code,
            text=post_text,
            stable_id=stable_id,
        )

        if post_id in seen_ids:
            continue

        seen_ids.add(post_id)

        posts.append(
            Post(
                post_id=post_id,
                text=post_text,
            )
        )

    return posts


def extract_posts_from_html_fallback(
    code: str,
    soup: BeautifulSoup,
) -> List[Post]:
    """
    JSONから取得できない場合に、
    HTML要素から投稿を取得する。
    """
    selector_groups = [
        '[data-testid*="comment"]',
        '[class*="Comment"]',
        "article",
        "li",
        "section",
        "div",
    ]

    for selector in selector_groups:
        posts: List[Post] = []
        seen_ids = set()
        seen_text = set()

        for node in soup.select(selector):
            text = normalize_text(
                node.get_text(
                    " ",
                    strip=True,
                )
            )

            if not looks_like_post_text(text):
                continue

            # 親要素全体を投稿として拾う誤検知を減らす
            if len(text) > 600:
                continue

            if text in seen_text:
                continue

            seen_text.add(text)

            post_id = make_post_id(
                code=code,
                text=text,
            )

            if post_id in seen_ids:
                continue

            seen_ids.add(post_id)

            posts.append(
                Post(
                    post_id=post_id,
                    text=text,
                )
            )

            if (
                len(posts)
                >= MAX_POSTS_PER_STOCK * 2
            ):
                break

        # このセレクタで投稿が取得できた場合は採用
        if posts:
            return posts

    return []


def fetch_posts(
    code: str,
) -> Tuple[List[Post], Optional[str]]:
    """
    Yahooファイナンス掲示板から投稿を取得する。

    一時的な通信エラーに備え、最大2回試行する。
    """
    url = board_url(code)
    last_error: Optional[str] = None

    for attempt in range(2):
        try:
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=25,
            )

            if response.status_code != 200:
                last_error = (
                    f"HTTP {response.status_code}"
                )

                if attempt == 0:
                    time.sleep(2)

                continue

            soup = BeautifulSoup(
                response.text,
                "html.parser",
            )

            posts: List[Post] = []
            seen_ids = set()

            # Next.jsなどの埋め込みJSONを探す
            for script in soup.find_all("script"):
                raw = (
                    script.string
                    or script.get_text()
                    or ""
                )

                raw = raw.strip()

                if not raw:
                    continue

                if not (
                    raw.startswith("{")
                    or raw.startswith("[")
                ):
                    continue

                try:
                    data = json.loads(raw)

                except (
                    json.JSONDecodeError,
                    TypeError,
                ):
                    continue

                extracted_posts = (
                    extract_posts_from_json(
                        code,
                        data,
                    )
                )

                for post in extracted_posts:
                    if post.post_id in seen_ids:
                        continue

                    seen_ids.add(post.post_id)
                    posts.append(post)

            # JSONから投稿を取れなかった場合
            if not posts:
                posts = (
                    extract_posts_from_html_fallback(
                        code,
                        soup,
                    )
                )

            unique_posts: List[Post] = []
            seen_text = set()

            for post in posts:
                normalized = normalize_text(
                    post.text
                )

                if normalized in seen_text:
                    continue

                seen_text.add(normalized)
                unique_posts.append(post)

                if (
                    len(unique_posts)
                    >= MAX_POSTS_PER_STOCK
                ):
                    break

            if not unique_posts:
                return (
                    [],
                    (
                        "投稿を取得できませんでした"
                        "（Yahoo側のHTML変更の可能性）"
                    ),
                )

            return unique_posts, None

        except requests.RequestException as exc:
            last_error = repr(exc)

            if attempt == 0:
                time.sleep(2)

        except Exception as exc:
            last_error = repr(exc)

            if attempt == 0:
                time.sleep(2)

    return (
        [],
        last_error or "unknown error",
    )


def load_state() -> Dict[str, Any]:
    """前回の監視状態を読み込む。"""
    if not STATE_PATH.exists():
        return {
            "stocks": {},
            "updated_at": None,
        }

    try:
        data = json.loads(
            STATE_PATH.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(data, dict):
            raise ValueError(
                "state is not a dictionary"
            )

        data.setdefault("stocks", {})

        return data

    except Exception as exc:
        print(
            "[WARN] 状態ファイルを"
            f"読み込めませんでした: {exc!r}"
        )

        return {
            "stocks": {},
            "updated_at": None,
        }


def save_state(
    state: Dict[str, Any],
) -> None:
    """今回の監視状態を保存する。"""
    state["updated_at"] = now_jst_iso()

    if STATE_PATH.parent != Path("."):
        STATE_PATH.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

    temporary_path = STATE_PATH.with_suffix(
        STATE_PATH.suffix + ".tmp"
    )

    temporary_path.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary_path.replace(STATE_PATH)


def send_ifttt(
    value1: str,
    value2: str,
    value3: str,
) -> bool:
    """IFTTT Webhooksへ通知する。"""
    if not IFTTT_WEBHOOK_KEY:
        print(
            "[WARN] IFTTT_WEBHOOK_KEY "
            "または IFTTT_KEY が未設定です。"
        )

        print(
            f"       {value1} | "
            f"{value2} | "
            f"{value3}"
        )

        return False

    url = (
        "https://maker.ifttt.com/trigger/"
        f"{IFTTT_EVENT_NAME}"
        "/with/key/"
        f"{IFTTT_WEBHOOK_KEY}"
    )

    payload = {
        "value1": value1[:1000],
        "value2": value2[:1000],
        "value3": value3[:1000],
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )

        if (
            200
            <= response.status_code
            < 300
        ):
            return True

        print(
            "[WARN] IFTTT通知失敗 "
            f"HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

        return False

    except requests.RequestException as exc:
        print(
            f"[WARN] IFTTT通知例外: {exc!r}"
        )

        return False


def make_snippet(
    posts: Sequence[Post],
    max_len: int = 220,
) -> str:
    """通知に載せる投稿の抜粋を作る。"""
    if not posts:
        return ""

    snippets: List[str] = []

    for post in posts[:3]:
        text = normalize_text(post.text)

        if len(text) > 90:
            text = text[:90] + "…"

        snippets.append(text)

    joined = " / ".join(snippets)

    if len(joined) > max_len:
        return joined[:max_len] + "…"

    return joined


def main() -> int:
    started_at = now_jst_iso()

    print(
        "=== Yahoo掲示板5分監視 start: "
        f"{started_at} ==="
    )

    print(f"state={STATE_PATH}")
    print(
        f"threshold={NEW_POST_THRESHOLD}"
    )

    state = load_state()

    stocks_state: Dict[str, Any] = (
        state.setdefault(
            "stocks",
            {},
        )
    )

    total_alerts = 0
    total_errors = 0

    for stock in WATCHLIST:
        code = stock["code"]
        name = stock["name"]
        url = board_url(code)

        print(
            f"\n[{code} {name}] "
            f"fetch {url}"
        )

        posts, error = fetch_posts(code)

        previous = stocks_state.get(
            code,
            {},
        )

        if error:
            total_errors += 1

            print(f"  ERROR: {error}")

            stocks_state[code] = {
                **previous,
                "name": name,
                "url": url,
                "last_error": error,
                "updated_at": now_jst_iso(),
            }

            time.sleep(
                REQUEST_INTERVAL_SEC
            )

            continue

        previous_seen_list = previous.get(
            "seen_post_ids",
            [],
        )

        if not isinstance(
            previous_seen_list,
            list,
        ):
            previous_seen_list = []

        previous_seen = set(
            previous_seen_list
        )

        baseline_initialized = bool(
            previous.get(
                "baseline_initialized",
                False,
            )
        )

        current_ids = [
            post.post_id
            for post in posts
        ]

        # 前回までに存在しなかった投稿を抽出
        new_posts = [
            post
            for post in posts
            if post.post_id
            not in previous_seen
        ]

        print(
            f"  posts={len(posts)} "
            f"new_since_previous_run="
            f"{len(new_posts)} "
            f"baseline="
            f"{baseline_initialized}"
        )

        # 新規投稿が5件以上なら通知
        should_alert = (
            len(new_posts)
            >= NEW_POST_THRESHOLD
        )

        # 初回実行は現在の投稿を基準として保存するだけ
        if (
            not baseline_initialized
            and not ALERT_ON_FIRST_RUN
        ):
            should_alert = False

            print(
                "  first run baseline only: "
                "no alert"
            )

        if should_alert:
            total_alerts += 1

            title = (
                "Yahoo掲示板急増｜"
                f"{code} {name}"
            )

            value2 = (
                "前回5分から"
                f"新規投稿{len(new_posts)}件"
                f"（通知基準"
                f"{NEW_POST_THRESHOLD}件以上）"
                f" / 抜粋: "
                f"{make_snippet(new_posts)}"
            )

            value3 = url

            sent = send_ifttt(
                title,
                value2,
                value3,
            )

            print(
                f"  ALERT sent={sent}: "
                f"{title}"
            )

        # 現在取得した投稿IDと過去の投稿IDを保存
        # 同じ投稿の再通知を防ぐ
        merged_seen = list(
            dict.fromkeys(
                current_ids
                + previous_seen_list
            )
        )[:800]

        stocks_state[code] = {
            "name": name,
            "url": url,
            "baseline_initialized": True,
            "seen_post_ids": merged_seen,
            "last_seen_count_on_page": (
                len(posts)
            ),
            "last_new_count": (
                len(new_posts)
            ),
            "last_alerted": bool(
                should_alert
            ),
            "last_error": None,
            "updated_at": now_jst_iso(),
        }

        time.sleep(
            REQUEST_INTERVAL_SEC
        )

    save_state(state)

    print(
        "\n=== done: "
        f"alerts={total_alerts}, "
        f"errors={total_errors}, "
        f"updated={now_jst_iso()} "
        "==="
    )

    # 一部銘柄でエラーが発生しても、
    # 他銘柄の結果を保存するため正常終了扱いにする
    return 0


if __name__ == "__main__":
    sys.exit(main())
