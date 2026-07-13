"""A test module with intentional issues for code review — FIXED."""
import sqlite3

# FIXED: SQL injection → parameterized query + connection cleanup
def get_user(user_id, conn=None):
    query = "SELECT * FROM users WHERE id = ?"
    should_close = conn is None
    if conn is None:
        conn = sqlite3.connect(":memory:")
    try:
        return conn.execute(query, (user_id,)).fetchone()
    finally:
        if should_close:
            conn.close()

# FIXED: mutable default → None + init pattern
def add_item(item, items=None):
    if items is None:
        items = []
    items.append(item)
    return items

# FIXED: bare except → specific exception
def parse_config(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""

# SECURITY: hardcoded API key pattern (test-only but flaggable)
DEFAULT_KEY = "sk-test-12345678901234567890"

# STYLE: magic number
TAX_RATE = 1.0825

def calculate(price):
    return price * TAX_RATE
