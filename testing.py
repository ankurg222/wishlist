import requests
import json
import time
import os
import logging
from pathlib import Path
import telebot
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= CONFIG =================

#PROXY_URL = os.getenv('PROXY_URL')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

#PROXY_URL = "http://27.34.242.98:80"

WISHLIST_API = "https://www.sheinindia.in/api/wishlist/getwishlist"

CHECK_INTERVAL = 10
TOTAL_PAGES = 9
PAGE_SIZE = 10
REQUEST_TIMEOUT = 5
MAX_RETRIES = 8
MAX_NOTIFICATIONS_PER_PRODUCT = 3
MAX_WORKERS = 5

LOG_FILE = "wishlist_monitor.log"

NOTIFICATION_COUNT_FILE = "notification_count.json"

# ================= BOT =================

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

MONITORING_ACTIVE = False
MONITOR_THREAD = None

PREVIOUS_STOCK_STATUS = {}
status_lock = threading.Lock()

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE)
    ]
)
logger = logging.getLogger()

# ================= COOKIE UTILS =================

def parse_cookie_header(cookie_string):
    cookies = {}
    for part in cookie_string.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return cookies

def save_cookies(cookies):
    os.makedirs("cookies", exist_ok=True)
    with open("cookies/cookies.json", "w") as f:
        json.dump(cookies, f, indent=2)
    logger.info(f"✅ Cookies saved ({len(cookies)})")

def load_cookies():
    path = Path("cookies/cookies.json")
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

# ================= NOTIFICATION UTILS =================

def load_notification_counts():
    if os.path.exists(NOTIFICATION_COUNT_FILE):
        with open(NOTIFICATION_COUNT_FILE) as f:
            return json.load(f)
    return {}

def save_notification_counts(data):
    with open(NOTIFICATION_COUNT_FILE, "w") as f:
        json.dump(data, f, indent=2)

NOTIFICATION_COUNTS = load_notification_counts()

# ================= TELEGRAM =================

def send_telegram_message(msg):
    resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }, timeout=10)

    if resp.status_code != 200:
        logger.error(f"Telegram send failed ({resp.status_code}): {resp.text}")

# ================= TELEGRAM COMMANDS =================

@bot.message_handler(commands=["start"])
def start_cmd(m):
    if Path("cookies/cookies.json").exists():
        bot.reply_to(
            m,
            "🚀 *SHEIN WISHLIST MONITOR*\n\n"
            "✅ Cookies found\n\n"
            "/startmonitor – Start monitoring\n"
            "/stopmonitor – Stop monitoring\n"
            "/setcookies – Update cookies\n"
            "/status – Check status",
            parse_mode="Markdown"
        )
    else:
        bot.reply_to(
            m,
            "🚀 *SHEIN WISHLIST MONITOR*\n\n"
            "❌ No cookies found\n"
            "Use /setcookies to upload cookies",
            parse_mode="Markdown"
        )

@bot.message_handler(commands=["setcookies"])
def setcookies_cmd(m):
    msg = bot.reply_to(
        m,
        "📂 *UPLOAD cookies.txt*\n\n"
        "Format:\n"
        "`cookie1=value1; cookie2=value2; ...`",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_cookies)

def process_cookies(m):
    if not m.document:
        bot.reply_to(m, "❌ Please upload a cookies file")
        return

    file = bot.download_file(bot.get_file(m.document.file_id).file_path)
    cookies = parse_cookie_header(file.decode())

    if len(cookies) < 5:
        bot.reply_to(m, "❌ Invalid cookies")
        return

    save_cookies(cookies)
    bot.reply_to(m, "✅ Cookies saved successfully. Use /startmonitor to start")

@bot.message_handler(commands=["status"])
def status_cmd(m):
    bot.reply_to(
        m,
        f"📡 Monitor: {'RUNNING' if MONITORING_ACTIVE else 'STOPPED'}\n"
        f"📦 Tracked products: {len(PREVIOUS_STOCK_STATUS)}\n"
        f"🔔 Alerts sent: {len(NOTIFICATION_COUNTS)}"
    )

@bot.message_handler(commands=["startmonitor"])
def start_monitor(m):
    global MONITORING_ACTIVE, MONITOR_THREAD

    if MONITORING_ACTIVE:
        bot.reply_to(m, "⚠️ Monitor already running")
        return

    if not load_cookies():
        bot.reply_to(m, "❌ Upload cookies first using /setcookies")
        return

    MONITORING_ACTIVE = True
    MONITOR_THREAD = threading.Thread(target=monitor_wishlist, daemon=True)
    MONITOR_THREAD.start()
    bot.reply_to(m, "🚀 Monitor started")

@bot.message_handler(commands=["stopmonitor"])
def stop_monitor(m):
    global MONITORING_ACTIVE
    MONITORING_ACTIVE = False
    bot.reply_to(m, "⏹️ Monitor stopped")

# ================= PAGE FETCH =================

def fetch_wishlist_page(session, page_num):
    params = {
        "currentPage": page_num,
        "pageSize": PAGE_SIZE
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(
                WISHLIST_API,
                params=params,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code == 200:
                data = response.json()
                return data.get("products", [])

            logger.warning(f"HTTP {response.status_code} on page {page_num}")

        except requests.exceptions.Timeout:
            logger.warning(f"Page {page_num} timeout ({attempt+1}/{MAX_RETRIES})")

        except Exception as e:
            logger.error(f"Page {page_num} error: {e}")
            return []

    return []

# ================= PARALLEL SCAN =================

def scan_pages_parallel(cookies):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://www.sheinindia.in/",
        "Authorization": f"Bearer {cookies.get('A', '')}"
    }

    session = requests.Session()
    session.headers.update(headers)
    session.cookies.update(cookies)

    in_stock_products = []
    total_products = 0
    all_seen_codes = set()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(fetch_wishlist_page, session, page)
            for page in range(TOTAL_PAGES + 1)
        ]

        for future in as_completed(futures):
            products = future.result()

            for product in products:
                code = product.get("productCode")
                if not code:
                    continue

                all_seen_codes.add(code)
                total_products += 1

                in_stock = False
                in_stock_sizes = []

                for variant in product.get("variantOptions", []):
                    if variant.get("stock", {}).get("stockLevelStatus") == "inStock":
                        in_stock = True
                        size = next(
                            (
                                q.get("value")
                                for q in variant.get("variantOptionQualifiers", [])
                                if q.get("qualifier") == "size"
                            ),
                            None
                        )
                        if size:
                            in_stock_sizes.append(size)

                if in_stock:
                    in_stock_products.append({
                        "productCode": code,
                        "name": product.get("name", "Unknown"),
                        "price": product.get("price", {}).get("value", 0),
                        "url": product.get("url", ""),
                        "sizes": sorted(set(in_stock_sizes))
                    })

    return in_stock_products, total_products, all_seen_codes

# ================= MONITOR =================

def monitor_wishlist():
    global MONITORING_ACTIVE

    cookies = load_cookies()
    if not cookies:
        logger.error("No cookies found. Monitoring stopped.")
        return

    logger.info("🚀 Wishlist monitor started")

    while MONITORING_ACTIVE:
        start_time = time.time()

        products, total, all_seen_codes = scan_pages_parallel(cookies)
        current_in_stock_codes = set()
        notified = 0

        # ===== RESTOCK DETECTION =====
        for product in products:
            code = product["productCode"]
            current_in_stock_codes.add(code)

            with status_lock:
                was_in_stock = PREVIOUS_STOCK_STATUS.get(code, False)

                if was_in_stock:
                    PREVIOUS_STOCK_STATUS[code] = True
                    continue   # still in stock → no alert

                notify_count = NOTIFICATION_COUNTS.get(code, 0)
                if notify_count >= MAX_NOTIFICATIONS_PER_PRODUCT:
                    continue

                NOTIFICATION_COUNTS[code] = notify_count + 1
                save_notification_counts(NOTIFICATION_COUNTS)

                PREVIOUS_STOCK_STATUS[code] = True

            url = product["url"]
            if not url.startswith("http"):
                url = f"https://www.sheinindia.in{url}"

            sizes = product.get("sizes", [])
            sizes_text = ", ".join(sizes) if sizes else "Unknown"

            send_telegram_message(
                f"🔔 *IN STOCK!*\n"
                f"📦 {product['name']}\n"
                f"📏 {sizes_text}\n"
                f"💰 Rs.{product['price']}\n"
                #f"🔖 `{code}`\n"
                f"🛒 [OPEN PRODUCT]({url})"
            )
            time.sleep(1)

            logger.info(f"📨 Alert sent: {code}")
            notified += 1

        # ===== OUT-OF-STOCK RESET =====
        with status_lock:
            for code in list(PREVIOUS_STOCK_STATUS.keys()):
        # Reset ONLY if product was fetched AND has no stock
                if code in all_seen_codes and code not in current_in_stock_codes:
                    PREVIOUS_STOCK_STATUS[code] = False
                    NOTIFICATION_COUNTS[code] = 0

        duration = time.time() - start_time
        logger.info(
            f"Scan done | {duration:.1f}s | "
            f"Total: {total} | In-stock: {len(products)} | Alerts: {notified}"
        )

        time.sleep(CHECK_INTERVAL)

# ================= MAIN =================

if __name__ == "__main__":
    logger.info("🤖 Bot started")
    bot.infinity_polling()