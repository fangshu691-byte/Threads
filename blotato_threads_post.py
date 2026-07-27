"""
Blotato経由 Threads自動投稿スクリプト
(本文投稿 + 公式LINEリンク付きリプライを同じスレッドにまとめて投稿)

【運用ルール】
- 7:00〜19:00の間、1時間に1回投稿(GitHub Actionsのcronで13回/日トリガー)
- content_backlog.json (91本 = 13本/日 × 7日) を1週間サイクルとして周回する
- 投稿するたびに post_log.jsonl に「どのテーマ・どのテンプレートを投稿したか」を
  記録する(後で analyze_performance.py と組み合わせて、
  どの切り口がバズったか集計できるようにするため)

【設計】
このスクリプトは「呼ばれたら、次の1件を今すぐ投稿してカーソルを進めるだけ」
というシンプルな作りです。時間の間隔や「7:00〜19:00」という制約は
すべて呼び出し側(GitHub Actionsのcron)が管理します。

state.json には次に投稿すべき content_backlog.json のインデックス(cursor)を
保存します。cursor は len(backlog) で割った余りを使うので、
最後の投稿が終わると自動的に先頭に戻り、1週間サイクルが繰り返されます。

前提:
- Blotato アカウントで Threads を連携済み
- 環境変数 BLOTATO_API_KEY にAPIキーを設定
- content_backlog.json がこのファイルと同じディレクトリにあること

使い方:
    python blotato_threads_post.py
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://backend.blotato.com/v2"
API_KEY = os.environ["BLOTATO_API_KEY"]
HEADERS = {
    "Content-Type": "application/json",
    "blotato-api-key": API_KEY,
}

STATE_FILE = Path(__file__).with_name("blotato_post_state.json")
BACKLOG_FILE = Path(__file__).with_name("content_backlog.json")
POST_LOG_FILE = Path(__file__).with_name("post_log.jsonl")


def load_backlog():
    with open(BACKLOG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"cursor": 0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def append_post_log(record):
    """投稿1件ごとに1行のJSONを追記する(後で分析スクリプトが読む)"""
    with open(POST_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def get_threads_account_id():
    """連携済みアカウント一覧からThreadsのaccountIdを取得"""
    resp = requests.get(
        f"{BASE_URL}/users/me/accounts",
        headers=HEADERS,
        params={"platform": "threads"},
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    for acc in items:
        if acc.get("platform", "").lower() == "threads":
            return acc["id"]
    raise RuntimeError("Threadsアカウントが見つかりません。Blotatoでの連携状況を確認してください。")


def build_payload(account_id, main_text, reply_text, scheduled_time=None):
    post = {
        "accountId": account_id,
        "content": {
            "text": main_text,
            "mediaUrls": [],
            "platform": "threads",
            "additionalPosts": [
                {"text": reply_text, "mediaUrls": []}
            ],
        },
        "target": {"targetType": "threads"},
    }
    payload = {"post": post}
    if scheduled_time:
        payload["scheduledTime"] = scheduled_time
    return payload


def submit_post(payload):
    resp = requests.post(f"{BASE_URL}/posts", headers=HEADERS, json=payload)
    if not resp.ok:
        print(f"Blotato APIエラー本文: {resp.text}")
    resp.raise_for_status()
    return resp.json()["postSubmissionId"]


def post_next_in_cycle(account_id=None, buffer_minutes=2):
    """
    content_backlog.json の「次の1件」を投稿してカーソルを進める。
    最後まで行ったら自動的に先頭に戻る(=1週間サイクル)。
    投稿するたびに post_log.jsonl に記録を残す。
    """
    backlog = load_backlog()
    if not backlog:
        raise RuntimeError("content_backlog.json が空です。投稿内容を追加してください。")

    if account_id is None:
        account_id = get_threads_account_id()

    state = load_state()
    cursor = state.get("cursor", 0) % len(backlog)
    item = backlog[cursor]

    # 即時投稿だと到達時に過去時刻扱いで弾かれることがあるため、少し先の時刻を指定
    scheduled_time = (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=buffer_minutes)
    ).isoformat().replace("+00:00", "Z")

    payload = build_payload(account_id, item["main_text"], item["reply_text"], scheduled_time)
    submission_id = submit_post(payload)

    print(f"投稿完了: [{cursor+1}/{len(backlog)}] {item['label']} / postSubmissionId={submission_id}")

    state["cursor"] = (cursor + 1) % len(backlog)
    save_state(state)

    append_post_log({
        "posted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scheduled_time": scheduled_time,
        "cycle_position": cursor,
        "id": item["id"],
        "label": item["label"],
        "theme": item.get("theme"),
        "template": item.get("template"),
        "day": item.get("day"),
        "slot": item.get("slot"),
        "postSubmissionId": submission_id,
    })

    return {
        "label": item["label"],
        "postSubmissionId": submission_id,
        "scheduledTime": scheduled_time,
        "cursor_before": cursor,
    }


if __name__ == "__main__":
    post_next_in_cycle()
