import csv
import json
import math
import os
import random
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


JST = ZoneInfo("Asia/Tokyo")

BOARD_CSV_PATH = "boards.csv"
STATE_PATH = "data/state.json"


# ============================================================
# IFTTT設定
# ============================================================

def normalize_ifttt_key(raw_value):
    """
    キーだけでなく、Webhook URL全体がSecretに入っていても対応する。
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
    イベント名だけでなく、Webhook URL全体が渡されても対応する。
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
# 動的通知基準
# ============================================================

# どの銘柄でも最低+5件以上
BASE_SURGE_THRESHOLD = 5

# この回数の履歴がたまるまでは固定基準
MIN_HISTORY_SAMPLES = 24

# 銘柄・時間帯ごとに保存する履歴数
HISTORY_LIMIT = 96

# 過去95パーセンタイルを使用
DYNAMIC_PERCENTILE = 0.95

# 過去95パーセンタイルより、さらに2件多い水準
PERCENTILE_MARGIN = 2

# 中央値＋MADによる異常判定倍率
MAD_MULTIPLIER = 3.0

# 自動通知基準の上限
MAX_DYNAMIC_THRESHOLD = 40

# 学習履歴に保存する1回分の上限
HISTORY_SAMPLE_CAP = 30

# 通知基準の半分以下まで落ち着いたら再通知可能
REARM_RATIO = 0.5

# 同じ銘柄の最低通知間隔
ALERT_COOLDOWN_MINUTES = 15

# 銘柄ごとの待機時間
MIN_SLEEP_SEC = 1.5
MAX_SLEEP_SEC = 3.5

# 同時に監視するグループ数
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
# 銘柄設定CSVの読み込み
# ============================================================

def parse_enabled(raw_value):
    """
    CSVのenabled列をboolへ変換する。
    """
    value = (raw_value or "true").strip().lower()

    true_values = {
        "",
        "true",
        "1",
        "yes",
        "on",
        "有効",
    }

    false_values = {
        "false",
        "0",
        "no",
        "off",
        "無効",
    }

    if value in true_values:
        return True

    if value in false_values:
        return False

    raise ValueError(
        f"enabledの値が不正です: {raw_value}"
    )


def parse_manual_threshold(raw_value):
    """
    manual_threshold列を整数へ変換する。
    空欄ならNone。
    """
    value = (raw_value or "").strip()

    if not value:
        return None

    threshold = int(value)

    if threshold < 1:
        raise ValueError(
            "manual_thresholdは1以上にしてください"
        )

    return threshold


def load_boards():
    """
    boards.csvから監視銘柄を読み込む。

    enabled=falseの銘柄は読み込まない。
    """
    if not os.path.exists(BOARD_CSV_PATH):
        raise FileNotFoundError(
            f"{BOARD_CSV_PATH}が見つかりません"
        )

    boards = []

    with open(
        BOARD_CSV_PATH,
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        reader = csv.DictReader(file)

        required_columns = {
            "name",
            "code",
        }

        actual_columns = set(
            reader.fieldnames or []
        )

        missing_columns = (
            required_columns - actual_columns
        )

        if missing_columns:
            raise RuntimeError(
                "boards.csvに必要な列がありません: "
                + ", ".join(sorted(missing_columns))
            )

        for line_number, row in enumerate(
            reader,
            start=2,
        ):
            name = (
                row.get("name")
                or ""
            ).strip()

            code = (
                row.get("code")
                or ""
            ).strip().upper()

            category = (
                row.get("category")
                or "未分類"
            ).strip()

            # 空行を無視
            if not name and not code:
                continue

            # nameまたはcodeが#から始まる行はコメント扱い
            if (
                name.startswith("#")
                or code.startswith("#")
            ):
                continue

            if not name:
                raise ValueError(
                    f"boards.csv {line_number}行目: "
                    "nameが空です"
                )

            if not code:
                raise ValueError(
                    f"boards.csv {line_number}行目: "
                    "codeが空です"
                )

            try:
                enabled = parse_enabled(
                    row.get("enabled")
                )

                manual_threshold = (
                    parse_manual_threshold(
                        row.get(
                            "manual_threshold"
                        )
                    )
                )

            except Exception as error:
                raise ValueError(
                    f"boards.csv {line_number}行目: "
                    f"{error}"
                ) from error

            if not enabled:
                continue

            boards.append(
                {
                    "name": name,
                    "code": code,
                    "category": category,
                    "manual_threshold": manual_threshold,
                    "url": (
                        "https://finance.yahoo.co.jp/"
                        f"quote/{code}.T/forum"
                    ),
                }
            )

    if not boards:
        raise RuntimeError(
            "有効な監視銘柄が1件もありません"
        )

    return boards


def validate_boards(boards):
    """
    証券コードの重複を確認する。
    """
    seen_codes = set()
    duplicate_codes = set()

    for board in boards:
        code = board["code"]

        if code in seen_codes:
            duplicate_codes.add(code)

        seen_codes.add(code)

    if duplicate_codes:
        raise RuntimeError(
            "監視コードが重複しています: "
            + ", ".join(
                sorted(duplicate_codes)
            )
        )


def split_boards(boards, group_count):
    """
    CSV順を維持しながら、複数グループへ交互に振り分ける。
    """
    actual_group_count = min(
        max(1, group_count),
        len(boards),
    )

    groups = [
        []
        for _ in range(actual_group_count)
    ]

    for index, board in enumerate(boards):
        group_index = (
            index % actual_group_count
        )

        groups[group_index].append(
            board
        )

    return groups


# ============================================================
# 状態ファイル
# ============================================================

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

    temporary_path = (
        STATE_PATH + ".tmp"
    )

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


# ============================================================
# 時間帯判定
# ============================================================

def is_market_open(now):
    """
    東証の取引時間内か判定する。
    祝日は未対応。
    """
    if now.weekday() >= 5:
        return False

    minutes = (
        now.hour * 60
        + now.minute
    )

    return (
        9 * 60
        <= minutes
        < 11 * 60 + 30
        or
        12 * 60 + 30
        <= minutes
        < 15 * 60 + 30
    )


def activity_mode(now):
    """
    投稿量が異なる時間帯ごとに履歴を分ける。
    """
    if now.weekday() >= 5:
        return "off"

    minutes = (
        now.hour * 60
        + now.minute
    )

    if (
        7 * 60 + 30
        <= minutes
        < 9 * 60
    ):
        return "preopen"

    if (
        9 * 60
        <= minutes
        < 11 * 60 + 30
        or
        12 * 60 + 30
        <= minutes
        < 15 * 60 + 30
    ):
        return "market"

    if (
        11 * 60 + 30
        <= minutes
        < 12 * 60 + 30
    ):
        return "lunch"

    return "off"


# ============================================================
# 動的基準
# ============================================================

def percentile(values, q):
    if not values:
        return 0.0

    sorted_values = sorted(values)

    if len(sorted_values) == 1:
        return float(
            sorted_values[0]
        )

    position = (
        len(sorted_values) - 1
    ) * q

    lower_index = math.floor(position)
    upper_index = math.ceil(position)

    lower_value = sorted_values[
        lower_index
    ]

    upper_value = sorted_values[
        upper_index
    ]

    if lower_index == upper_index:
        return float(lower_value)

    fraction = (
        position - lower_index
    )

    return (
        lower_value
        + (
            upper_value
            - lower_value
        )
        * fraction
    )


def calculate_dynamic_threshold(
    board,
    history,
):
    """
    銘柄ごとのCSV設定と過去履歴から通知基準を決める。
    """
    manual_threshold = (
        board.get("manual_threshold")
    )

    minimum_threshold = (
        BASE_SURGE_THRESHOLD
    )

    if manual_threshold is not None:
        minimum_threshold = max(
            minimum_threshold,
            manual_threshold,
        )

    if len(history) < MIN_HISTORY_SAMPLES:
        return (
            minimum_threshold,
            {
                "samples": len(history),
                "p95": None,
                "median": None,
                "mad": None,
                "learning": True,
            },
        )

    median_value = statistics.median(
        history
    )

    absolute_deviations = [
        abs(value - median_value)
        for value in history
    ]

    mad_value = statistics.median(
        absolute_deviations
    )

    p95_value = percentile(
        history,
        DYNAMIC_PERCENTILE,
    )

    robust_sigma = (
        1.4826 * mad_value
    )

    percentile_threshold = (
        math.ceil(p95_value)
        + PERCENTILE_MARGIN
    )

    mad_threshold = math.ceil(
        median_value
        + MAD_MULTIPLIER
        * robust_sigma
    )

    threshold = max(
        minimum_threshold,
        percentile_threshold,
        mad_threshold,
    )

    threshold = min(
        threshold,
        MAX_DYNAMIC_THRESHOLD,
    )

    return (
        threshold,
        {
            "samples": len(history),
            "p95": round(p95_value, 1),
            "median": round(
                float(median_value),
                1,
            ),
            "mad": round(
                float(mad_value),
                1,
            ),
            "learning": False,
        },
    )


def append_surge_history(
    history,
    surge,
):
    sample = max(
        0,
        int(surge),
    )

    sample = min(
        sample,
        HISTORY_SAMPLE_CAP,
    )

    updated = list(history)
    updated.append(sample)

    return updated[-HISTORY_LIMIT:]


# ============================================================
# Yahoo掲示板取得
# ============================================================

def log(group_number, message):
    print(
        f"[G{group_number}] {message}",
        flush=True,
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
                    f"fetch error "
                    f"attempt={attempt + 1}: "
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

            if (
                post_datetime
                > now + timedelta(minutes=1)
            ):
                continue

            results.append(
                post_datetime
            )

        if results:
            break

    results.sort(
        reverse=True
    )

    return results


def judge_spike(
    post_dates,
    now,
    threshold,
):
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

        elif diff_minutes >= 10:
            break

    surge = (
        last5 - prev5
    )

    should_alert = (
        surge >= threshold
    )

    return (
        should_alert,
        last5,
        prev5,
        surge,
    )


# ============================================================
# IFTTT通知
# ============================================================

def send_ifttt(
    board,
    last5,
    prev5,
    surge,
    threshold,
    market_open,
    group_number,
):
    if not IFTTT_KEY:
        log(
            group_number,
            "IFTTT_KEY not set.",
        )
        return False

    mode = (
        "市場時間内"
        if market_open
        else "市場時間外"
    )

    event = quote(
        IFTTT_EVENT.strip(),
        safe="",
    )

    key = quote(
        IFTTT_KEY.strip(),
        safe="",
    )

    webhook_url = (
        "https://maker.ifttt.com/"
        f"trigger/{event}/with/key/{key}"
    )

    payload = {
        "value1": (
            f"{board['name']} 掲示板急増"
        ),
        "value2": (
            f"{mode} / "
            f"{board['code']} / "
            f"直近5分:{last5}件"
        ),
        "value3": (
            f"前5分:{prev5}件 / "
            f"差:{surge:+d}件 / "
            f"基準:{threshold}件 / "
            f"{board['url']}"
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
                f"IFTTT status="
                f"{response.status_code} "
                f"body={response.text[:120]}"
            ),
        )

        return (
            200
            <= response.status_code
            < 300
        )

    except Exception as error:
        log(
            group_number,
            f"IFTTT error: {error}",
        )
        return False


# ============================================================
# グループ監視
# ============================================================

def process_group(
    group_number,
    boards,
    alerts_snapshot,
    history_snapshot,
    active_snapshot,
):
    checked = 0
    failed = 0
    alerted = 0

    alert_updates = {}
    history_updates = {}
    active_updates = {}

    session = requests.Session()
    session.headers.update(HEADERS)

    log(
        group_number,
        f"group start boards={len(boards)}",
    )

    try:
        for index, board in enumerate(
            boards,
            start=1,
        ):
            name = board["name"]
            code = board["code"]

            log(
                group_number,
                (
                    f"{index}/{len(boards)} "
                    f"{name} {code} "
                    f"category={board['category']}"
                ),
            )

            try:
                html = fetch_html(
                    session=session,
                    url=board["url"],
                    group_number=group_number,
                )

                board_now = datetime.now(JST)

                market_open = is_market_open(
                    board_now
                )

                mode = activity_mode(
                    board_now
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

                stock_history = list(
                    history_snapshot
                    .get(code, {})
                    .get(mode, [])
                )

                (
                    threshold,
                    threshold_info,
                ) = calculate_dynamic_threshold(
                    board=board,
                    history=stock_history,
                )

                (
                    should_alert,
                    last5,
                    prev5,
                    surge,
                ) = judge_spike(
                    post_dates=post_dates,
                    now=board_now,
                    threshold=threshold,
                )

                log(
                    group_number,
                    (
                        f"mode={mode} "
                        f"last5={last5} "
                        f"prev5={prev5} "
                        f"surge={surge:+d} "
                        f"threshold={threshold} "
                        f"manual="
                        f"{board['manual_threshold']} "
                        f"samples="
                        f"{threshold_info['samples']} "
                        f"p95={threshold_info['p95']} "
                        f"median="
                        f"{threshold_info['median']} "
                        f"mad={threshold_info['mad']} "
                        f"learning="
                        f"{threshold_info['learning']} "
                        f"should_alert={should_alert}"
                    ),
                )

                cooldown_ok = True

                last_alert_iso = (
                    alerts_snapshot.get(code)
                )

                if last_alert_iso:
                    try:
                        last_alert = (
                            datetime.fromisoformat(
                                last_alert_iso
                            )
                        )

                        minutes_since = (
                            board_now - last_alert
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

                alert_active = bool(
                    active_snapshot.get(
                        code,
                        False,
                    )
                )

                rearm_level = max(
                    1,
                    math.floor(
                        threshold * REARM_RATIO
                    ),
                )

                if (
                    alert_active
                    and surge <= rearm_level
                ):
                    alert_active = False

                    log(
                        group_number,
                        (
                            "alert rearmed "
                            f"surge={surge:+d} "
                            f"rearm_level={rearm_level}"
                        ),
                    )

                if (
                    should_alert
                    and cooldown_ok
                    and not alert_active
                ):
                    sent = send_ifttt(
                        board=board,
                        last5=last5,
                        prev5=prev5,
                        surge=surge,
                        threshold=threshold,
                        market_open=market_open,
                        group_number=group_number,
                    )

                    if sent:
                        alert_updates[code] = (
                            board_now.isoformat()
                        )

                        alert_active = True
                        alerted += 1

                elif (
                    should_alert
                    and alert_active
                ):
                    log(
                        group_number,
                        (
                            "same surge continues. "
                            "duplicate alert suppressed."
                        ),
                    )

                updated_history = (
                    append_surge_history(
                        history=stock_history,
                        surge=surge,
                    )
                )

                history_updates.setdefault(
                    code,
                    {},
                )[mode] = updated_history

                active_updates[code] = (
                    alert_active
                )

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
        "history_updates": history_updates,
        "active_updates": active_updates,
    }


# ============================================================
# メイン処理
# ============================================================

def main():
    all_boards = load_boards()

    validate_boards(
        all_boards
    )

    board_groups = split_boards(
        boards=all_boards,
        group_count=GROUP_COUNT,
    )

    started_at = datetime.now(JST)

    group_sizes = [
        len(group)
        for group in board_groups
    ]

    print(
        (
            f"start now={started_at.isoformat()} "
            f"total_boards={len(all_boards)} "
            f"group_sizes={group_sizes} "
            f"base_threshold="
            f"{BASE_SURGE_THRESHOLD}"
        ),
        flush=True,
    )

    print(
        (
            f"ifttt_key_configured="
            f"{bool(IFTTT_KEY)} "
            f"ifttt_event={IFTTT_EVENT}"
        ),
        flush=True,
    )

    state = load_state()

    alerts = state.setdefault(
        "alerts",
        {},
    )

    surge_history = state.setdefault(
        "surge_history",
        {},
    )

    alert_active = state.setdefault(
        "alert_active",
        {},
    )

    alerts_snapshot = dict(alerts)

    history_snapshot = {
        code: {
            mode: list(values)
            for mode, values
            in mode_histories.items()
        }
        for code, mode_histories
        in surge_history.items()
    }

    active_snapshot = dict(
        alert_active
    )

    group_results = []

    with ThreadPoolExecutor(
        max_workers=len(board_groups)
    ) as executor:
        futures = [
            executor.submit(
                process_group,
                group_number,
                boards,
                alerts_snapshot,
                history_snapshot,
                active_snapshot,
            )
            for group_number, boards
            in enumerate(
                board_groups,
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

        alert_active.update(
            result["active_updates"]
        )

        for (
            code,
            mode_updates,
        ) in result[
            "history_updates"
        ].items():
            stock_history = (
                surge_history.setdefault(
                    code,
                    {},
                )
            )

            stock_history.update(
                mode_updates
            )

    finished_at = datetime.now(JST)

    elapsed_seconds = (
        finished_at - started_at
    ).total_seconds()

    category_counts = {}

    for board in all_boards:
        category = board["category"]

        category_counts[category] = (
            category_counts.get(
                category,
                0,
            )
            + 1
        )

    state["last_run"] = (
        finished_at.isoformat()
    )

    state["last_result"] = {
        "total_boards": len(all_boards),
        "category_counts": category_counts,
        "checked": total_checked,
        "failed": total_failed,
        "alerted": total_alerted,
        "base_threshold": (
            BASE_SURGE_THRESHOLD
        ),
        "minimum_history_samples": (
            MIN_HISTORY_SAMPLES
        ),
        "elapsed_seconds": round(
            elapsed_seconds,
            1,
        ),
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

    save_state(state)

    print(
        "\nfinished",
        flush=True,
    )

    print(
        (
            f"checked={total_checked} "
            f"failed={total_failed} "
            f"alerted={total_alerted} "
            f"elapsed_seconds="
            f"{elapsed_seconds:.1f}"
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
