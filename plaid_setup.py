"""
One-time setup script — run this once per bank to get your Plaid access tokens.
It starts a local server, opens Plaid Link in your browser, and prints the
access token to paste into your .env file.

Usage:
    python plaid_setup.py
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

CALLBACK_PORT = 8080
captured_public_token = None

def get_client():
    configuration = plaid.Configuration(
        host=ENV_MAP.get(PLAID_ENV, plaid.Environment.Sandbox),
        api_key={"clientId": PLAID_CLIENT_ID, "secret": PLAID_SECRET},
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))

def create_link_token():
    client = get_client()
    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id="budget-tracker-user"),
        client_name="BudgetTrackerAI",
        products=[Products("transactions")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    response = client.link_token_create(request)
    return response["link_token"]

def exchange_public_token(public_token):
    client = get_client()
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return response["access_token"]

# Minimal HTML page that runs Plaid Link and redirects with the public token
LINK_PAGE = """
<!DOCTYPE html>
<html>
<head><title>BudgetTrackerAI — Link Bank</title></head>
<body>
<h2>Connecting your bank...</h2>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
  const handler = Plaid.create({{
    token: "{link_token}",
    onSuccess: function(public_token, metadata) {{
      window.location = "/callback?public_token=" + public_token + "&institution=" + encodeURIComponent(metadata.institution.name);
    }},
    onExit: function(err) {{
      document.body.innerHTML = "<h2>Cancelled.</h2><p>Close this window and try again.</p>";
    }}
  }});
  handler.open();
</script>
</body>
</html>
"""

class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global captured_public_token
        parsed = urlparse(self.path)

        if parsed.path == "/":
            link_token = create_link_token()
            html = LINK_PAGE.format(link_token=link_token)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

        elif parsed.path == "/callback":
            params = parse_qs(parsed.query)
            public_token  = params.get("public_token", [None])[0]
            institution   = params.get("institution", ["Unknown Bank"])[0]

            if public_token:
                access_token = exchange_public_token(public_token)
                captured_public_token = access_token

                # Show result in browser
                env_key = institution.upper().replace(" ", "_")
                html = f"""
                <html><body>
                <h2>✅ {institution} connected!</h2>
                <p>Add this to your <strong>.env</strong> file:</p>
                <pre>PLAID_ACCESS_TOKEN_{env_key}={access_token}</pre>
                <p>You can close this window.</p>
                </body></html>
                """
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode())

                print(f"\n✅  {institution} connected!")
                print(f"    Add to .env:  PLAID_ACCESS_TOKEN_{env_key}={access_token}\n")

                # Shut down server after a short delay
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_response(400)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress default request logs

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
    print("=" * 55)
    print("  BudgetTrackerAI — Plaid Bank Setup")
    print("=" * 55)

    validate_credentials()

    print(f"\nEnvironment: {PLAID_ENV}")
    print("\nStarting local server on http://localhost:8080 ...")
    print("Opening Plaid Link in your browser.\n")
    print("Run this script once for each bank:")
    print("  1. Chase")
    print("  2. Citi")
    print("  3. Capital One\n")

    webbrowser.open(f"http://localhost:{CALLBACK_PORT}/")
    server = HTTPServer(("localhost", CALLBACK_PORT), CallbackHandler)
    server.serve_forever()

if __name__ == "__main__":
    main()
