"""
毎朝7:00 自動投稿パイプライン（テキストのみ版）

前提:
- project/persona.md, offer.md, thread_template.md を読み込む
- 画像生成・Canva連携は使わず、テキスト投稿のみ
- LINE VOOMは自動投稿不可のため、通知のみ実装
"""

import os
import re
import json
import datetime
import requests

PROJECT_DIR = "project"

THEMES = [
    "白髪ぼかしの基本(染めるよりぼかす)",
    "分け目の白髪が気になる悩み",
    "生え際の白髪をどう目立たなくするか",
    "美容院に行く頻度を減らす方法",
    "白髪染めのランニングコストの悩み",
    "自宅でできる白髪ケアの工夫",
    "白髪が増えてきたと感じた瞬間のあるある",
    "白髪と付き合いやすいヘアスタイル選び",
    "プロに任せることで得られる安心感",
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
    jst = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(jst)
    slot_index = max(0, min(13, now.hour - 7))
    day_index = now.timetuple().tm_yday
    idx = (day_index * 14 + slot_index) % len(THEMES)
    return THEMES[idx]


def generate_thread_text(theme: str) -> dict:
    persona = load_template("persona.md")
    offer = load_template("offer.md")
    template = load_template("thread_template.md")

    prompt = f"""
以下のペルソナ・オファー・型に沿って、今日のThreads投稿(①→②→③→④)を1本作成してください。
今回のテーマは「{theme}」です。このテーマに沿った内容にしてください。

post_4(最後の投稿)は、LINE公式アカウントへの誘導キャプションのみにしてください。
URLはこちらで別途付与するので、post_4の文章にはURLを含めないでください。

# ペルソナ
{persona}

# オファー
{offer}

# 型
{template}

出力は次の1行のJSONオブジェクトのみとし、説明文・前置き・コードフェンスは一切含めないこと:
{{"post_1": "...", "post_2": "...", "post_3": "...", "post_4": "..."}}
Threadsは1投稿あたり500文字までのため、post_1〜post_4を全部つなげた合計が
400文字以内に収まるよう、各投稿は簡潔に(1〜2行程度)してください。
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
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    response.raise_for_status()
    data = response.json()
    text = data["content"][0]["text"].strip()

    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"JSONが見つかりませんでした。応答内容: {text[:500]}")
    json_text = match.group(0)

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON解析エラー: {e}\n応答内容: {json_text[:1000]}") from e


BLOTATO_BASE_URL = "https://backend.blotato.com/v2"
BLOTATO_ACCOUNT_IDS = {
    "threads": "8247",
    "instagram": None,
}


def post_thread_to_blotato(platform: str, main_text: str, additional_texts: list) -> str:
    """Blotato REST APIで、メイン投稿+スレッド(返信の連なり)として投稿する。"""
    api_key = os.environ["BLOTATO_API_KEY"]
    account_id = BLOTATO_ACCOUNT_IDS.get(platform)
    if not account_id:
        raise ValueError(f"{platform}のaccountIdが未設定です")

    payload = {
        "post": {
            "accountId": account_id,
            "content": {
                "text": main_text,
                "mediaUrls": [],
                "platform": platform,
                "additionalPosts": [
                    {"text": t, "mediaUrls": []} for t in additional_texts
                ],
            },
            "target": {"targetType": platform},
        }
    }
    response = requests.post(
        f"{BLOTATO_BASE_URL}/posts",
        headers={"blotato-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
    )
    if not response.ok:
        raise RuntimeError(
            f"Blotato投稿失敗 status={response.status_code} "
            f"body={response.text[:1000]} "
            f"payload={json.dumps(payload, ensure_ascii=False)[:500]}"
        )
    return response.json()["postSubmissionId"]


def notify_for_line_voom(text: str):
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("LINE VOOM投稿用テキスト:", text)
        return
    requests.post(webhook_url, json={"text": f"LINE VOOM手動投稿してください:\n{text}"})


def log_to_sheets(row: dict):
    print("log_to_sheets:", row)


def weighted_length(text: str) -> int:
    """全角文字(日本語など)を2、半角文字を1としてカウントする。
    ThreadsのAPIは全角文字を2文字分として500文字制限を判定しているため。"""
    length = 0
    for ch in text:
        code = ord(ch)
        if code <= 0x7E or 0xFF61 <= code <= 0xFF9F:
            length += 1
        else:
            length += 2
    return length


def build_threads_text(thread: dict) -> str:
    """Threadsは全角換算で500文字までのため、収まらない場合は段階的に短くする。"""
    full_text = "\n\n".join(thread.values())
    if weighted_length(full_text) <= 460:
        return full_text

    short_text = "\n\n".join([thread.get("post_1", ""), thread.get("post_4", "")])
    if weighted_length(short_text) <= 460:
        return short_text

    result = ""
    for ch in short_text:
        if weighted_length(result + ch) > 460:
            break
        result += ch
    return result


def main():
    theme = get_theme_for_now()
    thread = generate_thread_text(theme)

    main_text = "\n\n".join([
        thread.get("post_1", ""),
        thread.get("post_2", ""),
        thread.get("post_3", ""),
    ])
    line_thread_text = f"{thread.get('post_4', '')}\n\n{LINE_URL}"

    full_text = main_text + "\n\n" + line_thread_text

    threads_post_id = post_thread_to_blotato(
        "threads", main_text, [line_thread_text]
    )

    notify_for_line_voom(full_text)

    log_to_sheets({
        "date": datetime.datetime.now().isoformat(),
        "theme": theme,
        "text": full_text,
        "threads_post_id": threads_post_id,
    })


if __name__ == "__main__":
    main()
