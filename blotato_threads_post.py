"""
Blotato経由 Threads自動投稿スクリプト
(本文投稿 + 公式LINEリンク付きリプライを同じスレッドにまとめて投稿)

前提:
- Blotato アカウントで Threads を連携済み
- 環境変数 BLOTATO_API_KEY にAPIキーを設定
- accountId は GET /v2/users/me/accounts から取得(初回のみ確認すればOK)

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
import time
import requests
from datetime import datetime, timedelta, timezone

BASE_URL = "https://backend.blotato.com/v2"
API_KEY = os.environ["BLOTATO_API_KEY"]
HEADERS = {
    "Content-Type": "application/json",
    "blotato-api-key": API_KEY,
}

# 公式LINEのリンク(実際のURLに差し替えてください)
LINE_URL = "https://line.me/R/ti/p/@your_official_line_id"

# ---- 投稿キュー:今回追加する3パターン ----------------------------------
POST_QUEUE = [
    {
        "label": "拒絶体験・暴露系",
        "main_text": (
            "「その年齢だとブリーチはキツいです」\n\n"
            "これ、正直に言うと大体そのまま\n"
            "帰っちゃうお客さん多いんだけど\n\n"
            "実はブリーチしなくても\n"
            "白髪ぼかしながら抜け感出す方法あるからね\n\n"
            "知らないだけでみんな損してる"
        ),
        "reply_text": (
            "白髪ぼかしのやり方、公式LINEにまとめてます。\n"
            f"{LINE_URL}"
        ),
    },
    {
        "label": "老け見え不安・気づき系",
        "main_text": (
            "白髪気にしてる人、実は\n"
            "「白髪があること」より\n"
            "「暗く重く見えること」の方が\n"
            "老けて見えてる原因だったりする\n\n"
            "白髪隠す前に、まずそこ直した方が\n"
            "圧倒的に若返る\n\n"
            "美容師やっててマジで思う"
        ),
        "reply_text": (
            "垢抜けさせる具体的なやり方は公式LINEに置いてます。\n"
            f"{LINE_URL}"
        ),
    },
    {
        "label": "郷愁・共感系",
        "main_text": (
            "「髪綺麗だね」って\n"
            "最近言われなくなった気がする人\n\n"
            "それ、白髪のせいじゃなくて\n"
            "ただ手入れの仕方が\n"
            "昔と変わってないだけかも\n\n"
            "3回くらい通ってもらえたら\n"
            "普通に戻せます"
        ),
        "reply_text": (
            "髪の印象を戻す手順は公式LINEにまとめてます。\n"
            f"{LINE_URL}"
        ),
    },
]
# -------------------------------------------------------------------------


def get_threads_account_id():
    """連携済みアカウント一覧からThreadsのaccountIdを取得"""
    resp = requests.get(f"{BASE_URL}/users/me/accounts", headers=HEADERS)
    resp.raise_for_status()
    accounts = resp.json().get("accounts", resp.json())
    for acc in accounts:
        if acc.get("platform", "").lower() == "threads":
            return acc["id"] if "id" in acc else acc.get("accountId")
    raise RuntimeError("Threadsアカウントが見つかりません。Blotatoでの連携状況を確認してください。")


def build_payload(account_id, main_text, reply_text, scheduled_time=None, use_next_free_slot=False):
    post = {
        "accountId": account_id,
        "content": {
            "text": main_text,
            "mediaUrls": [],
            "platform": "threads",
            # additionalPosts に入れるだけで、Blotato側が自動でリプライスレッドとして連結する
            "additionalPosts": [
                {
                    "text": reply_text,
                    "mediaUrls": [],
                }
            ],
        },
        "target": {
            "targetType": "threads",
        },
    }
    payload = {"post": post}
    if scheduled_time:
        payload["scheduledTime"] = scheduled_time
    elif use_next_free_slot:
        payload["useNextFreeSlot"] = True
    return payload


def submit_post(payload):
    resp = requests.post(f"{BASE_URL}/posts", headers=HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()["postSubmissionId"]


def poll_post_status(post_submission_id, interval=3, timeout=60):
    """
    投稿ステータスをポーリングして最終結果を取得する(任意)。
    スケジュール投稿の場合は published になるまで待たず、
    「scheduled」の確認が取れた時点で返す。
    """
    elapsed = 0
    while elapsed < timeout:
        resp = requests.get(f"{BASE_URL}/posts/{post_submission_id}", headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status in ("published", "failed", "scheduled"):
            return data
        time.sleep(interval)
        elapsed += interval
    raise TimeoutError(f"ステータス確認がタイムアウトしました: {post_submission_id}")


def schedule_queue_hourly(queue=POST_QUEUE, account_id=None, interval_hours=1, start_time=None):
    """
    キューの投稿を1時間おき(interval_hours で変更可)にスケジュールする。
    Blotato側のキューに予約として積むだけなので、
    このスクリプトは実行後すぐ終了してOK(起動しっぱなしにする必要はない)。

    start_time: 1本目の投稿時刻(datetime, UTC想定)。省略時は「今すぐ」扱い。
                2本目以降は start_time から interval_hours おきに自動計算される。
    """
    if account_id is None:
        account_id = get_threads_account_id()

    # start_time省略時は「今すぐ」を基準に、1本目=即時投稿、2本目以降を1時間おきにスケジュール
    immediate_first = start_time is None
    base_time = datetime.now(timezone.utc) if immediate_first else start_time

    results = []
    for i, item in enumerate(queue):
        print(f"\n=== [{i+1}/{len(queue)}] {item['label']} をスケジュールします ===")

        if i == 0 and immediate_first:
            scheduled_time = None  # 即時投稿
        else:
            post_time = base_time + timedelta(hours=interval_hours * i)
            scheduled_time = post_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        payload = build_payload(
            account_id,
            item["main_text"],
            item["reply_text"],
            scheduled_time=scheduled_time,
        )
        submission_id = submit_post(payload)
        when = scheduled_time if scheduled_time else "即時"
        print(f"投稿を登録: postSubmissionId={submission_id} / 予定時刻={when}")

        results.append({
            "label": item["label"],
            "postSubmissionId": submission_id,
            "scheduledTime": scheduled_time,
        })

    return results


if __name__ == "__main__":
    # 例: 1本目は今すぐ、2本目は1時間後、3本目は2時間後...と1時間おきにスケジュール
    schedule_queue_hourly(POST_QUEUE, interval_hours=1)
