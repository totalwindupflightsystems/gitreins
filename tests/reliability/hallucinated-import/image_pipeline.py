"""
hallucinated-import / image_pipeline.py

Reliability benchmark — INTENTIONAL FLAWS.

This module demonstrates the "AI code generator hallucinates a module name"
anti-pattern. The very first thing the interpreter does — load the
module — fails, because the imported names do not exist on PyPI, are
misspelled, or live behind a different package.

The criteria.json in this directory defines the GitReins acceptance
criteria — every criterion below is expected to FAIL because every
top-level import in this module raises ModuleNotFoundError or ImportError.

Do not use as a template — this code is deliberately broken.
"""

# ── Flaw 1: misspelled submodule (`PIL.Imag` instead of `PIL.Image`) ──────────
from PIL import Imag  # noqa: F401  ← wrong name; correct is `Image`

# ── Flaw 2: package does not exist (`BeautifulSoup` instead of `bs4`) ─────────
import BeautifulSoup  # noqa: F401  ← wrong top-level package; correct is `from bs4 import BeautifulSoup`

# ── Flaw 3: misspelled sklearn estimator ──────────────────────────────────────
from sklearn.ensemble import GradientBoostedRegressor  # noqa: F401  ← typo; correct is `GradientBoostingRegressor` (trailing "ing")

# ── Flaw 4: deeply nested hallucinated submodule ──────────────────────────────
from sklearn.neural_network import DeepConvolutionalNetwork  # noqa: F401  ← hallucinated; sklearn exposes `MLPClassifier` only


# ── Functions (never reached because the module fails to load) ───────────────


def resize_image(path: str, width: int, height: int) -> bytes:
    """Resize an image to width x height and return PNG bytes."""
    with Imag.open(path) as img:  # type: ignore[name-defined]
        return img.resize((width, height)).tobytes()


def extract_links(html: str) -> list[str]:
    """Parse an HTML string and return all <a href="..."> links."""
    soup = BeautifulSoup.BeautifulSoup(html, "html.parser")  # type: ignore[attr-defined]
    return [a.get("href") for a in soup.find_all("a") if a.get("href")]


def train_boosted_model(X, y):
    """Fit a gradient-boosted regressor and return the trained estimator."""
    model = GradientBoostedRegressor(n_estimators=100, learning_rate=0.1)  # type: ignore[call-arg]
    model.fit(X, y)
    return model


def build_deep_classifier(input_shape: tuple, num_classes: int):
    """Construct a deep convolutional classifier."""
    return DeepConvolutionalNetwork(input_shape=input_shape, num_classes=num_classes)  # type: ignore[call-arg]