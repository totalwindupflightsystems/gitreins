"""
secret-leak / config.py

Reliability benchmark — INTENTIONAL FLAWS.

Companion to auth.py — additional hardcoded-secret anti-patterns.
This file represents a typical "generated config module" that an AI
helper might produce when asked to "set up the integration with
[service]". Every constant below embeds a credential directly in
source instead of reading from os.environ, a vault, or a secrets
manager.

The criteria.json in this directory defines the GitReins acceptance
criteria — every criterion below is expected to FAIL.
"""

# ── Flaw 1: Stripe API key (live mode, sk_live_) ──────────────────────────────
STRIPE_API_KEY: str = "sk_live_GITREINS_BENCHMARK_FAKE_KEY_not_real"

# ── Flaw 2: SendGrid API key ─────────────────────────────────────────────────
SENDGRID_API_KEY: str = "SG.pEu5WPhRRYm3rUJBB8xvFw.K3exampleKEYnotreal7q2exampleKEYnotreal"

# ── Flaw 3: Slack bot token in a config object ────────────────────────────────
SLACK_CONFIG: dict = {
    "bot_token":   "xoxb-GITREINS-BENCHMARK-FAKE-TOKEN-not-real",
    "signing_secret": "a]z]B]C]D]E]F]G]H]I]J]K]L]M]N]O]P]Q]R]S]T]U",
    "default_channel": "#general",
}

# ── Flaw 4: Twilio account SID + auth token as constants ──────────────────────
TWILIO_ACCOUNT_SID: str = "AC00000000000000000000000000000000"
TWILIO_AUTH_TOKEN:  str = "00000000000000000000000000000000"

# ── Flaw 5: `password = "..."` inside an integration function ─────────────────


def connect_to_smtp():
    """Return an SMTP connection object.

    FLAW: the username and password are baked into the function body
    as string literals. An AI generator that "just wires up the
    integration" typically writes them inline.
    """
    import smtplib
    username = "alerts@example.com"
    password = "Smtp-P@ssw0rd-2024!"     # ← hardcoded
    server = smtplib.SMTP("smtp.example.com", 587)
    server.login(username, password)
    return server


# ── Flaw 6: bearer token in a function that calls a private API ──────────────


def fetch_billing_report() -> dict:
    """GET the private billing endpoint.

    FLAW: the bearer token is hardcoded inside the function rather
    than loaded from an env var. Anyone with read access to the
    source has the credential.
    """
    import urllib.request
    api_token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    req = urllib.request.Request(
        "https://billing.internal.example.com/api/v1/report",
        headers={"Authorization": api_token},
    )
    with urllib.request.urlopen(req) as resp:
        import json
        return json.loads(resp.read().decode("utf-8"))


# ── Flaw 7: oauth client_secret as a tuple in a constants block ──────────────
GOOGLE_OAUTH: tuple = (
    "my-google-client-id-9999.apps.googleusercontent.com",
    "GOCSPX-AbCdEf1234567890_FAKE",        # ← client secret
    "https://auth.example.com/callback",
)