"""
GitHub Actions 上で動く公開係（Macが閉じていても、予約時刻にInstagramへ公開する）。
このファイルの正本は Obsidian の Instagram投稿/scripts/cloud/。`ig_pipeline.py cloud-install` でリポジトリへ配る。

リポジトリの中身:
  queue/<slug>.json    予約（原稿・時刻・ファイルのsha256）。Macが書く
  media/<slug>/        公開する画像（JPEG）または reel.mp4。公開後に消して、履歴からも消す
  status/<slug>.json   結果と進行状況。このスクリプトが書く

守ること:
  ・公開の前に status を publishing にして push（二重公開の防止）。media_publish の直前に committing を記録
  ・committing より前の失敗はやり直す。後の失敗は Instagram を照合し、自動では再投稿しない
  ・ファイルのsha256が予約時と違えば公開しない
  ・トークンはヘッダーで送り、ログ・通知には出さない
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

LOOKAHEAD = timedelta(minutes=25)  # この先の予約は、時刻まで待ってから公開する（定期実行の遅れに備える）
GRACE = timedelta(minutes=180)     # これ以上遅れたら公開しない
MAX_ATTEMPTS = 5
FINAL = ("posted", "error", "missed", "needs_check", "rehearsed")
JST = timezone(timedelta(hours=9))
ROOT = Path.cwd()
VER = "v25.0"
GRAPH = f"https://graph.facebook.com/{VER}"
SECRET_RE = re.compile(r"((?:access_token)=)[^&\s'\")]+|(EAA[A-Za-z0-9]{4})[A-Za-z0-9_-]{14,}")


def redact(text):
    return SECRET_RE.sub(lambda m: (m.group(1) or m.group(2) or "") + "[hidden]", str(text))


def log(msg):
    print(f"[{datetime.now(JST):%m-%d %H:%M:%S}] {redact(msg)}", flush=True)


def now():
    return datetime.now(JST)


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def sync():
    git("fetch", "--quiet", "origin", "main")
    git("checkout", "--quiet", "-B", "main", "origin/main")
    git("reset", "--hard", "--quiet", "origin/main")
    git("clean", "-fdq")


def commit_change(change, message, squash=False):
    """最新を取り込んで change() を適用して push。ほかの更新とぶつかったらやり直す。
    squash=True は、消した画像が履歴に残らないよう、いまの中身だけの1コミットに作り直す"""
    for attempt in range(6):
        sync()
        base = git("rev-parse", "HEAD")
        change()
        git("add", "-A")
        if squash:
            tree = git("write-tree")
            commit = git("commit-tree", tree, "-m", message)
            push = ["push", "--quiet", f"--force-with-lease=main:{base}", "origin", f"{commit}:main"]
        else:
            if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
                return
            git("commit", "--quiet", "-m", message)
            push = ["push", "--quiet", "origin", "HEAD:main"]
        try:
            git(*push)
            sync()
            return
        except subprocess.CalledProcessError as e:
            log(f"push やり直し {attempt + 1}: {e.stderr.strip()[-200:]}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("push に失敗しました")


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def status_of(slug):
    return read_json(ROOT / "status" / f"{slug}.json") or {}


def write_status(slug, data):
    (ROOT / "status").mkdir(exist_ok=True)
    (ROOT / "status" / f"{slug}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))


def set_status(slug, **data):
    data["updated_at"] = now().isoformat(timespec="seconds")
    commit_change(lambda: write_status(slug, {**status_of(slug), **data}), f"status {slug}: {data.get('stage', '')}")


def finish(slug, stage, **extra):
    """結果を残して、予約と画像を消す（履歴からも消す）"""
    def change():
        write_status(slug, {**status_of(slug), "stage": stage, "updated_at": now().isoformat(timespec="seconds"), **extra})
        (ROOT / "queue" / f"{slug}.json").unlink(missing_ok=True)
        shutil.rmtree(ROOT / "media" / slug, ignore_errors=True)
    commit_change(change, f"{stage}: {slug}", squash=True)


def notify(title, message, priority=3, click=None):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    body = {"topic": topic, "title": redact(title), "message": redact(message)[:3500], "priority": priority}
    if click:
        body["click"] = click
    try:
        requests.post("https://ntfy.sh", json=body, timeout=30).raise_for_status()
    except requests.RequestException as e:
        log(f"通知失敗: {type(e).__name__}")


def ig(method, path, **params):
    headers = {"Authorization": f"Bearer {os.environ['IG_ACCESS_TOKEN']}"}
    r = requests.request(method, f"{GRAPH}/{path}", params=params, headers=headers, timeout=60)
    if not r.ok:
        raise RuntimeError(redact(f"Instagram API {r.status_code}: {r.text}"))
    return r.json()


def wait_finished(container, tries=120):
    for _ in range(tries):
        s = ig("GET", container, fields="status_code").get("status_code")
        if s == "FINISHED":
            return
        if s in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram側の処理に失敗（{s}）")
        time.sleep(5)
    raise RuntimeError("Instagram側の処理がタイムアウト")


def create_container(item):
    uid = os.environ["IG_USER_ID"]
    slug = item["slug"]
    if item["kind"] == "reel":
        made = ig("POST", f"{uid}/media", media_type="REELS", upload_type="resumable", caption=item["caption"],
                  share_to_feed="true", thumb_offset="1500")
        cid = made["id"]
        uri = made.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{VER}/{cid}"
        data = (ROOT / "media" / slug / item["files"][0]["name"]).read_bytes()
        r = requests.post(uri, data=data, timeout=(15, 300), headers={
            "Authorization": f"OAuth {os.environ['IG_ACCESS_TOKEN']}", "offset": "0", "file_size": str(len(data))})
        if not r.ok:
            raise RuntimeError(f"動画のアップロードに失敗 {r.status_code}: {r.text[:300]}")
        wait_finished(cid)
        return cid
    sha = git("rev-parse", "HEAD")  # コミットを指定したURLにする（古いキャッシュを避ける）
    base = f"https://raw.githubusercontent.com/{os.environ['GITHUB_REPOSITORY']}/{sha}/media/{slug}"
    children = []
    for f in item["files"]:
        cid = ig("POST", f"{uid}/media", image_url=f"{base}/{f['name']}", is_carousel_item="true")["id"]
        wait_finished(cid, 40)
        children.append(cid)
    cid = ig("POST", f"{uid}/media", media_type="CAROUSEL", children=",".join(children), caption=item["caption"])["id"]
    wait_finished(cid, 40)
    return cid


def find_posted(caption, since):
    for m in ig("GET", f"{os.environ['IG_USER_ID']}/media", fields="id,caption,timestamp,permalink", limit=10)["data"]:
        ts = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        if (m.get("caption") or "").strip()[:60] == caption.strip()[:60] and ts >= since - timedelta(minutes=5):
            return m
    return None


def files_match(item):
    for f in item["files"]:
        path = ROOT / "media" / item["slug"] / f["name"]
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != f["sha256"]:
            return False
    return True


def resolve_after_commit(item, started, reason):
    """公開の操作中に失敗・停止したとき：出ていれば投稿済み、なければ確認待ち（再投稿しない）"""
    slug, title = item["slug"], item["title"]
    try:
        found = find_posted(item["caption"], started)
    except Exception as e:  # noqa: BLE001
        log(f"照合できません（次の回で確認）: {e}")
        return
    if found:
        finish(slug, "posted", media_id=found["id"], permalink=found.get("permalink", ""))
        notify(f"✅ 投稿しました：{title}", found.get("permalink", ""), click=found.get("permalink"))
    else:
        finish(slug, "needs_check", error=redact(reason)[:300])
        notify(f"⚠️ 公開状態の確認が必要：{title}",
               "公開の操作中にエラーが出ました。確認した時点では投稿は見つかりません。自動ではやり直しません。", priority=4)


def process(slug):
    item = read_json(ROOT / "queue" / f"{slug}.json")
    if not item:
        return
    st = status_of(slug)
    if st.get("stage") in FINAL:
        finish(slug, st["stage"])
        return
    due = datetime.fromisoformat(item["publish_at"])
    if st.get("stage") == "publishing" and st.get("committing"):  # 前の実行が公開の操作中に止まった
        resolve_after_commit(item, datetime.fromisoformat(st["started_at"]), "前の実行が途中で停止")
        return
    if due - now() > LOOKAHEAD:
        return
    if now() - due > GRACE:
        finish(slug, "missed", error="予約時刻から大きく遅れたため公開していません")
        notify(f"⏰ 公開していません：{item['title']}", f"予定 {due:%m/%d %H:%M} から3時間以上遅れました。", priority=4)
        return
    wait = (due - now()).total_seconds()
    if wait > 0:
        log(f"{slug}: {due:%H:%M} まで {wait:.0f} 秒待つ")
        time.sleep(wait)
    sync()
    item = read_json(ROOT / "queue" / f"{slug}.json")  # 待っている間に取り消し・差し替えがあれば従う
    if not item or datetime.fromisoformat(item["publish_at"]) > now() + timedelta(seconds=60):
        return
    if not files_match(item):
        finish(slug, "error", error="予約時の内容と一致しません")
        notify(f"⚠️ 公開していません：{item['title']}", "画像・動画が予約時の内容と一致しません。", priority=4)
        return
    started = now()
    attempts = status_of(slug).get("attempts", 0) + 1
    set_status(slug, stage="publishing", committing=False, attempts=attempts, started_at=started.isoformat(timespec="seconds"))
    committing = False
    try:
        container = create_container(item)
        if item.get("dry_run"):
            finish(slug, "rehearsed", seconds=round((now() - started).total_seconds()))
            notify(f"🧪 クラウドのリハーサル完了：{item['title']}", "公開の直前まで問題なく動きました。公開はしていません。")
            return
        set_status(slug, committing=True)
        committing = True
        media_id = ig("POST", f"{os.environ['IG_USER_ID']}/media_publish", creation_id=container)["id"]
        try:
            link = ig("GET", media_id, fields="permalink").get("permalink", "")
        except Exception:  # noqa: BLE001
            link = ""
        finish(slug, "posted", media_id=media_id, permalink=link)
        notify(f"✅ 投稿しました：{item['title']}", link or "投稿が完了しました。", click=link or None)
    except Exception as e:  # noqa: BLE001
        log(f"{slug}: 失敗: {e}")
        if committing:
            resolve_after_commit(item, started, str(e))
        elif attempts < MAX_ATTEMPTS:
            set_status(slug, stage="retry", error=redact(e)[:300])
        else:
            finish(slug, "error", error=redact(e)[:300])
            notify(f"⚠️ 公開できませんでした：{item['title']}",
                   f"公開の操作より前で失敗したので、投稿は出ていません。\n原因: {redact(e)[:300]}", priority=4)


def main():
    missing = [n for n in ("IG_USER_ID", "IG_ACCESS_TOKEN", "NTFY_TOPIC") if not os.environ.get(n)]
    for name in missing:  # 値は出さず、足りない名前だけを実行結果に表示する
        print(f"::error title=Secret未登録::{name} が Repository secrets に登録されていません", flush=True)
    if "IG_USER_ID" in missing or "IG_ACCESS_TOKEN" in missing:
        sys.exit(1)
    sync()
    queue = ROOT / "queue"
    items = sorted((json.loads(f.read_text()) for f in queue.glob("*.json")), key=lambda x: x["publish_at"]) if queue.exists() else []
    log(f"予約 {len(items)} 件")
    failed = False
    for item in items:
        try:
            process(item["slug"])
        except Exception as e:  # noqa: BLE001
            log(f"{item['slug']}: 処理エラー: {e}")
            failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
