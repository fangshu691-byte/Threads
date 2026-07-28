"""
Threads自動投稿スクリプト(Blotato経由・1本化版)

【このファイルがこのリポジトリで唯一の投稿スクリプトです】
過去に daily-post.yml / blotato-daily-post.yml の2系統が併存し、
リネームが中途半端に反映されて混乱した経緯があるため、
今回はワークフローもスクリプトも1本に統合しています。

【運用ルール】
- 7:00〜19:00 (JST) の間、1時間に1回投稿 (GitHub Actionsのcronで13回/日)
- content_backlog.json (49本 = 7本/日 × 7日) を1週間サイクルで周回
- 投稿するたびに post_log.jsonl に記録(analyze_performance.py が読む)

【これまで踏んだ地雷と、その対策】
1. state未永続化 → state.json をワークフロー側でコミットして永続化
2. アカウント取得APIのレスポンスキーが "accounts" ではなく "items" だった
   → get_threads_account_id() で "items" を参照
3. scheduledTime にマイクロ秒が入っていた/過去時刻扱いになっていた
   → マイクロ秒を切り捨て、+2分のバッファを必ず入れる
4. 429(レート制限)・5xx(サーバー混雑)に対するリトライが無かった
   → _request_with_retry() で指数バックオフ
5. cronで一度も自動発火した実績が確認できていない
   → 実行のたびに GITHUB_EVENT_NAME を出力し、
     scheduleによる自動実行か手動実行かをログで区別できるようにした

前提:
- Blotato アカウントで Threads を連携済み
- 環境変数 BLOTATO_API_KEY にAPIキーを設定
- content_backlog.json がこのファイルと同じディレクトリにあること
"""

import os
import sys
import json
import time
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
    with open(POST_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _request_with_retry(method, url, max_retries=5, **kwargs):
    """429・5xx系を指数バックオフでリトライする"""
    for attempt in range(max_retries):
        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = min(2 ** attempt, 30)
            print(f"[retry] status={resp.status_code} {wait}秒待機してリトライ "
                  f"({attempt+1}/{max_retries})")
            time.sleep(wait)
            continue
        return resp
    raise RuntimeError(f"リトライ上限({max_retries}回)に到達: {url}")


def get_threads_account_id():
    """連携済みアカウント一覧からThreadsのaccountIdを取得(レスポンスキーは items)"""
    resp = _request_with_retry(
        "GET", f"{BASE_URL}/users/me/accounts",
        headers=HEADERS, params={"platform": "threads"},
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    for acc in items:
        if acc.get("platform", "").lower() == "threads":
            return acc["id"]
    raise RuntimeError("Threadsアカウントが見つかりません。Blotatoでの連携状況を確認してください。")


def build_payload(account_id, main_text, reply_text, scheduled_time):
    return {
        "post": {
            "accountId": account_id,
            "content": {
                "text": main_text,
                "mediaUrls": [],
                "platform": "threads",
                "additionalPosts": [{"text": reply_text, "mediaUrls": []}],
            },
            "target": {"targetType": "threads"},
        },
        "scheduledTime": scheduled_time,
    }


def submit_post(payload):
    resp = _request_with_retry("POST", f"{BASE_URL}/posts", headers=HEADERS, json=payload)
    if not resp.ok:
        print(f"[error] Blotato APIエラー本文: {resp.text}")
    resp.raise_for_status()
    return resp.json()["postSubmissionId"]


def post_next_in_cycle(account_id=None, buffer_minutes=2):
    backlog = load_backlog()
    if not backlog:
        raise RuntimeError("content_backlog.json が空です。")

    if account_id is None:
        account_id = get_threads_account_id()

    state = load_state()
    cursor = state.get("cursor", 0) % len(backlog)
    item = backlog[cursor]

    scheduled_time = (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=buffer_minutes)
    ).isoformat().replace("+00:00", "Z")

    payload = build_payload(account_id, item["main_text"], item["reply_text"], scheduled_time)
    submission_id = submit_post(payload)

    trigger = os.environ.get("GITHUB_EVENT_NAME", "local")
    print(f"[trigger={trigger}] 投稿完了: [{cursor+1}/{len(backlog)}] "
          f"{item['label']} / postSubmissionId={submission_id} / 予定={scheduled_time}")

    state["cursor"] = (cursor + 1) % len(backlog)
    save_state(state)

    append_post_log({
        "posted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scheduled_time": scheduled_time,
        "trigger": trigger,
        "cycle_position": cursor,
        "id": item["id"],
        "label": item["label"],
        "theme": item.get("theme"),
        "template": item.get("template"),
        "day": item.get("day"),
        "slot": item.get("slot"),
        "postSubmissionId": submission_id,
    })

    return submission_id


if __name__ == "__main__":
    try:
        post_next_in_cycle()
    except Exception as e:
        print(f"[fatal] {type(e).__name__}: {e}")
        sys.exit(1)
