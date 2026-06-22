# Cross-file type error — models.py
# mypy catches this because it analyzes ALL files together.
# LSP (single-file) cannot see that consumer.py will pass str where int is expected.

from typing import Protocol


class UserRepository(Protocol):
    def get_user(self, user_id: int) -> dict[str, str]: ...


def fetch_user(repo: UserRepository, user_id: int) -> dict[str, str]:
    return repo.get_user(user_id)
