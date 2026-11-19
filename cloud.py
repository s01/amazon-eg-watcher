"""Run one scan and persist acknowledged alerts on the monitor-state branch."""
import base64
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from watcher import BUTTON, ServiceError, deliver, send, telegram

BRANCH = "monitor-state"
STATE_FILE = "state.json"


def api(path, payload=None, method="GET"):
    command = ["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/{path}", "-X", method]
    if payload is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(command, input=json.dumps(payload), capture_output=True, text=True)
    if result.returncode:
        raise ServiceError(f"GitHub state request failed ({method})")
    return json.loads(result.stdout)


def snapshot(database, offset):
    return {"offset": offset, "sent": dict(database.execute("SELECT id, price FROM sent"))}


def export_local(path):
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as database:
        saved = database.execute("SELECT value FROM settings WHERE key = 'offset'").fetchone()
        offset = saved[0] if saved else json.loads(path.with_name("config.json").read_text())["offset"]
        return snapshot(database, offset)


def run():
    config = {"token": os.environ["TELEGRAM_BOT_TOKEN"], "chat_id": int(os.environ["TELEGRAM_CHAT_ID"])}
    file = api(f"contents/{STATE_FILE}?ref={BRANCH}")
    previous = json.loads(base64.b64decode(file["content"]))
    offset = previous["offset"]
    with sqlite3.connect(":memory:") as database:
        database.execute("CREATE TABLE sent (id TEXT PRIMARY KEY, price TEXT)")
        database.executemany("INSERT INTO sent VALUES (?, ?)", previous["sent"].items())
        manual = False
        try:
            updates = telegram(config["token"], "getUpdates", offset=offset, timeout=0,
                               allowed_updates=["message"])
            for update in updates:
                message = update.get("message", {})
                chat = message.get("chat", {})
                if chat.get("id") == config["chat_id"] and chat.get("type") == "private":
                    command = message.get("text")
                    manual |= command in {BUTTON, "/scan"}
                    if command in {"/start", "/menu"}:
                        send(config, "عروض أمازون مصر فوق 70%. فحص تلقائي كل 5 دقايق حسب الجدولة.", True)
                offset = update["update_id"] + 1
            found, sent = deliver(config, database)
            print(f"Scan complete: {found} matching, {sent} sent")
            if manual:
                send(config, f"الفحص خلص. لقيت {found} عرض مطابق وبعت {sent} تنبيه جديد.")
        except ServiceError:
            if manual:
                send(config, "الفحص مقدرش يخلص دلوقتي. جرّب تاني بعد شوية.")
            raise
        finally:
            state = snapshot(database, offset)
            if state != previous:
                api(f"contents/{STATE_FILE}", {
                    "message": "Save alert state",
                    "branch": BRANCH,
                    "sha": file["sha"],
                    "content": base64.b64encode(json.dumps(state, sort_keys=True).encode()).decode(),
                }, "PUT")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 2:
            print(json.dumps(export_local(Path(sys.argv[1])), sort_keys=True))
        else:
            run()
    except (ServiceError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
