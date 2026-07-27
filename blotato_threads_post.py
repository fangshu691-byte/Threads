"""
Blotato経由 Threads自動投稿スクリプト
(本文投稿 + 公式LINEリンク付きリプライを同じスレッドにまとめて投稿)

【重要】前バージョンからの変更点:
これまでは実行するたびに「今」を起点に1時間おきの時刻を計算し直す
一回きりのスクリプトだったため、
- 1回実行してキュー内の投稿を予約したらそこで終わり
- 続きを予約する仕組みが無い
- 再実行しても前回の予約状況を無視して「今」からやり直す
という設計上の欠陥がありました。これが「昨日の手動投稿から
自動投稿が動いていない」の原因です(エラーで落ちていたのではなく、
そもそも1回分しか予約しない作りだった、ということです)。

今回から state.json に「次の予約時刻」と「予約済みID」を保存し、
- 何度実行しても続きから予約される(重複しない)
- content_backlog.json に新しい投稿を追記していけば、そのまま続きが積まれる
という形にしています。

運用方法:
1. content_backlog.json (同じディレクトリ) に投稿したい内容を追記していく
2. このスクリプトを cron などで「1日1回」実行する
   (1時間おきに実行する必要はありません。実行するたびに
    未予約の投稿をBlotatoのscheduledTimeで積んでいくだけです)
3. Blotato側が指定時刻に実際の投稿を行います

前提:
- Blotato アカウントで Threads を連携済み
- 環境変数 BLOTATO_API_KEY にAPIキーを設定

参考:
- Base URL: https://backend.blotato.com/v2
- 認証ヘッダー: blotato-api-key
- Threads は Twitter/Bluesky と同じ仕組みで、
  content.additionalPosts[] に入れた投稿が自動でリプライスレッドとして
  1回のAPI呼び出しで連結される(reply_to_id を自分で管理する必要はない)

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
CONTENT_BACKLOG_FILE = Path(__file__).with_name("content_backlog.json")

# 公式LINEのリンク(実際のURLに差し替えてください)
LINE_URL = "https://line.me/R/ti/p/@your_official_line_id"


def load_content_backlog():
    """content_backlog.json から投稿バックログを読み込む。
    ここに項目を追記していけば、そのまま続きが1時間おきで積まれる。"""
    with open(CONTENT_BACKLOG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"next_slot": None, "posted_ids": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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


def build_payload(account_id, main_text, reply_text, scheduled_time):
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
    return {"post": post, "scheduledTime": scheduled_time}


def submit_post(payload):
    resp = requests.post(f"{BASE_URL}/posts", headers=HEADERS, json=payload)
    if not resp.ok:
        print(f"Blotato APIエラー本文: {resp.text}")
    resp.raise_for_status()
    return resp.json()["postSubmissionId"]


def schedule_new_backlog_items(backlog=None, account_id=None, interval_hours=1):
    """
    まだ予約していない投稿だけを、前回の続きの時刻から1時間おきで予約する。
    何度実行しても安全(posted_ids で重複防止)。
    """
    if backlog is None:
        backlog = load_content_backlog()
    if account_id is None:
        account_id = get_threads_account_id()

    state = load_state()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # 「今すぐ」扱いにすると、リクエスト到達時には過去の時刻になり
    # バリデーションで弾かれることがあるため、最低2分先を下限にする
    floor_time = now + timedelta(minutes=2)

    if state["next_slot"] is None:
        next_slot = floor_time
    else:
        next_slot = datetime.fromisoformat(state["next_slot"].replace("Z", "+00:00")).replace(microsecond=0)
        if next_slot < floor_time:
            # 積み残し・実行漏れがあった場合は「今」から仕切り直す
            next_slot = floor_time

    new_items = [item for item in backlog if item["id"] not in state["posted_ids"]]

    if not new_items:
        print("新しく予約する投稿はありません(バックログは全て予約済みです)。")
        return []

    results = []
    for item in new_items:
        scheduled_time_str = next_slot.isoformat().replace("+00:00", "Z")
        payload = build_payload(account_id, item["main_text"], item["reply_text"], scheduled_time_str)
        submission_id = submit_post(payload)

        print(f"予約完了: {item['label']} / postSubmissionId={submission_id} / 予定時刻={scheduled_time_str}")

        state["posted_ids"].append(item["id"])
        next_slot = next_slot + timedelta(hours=interval_hours)
        state["next_slot"] = next_slot.isoformat().replace("+00:00", "Z")
        save_state(state)  # 1件ごとに保存。途中で落ちても続きから再開できる

        results.append({
            "label": item["label"],
            "postSubmissionId": submission_id,
            "scheduledTime": scheduled_time_str,
        })

    return results


if __name__ == "__main__":
    schedule_new_backlog_items(interval_hours=1)
