import os
import re
import json
import time
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


JST = ZoneInfo("Asia/Tokyo")
STATE_PATH = "data/state.json"

IFTTT_KEY = os.environ.get("IFTTT_KEY", "")
IFTTT_EVENT = os.environ.get("IFTTT_EVENT", "yahoo_board_spike")

# 直近5分の投稿数が、その前の5分より何件増えたら通知するか
SURGE_THRESHOLD = 5

# 同じ銘柄の重複通知を防ぐ時間
ALERT_COOLDOWN_MINUTES = 15

# Yahoo側に負荷をかけすぎないよう、銘柄ごとに少し待つ
MIN_SLEEP_SEC = 1.5
MAX_SLEEP_SEC = 3.5


# ============================================================
# 監視銘柄
# ============================================================

BOARDS = [
    {
        "name": "リケンNPR",
        "code": "6209",
        "url": "https://finance.yahoo.co.jp/quote/6209.T/forum",
    },
    {
        "name": "QPSホールディングス",
        "code": "464A",
        "url": "https://finance.yahoo.co.jp/quote/464A.T/forum",
    },
    {
        "name": "アストロスケールHD",
        "code": "186A",
        "url": "https://finance.yahoo.co.jp/quote/186A.T/forum",
    },
    {
        "name": "Terra Drone",
        "code": "278A",
        "url": "https://finance.yahoo.co.jp/quote/278A.T/forum",
    },
    {
        "name": "KLab",
        "code": "3656",
        "url": "https://finance.yahoo.co.jp/quote/3656.T/forum",
    },
    {
        "name": "gumi",
        "code": "3903",
        "url": "https://finance.yahoo.co.jp/quote/3903.T/forum",
    },
    {
        "name": "ワンダープラネット",
        "code": "4199",
        "url": "https://finance.yahoo.co.jp/quote/4199.T/forum",
    },
    {
        "name": "ジャパンエンジン",
        "code": "6016",
        "url": "https://finance.yahoo.co.jp/quote/6016.T/forum",
    },
    {
        "name": "ELEMENTS",
        "code": "5246",
        "url": "https://finance.yahoo.co.jp/quote/5246.T/forum",
    },
    {
        "name": "中越パルプ工業",
        "code": "3877",
        "url": "https://finance.yahoo.co.jp/quote/3877.T/forum",
    },
    {
        "name": "明電舎",
        "code": "6508",
        "url": "https://finance.yahoo.co.jp/quote/6508.T/forum",
    },
    {
        "name": "カナデビア",
        "code": "7004",
        "url": "https://finance.yahoo.co.jp/quote/7004.T/forum",
    },
    {
        "name": "日本触媒",
        "code": "4114",
        "url": "https://finance.yahoo.co.jp/quote/4114.T/forum",
    },
    {
        "name": "駒井ハルテック",
        "code": "5915",
        "url": "https://finance.yahoo.co.jp/quote/5915.T/forum",
    },
    {
        "name": "ACSL",
        "code": "6232",
        "url": "https://finance.yahoo.co.jp/quote/6232.T/forum",
    },
    {
        "name": "ザインエレクトロニクス",
        "code": "6769",
        "url": "https://finance.yahoo.co.jp/quote/6769.T/forum",
    },
]


def load_state():
    """
    前回までの通知時刻などを読み込む。
    状態ファイルが存在しない場合や壊れている場合は空で開始する。
    """
    if not os.path.exists(STATE_PATH):
        return {}

    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

        return {}

    except Exception as e:
        print(f"state load error: {e}")
        return {}


def save_state(state):
    """
    通知時刻や最終実行結果を状態ファイルへ保存する。
    """
    state_dir = os.path.dirname(STATE_PATH)

    if state_dir:
        os.makedirs(state_dir, exist_ok=True)

    temporary_path = STATE_PATH + ".tmp"

    with open(temporary_path, "w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(temporary_path, STATE_PATH)


def is_market_open(now):
    """
    日本株の市場時間内か判定する。

    土日は市場時間外。
    祝日判定には未対応。
    """
    if now.weekday() >= 5:
        return False

    minutes = now.hour * 60 + now.minute

    morning_open = 9 * 60
    morning_close = 11 * 60 + 30

    afternoon_open = 12 * 60 + 30
    afternoon_close = 15 * 60 + 30

    return (
        morning_open <= minutes < morning_close
        or afternoon_open <= minutes < afternoon_close
    )


def fetch_html(url):
    """
    Yahooファイナンス掲示板のHTMLを取得する。

    一時的なアクセスエラーに備えて最大3回試す。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 "
            "(iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    last_error = None

    for attempt in range(3):
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=20,
            )

            print(
                f"  fetch attempt={attempt + 1} "
                f"status={response.status_code}"
            )

            if response.status_code == 200 and response.text:
                return response.text

            last_error = f"status={response.status_code}"

        except Exception as e:
            last_error = str(e)

            print(
                f"  fetch error "
                f"attempt={attempt + 1}: {e}"
            )

        # 失敗した場合は少し待ってから再試行
        time.sleep(3 + attempt * 3)

    raise RuntimeError(
        f"fetch failed: {last_error}"
    )


def html_to_text(html):
    """
    HTMLからスクリプトやスタイルを除き、画面上の文章へ変換する。
    """
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    for tag in soup(
        [
            "script",
            "style",
            "noscript",
        ]
    ):
        tag.decompose()

    text = soup.get_text("\n")

    text = re.sub(
        r"\n+",
        "\n",
        text,
    )

    return text


def extract_post_dates(html, now):
    """
    掲示板ページから投稿日時を抽出する。

    同じ分に複数の投稿があった場合も、
    それぞれ別の投稿として数える。
    """
    text = html_to_text(html)

    patterns = [
        r"(\d{4})/(\d{1,2})/(\d{1,2})\s+([0-2]?\d):([0-5]\d)",
        r"(\d{4})-(\d{1,2})-(\d{1,2})\s+([0-2]?\d):([0-5]\d)",
        r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+([0-2]?\d):([0-5]\d)",
    ]

    results = []

    for pattern in patterns:
        matches = list(
            re.finditer(
                pattern,
                text,
            )
        )

        # 日付形式は通常いずれか1種類なので、
        # 投稿日時を見つけた形式だけを採用する
        if not matches:
            continue

        for match in matches:
            try:
                year, month, day, hour, minute = map(
                    int,
                    match.groups(),
                )

                post_datetime = datetime(
                    year,
                    month,
                    day,
                    hour,
                    minute,
                    tzinfo=JST,
                )

            except (ValueError, TypeError):
                continue

            # 未来の日時は誤取得として除外
            if post_datetime > now + timedelta(minutes=1):
                continue

            # 古すぎる投稿は今回の判定には不要
            if post_datetime < now - timedelta(minutes=20):
                continue

            results.append(post_datetime)

        # 複数の日付形式による重複カウントを防ぐ
        if results:
            break

    results.sort(reverse=True)

    return results


def judge_spike(post_dates, now):
    """
    直近5分と、その前の5分の投稿数を比較する。

    例:
    直近5分 8件
    前の5分 3件
    差は+5件なので通知対象。
    """
    last5 = 0
    prev5 = 0

    for post_datetime in post_dates:
        diff_minutes = (
            now - post_datetime
        ).total_seconds() / 60

        if 0 <= diff_minutes < 5:
            last5 += 1

        elif 5 <= diff_minutes < 10:
            prev5 += 1

    surge = last5 - prev5

    should_alert = surge >= SURGE_THRESHOLD

    return (
        should_alert,
        last5,
        prev5,
        surge,
    )


def send_ifttt(
    name,
    code,
    url,
    last5,
    prev5,
    surge,
    market_open,
):
    """
    IFTTT Webhooksへ通知する。
    """
    if not IFTTT_KEY:
        print(
            "  IFTTT_KEY not set. "
            "skip notification."
        )
        return False

    mode = (
        "市場時間内"
        if market_open
        else "市場時間外"
    )

    webhook_url = (
        "https://maker.ifttt.com/trigger/"
        f"{IFTTT_EVENT}"
        "/with/key/"
        f"{IFTTT_KEY}"
    )

    payload = {
        "value1": (
            f"{name} 掲示板急増"
        ),
        "value2": (
            f"{mode} / {code} / "
            f"直近5分:{last5}件"
        ),
        "value3": (
            f"前5分:{prev5}件 / "
            f"差:{surge:+d}件 / "
            f"{url}"
        ),
    }

    try:
        response = requests.post(
            webhook_url,
            data=payload,
            timeout=20,
        )

        print(
            f"  IFTTT status="
            f"{response.status_code} "
            f"body={response.text[:120]}"
        )

        return (
            200
            <= response.status_code
            < 300
        )

    except Exception as e:
        print(
            f"  IFTTT error: {e}"
        )

        return False


def main():
    now = datetime.now(JST)
    market_open = is_market_open(now)

    print(
        f"start now={now.isoformat()} "
        f"market_open={market_open}"
    )

    print(
        f"boards={len(BOARDS)} "
        f"surge_threshold={SURGE_THRESHOLD}"
    )

    state = load_state()

    alerts = state.setdefault(
        "alerts",
        {},
    )

    checked = 0
    failed = 0
    alerted = 0

    for board in BOARDS:
        name = board["name"]
        code = board["code"]
        url = board["url"]

        print(
            f"\n{name} {code}"
        )

        try:
            html = fetch_html(url)

            post_dates = extract_post_dates(
                html,
                now,
            )

            if not post_dates:
                print(
                    "  post date not found"
                )

                failed += 1
                continue

            (
                should_alert,
                last5,
                prev5,
                surge,
            ) = judge_spike(
                post_dates,
                now,
            )

            print(
                f"  dates={len(post_dates)} "
                f"last5={last5} "
                f"prev5={prev5} "
                f"surge={surge:+d} "
                f"should_alert={should_alert}"
            )

            last_alert_iso = alerts.get(code)
            cooldown_ok = True

            if last_alert_iso:
                try:
                    last_alert = datetime.fromisoformat(
                        last_alert_iso
                    )

                    minutes_since = (
                        now - last_alert
                    ).total_seconds() / 60

                    if (
                        minutes_since
                        < ALERT_COOLDOWN_MINUTES
                    ):
                        cooldown_ok = False

                        print(
                            "  cooldown skip "
                            f"minutes_since="
                            f"{minutes_since:.1f}"
                        )

                except Exception as e:
                    print(
                        "  cooldown state error: "
                        f"{e}"
                    )

            if should_alert and cooldown_ok:
                notification_sent = send_ifttt(
                    name=name,
                    code=code,
                    url=url,
                    last5=last5,
                    prev5=prev5,
                    surge=surge,
                    market_open=market_open,
                )

                # IFTTT送信が成功した場合だけ
                # クールダウン開始時刻を保存する
                if notification_sent:
                    alerts[code] = now.isoformat()
                    alerted += 1

            checked += 1

        except Exception as e:
            print(
                f"  error: {e}"
            )

            failed += 1

        time.sleep(
            random.uniform(
                MIN_SLEEP_SEC,
                MAX_SLEEP_SEC,
            )
        )

    state["last_run"] = now.isoformat()

    state["last_result"] = {
        "checked": checked,
        "failed": failed,
        "alerted": alerted,
        "market_open": market_open,
        "surge_threshold": SURGE_THRESHOLD,
    }

    save_state(state)

    print("\nfinished")

    print(
        f"checked={checked} "
        f"failed={failed} "
        f"alerted={alerted}"
    )


if __name__ == "__main__":
    main()
