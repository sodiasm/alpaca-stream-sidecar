import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
from alpaca.trading.stream import TradingStream

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

WEBHOOK = os.environ["N8N_WEBHOOK_URL"]
PORT = int(os.getenv("PORT", 8080))


# ── Parse accounts from env ────────────────────────────────────────────────────
def load_accounts() -> list[dict]:
    """
    Reads ACCOUNTS=acc1,acc2,... then loads each account's KEY/SECRET/LABEL/IS_PAPER.
    """
    raw = os.environ.get("ACCOUNTS", "")
    if not raw:
        raise EnvironmentError("ACCOUNTS env var is not set. Example: ACCOUNTS=acc1,acc2")

    accounts = []
    for name in [a.strip() for a in raw.split(",") if a.strip()]:
        prefix = name.upper()
        key = os.environ.get(f"{prefix}_KEY")
        secret = os.environ.get(f"{prefix}_SECRET")
        label = os.environ.get(f"{prefix}_LABEL", name)
        is_paper = os.environ.get(f"{prefix}_IS_PAPER", "true").lower() == "true"

        if not key or not secret:
            log.warning(f"Skipping '{name}': missing {prefix}_KEY or {prefix}_SECRET")
            continue

        accounts.append({
            "name": name,
            "key": key,
            "secret": secret,
            "label": label,
            "is_paper": is_paper,
        })
        log.info(f"Loaded account: [{label}] paper={is_paper}")

    if not accounts:
        raise EnvironmentError("No valid accounts found. Check your env variables.")
    return accounts


# ── POST to n8n ────────────────────────────────────────────────────────────────
import asyncio


async def post_to_n8n(payload: dict):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                WEBHOOK, json=payload, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    log.warning(
                        f"n8n returned HTTP {resp.status} for account [{payload.get('account_label')}]"
                    )
    except Exception as e:
        log.error(f"Failed to POST to n8n: {e}")


# ── Stream handler factory (one per account) ───────────────────────────────────
def make_handler(account: dict):
    async def trade_update_handler(data):
        order = data.order
        payload = {
            # Account identity
            "account_label": account["label"],
            "account_name": account["name"],
            "is_paper": account["is_paper"],
            # Trade event
            "event": data.event,
            "timestamp": str(data.timestamp),
            "order_id": str(order.id),
            "client_order_id": str(order.client_order_id),
            "symbol": order.symbol,
            "side": order.side.value,
            "type": order.type.value,
            "qty": str(order.qty),
            "filled_qty": str(order.filled_qty),
            "filled_avg_price": str(order.filled_avg_price),
            "status": order.status.value,
            "time_in_force": order.time_in_force.value,
            "limit_price": str(order.limit_price),
            "stop_price": str(order.stop_price),
        }
        log.info(
            f"[{account['label']}] [{data.event.upper()}] {order.symbol} qty={order.qty} @ {order.filled_avg_price}"
        )
        await post_to_n8n(payload)

    return trade_update_handler


# ── Run one stream per account in its own thread ───────────────────────────────
def run_stream(account: dict):
    log.info(f"Starting stream for [{account['label']}] (paper={account['is_paper']})")
    stream = TradingStream(
        api_key=account["key"],
        secret_key=account["secret"],
        paper=account["is_paper"],
    )
    stream.subscribe_trade_updates(make_handler(account))
    stream.run()  # blocks this thread; auto-reconnects internally


# ── Health check server ────────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    log.info(f"Health check server on port {PORT}")
    server.serve_forever()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    accounts = load_accounts()

    # Health server in background
    threading.Thread(target=run_health_server, daemon=True).start()

    # One thread per account stream
    threads = []
    for account in accounts:
        t = threading.Thread(
            target=run_stream, args=(account,), daemon=True, name=account["label"]
        )
        t.start()
        threads.append(t)

    log.info(f"Streaming {len(accounts)} account(s): {[a['label'] for a in accounts]}")

    # Keep main thread alive
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
