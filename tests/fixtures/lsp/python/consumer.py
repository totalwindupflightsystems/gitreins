# Cross-file type error — consumer.py
# ERROR: passes str where fetch_user expects int for user_id.
# mypy catches this across files because it analyzes ALL files together.
# LSP (single-file) CANNOT see this error — consumer.py has no visible
# type violation in isolation (the violation is in the cross-file call).

from python.models import fetch_user


class FakeRepo:
    def get_user(self, user_id: int) -> dict[str, str]:
        return {"id": str(user_id), "name": "Test"}


repo = FakeRepo()
result = fetch_user(repo, "not-an-int")  # ❌ cross-file type error
print(result)
