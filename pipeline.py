"""
毎朝7:00 自動投稿パイプライン（テキストのみ版）

前提:
- project/persona.md, offer.md, thread_template.md を読み込む
- 画像生成・Canva連携は使わず、テキスト投稿のみ
- LINE VOOMは自動投稿不可のため、通知のみ実装
"""

import os
import json
import datetime
import requests

PROJECT_DIR = "project"

# 7:00〜20:00の1時間おき14枠で自動的に切り替えるテーマ一覧。
# 日付×時間枠のインデックスで選ぶため、同じ日でも枠ごとに、日を跨いでも
# 単純な曜日パターンにならないよう回転する。テーマ数は自由に増減可能。
THEMES = [
    "白髪ぼかしの基本(染めるよりぼかす)",
    "分け目の白髪が気になる悩み",
    "生え際の白髪をどう目立たなくするか",
    "美容院に行く頻度を減らす方法",
    "白髪染めのランニングコストの悩み",
    "自宅でできる白髪ケアの工夫",
    "白髪が増えてきたと感じた瞬間のあるある",
    "白髪と付き合いやすいヘアスタイル選び",
    "セルフカラーで失敗しやすいポイント",
    "白髪ぼかしとハイライトの違い",
    "白髪が気になり始める年代特有の悩み",
    "美容師に相談しづらい白髪の話",
    "白髪染めのタイミングの見極め方",
    "白髪ケアで後悔しないための知識",
]


LINE_URL = "https://lin.ee/9UzZKWk"


def load_template(filename: str) -> str:
    path = os.path.join(PROJECT_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def get_theme_for_now() -> str:
    """現在時刻(JST)から、今回投稿すべきテーマを自動選択する。"""
    jst = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(jst)
    slot_index = max(0, min(13, now.hour - 7))  # 7時=0枠 〜 20時=13枠
    day_index = now.timetuple().tm_yday
    idx = (day_index * 14 + slot_index) % len(THEMES)
    return THEMES[idx]


# ---------- 1. Threads文章生成（Claude API） ----------
def generate_thread_text(theme: str) -> dict:
    persona = load_template("persona.md")
    offer = load_template("offer.md")
    template = load_template("thread_template.md")

    prompt = f"""
以下のペルソナ・オファー・型に沿って、今日のThreads投稿(①→②→③→④)を1本作成してください。
今回のテーマは「{theme}」です。このテーマに沿った内容にしてください。

post_4(最後の投稿)には、LINE公式アカウントへの登録リンクとして必ず次のURLをそのまま含めてください:
{LINE_URL}

# ペルソナ
{persona}

# オファー
{offer}

# 型
{template}

出力はJSON形式のみで、それ以外の文章は含めないこと。以下のキーを持つこと:
{{"post_1": "...", "post_2": "...", "post_3": "...", "post_4": "..."}}
"""

    api_key = os.environ["ANTHROPIC_API_KEY"]
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    response.raise_for_status()
    data = response.json()
    text = data["content"][0]["text"].strip()

    # ```json ... ``` のようなコードフェンスが付いた場合に備えて除去
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()

    return json.loads(text)


# ---------- 2. Threads / Instagram投稿(Blotato REST API・無人実行用) ----------
# Claude Code + MCPはチャットセッションが必要なため、GitHub Actionsのような
# 無人自動実行では使えない。ここではBlotatoの通常のREST APIを直接叩く。
BLOTATO_BASE_URL = "https://backend.blotato.com/v2"
BLOTATO_ACCOUNT_IDS = {
    "threads": "8247",   # massu_aile (テスト投稿で確認済み)
    "instagram": None,   # TODO: Instagram接続後にaccountIdを設定
}


def post_to_blotato(platform: str, text: str) -> str:
    """Blotato REST APIで指定プラットフォームにテキスト投稿する。"""
    api_key = os.environ["BLOTATO_API_KEY"]
    account_id = BLOTATO_ACCOUNT_IDS.get(platform)
    if not account_id:
        raise ValueError(f"{platform}のaccountIdが未設定です")

    payload = {
        "post": {
            "accountId": account_id,
            "content": {
                "text": text,
                "mediaUrls": [],
                "platform": platform,
            },
            "target": {"targetType": platform},
        }
    }
    response = requests.post(
        f"{BLOTATO_BASE_URL}/posts",
        headers={"blotato-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
    )
    response.raise_for_status()
    return response.json()["postSubmissionId"]


# ---------- 3. LINE VOOM(自動投稿不可 → 通知のみ) ----------
def notify_for_line_voom(text: str):
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("LINE VOOM投稿用テキスト:", text)
        return
    requests.post(webhook_url, json={"text": f"LINE VOOM手動投稿してください:\n{text}"})


# ---------- 4. Googleスプレッドシートへログ保存 ----------
def log_to_sheets(row: dict):
    # TODO: gspread または Google Sheets API v4 で1行追加
    raise NotImplementedError


def main():
    theme = get_theme_for_now()
    thread = generate_thread_text(theme)
    full_text = "\n\n".join(thread.values())

    # Threads / Instagramへ実際に投稿(無人実行なのでREST APIを直接叩く)
    threads_post_id = post_to_blotato("threads", full_text)
    # instagram_post_id = post_to_blotato("instagram", full_text)  # accountId設定後に有効化

    notify_for_line_voom(full_text)

    log_to_sheets({
        "date": datetime.datetime.now().isoformat(),
        "theme": theme,
        "text": full_text,
        "threads_post_id": threads_post_id,
    })


if __name__ == "__main__":
    main()
