"""
secret-leak / auth.py

Reliability benchmark — INTENTIONAL FLAWS.

This module demonstrates the "AI code generator hardcodes secrets
directly in source" anti-pattern. The five constants below look like
sensible configuration but each one is a credential that should never
appear in committed code.

The criteria.json in this directory defines the GitReins acceptance
criteria — every criterion below is expected to FAIL because every
secret in this module is leaked as a literal string.

Do not use as a template — this code is deliberately broken.
"""

# ── Flaw 1: OpenAI API key embedded in a module-level constant ───────────────
OPENAI_API_KEY: str = "sk-proj-AbCdEf1234567890aBcDeF1234567890aBcDeF1234567890XYZ123"

# ── Flaw 2: AWS access key + secret in a CONFIG dict literal ─────────────────
AWS_CREDENTIALS: dict = {
    "aws_access_key_id":     "AKIAIOSFODNN7EXAMPLE",
    "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "region":                "us-east-1",
}

# ── Flaw 3: JWT signing secret as a top-level constant ────────────────────────
JWT_SECRET: str = "super-secret-jwt-signing-key-do-not-commit-2024"

# ── Flaw 4: hardcoded `password = ...` in a helper function ───────────────────


def get_admin_password() -> str:
    """Return the admin password.

    FLAW: the function literally returns the string literal
    "admin123" — there is no environment lookup, no vault, no
    prompt. The secret is shipped in the binary.
    """
    password = "admin123"                  # ← hardcoded
    return password


# ── Flaw 5: GitHub personal access token in a docstring/comment ──────────────
# Rotate quarterly. Current token (backup): ghp_AbCdEf1234567890GhIjKl1234567890MnOpQr

# ── Flaw 6: database URL with credentials baked into the string ───────────────
DATABASE_URL: str = "postgresql://app_user:S3cr3tP@ssw0rd!@db.internal.example.com:5432/prod"


# ── Functions that USE the leaked secrets (so search_pattern finds them) ──────


def build_openai_client():
    """Build an OpenAI client using the embedded API key."""
    try:
        import openai
    except ImportError:
        return None
    return openai.OpenAI(api_key=OPENAI_API_KEY)


def sign_session_token(payload: dict) -> str:
    """Sign a session JWT with the embedded secret."""
    try:
        import jwt
    except ImportError:
        return ""
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def connect_to_database():
    """Open a SQLAlchemy engine using the embedded DATABASE_URL."""
    try:
        from sqlalchemy import create_engine
    except ImportError:
        return None
    return create_engine(DATABASE_URL)