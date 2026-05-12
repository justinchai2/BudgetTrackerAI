"""
One-time setup script — run this once per bank to get your Plaid access tokens.
Starts a local server, opens Plaid Link in your browser, and prints the
access token to paste into your .env file.

Usage:
    python plaid_setup.py

Works for any bank Plaid supports. Handles both standard and OAuth banks
(Chase, Bank of America, Wells Fargo, etc.) in production.

Production setup:
    1. Set PLAID_ENV=production and your production PLAID_SECRET in .env
    2. Register http://localhost:8080/oauth-return in Plaid dashboard:
       Dashboard → Team Settings → API → Redirect URIs
    3. Run this script for each bank you want to connect
"""

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import plaid
from plaid.api import plaid_api
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.products import Products
from plaid.model.country_code import CountryCode

from config import PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV

ENV_MAP = {
    "sandbox":    plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}

CALLBACK_PORT   = 8080
REDIRECT_URI    = f"http://localhost:{CALLBACK_PORT}/oauth-return"
IS_PRODUCTION   = PLAID_ENV == "production"

captured_public_token = None

def get_client():
    configuration = plaid.Configuration(
        host=ENV_MAP.get(PLAID_ENV, plaid.Environment.Sandbox),
        api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))

def create_link_token():
    client  = get_client()
    kwargs  = dict(
        user=LinkTokenCreateRequestUser(client_user_id="budget-tracker-user"),
        client_name="BudgetTrackerAI",
        products=[Products("transactions")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    # Production OAuth banks (Chase, BofA, Wells Fargo, etc.) require redirect_uri
    if IS_PRODUCTION:
        kwargs["redirect_uri"] = REDIRECT_URI

    response = client.link_token_create(LinkTokenCreateRequest(**kwargs))
    return response["link_token"]

def exchange_public_token(public_token):
    client   = get_client()
    request  = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return response["access_token"]

# Main Plaid Link page
LINK_PAGE = """<!DOCTYPE html>
<html>
<head><title>BudgetTrackerAI — Link Bank</title></head>
<body>
<h2>Connecting your bank...</h2>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
  var handler = Plaid.create({{
    token: "{link_token}",
    onSuccess: function(public_token, metadata) {{
      window.location = "/callback?public_token=" + public_token
        + "&institution=" + encodeURIComponent(metadata.institution.name);
    }},
    onExit: function(err) {{
      document.body.innerHTML = "<h2>Cancelled.</h2><p>Close this window and try again.</p>";
    }}
  }});
  handler.open();
</script>
</body>
</html>"""

# OAuth return page — re-initializes Plaid Link to complete the OAuth flow
OAUTH_RETURN_PAGE = """<!DOCTYPE html>
<html>
<head><title>BudgetTrackerAI — OAuth Return</title></head>
<body>
<h2>Completing bank connection...</h2>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
  var handler = Plaid.create({{
    token: "{link_token}",
    receivedRedirectUri: window.location.href,
    onSuccess: function(public_token, metadata) {{
      window.location = "/callback?public_token=" + public_token
        + "&institution=" + encodeURIComponent(metadata.institution.name);
    }},
    onExit: function(err) {{
      document.body.innerHTML = "<h2>Cancelled.</h2><p>Close this window and try again.</p>";
    }}
  }});
  handler.open();
</script>
</body>
</html>"""

# Store the link token so the OAuth return page can reuse it
_current_link_token = None

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global captured_public_token, _current_link_token
        parsed = urlparse(self.path)

        if parsed.path == "/":
            _current_link_token = create_link_token()
            html = LINK_PAGE.format(link_token=_current_link_token)
            self._respond(200, html)

        elif parsed.path == "/oauth-return":
            # OAuth bank returned — re-open Link with the same token + receivedRedirectUri
            html = OAUTH_RETURN_PAGE.format(link_token=_current_link_token or "")
            self._respond(200, html)

        elif parsed.path == "/callback":
            params       = parse_qs(parsed.query)
            public_token = params.get("public_token", [None])[0]
            institution  = params.get("institution", ["Unknown Bank"])[0]

            if public_token:
                access_token = exchange_public_token(public_token)
                captured_public_token = access_token

                env_key = institution.upper().replace(" ", "_")
                html = f"""<html><body>
<h2>&#x2705; {institution} connected!</h2>
<p>Add this to your <strong>.env</strong> file:</p>
<pre>PLAID_ACCESS_TOKEN_{env_key}={access_token}</pre>
<p>You can close this window and run the script again for another bank.</p>
</body></html>"""
                self._respond(200, html)

                print(f"\n✅  {institution} connected!")
                print(f"    Add to .env:  PLAID_ACCESS_TOKEN_{env_key}={access_token}\n")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._respond(400, "<html><body>Missing public_token.</body></html>")
        else:
            self._respond(404, "<html><body>Not found.</body></html>")

    def _respond(self, status, html):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress request logs

def validate_credentials():
    errors = []
    if not PLAID_CLIENT_ID or "your_" in str(PLAID_CLIENT_ID):
        errors.append("  ❌  PLAID_CLIENT_ID is missing or still a placeholder in .env")
    if not PLAID_SECRET or "your_" in str(PLAID_SECRET):
        errors.append("  ❌  PLAID_SECRET is missing or still a placeholder in .env")
    if errors:
        print("\n".join(errors))
        print("\nOpen BudgetTrackerAI/.env and fill in your Plaid credentials.")
        print("Get them from: https://dashboard.plaid.com → Team Settings → Keys\n")
        raise SystemExit(1)

def main():
    print("=" * 60)
    print("  BudgetTrackerAI — Plaid Bank Setup")
    print("=" * 60)
    validate_credentials()
    print(f"\nEnvironment: {PLAID_ENV.upper()}")

    if IS_PRODUCTION:
        print("\n⚠️  Production mode — make sure you have:")
        print("   1. Switched to your Production secret in .env")
        print("   2. Registered the redirect URI in Plaid dashboard:")
        print(f"      {REDIRECT_URI}")
        print("      (Dashboard → Team Settings → API → Redirect URIs)")

    print(f"\nStarting local server on http://localhost:{CALLBACK_PORT} ...")
    print("A browser window will open. Connect any bank Plaid supports.")
    print("Run this script once per bank. The access token prints here and in the browser.\n")

    webbrowser.open(f"http://localhost:{CALLBACK_PORT}/")
    server = HTTPServer(("localhost", CALLBACK_PORT), CallbackHandler)
    server.serve_forever()

if __name__ == "__main__":
    main()
