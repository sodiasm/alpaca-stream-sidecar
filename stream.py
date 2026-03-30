import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
from alpaca.trading.stream import TradingStream

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

WEBHOOK = os.environ["N8N_WEBHOOK_URL"]
PORT = int(os.getenv("PORT", 8080))

# ── Retry / stagger configuration ─────────────────────────────────────────────
WEBHOOK_MAX_RETRIES  = int(os.getenv("WEBHOOK_MAX_RETRIES", 3))
WEBHOOK_RETRY_BASE_S = float(os.getenv("WEBHOOK_RETRY_BASE_S", 1.0))  # doubles each attempt
WEBHOOK_TIMEOUT_S    = float(os.getenv("WEBHOOK_TIMEOUT_S", 5.0))
WEBHOOK_MIN_INTERVAL = float(os.getenv("WEBHOOK_MIN_INTERVAL_S", 0.25))  # min gap between calls

# Per-account serialisation: ensures at most 1 in-flight POST per account at
# a time AND a minimum inter-call delay to absorb bracket/OCO event bursts.
_account_locks: dict[str, asyncio.Lock] = {}
_account_last_call: dict[str, float] = {}


def _get_account_lock(label: str) -> asyncio.Lock:
    if label not in _account_locks:
        _account_locks[label] = asyncio.Lock()
        _account_last_call[label] = 0.0
    return _account_locks[label]


# ── Dead-letter queue (SQLite) ─────────────────────────────────────────────────
DLQ_PATH = os.getenv("DLQ_PATH", "/data/dlq.sqlite3")


def _dlq_init():
    """Create the DLQ table if it does not already exist."""
    os.makedirs(os.path.dirname(DLQ_PATH), exist_ok=True)
    with sqlite3.connect(DLQ_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS failed_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                account     TEXT    NOT NULL,
                event_time  TEXT    NOT NULL,
                payload     TEXT    NOT NULL,
                last_error  TEXT,
                attempts    INTEGER DEFAULT 0,
                created_at  TEXT    DEFAULT (datetime('now')),
                resolved    INTEGER DEFAULT 0
            )
        """)
        conn.commit()
    log.info(f"DLQ initialised at {DLQ_PATH}")


def _dlq_write(account_label: str, payload: dict, error: str):
    """Persist a failed payload to the dead-letter queue."""
    try:
        with sqlite3.connect(DLQ_PATH) as conn:
            conn.execute(
                "INSERT INTO failed_events "
                "(account, event_time, payload, last_error, attempts) VALUES (?,?,?,?,?)",
                (
                    account_label,
                    payload.get("timestamp", datetime.utcnow().isoformat()),
                    json.dumps(payload, default=str),
                    str(error),
                    WEBHOOK_MAX_RETRIES,
                ),
            )
            conn.commit()
        log.warning(
            f"[DLQ] Stored failed event for [{account_label}] — "
            f"{payload.get('event')} {payload.get('symbol')}"
        )
    except Exception as db_err:
        log.error(f"[DLQ] Could not write to SQLite: {db_err}")


def dlq_pending_count() -> int:
    """Return count of unresolved DLQ entries (used by health endpoint)."""
    try:
        with sqlite3.connect(DLQ_PATH) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM failed_events WHERE resolved=0"
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return -1


# ── POST to n8n with retry + stagger ──────────────────────────────────────────
async def post_to_n8n(payload: dict):
    """
    POST payload to n8n webhook with:
      • per-account serialisation lock (no concurrent bursts for same account)
      • minimum inter-call stagger (WEBHOOK_MIN_INTERVAL_S)
      • exponential-backoff retry up to WEBHOOK_MAX_RETRIES attempts
      • dead-letter queue on final failure (no event is silently lost)
    """
    label = payload.get("account_label", "unknown")
    lock = _get_account_lock(label)

    async with lock:
        # Stagger: enforce minimum gap since last successful call ────────
        elapsed = time.monotonic() - _account_last_call.get(label, 0.0)
        wait_s = WEBHOOK_MIN_INTERVAL - elapsed
        if wait_s > 0:
            log.debug(f"[{label}] Staggering webhook call by {wait_s:.3f}s")
            await asyncio.sleep(wait_s)

        last_error: Exception | None = None
        for attempt in range(1, WEBHOOK_MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        WEBHOOK,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=WEBHOOK_TIMEOUT_S),
                    ) as resp:
                        _account_last_call[label] = time.monotonic()
                        if resp.status == 200:
                            log.debug(f"[{label}] Webhook OK (attempt {attempt})")
                            return
                        last_error = ValueError(f"HTTP {resp.status}")
                        log.warning(
                            f"[{label}] n8n returned HTTP {resp.status} "
                            f"(attempt {attempt}/{WEBHOOK_MAX_RETRIES})"
                        )
            except Exception as exc:
                last_error = exc
                log.warning(
                    f"[{label}] Webhook attempt {attempt}/{WEBHOOK_MAX_RETRIES} failed: {exc}"
                )

            if attempt < WEBHOOK_MAX_RETRIES:
                backoff = WEBHOOK_RETRY_BASE_S * (2 ** (attempt - 1))
                log.info(f"[{label}] Retrying in {backoff:.1f}s …")
                await asyncio.sleep(backoff)

        # All retries exhausted → DLQ ────────────────────────────────────
        log.error(
            f"[{label}] Webhook failed after {WEBHOOK_MAX_RETRIES} attempts. "
            f"Sending to dead-letter queue. Last error: {last_error}"
        )
        _dlq_write(label, payload, str(last_error))


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
            "qty":                  safe(getattr(data, "qty", None)),

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
            "order_qty":            safe(order.qty),
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
            "legs": [safe(leg) for leg in (order.legs or [])] if getattr(order, "legs", None) else [],
        }

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
        pending = dlq_pending_count()
        body = json.dumps({"status": "ok", "dlq_pending": pending}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    log.info(f"Health check server on port {PORT}")
    server.serve_forever()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    _dlq_init()
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
