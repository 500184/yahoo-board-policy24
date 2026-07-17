import os
import re
import json
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


JST = ZoneInfo("Asia/Tokyo")
STATE_PATH = "data/state.json"


# ============================================================
# IFTTT設定
# ============================================================

def normalize_ifttt_key(raw_value):
    """
    GitHub Secretに以下のどちらが登録されていても対応する。

    1. Webhooksキーだけ
    2. Webhook URL全体
       https://maker.ifttt.com/trigger/.../with/key/XXXX
    """
    value = (raw_value or "").strip()

    if "/with/key/" in value:
        value = value.split("/with/key/", 1)[1]

    value = value.split("?", 1)[0]
    value = value.split("#", 1)[0]
    value = value.strip("/")

    return unquote(value)


def normalize_ifttt_event(raw_value):
    """
    イベント名だけでなく、Webhook URL全体が渡された場合にも対応する。
    """
    value = (raw_value or "").strip()

    if "/trigger/" in value:
        value = value.split("/trigger/", 1)[1]
        value = value.split("/", 1)[0]

    value = value.split("?", 1)[0]
    value = value.split("#", 1)[0]
    value = value.strip("/")

    return unquote(value) or "yahoo_board_spike"


IFTTT_KEY = normalize_ifttt_key(
    os.environ.get("IFTTT_KEY")
    or os.environ.get("IFTTT_WEBHOOK_KEY")
    or ""
)

IFTTT_EVENT = normalize_ifttt_event(
    os.environ.get("IFTTT_EVENT")
    or os.environ.get("IFTTT_EVENT_NAME")
    or "yahoo_board_spike"
)


# ============================================================
# 監視設定
# ============================================================

# 直近5分の投稿数が、その前の5分より5件以上増えたら通知
SURGE_THRESHOLD = 5

# 同じ銘柄の重複通知を防ぐ時間
ALERT_COOLDOWN_MINUTES = 15

# 各グループ内で銘柄ごとに待つ時間
MIN_SLEEP_SEC = 1.5
MAX_SLEEP_SEC = 3.5

# 同時に動かすグループ数
GROUP_COUNT = 2


# ============================================================
# Yahooアクセス用ヘッダー
# ============================================================

HEADERS = {
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


# ============================================================
# 監視銘柄：91銘柄
# ============================================================

BOARD_ITEMS = [
    # --------------------------------------------------------
    # 政府採択・AI・デジタル：35銘柄
    # --------------------------------------------------------
    ("ELEMENTS", "5246"),
    ("ABEJA", "5574"),
    ("Ridge-i", "5572"),
    ("ヘッドウォータース", "4011"),
    ("Fusic", "5256"),
    ("フィックスターズ", "3687"),
    ("エクサウィザーズ", "4259"),
    ("PKSHA Technology", "3993"),
    ("AI inside", "4488"),
    ("Laboro.AI", "5586"),
    ("HEROZ", "4382"),
    ("グリッド", "5582"),
    ("オプティム", "3694"),
    ("JDSC", "4418"),
    ("データセクション", "3905"),
    ("さくらインターネット", "3778"),
    ("ジーデップ・アドバンス", "5885"),
    ("FIXER", "5129"),
    ("サイバートラスト", "4498"),
    ("FFRIセキュリティ", "3692"),
    ("サイバーセキュリティクラウド", "4493"),
    ("網屋", "4258"),
    ("ブロードバンドセキュリティ", "4398"),
    ("ソリトンシステムズ", "3040"),
    ("グローバルセキュリティエキスパート", "4417"),
    ("セグエグループ", "3968"),
    ("HENNGE", "4475"),
    ("GMOグローバルサイン・ホールディングス", "3788"),
    ("セキュア", "4264"),
    ("サーバーワークス", "4434"),
    ("BeeX", "4270"),
    ("テラスカイ", "3915"),
    ("システムサポートホールディングス", "4396"),
    ("ARアドバンストテクノロジ", "5578"),
    ("JIG-SAW", "3914"),

    # --------------------------------------------------------
    # 宇宙・ドローン・インフラ：20銘柄
    # --------------------------------------------------------
    ("アストロスケールHD", "186A"),
    ("Synspective", "290A"),
    ("QPSホールディングス", "464A"),
    ("ispace", "9348"),
    ("Terra Drone", "278A"),
    ("ACSL", "6232"),
    ("Liberaware", "218A"),
    ("Kudan", "4425"),
    ("セーフィー", "4375"),
    ("アイサンテクノロジー", "4667"),
    ("ゼンリン", "9474"),
    ("エコモット", "3987"),
    ("ウェザーニューズ", "4825"),
    ("スパイダープラス", "4192"),
    ("IMV", "7760"),
    ("ウエスコホールディングス", "6091"),
    ("フコク", "5185"),
    ("シンフォニアテクノロジー", "6507"),
    ("イーグル工業", "6486"),
    ("技研製作所", "6289"),

    # --------------------------------------------------------
    # 防衛・センサー・通信：12銘柄
    # --------------------------------------------------------
    ("ジャパンエンジンコーポレーション", "6016"),
    ("東京計器", "7721"),
    ("日本アビオニクス", "6946"),
    ("石川製作所", "6208"),
    ("豊和工業", "6203"),
    ("細谷火工", "4274"),
    ("多摩川ホールディングス", "6838"),
    ("新明和工業", "7224"),
    ("QDレーザ", "6613"),
    ("santec Holdings", "6777"),
    ("アンリツ", "6754"),
    ("小野測器", "6858"),

    # --------------------------------------------------------
    # ゲーム：24銘柄
    # --------------------------------------------------------
    ("KLab", "3656"),
    ("enish", "3667"),
    ("オルトプラス", "3672"),
    ("アエリア", "3758"),
    ("ケイブ", "3760"),
    ("ドリコム", "3793"),
    ("サイバーステップ", "3810"),
    ("日本一ソフトウェア", "3851"),
    ("gumi", "3903"),
    ("Aiming", "3911"),
    ("モバイルファクトリー", "3912"),
    ("マイネット", "3928"),
    ("アカツキグループ", "3932"),
    ("エディア", "3935"),
    ("アピリッツ", "4174"),
    ("coly", "4175"),
    ("ワンダープラネット", "4199"),
    ("バンク・オブ・イノベーション", "4393"),
    ("イマジニア", "4644"),
    ("東京通信グループ", "7359"),
    ("マーベラス", "7844"),
    ("ブシロード", "7803"),
    ("IGポート", "3791"),
    ("日本ファルコム", "3723"),
]


ALL_BOARDS = [
    {
        "name": name,
        "code": code,
        "url": f"https://finance.yahoo.co.jp/quote/{code}.T/forum",
    }
    for name, code in BOARD_ITEMS
]


# 交互に振り分けて、46銘柄と45銘柄に分割
BOARD_GROUPS = [
    ALL_BOARDS[0::2],
    ALL_BOARDS[1::2],
]


def log(group_number, message):
    print(
        f"[G{group_number}] {message}",
        flush=True,
    )


def validate_boards():
    codes = [
        board["code"]
        for board in ALL_BOARDS
    ]

    duplicate_codes = sorted(
        {
            code
            for code in codes
            if codes.count(code) > 1
        }
    )

    if duplicate_codes:
        raise RuntimeError(
            "監視コードが重複しています: "
            + ", ".join(duplicate_codes)
        )

    grouped_count = sum(
        len(group)
        for group in BOARD_GROUPS
    )

    if grouped_count != len(ALL_BOARDS):
        raise RuntimeError(
            "グループ分割後の銘柄数が一致しません"
        )


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}

    try:
        with open(
            STATE_PATH,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if isinstance(data, dict):
            return data

        return {}

    except Exception as error:
        print(
            f"state load error: {error}",
            flush=True,
        )
        return {}


def save_state(state):
    state_dir = os.path.dirname(
        STATE_PATH
    )

    if state_dir:
        os.makedirs(
            state_dir,
            exist_ok=True,
        )

    temporary_path = STATE_PATH + ".tmp"

    with open(
        temporary_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        temporary_path,
        STATE_PATH,
    )


def is_market_open(now):
    # 土日は市場時間外。祝日は未対応。
    if now.weekday() >= 5:
        return False

    minutes = (
        now.hour * 60
        + now.minute
    )

    morning_open = 9 * 60
    morning_close = 11 * 60 + 30

    afternoon_open = 12 * 60 + 30
    afternoon_close = 15 * 60 + 30

    return (
        morning_open
        <= minutes
        < morning_close
        or afternoon_open
        <= minutes
        < afternoon_close
    )


def fetch_html(
    session,
    url,
    group_number,
):
    last_error = None

    for attempt in range(3):
        try:
            response = session.get(
                url,
                timeout=20,
            )

            log(
                group_number,
                (
                    f"fetch attempt={attempt + 1} "
                    f"status={response.status_code}"
                ),
            )

            if (
                response.status_code == 200
                and response.text
            ):
                return response.text

            last_error = (
                f"status={response.status_code}"
            )

        except Exception as error:
            last_error = str(error)

            log(
                group_number,
                (
                    f"fetch error attempt={attempt + 1}: "
                    f"{error}"
                ),
            )

        if attempt < 2:
            time.sleep(
                3 + attempt * 3
            )

    raise RuntimeError(
        f"fetch failed: {last_error}"
    )


def html_to_text(html):
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

    return re.sub(
        r"\n+",
        "\n",
        text,
    )


def extract_post_dates(
    html,
    now,
):
    text = html_to_text(html)

    patterns = [
        (
            r"(\d{4})/(\d{1,2})/(\d{1,2})"
            r"\s+([0-2]?\d):([0-5]\d)"
        ),
        (
            r"(\d{4})-(\d{1,2})-(\d{1,2})"
            r"\s+([0-2]?\d):([0-5]\d)"
        ),
        (
            r"(\d{4})年(\d{1,2})月(\d{1,2})日"
            r"\s+([0-2]?\d):([0-5]\d)"
        ),
    ]

    results = []

    for pattern in patterns:
        matches = list(
            re.finditer(
                pattern,
                text,
            )
        )

        if not matches:
            continue

        for match in matches:
            try:
                (
                    year,
                    month,
                    day,
                    hour,
                    minute,
                ) = map(
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

            except (
                ValueError,
                TypeError,
            ):
                continue

            # 現在より1分を超えて未来の日時は除外
            if (
                post_datetime
                > now + timedelta(minutes=1)
            ):
                continue

            results.append(
                post_datetime
            )

        # 最初に見つかった日付形式だけを使用
        if results:
            break

    results.sort(
        reverse=True
    )

    return results


def judge_spike(
    post_dates,
    now,
):
    """
    銘柄ごとに以下を計算する。

    直近5分の投稿数
    －
    その前の5分の投稿数

    差が5件以上なら通知対象。
    """
    last5 = 0
    prev5 = 0

    for post_datetime in post_dates:
        diff_minutes = (
            now - post_datetime
        ).total_seconds() / 60

        if (
            0
            <= diff_minutes
            < 5
        ):
            last5 += 1

        elif (
            5
            <= diff_minutes
            < 10
        ):
            prev5 += 1

        elif diff_minutes >= 10:
            break

    surge = (
        last5 - prev5
    )

    should_alert = (
        surge >= SURGE_THRESHOLD
    )

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
    group_number,
):
    if not IFTTT_KEY:
        log(
            group_number,
            (
                "IFTTT_KEY not set. "
                "GitHub ActionsのSecretとenv設定を確認してください。"
            ),
        )
        return False

    if not IFTTT_EVENT:
        log(
            group_number,
            "IFTTT_EVENT is empty.",
        )
        return False

    mode = (
        "市場時間内"
        if market_open
        else "市場時間外"
    )

    encoded_event = quote(
        IFTTT_EVENT.strip(),
        safe="",
    )

    encoded_key = quote(
        IFTTT_KEY.strip(),
        safe="",
    )

    webhook_url = (
        "https://maker.ifttt.com/trigger/"
        f"{encoded_event}"
        "/with/key/"
        f"{encoded_key}"
    )

    payload = {
        "value1": f"{name} 掲示板急増",
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
            json=payload,
            timeout=20,
        )

        log(
            group_number,
            (
                f"IFTTT status={response.status_code} "
                f"body={response.text[:120]}"
            ),
        )

        if (
            200
            <= response.status_code
            < 300
        ):
            return True

        log(
            group_number,
            (
                "IFTTT notification failed. "
                "Webhookキー、イベント名、"
                "IFTTTアプレットの有効状態を確認してください。"
            ),
        )

        return False

    except Exception as error:
        log(
            group_number,
            f"IFTTT error: {error}",
        )
        return False


def process_group(
    group_number,
    boards,
    alerts_snapshot,
):
    checked = 0
    failed = 0
    alerted = 0

    alert_updates = {}

    session = requests.Session()
    session.headers.update(
        HEADERS
    )

    log(
        group_number,
        (
            f"group start boards={len(boards)}"
        ),
    )

    try:
        for index, board in enumerate(
            boards,
            start=1,
        ):
            name = board["name"]
            code = board["code"]
            url = board["url"]

            log(
                group_number,
                (
                    f"{index}/{len(boards)} "
                    f"{name} {code}"
                ),
            )

            try:
                html = fetch_html(
                    session=session,
                    url=url,
                    group_number=group_number,
                )

                # 各銘柄を取得した時点の時刻で判定
                board_now = datetime.now(
                    JST
                )

                board_market_open = (
                    is_market_open(
                        board_now
                    )
                )

                post_dates = extract_post_dates(
                    html=html,
                    now=board_now,
                )

                if not post_dates:
                    log(
                        group_number,
                        "post date not found",
                    )

                    failed += 1
                    continue

                (
                    should_alert,
                    last5,
                    prev5,
                    surge,
                ) = judge_spike(
                    post_dates=post_dates,
                    now=board_now,
                )

                log(
                    group_number,
                    (
                        f"dates={len(post_dates)} "
                        f"last5={last5} "
                        f"prev5={prev5} "
                        f"surge={surge:+d} "
                        f"should_alert={should_alert}"
                    ),
                )

                last_alert_iso = (
                    alerts_snapshot.get(
                        code
                    )
                )

                cooldown_ok = True

                if last_alert_iso:
                    try:
                        last_alert = (
                            datetime.fromisoformat(
                                last_alert_iso
                            )
                        )

                        minutes_since = (
                            board_now
                            - last_alert
                        ).total_seconds() / 60

                        if (
                            minutes_since
                            < ALERT_COOLDOWN_MINUTES
                        ):
                            cooldown_ok = False

                            log(
                                group_number,
                                (
                                    "cooldown skip "
                                    f"minutes_since="
                                    f"{minutes_since:.1f}"
                                ),
                            )

                    except Exception as error:
                        log(
                            group_number,
                            (
                                "cooldown state error: "
                                f"{error}"
                            ),
                        )

                if (
                    should_alert
                    and cooldown_ok
                ):
                    notification_sent = send_ifttt(
                        name=name,
                        code=code,
                        url=url,
                        last5=last5,
                        prev5=prev5,
                        surge=surge,
                        market_open=board_market_open,
                        group_number=group_number,
                    )

                    # IFTTT送信成功時だけクールダウン開始
                    if notification_sent:
                        alert_updates[code] = (
                            board_now.isoformat()
                        )

                        alerted += 1

                checked += 1

            except Exception as error:
                log(
                    group_number,
                    f"error: {error}",
                )

                failed += 1

            if index < len(boards):
                time.sleep(
                    random.uniform(
                        MIN_SLEEP_SEC,
                        MAX_SLEEP_SEC,
                    )
                )

    finally:
        session.close()

    log(
        group_number,
        (
            f"group finished "
            f"checked={checked} "
            f"failed={failed} "
            f"alerted={alerted}"
        ),
    )

    return {
        "group": group_number,
        "boards": len(boards),
        "checked": checked,
        "failed": failed,
        "alerted": alerted,
        "alert_updates": alert_updates,
    }


def main():
    validate_boards()

    started_at = datetime.now(
        JST
    )

    print(
        (
            f"start now={started_at.isoformat()} "
            f"total_boards={len(ALL_BOARDS)} "
            f"group1={len(BOARD_GROUPS[0])} "
            f"group2={len(BOARD_GROUPS[1])} "
            f"surge_threshold={SURGE_THRESHOLD}"
        ),
        flush=True,
    )

    # キー自体は表示せず、設定状態だけ表示
    print(
        (
            f"ifttt_key_configured={bool(IFTTT_KEY)} "
            f"ifttt_event={IFTTT_EVENT}"
        ),
        flush=True,
    )

    state = load_state()

    alerts = state.setdefault(
        "alerts",
        {},
    )

    alerts_snapshot = dict(
        alerts
    )

    group_results = []

    with ThreadPoolExecutor(
        max_workers=GROUP_COUNT
    ) as executor:
        futures = [
            executor.submit(
                process_group,
                group_number,
                boards,
                alerts_snapshot,
            )
            for group_number, boards
            in enumerate(
                BOARD_GROUPS,
                start=1,
            )
        ]

        for future in as_completed(
            futures
        ):
            try:
                group_results.append(
                    future.result()
                )

            except Exception as error:
                print(
                    (
                        "group execution error: "
                        f"{error}"
                    ),
                    flush=True,
                )

    total_checked = 0
    total_failed = 0
    total_alerted = 0

    for result in group_results:
        total_checked += result[
            "checked"
        ]

        total_failed += result[
            "failed"
        ]

        total_alerted += result[
            "alerted"
        ]

        alerts.update(
            result["alert_updates"]
        )

    finished_at = datetime.now(
        JST
    )

    elapsed_seconds = (
        finished_at - started_at
    ).total_seconds()

    state["last_run"] = (
        finished_at.isoformat()
    )

    state["last_result"] = {
        "total_boards": len(
            ALL_BOARDS
        ),
        "checked": total_checked,
        "failed": total_failed,
        "alerted": total_alerted,
        "surge_threshold": SURGE_THRESHOLD,
        "elapsed_seconds": round(
            elapsed_seconds,
            1,
        ),
        "ifttt_key_configured": bool(
            IFTTT_KEY
        ),
        "ifttt_event": IFTTT_EVENT,
        "groups": [
            {
                "group": result["group"],
                "boards": result["boards"],
                "checked": result["checked"],
                "failed": result["failed"],
                "alerted": result["alerted"],
            }
            for result in sorted(
                group_results,
                key=lambda item: item[
                    "group"
                ],
            )
        ],
    }

    save_state(
        state
    )

    print(
        "\nfinished",
        flush=True,
    )

    print(
        (
            f"checked={total_checked} "
            f"failed={total_failed} "
            f"alerted={total_alerted} "
            f"elapsed_seconds={elapsed_seconds:.1f}"
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
