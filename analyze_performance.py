"""
post_log.jsonl を元に、Blotatoの分析API(GET /v2/posts/{id}/analytics)から
実際のエンゲージメント(views/likes/comments/shares/reach)を取得し、

1. 個別投稿のランキング(バズった投稿の抽出)
2. テーマ別の平均成績
3. テンプレート別(教育型/共感・ファン化型/問題解決型)の平均成績

をまとめてCSVで出力する。

前提:
- post_log.jsonl が blotato_threads_post.py と同じディレクトリにあること
  (post_next_in_cycle() を実行するたびに1行ずつ追記される)
- 環境変数 BLOTATO_API_KEY にAPIキーを設定
- Threadsの分析はBlotato側で反映まで数時間〜1日ほどラグが出ることがあるため、
  投稿直後ではなく翌日以降にまとめて実行するのがおすすめ

使い方:
    python analyze_performance.py
    python analyze_performance.py --top 10   # 上位10件だけ表示
"""

import os
import json
import argparse
import requests
from pathlib import Path
from collections import defaultdict

BASE_URL = "https://backend.blotato.com/v2"
API_KEY = os.environ["BLOTATO_API_KEY"]
HEADERS = {
    "Content-Type": "application/json",
    "blotato-api-key": API_KEY,
}

POST_LOG_FILE = Path(__file__).with_name("post_log.jsonl")
REPORT_FILE = Path(__file__).with_name("performance_report.csv")
SUMMARY_FILE = Path(__file__).with_name("performance_summary.csv")

# 「バズった」と判定するスコアの重み(必要に応じて調整してください)
SCORE_WEIGHTS = {"views": 1, "likes": 3, "comments": 5, "shares": 8, "reach": 1}


def load_post_log():
    if not POST_LOG_FILE.exists():
        return []
    records = []
    with open(POST_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def fetch_analytics(post_submission_id):
    """1投稿分の分析データを取得。まだ反映されていない場合はNoneを返す"""
    resp = requests.get(
        f"{BASE_URL}/posts/{post_submission_id}/analytics",
        headers=HEADERS,
    )
    if resp.status_code == 202:
        print(f"  分析データ反映待ち(202): {post_submission_id}")
        return None
    if resp.status_code == 424:
        print(f"  分析取得失敗(424・プラットフォーム側エラー): {post_submission_id}")
        return None
    if not resp.ok:
        print(f"  分析取得エラー({resp.status_code}): {post_submission_id} / {resp.text}")
        return None
    return resp.json()


def compute_score(metrics):
    score = 0
    for key, weight in SCORE_WEIGHTS.items():
        score += metrics.get(key, 0) * weight
    return score


def main(top_n=20):
    records = load_post_log()
    if not records:
        print("post_log.jsonl が見つからないか空です。まだ投稿実績がありません。")
        return

    rows = []
    for rec in records:
        print(f"分析取得中: {rec['label']} ({rec['postSubmissionId']})")
        analytics = fetch_analytics(rec["postSubmissionId"])
        if analytics is None:
            continue

        metrics = analytics.get("metrics", analytics)  # レスポンス形式の揺れに対応
        row = {
            "id": rec["id"],
            "label": rec["label"],
            "theme": rec.get("theme", ""),
            "template": rec.get("template", ""),
            "day": rec.get("day", ""),
            "slot": rec.get("slot", ""),
            "posted_at": rec.get("posted_at", ""),
            "postSubmissionId": rec["postSubmissionId"],
            "views": metrics.get("views", 0),
            "likes": metrics.get("likes", 0),
            "comments": metrics.get("comments", 0),
            "shares": metrics.get("shares", 0),
            "reach": metrics.get("reach", 0),
        }
        row["score"] = compute_score(row)
        rows.append(row)

    if not rows:
        print("分析データを取得できた投稿がまだありません(反映待ちの可能性があります)。")
        return

    rows.sort(key=lambda r: r["score"], reverse=True)

    # ---- 個別投稿ランキングCSV ----
    fieldnames = ["rank", "score", "label", "theme", "template", "views", "likes",
                  "comments", "shares", "reach", "posted_at", "id", "postSubmissionId"]
    with open(REPORT_FILE, "w", encoding="utf-8-sig") as f:
        f.write(",".join(fieldnames) + "\n")
        for i, r in enumerate(rows, start=1):
            f.write(",".join(str(x).replace(",", "、") for x in [
                i, r["score"], r["label"], r["theme"], r["template"],
                r["views"], r["likes"], r["comments"], r["shares"], r["reach"],
                r["posted_at"], r["id"], r["postSubmissionId"],
            ]) + "\n")

    print(f"\n=== バズった投稿 TOP {min(top_n, len(rows))} ===")
    for i, r in enumerate(rows[:top_n], start=1):
        print(f"{i}. [{r['score']}pt] {r['label']} "
              f"(views={r['views']} likes={r['likes']} comments={r['comments']} shares={r['shares']})")

    # ---- テーマ別・テンプレート別の平均成績 ----
    by_theme = defaultdict(list)
    by_template = defaultdict(list)
    for r in rows:
        by_theme[r["theme"]].append(r["score"])
        by_template[r["template"]].append(r["score"])

    def avg(lst):
        return round(sum(lst) / len(lst), 1) if lst else 0

    with open(SUMMARY_FILE, "w", encoding="utf-8-sig") as f:
        f.write("category,key,post_count,avg_score\n")
        for theme, scores in sorted(by_theme.items(), key=lambda kv: -avg(kv[1])):
            f.write(f"theme,{theme},{len(scores)},{avg(scores)}\n")
        for tmpl, scores in sorted(by_template.items(), key=lambda kv: -avg(kv[1])):
            f.write(f"template,{tmpl},{len(scores)},{avg(scores)}\n")

    print("\n=== テーマ別 平均スコア ===")
    for theme, scores in sorted(by_theme.items(), key=lambda kv: -avg(kv[1])):
        print(f"{theme}: 平均{avg(scores)}pt ({len(scores)}件)")

    print("\n=== テンプレート別 平均スコア ===")
    for tmpl, scores in sorted(by_template.items(), key=lambda kv: -avg(kv[1])):
        print(f"{tmpl}: 平均{avg(scores)}pt ({len(scores)}件)")

    print(f"\n個別ランキング: {REPORT_FILE}")
    print(f"カテゴリ別集計: {SUMMARY_FILE}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=20, help="上位何件を表示するか")
    args = parser.parse_args()
    main(top_n=args.top)
