import argparse
import fcntl
import getpass
import json
import os
import re
import secrets
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SOURCES = (
    "https://www.amazon.eg/-/en/deals",
    "https://www.amazon.eg/whitefriday?language=en",
)
INTERVAL = 900
BUTTON = "🔎 افحص العروض دلوقتي"


class ServiceError(Exception):
    pass


def request(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"User-Agent": "AmazonEGPersonalDealsWatcher/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=35) as response:
            return response.read().decode("utf-8")
    except HTTPError as error:
        raise ServiceError(f"HTTP {error.code} from {urlsplit(url).hostname}") from None
    except (URLError, TimeoutError):
        raise ServiceError(f"Connection failed to {urlsplit(url).hostname}") from None


def parse_offer(product):
    asin = product.get("asin", "")
    name = product.get("title", "")
    if not re.fullmatch(r"[A-Z0-9]{10}", asin) or not name.strip():
        return None
    if (product.get("dealDetails") or {}).get("state") != "AVAILABLE":
        return None
    pricing = product.get("price") or {}
    try:
        price = Decimal((pricing.get("priceToPay") or {}).get("price", ""))
        reference = Decimal((pricing.get("basisPrice") or {}).get("price", ""))
        if not price.is_finite() or not reference.is_finite() or not 0 < price < reference:
            return None
        if (reference - price) * 100 <= reference * 70:
            return None
    except (InvalidOperation, TypeError):
        return None
    return asin, price, reference, " ".join(name.split())[:320]


def amazon_products(page):
    products = {}
    decoder = json.JSONDecoder()
    for match in re.finditer(r'"productSearchResponse"\s*:\s*', page):
        try:
            response, _ = decoder.raw_decode(page, match.end())
        except ValueError:
            continue
        if isinstance(response, dict):
            for product in response.get("products") or []:
                if isinstance(product, dict) and product.get("asin"):
                    products[product["asin"]] = product
    if not products:
        raise ServiceError("Amazon Egypt page returned no readable deal cards")
    return products.values()


def scan():
    for url in SOURCES:
        try:
            products = amazon_products(request(url))
        except ServiceError as error:
            print(str(error), flush=True)
            continue
        print(f"Amazon Egypt: read {len(products)} deal cards", flush=True)
        offers = filter(None, map(parse_offer, products))
        return sorted(offers, key=lambda offer: offer[1] / offer[2])
    raise ServiceError("Could not read Amazon Egypt deals from either public page")


def telegram(token, method, **payload):
    result = json.loads(request(f"https://api.telegram.org/bot{token}/{method}", payload))
    if not result.get("ok"):
        raise ServiceError(f"Telegram {method} failed")
    return result["result"]


def send(config, message, keyboard=False):
    options = {"reply_markup": {"keyboard": [[BUTTON]], "resize_keyboard": True}} if keyboard else {}
    telegram(config["token"], "sendMessage", chat_id=config["chat_id"], text=message,
             link_preview_options={"is_disabled": True}, **options)


def setup():
    path = ROOT / "config.json"
    if path.exists():
        raise ServiceError("This folder already has a paired bot")
    token = getpass.getpass("Telegram token (hidden): ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
        raise ServiceError("Invalid Telegram token format")
    bot = telegram(token, "getMe")
    if bot["username"].lower() != "amazonsaifbot":
        raise ServiceError("Token does not belong to @amazonsaifbot")
    code = "/start " + secrets.token_urlsafe(12)
    print(f"Send this to @{bot['username']} in private Telegram chat:\n{code}", flush=True)
    offset = 0
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        updates = telegram(token, "getUpdates", offset=offset, timeout=10,
                           allowed_updates=["message"])
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message", {})
            chat = message.get("chat", {})
            if chat.get("type") != "private" or message.get("text") != code:
                continue
            config = {"token": token, "chat_id": chat["id"], "offset": offset}
            send(config, "✅ تم ربط بوت عروض أمازون مصر. الفحص التلقائي كل 15 دقيقة أثناء التشغيل.", True)
            with path.open("x") as file:
                json.dump(config, file)
            print("Telegram pairing verified.")
            return
    raise ServiceError("Pairing code expired")


def deliver(config, database):
    offers = scan()
    sent = 0
    for product_id, price, reference, name in offers:
        previous = database.execute("SELECT price FROM sent WHERE id = ?", (product_id,)).fetchone()
        if previous and price >= Decimal(previous[0]):
            continue
        percentage = (reference - price) * 100 / reference
        link = f"https://www.amazon.eg/dp/{product_id}"
        send(config, f"🔥 خصم {percentage:.2f}% على أمازون مصر\n{name}\n"
             f"السعر {price:,.2f} جنيه | المرجعي {reference:,.2f} جنيه\n"
             f"{link}\nراجع السعر والتوافر على أمازون.")
        database.execute("INSERT OR REPLACE INTO sent VALUES (?, ?)", (product_id, str(price)))
        database.commit()
        sent += 1
        time.sleep(1)
    return len(offers), sent


def run(config):
    with sqlite3.connect(ROOT / "state.sqlite3") as database:
        database.execute("CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, price TEXT)")
        database.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER)")
        saved = database.execute("SELECT value FROM settings WHERE key = 'offset'").fetchone()
        offset = saved[0] if saved else config["offset"]
        due = 0
        while True:
            try:
                updates = telegram(config["token"], "getUpdates", offset=offset,
                                   timeout=max(0, min(10, int(due - time.monotonic()))),
                                   allowed_updates=["message"])
                manual = False
                for update in updates:
                    offset = update["update_id"] + 1
                    message = update.get("message", {})
                    chat = message.get("chat", {})
                    if chat.get("id") != config["chat_id"] or chat.get("type") != "private":
                        continue
                    text = message.get("text")
                    manual |= text in {"/scan", BUTTON}
                    if text in {"/start", "/menu"}:
                        send(config, "عروض أمازون مصر بخصم أكتر من 70%.", True)
                if updates:
                    database.execute("INSERT OR REPLACE INTO settings VALUES ('offset', ?)", (offset,))
                    database.commit()
                if manual or time.monotonic() >= due:
                    due = time.monotonic() + INTERVAL
                    try:
                        found, sent = deliver(config, database)
                        print(f"Scan complete: {found} matching, {sent} sent", flush=True)
                        if manual:
                            send(config, f"الفحص خلص. لقيت {found} عرض مطابق وبعت {sent} تنبيه جديد.")
                    except ServiceError:
                        if manual:
                            send(config, "الفحص مقدرش يجيب العروض دلوقتي. جرّب تاني بعد شوية.")
                        raise
            except ServiceError as error:
                print(error, flush=True)
                time.sleep(15)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["check", "setup", "run"])
    mode = parser.parse_args().mode
    if mode == "check":
        for _, price, reference, name in scan():
            print(f"{100 * (1 - price / reference):.2f}% | {price:,.2f} EGP | {name}")
        return
    with (ROOT / "watcher.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if mode == "setup":
            setup()
        else:
            run(json.loads((ROOT / "config.json").read_text()))


if __name__ == "__main__":
    try:
        main()
    except (ServiceError, OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
