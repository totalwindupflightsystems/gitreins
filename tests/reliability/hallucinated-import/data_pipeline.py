"""
hallucinated-import / data_pipeline.py

Reliability benchmark — INTENTIONAL FLAWS.

Companion to image_pipeline.py — more variants of the hallucinated-import
anti-pattern. The module fails to load because every top-level import
points to a package or symbol that does not exist on PyPI, is misspelled,
or is not installed in the runtime environment.

The criteria.json in this directory defines the GitReins acceptance
criteria — every criterion below is expected to FAIL.
"""

# ── Flaw 1: misspelled top-level package alias ────────────────────────────────
import pandas_as  # noqa: F401  ← wrong package; correct is `import pandas as pd`

# ── Flaw 2: package not installed (async file IO) ─────────────────────────────
import aiofiles  # noqa: F401  ← not installed in this environment

# ── Flaw 3: async-requests package that does not exist ────────────────────────
import requests_async  # noqa: F401  ← package does not exist; correct is `httpx` / `aiohttp`

# ── Flaw 4: financial-NumPy extension under wrong / undeclared name ────────────
import numpy_financial  # noqa: F401  ← undeclared dependency

# ── Flaw 5: deep-learning framework assumed installed ────────────────────────
from tensorflow.keras.models import Sequential  # noqa: F401  ← not installed


# ── Functions (never reached because the module fails to load) ───────────────


def load_csv(path: str):
    """Load a CSV file as a DataFrame-like object."""
    return pandas_as.read_csv(path)  # type: ignore[attr-defined]


async def read_file_async(path: str) -> str:
    """Asynchronously read a text file."""
    async with aiofiles.open(path, mode="r") as f:  # type: ignore[attr-defined]
        return await f.read()


async def fetch_json_async(url: str) -> dict:
    """GET a URL and return the parsed JSON."""
    response = await requests_async.get(url)  # type: ignore[attr-defined]
    return response.json()


def compute_irr(cashflows: list[float]) -> float:
    """Compute the internal rate of return for a cashflow series."""
    return float(numpy_financial.irr(cashflows))  # type: ignore[attr-defined]


def build_keras_model(input_dim: int, output_dim: int):
    """Build a tiny Keras Sequential model."""
    from tensorflow.keras.layers import Dense  # type: ignore[import-not-found]
    model = Sequential([  # type: ignore[name-defined]
        Dense(64, activation="relu", input_dim=input_dim),
        Dense(output_dim, activation="softmax"),
    ])
    return model
