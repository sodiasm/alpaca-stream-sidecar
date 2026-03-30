# Alpaca Trade Stream Sidecar

A lightweight Python sidecar service that connects to the **Alpaca Trading WebSocket API** and forwards trade update events to **n8n** via webhook. Supports **multiple Alpaca accounts** running concurrently.

---

## Architecture

```
Alpaca WebSocket
      ↓
 alpaca-stream (this service)
      ↓  POST /webhook/alpaca-trades  (retry + stagger)
    n8n service
      ↓
 Your workflow logic (alerts, logging, order management...)
```

Both services should be deployed in the **same Zeabur project** so the sidecar can reach n8n via the internal hostname without public internet exposure.

---

## Features

- 🔄 **Persistent WebSocket** connection with auto-reconnect (handled by `alpaca-py`)
- 👥 **Multi-account** support — stream any number of Alpaca accounts simultaneously
- 🏷️ **Account labelling** — each n8n payload includes `account_label` to route per account in n8n
- 🩺 **Health check HTTP server** — required for Zeabur to confirm the service is alive
- 📦 **Binary frame support** — uses `alpaca-py`'s native `TradingStream` which handles MessagePack encoding
- 🔒 **No secrets in code** — all credentials loaded from environment variables
- ♻️ **Retry with exponential backoff** — up to `WEBHOOK_MAX_RETRIES` attempts before logging failure
- 🚦 **Staggered webhook calls** — per-account lock + minimum interval prevents burst flooding

---

## Files

```
alpaca-stream-sidecar/
├── stream.py          # Main application
├── requirements.txt   # Python dependencies
├── zbpack.json        # Zeabur build config
├── .env.example       # Environment variable template
└── .gitignore
```

---

## Environment Variables

Copy `.env.example` to `.env` for local development. On Zeabur, set these in the **Variables** tab of the service.

### Global Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `N8N_WEBHOOK_URL` | ✅ | — | Full URL of your n8n Webhook Trigger node |
| `ACCOUNTS` | ✅ | — | Comma-separated account labels e.g. `acc1,acc2` |
| `PORT` | ❌ | `8080` | Health check server port |
| `WEBHOOK_MAX_RETRIES` | ❌ | `3` | Max POST attempts before logging failure |
| `WEBHOOK_RETRY_BASE_S` | ❌ | `1.0` | Backoff base in seconds (doubles each attempt) |
| `WEBHOOK_TIMEOUT_S` | ❌ | `5.0` | Per-request HTTP timeout in seconds |
| `WEBHOOK_MIN_INTERVAL_S` | ❌ | `0.25` | Minimum gap between consecutive calls per account |

### Per-Account Variables

For each label defined in `ACCOUNTS`, add four variables using the label as the prefix (uppercased):

| Variable | Required | Description |
|---|---|---|
| `{PREFIX}_KEY` | ✅ | Alpaca API Key ID |
| `{PREFIX}_SECRET` | ✅ | Alpaca API Secret Key |
| `{PREFIX}_LABEL` | ❌ | Human-readable name sent in webhook payload (default: prefix name) |
| `{PREFIX}_IS_PAPER` | ❌ | `true` for paper trading, `false` for live (default: `true`) |

### Example (2 accounts)

```env
N8N_WEBHOOK_URL=http://n8n.zeabur.internal:5678/webhook/alpaca-trades
PORT=8080
ACCOUNTS=acc1,acc2

# Retry / stagger tuning (optional)
WEBHOOK_MAX_RETRIES=3
WEBHOOK_RETRY_BASE_S=1.0
WEBHOOK_TIMEOUT_S=5.0
WEBHOOK_MIN_INTERVAL_S=0.25

ACC1_KEY=PKXXXXXXXXXXXXXXXXXX
ACC1_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ACC1_LABEL=Main Account Paper
ACC1_IS_PAPER=true

ACC2_KEY=AKYYYYYYYYYYYYYYYYYY
ACC2_SECRET=yyyyyyyyyyyyyyyyyyyyyyyyyyyyyy
ACC2_LABEL=Live Account
ACC2_IS_PAPER=false
```

---

## Retry & Stagger Behaviour

### Retry with Exponential Backoff

Every webhook POST is attempted up to `WEBHOOK_MAX_RETRIES` times. Both network exceptions and non-200 HTTP responses trigger a retry. The sleep between attempts follows:

```
backoff = WEBHOOK_RETRY_BASE_S × 2^(attempt - 1)
```

With the default 1 s base: **1 s → 2 s → 4 s** before giving up.

If all retries are exhausted the full event payload is logged at `ERROR` level so it remains visible in your Zeabur or n8n log stream.

### Staggered Calls

Two mechanisms prevent burst flooding — particularly relevant when bracket/OCO orders fire several fill events in rapid succession:

- **Per-account `asyncio.Lock`** — serialises concurrent coroutines for the same account so they queue rather than pile on.
- **`WEBHOOK_MIN_INTERVAL_S`** — enforces a minimum wall-clock gap between consecutive POSTs for the same account.

---

## n8n Webhook Payload

Each trade update event sends a POST request with this JSON body:

```json
{
  "account_label":    "Main Account Paper",
  "account_name":     "acc1",
  "is_paper":         true,
  "event":            "fill",
  "timestamp":        "2026-02-20 02:49:36+00:00",
  "order_id":         "abc123",
  "client_order_id":  "client-xyz",
  "symbol":           "AAPL",
  "side":             "buy",
  "type":             "market",
  "qty":              "10",
  "filled_qty":       "10",
  "filled_avg_price": "185.23",
  "status":           "filled",
  "time_in_force":    "day",
  "limit_price":      "None",
  "stop_price":       "None"
}
```

### Trade Event Types

| Event | Description |
|---|---|
| `new` | Order routed to exchange |
| `fill` | Order fully filled |
| `partial_fill` | Order partially filled |
| `canceled` | Cancel confirmed |
| `expired` | Order expired (time-in-force reached) |
| `rejected` | Order rejected by exchange |
| `replaced` | Order replace confirmed |

---

## Health Check

`GET /` returns:

```json
{"status": "ok"}
```

No volume mount or persistent storage required.

---

## Deploy to Zeabur

### Option A — GitHub (Recommended)

1. Fork or clone this repo
2. Open your **existing Zeabur project** (where n8n is already deployed)
3. Click **Add Service → GitHub** and select this repo
4. Set all environment variables in the **Variables** tab
5. Zeabur auto-detects Python and deploys

### Option B — Local Upload

1. Open your existing Zeabur project
2. Click **Add Service → Local Project**
3. Drag and drop this project folder
4. Set environment variables in the **Variables** tab

### Option C — Zeabur CLI

```bash
npx zeabur@latest auth login
npx zeabur@latest deploy
```

### Networking

- Expose **port `8080`** in the service's Networking tab (health check)
- No public domain needed — the sidecar only makes outbound calls to n8n
- Use the **internal hostname** for `N8N_WEBHOOK_URL` if both services are in the same Zeabur project:
  ```
  http://n8n.zeabur.internal:5678/webhook/alpaca-trades
  ```

---

## n8n Workflow Design

After the **Webhook Trigger** node, use a **Switch** node to route by account and event:

```
Webhook Trigger
      ↓
Switch (by account_label)
  ├── "Main Account Paper"
  │       ↓
  │   Switch (by event)
  │     ├── fill         → log / notify
  │     ├── partial_fill → update tracker
  │     └── rejected     → alert
  │
  └── "Live Account"
          ↓
      Switch (by event)
        ├── fill         → log / notify / execute hedge
        └── canceled     → retry logic
```

---

## Local Development

```bash
# 1. Clone the repo
git clone https://github.com/sodiasm/alpaca-stream-sidecar.git
cd alpaca-stream-sidecar

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and fill in environment variables
cp .env.example .env
# Edit .env with your actual keys

# 5. Run
python stream.py
```

---

## Expected Startup Logs

```
Loaded account: [Main Account Paper] paper=True
Loaded account: [Live Account] paper=False
Health check server on port 8080
Starting stream for [Main Account Paper] (paper=True)
Starting stream for [Live Account] (paper=False)
Streaming 2 account(s): ['Main Account Paper', 'Live Account']
connected to: BaseURL.TRADING_STREAM_PAPER
connected to: BaseURL.TRADING_STREAM
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `alpaca-py` | Official Alpaca Python SDK — handles WebSocket, auth, binary frames, reconnect |
| `aiohttp` | Async HTTP client for posting events to n8n |

---

## License

MIT
