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
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
SOURCE = "https://souqnile.com/deals.php?min_discount=70&lang=en"
INTERVAL = 900
BUTTON = "🔎 افحص العروض دلوقتي"


class ServiceError(Exception):
    pass


class Cards(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = []
        self.card = None
        self.field = None
        self.next_url = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if "data-next-url" in attrs:
            self.next_url = attrs["data-next-url"]
        if tag == "article" and "sn-card" in classes:
            self.card = {**attrs, "name": "", "reference": "", "source": ""}
        if self.card is None:
            return
        for class_name, field in (("sn-card__name", "name"),
                                  ("sn-card__price-original", "reference"),
                                  ("sn-card__source", "source")):
            if class_name in classes:
                self.field = field
                if field == "name":
                    self.card["href"] = attrs.get("href", "")

    def handle_data(self, data):
        if self.card is not None and self.field:
            self.card[self.field] += data

    def handle_endtag(self, tag):
        if tag in {"a", "span"}:
            self.field = None
        if tag == "article" and self.card is not None:
            self.cards.append(self.card)
            self.card = None


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


def parse_offer(card):
    product_id = card.get("data-product-id", "")
    if not re.fullmatch(r"[A-Z0-9]{26}", product_id):
        return None
    if card["source"].strip() != "Checkout on Amazon Egypt":
        return None
    reference = re.fullmatch(r"\s*([\d,.]+)\s*ج\.م\s*", card["reference"])
    if not reference or not card["name"].strip():
        return None
    try:
        price = Decimal(card.get("data-price", ""))
        old = Decimal(reference[1].replace(",", ""))
        if not price.is_finite() or not 0 < price < old:
            return None
        if (old - price) * 100 <= old * 70:
            return None
    except InvalidOperation:
        return None
    return product_id, price, old, " ".join(card["name"].split())[:320]


def scan():
    url = SOURCE
    offers = {}
    visited = set()
    while url:
        if url in visited or len(visited) == 10:
            raise ServiceError("Unexpected deals pagination")
        visited.add(url)
        page = Cards()
        page.feed(request(url))
        if page.next_url is None:
            raise ServiceError("Deals page format changed")
        for card in page.cards:
            offer = parse_offer(card)
            if offer is not None:
                offers[offer[0]] = offer
        url = urljoin(SOURCE, page.next_url) if page.next_url else ""
        if url and urlsplit(url).netloc != "souqnile.com":
            raise ServiceError("Unexpected deals page host")
        if url:
            time.sleep(1)
    return sorted(offers.values(), key=lambda offer: offer[1] / offer[2])


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
        link = f"https://souqnile.com/out.php?id={product_id}&placement=product-card&lang=en"
        send(config, f"🔥 خصم {percentage:.2f}% حسب سوق نايل\n{name}\n"
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
