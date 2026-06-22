# Python LSP Test Fixture
# LSP: pyright
# Expected diagnostic: Argument of type "str" cannot be assigned to parameter "user_id" of type "int"

def get_user(user_id: int) -> dict:
    return {"id": user_id, "name": "Alice"}

# ERROR: passing string where int is expected
result = get_user("abc123")
