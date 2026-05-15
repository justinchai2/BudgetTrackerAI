"""
Plaid webhook receiver + re-auth web UI — runs as an aiohttp server alongside the Discord bot.

Routes:
  POST /plaid/webhook   — receives Plaid event notifications
  GET  /reauth          — serves Plaid Link page for re-authorization (?token=link-xxx&bank=Chase)
  GET  /oauth-return    — OAuth redirect URI for banks like Chase / Capital One
  GET  /health          — simple liveness check

Plaid webhook events handled:
  ITEM / ERROR               — item has an error (e.g. ITEM_LOGIN_REQUIRED)
  ITEM / PENDING_EXPIRATION  — OAuth token expiring in ~7 days
  ITEM / USER_PERMISSION_REVOKED — user revoked data access
  TRANSACTIONS / SYNC_UPDATES_AVAILABLE — new transactions available
"""

import json
import hmac
import hashlib
from aiohttp import web
from config import PLAID_WEBHOOK_SECRET, WEBHOOK_PORT

# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _verify_signature(body_bytes: bytes, headers: dict) -> bool:
    """
    Verify Plaid's HMAC-SHA256 webhook signature.
    Skipped if PLAID_WEBHOOK_SECRET is not configured (dev mode).
    """
    if not PLAID_WEBHOOK_SECRET:
        return True
    sig_header = headers.get("Plaid-Verification", "")
    expected   = hmac.new(
        PLAID_WEBHOOK_SECRET.encode(),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig_header)

# ---------------------------------------------------------------------------
# Re-auth HTML page
# ---------------------------------------------------------------------------

def _reauth_page(link_token: str, bank: str, oauth_redirect_uri: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Re-authorize {bank} — BudgetTrackerAI</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; min-height: 100vh; margin: 0;
      background: #1a1a2e; color: #eee;
    }}
    h1 {{ font-size: 1.4rem; margin-bottom: 0.5rem; }}
    p  {{ color: #aaa; margin-bottom: 2rem; }}
    button {{
      background: #5865f2; color: white; border: none;
      padding: 0.8rem 2rem; border-radius: 8px;
      font-size: 1rem; cursor: pointer;
    }}
    button:hover {{ background: #4752c4; }}
    #status {{ margin-top: 1.5rem; color: #aaa; font-size: 0.9rem; }}
  </style>
</head>
<body>
  <h1>Re-authorize {bank}</h1>
  <p>Click the button below to reconnect your bank account.</p>
  <button id="open-link">Connect {bank}</button>
  <div id="status"></div>

  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <script>
    const handler = Plaid.create({{
      token: "{link_token}",
      receivedRedirectUri: window.location.href.includes("oauth_state_id")
                           ? window.location.href : undefined,
      onSuccess: (public_token, metadata) => {{
        document.getElementById("status").textContent =
          "✅ Re-authorization successful! You can close this window.";
        document.getElementById("open-link").style.display = "none";
      }},
      onExit: (err, metadata) => {{
        if (err) {{
          document.getElementById("status").textContent =
            "❌ Error: " + JSON.stringify(err);
        }} else {{
          document.getElementById("status").textContent =
            "Window closed. Reopen the link from Discord if needed.";
        }}
      }},
    }});

    // Auto-open if returning from OAuth redirect
    if (window.location.href.includes("oauth_state_id")) {{
      handler.open();
    }}

    document.getElementById("open-link").addEventListener("click", () => {{
      handler.open();
    }});
  </script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

_callback        = None   # set by start_webhook_server()
_public_base_url = ""     # set by start_webhook_server()

async def _handle_webhook(request: web.Request) -> web.Response:
    body_bytes = await request.read()

    if not _verify_signature(body_bytes, dict(request.headers)):
        print("[webhook] Rejected request — invalid signature")
        return web.Response(status=401, text="Unauthorized")

    try:
        body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return web.Response(status=400, text="Invalid JSON")

    webhook_type = body.get("webhook_type", "")
    webhook_code = body.get("webhook_code", "")
    item_id      = body.get("item_id", "")
    error        = body.get("error") or {}

    print(f"[webhook] {webhook_type}/{webhook_code}  item={item_id}")

    if _callback:
        try:
            await _callback(webhook_type, webhook_code, item_id, error, body)
        except Exception as e:
            print(f"[webhook] Callback error: {e}")

    return web.Response(status=200, text="OK")

async def _handle_reauth(request: web.Request) -> web.Response:
    token = request.rel_url.query.get("token", "")
    bank  = request.rel_url.query.get("bank", "your bank")
    if not token:
        return web.Response(status=400, text="Missing token parameter")
    oauth_redirect = f"{_public_base_url}/oauth-return"
    return web.Response(
        content_type="text/html",
        text=_reauth_page(token, bank, oauth_redirect),
    )

async def _handle_oauth_return(request: web.Request) -> web.Response:
    """
    OAuth banks (Chase, Capital One) redirect here after login.
    We serve the same Plaid Link page — the JS detects oauth_state_id in the URL
    and automatically resumes the Link flow.
    """
    token = request.rel_url.query.get("link_token", "")
    bank  = request.rel_url.query.get("bank", "your bank")
    return web.Response(
        content_type="text/html",
        text=_reauth_page(token, bank, f"{_public_base_url}/oauth-return"),
    )

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def start_webhook_server(callback, public_base_url: str = "") -> web.AppRunner:
    """
    Start the aiohttp webhook + re-auth server.
    public_base_url: the publicly reachable base URL (e.g. https://xxxx.ngrok-free.app)
    """
    global _callback, _public_base_url
    _callback        = callback
    _public_base_url = public_base_url.rstrip("/")

    app = web.Application()
    app.router.add_post("/plaid/webhook",  _handle_webhook)
    app.router.add_get("/reauth",          _handle_reauth)
    app.router.add_get("/oauth-return",    _handle_oauth_return)
    app.router.add_get("/health",          lambda r: web.Response(text="OK"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    print(f"[webhook] Server listening on port {WEBHOOK_PORT}")
    return runner
