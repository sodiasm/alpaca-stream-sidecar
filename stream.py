import json
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

        def safe(val):
            """Serialize any value to a JSON-safe string or primitive."""
            if val is None:
                return None
            if hasattr(val, "value"):       # Enum → string
                return val.value
            if hasattr(val, "isoformat"):   # datetime → ISO string
                return val.isoformat()
            return str(val)

        payload = {
            # ── Account identity ──────────────────────────────────────────
            "account_label":        account["label"],
            "account_name":         account["name"],
            "is_paper":             account["is_paper"],

            # ── TradeUpdate top-level fields ──────────────────────────────
            "event":                safe(data.event),
            "timestamp":            safe(data.timestamp),
            "execution_id":         safe(getattr(data, "execution_id", None)),
            "position_qty":         safe(getattr(data, "position_qty", None)),
            "price":                safe(getattr(data, "price", None)),
            "qty":                  safe(getattr(data, "qty", None)),   # event-level fill qty

            # ── Order identity ────────────────────────────────────────────
            "order_id":             safe(order.id),
            "client_order_id":      safe(order.client_order_id),
            "asset_id":             safe(getattr(order, "asset_id", None)),
            "asset_class":          safe(getattr(order, "asset_class", None)),
            "symbol":               safe(order.symbol),

            # ── Order parameters ──────────────────────────────────────────
            "side":                 safe(order.side),
            "type":                 safe(order.type),
            "order_class":          safe(getattr(order, "order_class", None)),
            "time_in_force":        safe(order.time_in_force),
            "order_qty":            safe(order.qty),                    # original order qty
            "notional":             safe(getattr(order, "notional", None)),
            "limit_price":          safe(order.limit_price),
            "stop_price":           safe(order.stop_price),
            "trail_price":          safe(getattr(order, "trail_price", None)),
            "trail_percent":        safe(getattr(order, "trail_percent", None)),
            "hwm":                  safe(getattr(order, "hwm", None)),
            "extended_hours":       safe(getattr(order, "extended_hours", None)),

            # ── Order fill info ───────────────────────────────────────────
            "status":               safe(order.status),
            "filled_qty":           safe(order.filled_qty),
            "filled_avg_price":     safe(order.filled_avg_price),

            # ── Order timestamps ──────────────────────────────────────────
            "created_at":           safe(getattr(order, "created_at", None)),
            "updated_at":           safe(getattr(order, "updated_at", None)),
            "submitted_at":         safe(getattr(order, "submitted_at", None)),
            "filled_at":            safe(getattr(order, "filled_at", None)),
            "expired_at":           safe(getattr(order, "expired_at", None)),
            "canceled_at":          safe(getattr(order, "canceled_at", None)),
            "failed_at":            safe(getattr(order, "failed_at", None)),
            "replaced_at":          safe(getattr(order, "replaced_at", None)),

            # ── Order replace chain ───────────────────────────────────────
            "replaced_by":          safe(getattr(order, "replaced_by", None)),
            "replaces":             safe(getattr(order, "replaces", None)),

            # ── Legs (bracket/OCO orders) ─────────────────────────────────
            "legs":                 [safe(leg) for leg in (order.legs or [])] if getattr(order, "legs", None) else [],
        }

        # Structured JSON log — full payload visible in stdout / n8n log scraper
        log.info(json.dumps(payload, default=str))

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
