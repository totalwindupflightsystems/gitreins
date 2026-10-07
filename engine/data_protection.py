"""Configurable Tier-1 data-protection filters (GR-146).

Tier 1 already fails closed on **credentials** (``secrets``: gitleaks + the
built-in cross-check).  This module adds a SECOND, opt-in filter over the
*personal* and *network* data classes an agent can leak into a repository —
PII (emails, phone numbers, SSNs, passport numbers, medical and financial
identifiers) and IP literals / CIDR ranges — with a documented, per-category
policy for detection (``off``/``warn``/``block``) and safe handling
(``preserve``/``redact``/``replace``).

Design rules (why this shape):

* **Off by default.**  ``data_protection.enabled`` ships ``false`` and every
  category ships ``detection: off``: an existing repository is byte-for-byte
  unchanged until an operator opts in.  Backwards compatibility is the default
  contract, not a special case (AC1).
* **Credentials stay separate.**  The ``credentials`` category is inventoried
  but *not configurable here*: an exception or policy that could weaken the
  secrets scanner is rejected loudly (AC4, AC7).  gitleaks remains the single
  authority for secrets.
* **The same policy object drives both surfaces.**  The guard lane
  (``engine.guard_manager``) and the Tier-1 judge pipeline
  (``engine.pipeline.tier1_plan``) build the policy from the same
  ``data_protection:`` config block via :func:`build_policy`, so a judge verdict
  and a commit-time guard can never disagree (AC1).
* **Redaction is data-driven.**  :meth:`DataProtectionPolicy.redact_text`
  rewrites values in place for any handling mode other than ``preserve``; the
  guard and the verdict/run-log writers route their text through it so a canary
  value never survives into an artifact (AC5).
* **Synthetic data only.**  Nothing here ships a real name, address or
  credential; the fixtures under ``tests/fixtures/data_protection/`` are
  synthetic and labeled (AC6).

The module is intentionally dependency-free (stdlib only, like the rest of the
Tier-1 guard surface).
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

# ── Policy vocabulary ────────────────────────────────────────────────────────

DETECTION_OFF = "off"
DETECTION_WARN = "warn"
DETECTION_BLOCK = "block"
DETECTION_LEVELS: tuple[str, ...] = (DETECTION_OFF, DETECTION_WARN, DETECTION_BLOCK)

HANDLING_PRESERVE = "preserve"
HANDLING_REDACT = "redact"
HANDLING_REPLACE = "replace"
HANDLING_MODES: tuple[str, ...] = (HANDLING_PRESERVE, HANDLING_REDACT, HANDLING_REPLACE)

# Category ids.  ``pii`` and ``ip_addresses`` are the configurable detector
# categories; ``credentials`` is inventoried for the operator (AC7) but owned
# by the secrets scanner and never filtered here.
CATEGORY_PII = "pii"
CATEGORY_IP = "ip_addresses"
CATEGORY_CREDENTIALS = "credentials"
CONFIGURABLE_CATEGORIES: tuple[str, ...] = (CATEGORY_PII, CATEGORY_IP)

#: The redaction placeholder for a category, ``[REDACTED:<category>]`` (AC5).
REDACTION_PLACEHOLDER = "[REDACTED:{category}]"


class DataProtectionConfigError(ValueError):
    """A malformed or ambiguous ``data_protection:`` policy.

    Raised by :meth:`DataProtectionPolicy.validate` / :func:`build_policy`.
    The message always names the offending key and value so an operator can
    fix the config without reading this module (AC3).
    """


# ── Category inventory (AC7) ─────────────────────────────────────────────────


@dataclass(frozen=True)
class CategorySpec:
    """Documented inventory entry: which detector owns a data class."""

    name: str
    description: str
    detector: str
    configurable: bool
    classes: tuple[str, ...]
    supported: str
    limitations: str


# ── Detection rules ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DetectionRule:
    """One selectable detector rule (a PII class, or an IP shape)."""

    rule: str
    category: str
    description: str
    base_confidence: float
    placeholder: str
    supported: str
    limitations: str


@dataclass(frozen=True)
class _Match:
    rule: str
    start: int
    end: int
    value: str
    confidence: float


# -- PII patterns -------------------------------------------------------------
# Every pattern is synthetic-fixture-tested.  Confidences are deliberately
# *lower* for context-free identifiers (names, passports) so that the default
# confidence threshold (0.7) leaves them out until an operator opts into the
# noise — the documented sensitivity control (AC3).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[ .\-]?)?(?:\(\d{2,4}\)|\d{2,4})[ .\-]?\d{3,4}[ .\-]?\d{3,4}"
    r"(?!\.\d)(?![\w])"
)
_PHONE_E164_RE = re.compile(r"(?<![\w.])\+\d{7,15}(?![\w])")
_SSN_RE = re.compile(r"(?<!\d)(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?!\d)")
_PASSPORT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,2}\d{6,9}(?![A-Za-z0-9])")
_MEDICAL_RE = re.compile(
    r"(?i)\b(?:mrn|medical record(?: number)?|patient id|diagnosis(?: code)?|icd-?10)"
    r"\s*[:#]?\s*([A-Z]?\d{1,2}(?:\.\d{1,3})?)\b"
)
_CREDIT_CARD_CANDIDATE_RE = re.compile(r"(?<![\d.])\d(?:[ \-]?\d){12,18}(?![\d])")
_IBAN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}[A-Z0-9]{11,30}(?![A-Za-z0-9])")
_NAME_LABEL_RE = re.compile(
    r"(?i:\b(?:full name|patient name|client name|customer name|name))\s*[:=]\s*"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})"
)


def _luhn(number: str) -> bool:
    """Standard Luhn checksum over a digits-only string."""
    digits = [int(c) for c in number]
    if len(digits) < 13:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _valid_credit_card(value: str) -> bool:
    digits = re.sub(r"[ \-]", "", value)
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    if digits[0] not in "23456":
        return False
    return _luhn(digits)


def _valid_iban(value: str) -> bool:
    """IBAN mod-97 check (ISO 13616)."""
    compact = re.sub(r"\s", "", value).upper()
    if not (15 <= len(compact) <= 34):
        return False
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return False


def _iter_email(text: str) -> Iterator[_Match]:
    for m in _EMAIL_RE.finditer(text):
        yield _Match("email", m.start(), m.end(), m.group(0), 0.95)


def _iter_phone(text: str) -> Iterator[_Match]:
    seen: set[tuple[int, int]] = set()
    for pattern in (_PHONE_E164_RE, _PHONE_RE):
        for m in pattern.finditer(text):
            if (m.start(), m.end()) in seen:
                continue
            # A separator-laden run of 12+ digits is card/account-shaped, not a
            # phone number: the grouped pattern happily spans "4111 1111 1111
            # 1111" and "999.999.999.999". Measured FP class (see
            # docs/data-protection.md) — excluded here so phone stays
            # precision-first.
            if len(re.sub(r"\D", "", m.group(0))) >= 12:
                continue
            seen.add((m.start(), m.end()))
            yield _Match("phone", m.start(), m.end(), m.group(0), 0.6)


def _iter_ssn(text: str) -> Iterator[_Match]:
    for m in _SSN_RE.finditer(text):
        yield _Match("ssn", m.start(), m.end(), m.group(0), 0.9)


def _iter_passport(text: str) -> Iterator[_Match]:
    for m in _PASSPORT_RE.finditer(text):
        yield _Match("passport", m.start(), m.end(), m.group(0), 0.45)


def _iter_medical(text: str) -> Iterator[_Match]:
    for m in _MEDICAL_RE.finditer(text):
        # The capture group is the identifier; fall back to the whole match.
        span_start = m.start(1) if m.group(1) else m.start()
        span_end = m.end(1) if m.group(1) else m.end()
        value = text[span_start:span_end]
        yield _Match("medical", span_start, span_end, value, 0.55)


def _iter_financial(text: str) -> Iterator[_Match]:
    for m in _CREDIT_CARD_CANDIDATE_RE.finditer(text):
        if _valid_credit_card(m.group(0)):
            yield _Match("financial", m.start(), m.end(), m.group(0), 0.85)
    for m in _IBAN_RE.finditer(text):
        if _valid_iban(m.group(0)):
            yield _Match("financial", m.start(), m.end(), m.group(0), 0.85)


def _iter_names(text: str) -> Iterator[_Match]:
    for m in _NAME_LABEL_RE.finditer(text):
        start, end = m.start(1), m.end(1)
        yield _Match("names", start, end, text[start:end], 0.4)


_PII_ITERATORS: dict[str, Callable[[str], Iterator[_Match]]] = {
    "email": _iter_email,
    "phone": _iter_phone,
    "ssn": _iter_ssn,
    "passport": _iter_passport,
    "medical": _iter_medical,
    "financial": _iter_financial,
    "names": _iter_names,
}


# -- IP literals / CIDR -------------------------------------------------------
_IPV4_CANDIDATE_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d.])")
_IPV6_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}"
    r"(?:/\d{1,3})?(?![0-9A-Za-z:])"
)


def _iter_ipv4(text: str) -> Iterator[_Match]:
    for m in _IPV4_CANDIDATE_RE.finditer(text):
        token = m.group(0)
        if "/" in token:
            continue  # CIDR handled by the cidr rule
        try:
            ipaddress.IPv4Address(token)
        except ValueError:
            continue
        yield _Match("ipv4", m.start(), m.end(), token, 0.9)


def _iter_cidr(text: str) -> Iterator[_Match]:
    for pattern in (_IPV4_CANDIDATE_RE, _IPV6_CANDIDATE_RE):
        for m in pattern.finditer(text):
            token = m.group(0)
            if "/" not in token:
                continue
            try:
                ipaddress.ip_network(token, strict=False)
            except ValueError:
                continue
            yield _Match("cidr", m.start(), m.end(), token, 0.9)


def _iter_ipv6(text: str) -> Iterator[_Match]:
    for m in _IPV6_CANDIDATE_RE.finditer(text):
        token = m.group(0)
        if "/" in token:
            continue
        try:
            ipaddress.IPv6Address(token)
        except ValueError:
            continue
        yield _Match("ipv6", m.start(), m.end(), token, 0.9)


_IP_ITERATORS: dict[str, Callable[[str], Iterator[_Match]]] = {
    "ipv4": _iter_ipv4,
    "ipv6": _iter_ipv6,
    "cidr": _iter_cidr,
}

_ALL_ITERATORS: dict[str, Callable[[str], Iterator[_Match]]] = {
    **_PII_ITERATORS,
    **_IP_ITERATORS,
}


# -- Rule registry ------------------------------------------------------------

PII_RULES: tuple[DetectionRule, ...] = (
    DetectionRule(
        rule="email",
        category=CATEGORY_PII,
        description="email addresses",
        base_confidence=0.95,
        placeholder="[PII:EMAIL]",
        supported="user@host.tld with ASCII local part and 2+ char TLD",
        limitations=(
            "none observed on the synthetic corpus; quoted/escaped forms are not special-cased"
        ),
    ),
    DetectionRule(
        rule="phone",
        category=CATEGORY_PII,
        description="phone numbers (E.164 and grouped national forms)",
        base_confidence=0.6,
        placeholder="[PII:PHONE]",
        supported="+CC followed by 7-15 digits, and 2-4/3-4/3-4 digit groups",
        limitations=(
            "precision-only class: a bare 10-digit id can match; confidence 0.6 "
            "keeps it below the default threshold"
        ),
    ),
    DetectionRule(
        rule="ssn",
        category=CATEGORY_PII,
        description="US Social Security numbers",
        base_confidence=0.9,
        placeholder="[PII:SSN]",
        supported="NNN-NN-NNNN with area/group/serial validity ranges excluded",
        limitations="dashed form only; a separator-free 9-digit run is not classified",
    ),
    DetectionRule(
        rule="passport",
        category=CATEGORY_PII,
        description="passport-number-shaped identifiers",
        base_confidence=0.45,
        placeholder="[PII:PASSPORT]",
        supported="1-2 upper-case letters + 6-9 digits",
        limitations=(
            "shape-only and low confidence: also matches order/asset codes; "
            "disabled by the default threshold"
        ),
    ),
    DetectionRule(
        rule="medical",
        category=CATEGORY_PII,
        description="medical record numbers and labelled diagnosis codes",
        base_confidence=0.55,
        placeholder="[PII:MEDICAL]",
        supported="values labelled MRN/medical record/patient id/diagnosis/ICD-10",
        limitations="requires an explicit label; an unlabelled code is not detected",
    ),
    DetectionRule(
        rule="financial",
        category=CATEGORY_PII,
        description="payment-card and IBAN numbers",
        base_confidence=0.85,
        placeholder="[PII:FINANCIAL]",
        supported=(
            "13-19 digit Luhn-valid card numbers (issuer prefixes 2-6) and mod-97-valid IBANs"
        ),
        limitations="cheque/account numbers without a scheme checksum are out of scope",
    ),
    DetectionRule(
        rule="names",
        category=CATEGORY_PII,
        description="personal names after an explicit label",
        base_confidence=0.4,
        placeholder="[PII:NAME]",
        supported="2-4 capitalised words following name=/full name:/patient name: and friends",
        limitations="label-anchored only; low confidence, off by default",
    ),
)

IP_RULES: tuple[DetectionRule, ...] = (
    DetectionRule(
        rule="ipv4",
        category=CATEGORY_IP,
        description="IPv4 literals",
        base_confidence=0.9,
        placeholder="[IP:IPV4]",
        supported="dotted-quad with each octet 0-255 (validated, not merely regex-shaped)",
        limitations=(
            "a four-component version string (1.2.3.4) is syntactically a valid "
            "address; preserve_list entries are the escape hatch"
        ),
    ),
    DetectionRule(
        rule="ipv6",
        category=CATEGORY_IP,
        description="IPv6 literals",
        base_confidence=0.9,
        placeholder="[IP:IPV6]",
        supported="full and ::-compressed forms validated by the stdlib parser",
        limitations="must contain at least two colon groups to avoid matching clock times",
    ),
    DetectionRule(
        rule="cidr",
        category=CATEGORY_IP,
        description="IPv4/IPv6 CIDR ranges",
        base_confidence=0.9,
        placeholder="[IP:CIDR]",
        supported="address/prefix with a valid prefix length for the family",
        limitations="a CIDR whose inner literal is also present is counted once (range wins)",
    ),
)

ALL_RULES: tuple[DetectionRule, ...] = PII_RULES + IP_RULES
RULE_BY_NAME: dict[str, DetectionRule] = {r.rule: r for r in ALL_RULES}
RULES_BY_CATEGORY: dict[str, tuple[DetectionRule, ...]] = {
    CATEGORY_PII: PII_RULES,
    CATEGORY_IP: IP_RULES,
}

#: Default per-category confidence floor when ``confidence`` is not written.
#: PII is deliberately strict (0.7) so the noisy, context-free classes (names,
#: passports, phones) stay silent until an operator lowers the floor; IP shapes
#: are exact (0.0) because they are validated with the stdlib parser, not a
#: shape regex.  These are the documented defaults (AC3).
DEFAULT_CATEGORY_CONFIDENCE: dict[str, float] = {
    CATEGORY_PII: 0.7,
    CATEGORY_IP: 0.0,
}

CATEGORY_INVENTORY: tuple[CategorySpec, ...] = (
    CategorySpec(
        name=CATEGORY_CREDENTIALS,
        description="API keys, tokens, passwords, private keys",
        detector="secrets (gitleaks + built-in cross-check)",
        configurable=False,
        classes=("provider tokens", "high-entropy strings", "private-key blocks"),
        supported="always on; fail-closed; never filtered or excepted by data_protection",
        limitations="entropy/model heuristics — see docs/data-protection.md",
    ),
    CategorySpec(
        name=CATEGORY_PII,
        description="personally identifying information",
        detector="data_protection (this module)",
        configurable=True,
        classes=tuple(r.rule for r in PII_RULES),
        supported="labelled and format-bearing identifiers, confidence-filtered per class",
        limitations=(
            "regex-based: unlabelled/obfuscated values are not detected (measured in tests)"
        ),
    ),
    CategorySpec(
        name=CATEGORY_IP,
        description="network identifiers",
        detector="data_protection (this module)",
        configurable=True,
        classes=tuple(r.rule for r in IP_RULES),
        supported="IPv4/IPv6 literals and CIDR ranges, with an explicit preserve allowlist",
        limitations=(
            "cannot distinguish a documentation address from a real one without preserve_list"
        ),
    ),
)


# ── Policy dataclasses ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClassPolicy:
    """Per-class override inside a category (``classes.<rule>``)."""

    enabled: bool = True
    detection: str | None = None
    handling: str | None = None
    confidence: float | None = None

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "detection": self.detection,
            "handling": self.handling,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, value: object | None, *, where: str) -> "ClassPolicy":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise DataProtectionConfigError(f"{where} must be a mapping, got {value!r}")
        _reject_unknown_keys(value, {"enabled", "detection", "handling", "confidence"}, where)
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise DataProtectionConfigError(f"{where}.enabled must be true or false")
        detection = value.get("detection")
        handling = value.get("handling")
        confidence = value.get("confidence")
        if detection is not None:
            detection = _check_choice(detection, DETECTION_LEVELS, f"{where}.detection")
        if handling is not None:
            handling = _check_choice(handling, HANDLING_MODES, f"{where}.handling")
        if confidence is not None:
            confidence = _check_confidence(confidence, f"{where}.confidence")
        return cls(enabled=enabled, detection=detection, handling=handling, confidence=confidence)


@dataclass(frozen=True)
class CategoryPolicy:
    """Per-category detection + handling policy (``categories.<name>``)."""

    name: str
    detection: str = DETECTION_OFF
    handling: str = HANDLING_REDACT
    confidence: float | None = None
    classes: dict[str, ClassPolicy] = field(default_factory=dict)
    preserve_list: tuple[str, ...] = ()
    default_action: str | None = None

    def class_policy(self, rule: str) -> ClassPolicy:
        return self.classes.get(rule, ClassPolicy())

    def to_dict(self) -> dict:
        out: dict = {"detection": self.detection, "handling": self.handling}
        if self.confidence is not None:
            out["confidence"] = self.confidence
        if self.classes:
            out["classes"] = {k: v.to_dict() for k, v in self.classes.items()}
        if self.preserve_list:
            out["preserve_list"] = list(self.preserve_list)
        if self.default_action is not None:
            out["default_action"] = self.default_action
        return out


@dataclass(frozen=True)
class ExceptionPolicy:
    """A narrow, auditable false-positive exception (AC4)."""

    category: str
    reason: str
    rule: str | None = None
    match: str | None = None
    scope: str = "*"

    def matches(self, *, category: str, rule: str, value: str, path: str | None) -> bool:
        if self.category != category:
            return False
        if self.rule is not None and self.rule != rule:
            return False
        if self.match is not None and self.match != value:
            return False
        return _scope_matches(self.scope, path)

    def to_dict(self) -> dict:
        out = {"category": self.category, "reason": self.reason, "scope": self.scope}
        if self.rule is not None:
            out["rule"] = self.rule
        if self.match is not None:
            out["match"] = self.match
        return out


@dataclass(frozen=True)
class Finding:
    """One detected value and the action the policy resolves for it."""

    category: str
    rule: str
    value: str
    line: int
    start: int
    end: int
    confidence: float
    detection: str
    handling: str
    suppressed: bool = False
    exception_reason: str = ""

    @property
    def blocked(self) -> bool:
        return not self.suppressed and self.detection == DETECTION_BLOCK

    @property
    def warned(self) -> bool:
        return not self.suppressed and self.detection == DETECTION_WARN

    @property
    def redacted_value(self) -> str:
        """The replacement text this finding contributes when handling applies."""
        if self.handling == HANDLING_REPLACE:
            return RULE_BY_NAME[self.rule].placeholder
        return REDACTION_PLACEHOLDER.format(category=self.category)

    @property
    def safe_value(self) -> str:
        """The value as it may appear in output for this finding.

        ``preserve`` is report-only *by policy* (AC1/AC2), so a preserved
        finding shows its value; every other handling (and every suppressed
        finding) shows only the redaction placeholder — a canary value never
        survives when the policy did not explicitly ask to preserve it (AC5).
        """
        if self.handling == HANDLING_PRESERVE and not self.suppressed:
            return self.value
        return self.redacted_value

    def render(self, *, reveal: bool = False) -> str:
        """One-line description; the value is redacted unless ``reveal``."""
        shown = self.value if reveal else self.safe_value
        state = "suppressed" if self.suppressed else self.detection
        return (
            f"{self.category}/{self.rule} conf={self.confidence:.2f} "
            f"line={self.line} action={state}/{self.handling} value={shown}"
        )

    def to_dict(self, *, reveal: bool = False) -> dict:
        data: dict = {
            "category": self.category,
            "rule": self.rule,
            "line": self.line,
            "confidence": round(self.confidence, 3),
            "detection": self.detection,
            "handling": self.handling,
            "suppressed": self.suppressed,
        }
        if self.suppressed and self.exception_reason:
            data["exception_reason"] = self.exception_reason
        data["value" if (reveal or self.handling == HANDLING_PRESERVE) else "value_redacted"] = (
            self.value if reveal else self.safe_value
        )
        return data


@dataclass(frozen=True)
class ScanResult:
    """Findings for one text (a file body or an output chunk) plus its scrub."""

    findings: tuple[Finding, ...] = ()
    redacted_text: str = ""

    @property
    def active(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if not f.suppressed)

    @property
    def suppressed(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.suppressed)

    @property
    def blocked(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.blocked)

    @property
    def warned(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.warned)

    @property
    def passed(self) -> bool:
        return not self.blocked


# ── The policy ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DataProtectionPolicy:
    """The whole ``data_protection:`` policy.

    Build one from config with :meth:`from_dict` (or :func:`build_policy`);
    the defaults are the backwards-compatible no-op: ``enabled=False`` and
    every category ``off``.
    """

    enabled: bool = False
    categories: dict[str, CategoryPolicy] = field(default_factory=dict)
    exceptions: tuple[ExceptionPolicy, ...] = ()

    # -- construction ---------------------------------------------------------

    @classmethod
    def default(cls) -> "DataProtectionPolicy":
        return cls(
            enabled=False,
            categories={
                CATEGORY_PII: CategoryPolicy(name=CATEGORY_PII),
                CATEGORY_IP: CategoryPolicy(name=CATEGORY_IP),
            },
        )

    @classmethod
    def from_dict(cls, raw: dict | None) -> "DataProtectionPolicy":
        raw = raw if isinstance(raw, dict) else {}
        policy = cls.default()
        if not raw:
            return policy
        _reject_unknown_keys(
            raw,
            {"enabled", "categories", "exceptions"},
            "data_protection",
        )
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise DataProtectionConfigError(
                f"data_protection.enabled must be true or false, got {enabled!r}"
            )

        categories: dict[str, CategoryPolicy] = dict(policy.categories)
        raw_categories = raw.get("categories", {})
        if raw_categories is None:
            raw_categories = {}
        if not isinstance(raw_categories, dict):
            raise DataProtectionConfigError("data_protection.categories must be a mapping")
        for name, block in raw_categories.items():
            if name == CATEGORY_CREDENTIALS:
                raise DataProtectionConfigError(
                    "data_protection.categories.credentials is not configurable: "
                    "credentials are owned by the secrets scanner (gitleaks) and "
                    "data_protection must never weaken them"
                )
            if name not in CONFIGURABLE_CATEGORIES:
                raise DataProtectionConfigError(
                    f"unknown data_protection category {name!r}; "
                    f"supported: {', '.join(CONFIGURABLE_CATEGORIES)}"
                )
            categories[name] = _category_from_dict(name, block)

        exceptions = _exceptions_from_raw(raw.get("exceptions", []))
        result = cls(enabled=enabled, categories=categories, exceptions=exceptions)
        result.validate()
        return result

    # -- validation -----------------------------------------------------------

    def validate(self) -> None:
        """Raise :class:`DataProtectionConfigError` on malformed policy (AC3)."""
        if not isinstance(self.enabled, bool):
            raise DataProtectionConfigError("data_protection.enabled must be true or false")
        for name, category in self.categories.items():
            if name not in CONFIGURABLE_CATEGORIES:
                raise DataProtectionConfigError(f"unknown data_protection category {name!r}")
            _check_choice(category.detection, DETECTION_LEVELS, f"categories.{name}.detection")
            _check_choice(category.handling, HANDLING_MODES, f"categories.{name}.handling")
            if category.default_action is not None:
                _check_choice(
                    category.default_action,
                    HANDLING_MODES,
                    f"categories.{name}.default_action",
                )
            if category.confidence is not None:
                _check_confidence(category.confidence, f"categories.{name}.confidence")
            valid_rules = {r.rule for r in RULES_BY_CATEGORY[name]}
            for rule in category.classes:
                if rule not in valid_rules:
                    raise DataProtectionConfigError(
                        f"unknown class {rule!r} for category {name!r}; "
                        f"supported: {', '.join(sorted(valid_rules))}"
                    )
            for entry in category.preserve_list:
                _check_preserve_entry(entry, name)
        for exception in self.exceptions:
            if exception.category not in CONFIGURABLE_CATEGORIES:
                raise DataProtectionConfigError(
                    f"exception category {exception.category!r} is not configurable; "
                    "credentials can never be excepted"
                )
            valid_rules = {r.rule for r in RULES_BY_CATEGORY[exception.category]}
            if exception.rule is not None and exception.rule not in valid_rules:
                raise DataProtectionConfigError(
                    f"exception rule {exception.rule!r} is not valid for category "
                    f"{exception.category!r}; supported: {', '.join(sorted(valid_rules))}"
                )
            if exception.rule is None and exception.match is None:
                raise DataProtectionConfigError(
                    "exception must narrow a finding with 'match' and/or 'rule'; "
                    "a category-wide exception would broaden the allowlist"
                )
            if not exception.reason.strip():
                raise DataProtectionConfigError("exception requires a non-empty 'reason'")

    # -- introspection --------------------------------------------------------

    @property
    def active(self) -> bool:
        """True when at least one category actually scans."""
        return self.enabled and any(c.detection != DETECTION_OFF for c in self.categories.values())

    def category(self, name: str) -> CategoryPolicy:
        return self.categories.get(name, CategoryPolicy(name=name))

    def rule_enabled(self, rule: str) -> bool:
        spec = RULE_BY_NAME[rule]
        category = self.category(spec.category)
        if category.detection == DETECTION_OFF:
            return False
        class_policy = category.class_policy(rule)
        if not class_policy.enabled:
            return False
        effective_detection = class_policy.detection or category.detection
        return effective_detection != DETECTION_OFF

    def effective_detection(self, rule: str) -> str:
        spec = RULE_BY_NAME[rule]
        category = self.category(spec.category)
        class_policy = category.class_policy(rule)
        return class_policy.detection or category.detection

    def effective_handling(self, rule: str) -> str:
        spec = RULE_BY_NAME[rule]
        category = self.category(spec.category)
        class_policy = category.class_policy(rule)
        return class_policy.handling or category.handling

    def confidence_threshold(self, rule: str) -> float:
        spec = RULE_BY_NAME[rule]
        category = self.category(spec.category)
        class_policy = category.class_policy(rule)
        if class_policy.confidence is not None:
            return class_policy.confidence
        if category.confidence is not None:
            return category.confidence
        return DEFAULT_CATEGORY_CONFIDENCE.get(spec.category, 0.0)

    # -- scanning -------------------------------------------------------------

    def scan(self, text: str, path: str | None = None) -> ScanResult:
        """Detect policy-selected values in *text*, resolving precedence.

        Precedence (AC2), first match wins:

        1. an explicit exception for this category/rule/value/scope → suppressed;
        2. an IP covered by ``preserve_list`` → handled as ``preserve``;
        3. the class override, else the category default (``default_action`` for
           unmatched IPs, else ``handling``).

        ``detection`` (off/warn/block) is resolved independently: ``warn``
        reports, ``block`` fails the lane; ``off`` never scans.
        """
        if not self.active or not text:
            return ScanResult(findings=(), redacted_text=text)

        matches: list[_Match] = []
        covered: list[tuple[int, int]] = []
        # CIDR ranges win over the literal inside them.
        for rule in ("cidr",):
            for match in _ALL_ITERATORS[rule](text):
                if self._rule_scans(rule, match.confidence):
                    matches.append(match)
                    covered.append((match.start, match.end))
        for rule, iterator in _ALL_ITERATORS.items():
            if rule == "cidr":
                continue
            if not self._rule_scans(rule, None):
                continue
            for match in iterator(text):
                if _overlaps(match.start, match.end, covered):
                    continue
                if match.confidence < self.confidence_threshold(rule):
                    continue
                matches.append(match)
            # Only CIDR imposes overlap; other classes may legitimately nest.

        findings = [self._resolve(match, text, path) for match in matches]
        findings.sort(key=lambda f: (f.start, f.end))
        return ScanResult(
            findings=tuple(findings),
            redacted_text=_apply_redactions(text, findings),
        )

    def _rule_scans(self, rule: str, confidence: float | None) -> bool:
        if not self.rule_enabled(rule):
            return False
        if confidence is not None and confidence < self.confidence_threshold(rule):
            return False
        return True

    def _resolve(self, match: _Match, text: str, path: str | None) -> Finding:
        spec = RULE_BY_NAME[match.rule]
        category = self.category(spec.category)
        class_policy = category.class_policy(match.rule)
        detection = class_policy.detection or category.detection
        handling = class_policy.handling or category.handling
        if spec.category == CATEGORY_IP:
            handling = self._ip_handling(match.value, handling, category)
        for exception in self.exceptions:
            if exception.matches(
                category=spec.category, rule=match.rule, value=match.value, path=path
            ):
                return Finding(
                    category=spec.category,
                    rule=match.rule,
                    value=match.value,
                    line=_line_of(text, match.start),
                    start=match.start,
                    end=match.end,
                    confidence=match.confidence,
                    detection=detection,
                    handling=handling,
                    suppressed=True,
                    exception_reason=exception.reason,
                )
        return Finding(
            category=spec.category,
            rule=match.rule,
            value=match.value,
            line=_line_of(text, match.start),
            start=match.start,
            end=match.end,
            confidence=match.confidence,
            detection=detection,
            handling=handling,
        )

    @staticmethod
    def _ip_handling(value: str, handling: str, category: CategoryPolicy) -> str:
        """Apply ``preserve_list`` / ``default_action`` precedence (AC2)."""
        if category.preserve_list and _address_in_preserve_list(value, category.preserve_list):
            return HANDLING_PRESERVE
        if category.default_action is not None:
            return category.default_action
        return handling

    # -- output scrubbing -----------------------------------------------------

    def redact_text(self, text: str, path: str | None = None) -> str:
        """Scrub detected values from arbitrary text per the policy (AC5).

        Used by the guard lane for its own output, the run-log writer and the
        verdict artifact writer, so a canary value cannot survive into a
        human- or machine-readable artifact when handling is not ``preserve``.
        ``preserve`` intentionally leaves the value visible (report-only).
        """
        if not self.active or not text:
            return text
        return self.scan(text, path=path).redacted_text

    def redact_outputs(self, values: Iterable[str], path: str | None = None) -> list[str]:
        return [self.redact_text(v, path=path) for v in values]

    # -- serialization --------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "categories": {name: cat.to_dict() for name, cat in self.categories.items()},
            "exceptions": [e.to_dict() for e in self.exceptions],
        }


# ── Config plumbing ───────────────────────────────────────────────────────────


def build_policy(config: dict | None) -> DataProtectionPolicy:
    """Build a policy from a raw ``.gitreins`` config dict.

    Reads the top-level ``data_protection:`` block; an absent block yields the
    backwards-compatible default (disabled).  A malformed block raises
    :class:`DataProtectionConfigError` — fail loud, never silently broaden.
    """
    if not isinstance(config, dict):
        return DataProtectionPolicy.default()
    block = config.get("data_protection")
    if block is None:
        return DataProtectionPolicy.default()
    if not isinstance(block, dict):
        raise DataProtectionConfigError(f"data_protection must be a mapping, got {block!r}")
    return DataProtectionPolicy.from_dict(block)


def policy_from_config(config: dict | None) -> DataProtectionPolicy:
    """Alias kept for callers that prefer the `policy_` prefix."""
    return build_policy(config)


# ── Measurement helpers (AC6) ─────────────────────────────────────────────────


def measure_fixtures(fixtures: Iterable[dict], policy: DataProtectionPolicy | None = None) -> dict:
    """Per-rule TP/FP/FN measurement over labeled synthetic fixtures.

    Each fixture is a mapping::

        {"id": ..., "text": ..., "expected": ["email", ...], "benign": bool}

    ``expected`` lists the rules that must fire.  A fixture marked ``benign``
    must produce no findings (a benign near-match).  Returns counts per rule
    plus the aggregate missed/extra sets — the numbers docs/data-protection.md
    quotes are produced by this function, not written by hand.
    """
    policy = policy or DataProtectionPolicy.default()
    fixtures = list(fixtures)
    per_rule: dict[str, dict[str, int]] = {
        rule: {"true_positive": 0, "false_positive": 0, "missed": 0} for rule in RULE_BY_NAME
    }
    missed: list[str] = []
    extra: list[str] = []
    for fixture in fixtures:
        expected = set(fixture.get("expected") or [])
        detected = {
            f.rule for f in policy.scan(fixture.get("text", ""), path=fixture.get("path")).active
        }
        for rule in expected & detected:
            per_rule[rule]["true_positive"] += 1
        for rule in expected - detected:
            per_rule[rule]["missed"] += 1
            missed.append(f"{fixture.get('id')}:{rule}")
        for rule in detected - expected:
            per_rule[rule]["false_positive"] += 1
            extra.append(f"{fixture.get('id')}:{rule}")
    return {
        "per_rule": per_rule,
        "missed": missed,
        "false_positives": extra,
        "fixtures": len(fixtures),
    }


def load_fixtures(path: str) -> list[dict]:
    """Load a JSON fixture corpus (used by tests and the docs generator)."""
    import json

    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise DataProtectionConfigError(f"fixture corpus {path} must be a JSON list")
    return data


# ── Internal helpers ──────────────────────────────────────────────────────────


def _reject_unknown_keys(block: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise DataProtectionConfigError(
            f"{where}: unknown key(s) {', '.join(unknown)}; supported: {', '.join(sorted(allowed))}"
        )


def _check_choice(value: object, choices: tuple[str, ...], where: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise DataProtectionConfigError(
            f"{where} must be one of {', '.join(choices)}, got {value!r}"
        )
    return value


def _check_confidence(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataProtectionConfigError(f"{where} must be a number between 0 and 1")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise DataProtectionConfigError(f"{where} must be between 0 and 1, got {value!r}")
    return number


def _check_preserve_entry(entry: str, category: str) -> None:
    if not isinstance(entry, str) or not entry.strip():
        raise DataProtectionConfigError(
            f"categories.{category}.preserve_list entries must be CIDR strings"
        )
    try:
        network = ipaddress.ip_network(entry, strict=False)
    except ValueError as exc:
        raise DataProtectionConfigError(
            f"categories.{category}.preserve_list entry {entry!r} is not a valid IP or CIDR: {exc}"
        ) from None
    if network.prefixlen == 0:
        raise DataProtectionConfigError(
            f"categories.{category}.preserve_list entry {entry!r} would preserve "
            "every address — refusing to broaden the allowlist to the whole space"
        )


def _category_from_dict(name: str, block: object | None) -> CategoryPolicy:
    where = f"data_protection.categories.{name}"
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise DataProtectionConfigError(f"{where} must be a mapping, got {block!r}")
    allowed = {
        "detection",
        "handling",
        "confidence",
        "classes",
        "preserve_list",
        "default_action",
    }
    _reject_unknown_keys(block, allowed, where)
    detection = _check_choice(
        block.get("detection", DETECTION_OFF), DETECTION_LEVELS, f"{where}.detection"
    )
    handling = _check_choice(
        block.get("handling", HANDLING_REDACT), HANDLING_MODES, f"{where}.handling"
    )
    confidence = block.get("confidence")
    if confidence is not None:
        confidence = _check_confidence(confidence, f"{where}.confidence")
    default_action = block.get("default_action")
    if default_action is not None:
        default_action = _check_choice(default_action, HANDLING_MODES, f"{where}.default_action")
        if name != CATEGORY_IP:
            raise DataProtectionConfigError(
                f"{where}.default_action is only valid for {CATEGORY_IP!r} "
                "(it resolves unmatched network ranges)"
            )
    classes_raw = block.get("classes", {})
    if classes_raw is None:
        classes_raw = {}
    if not isinstance(classes_raw, dict):
        raise DataProtectionConfigError(f"{where}.classes must be a mapping")
    valid_rules = {r.rule for r in RULES_BY_CATEGORY[name]}
    classes: dict[str, ClassPolicy] = {}
    for rule, class_block in classes_raw.items():
        if rule not in valid_rules:
            raise DataProtectionConfigError(
                f"{where}.classes.{rule}: unknown class; supported: "
                f"{', '.join(sorted(valid_rules))}"
            )
        classes[rule] = ClassPolicy.from_dict(class_block, where=f"{where}.classes.{rule}")
    preserve_raw = block.get("preserve_list", [])
    if preserve_raw is None:
        preserve_raw = []
    if not isinstance(preserve_raw, (list, tuple)):
        raise DataProtectionConfigError(f"{where}.preserve_list must be a list of CIDRs")
    if name != CATEGORY_IP and preserve_raw:
        raise DataProtectionConfigError(f"{where}.preserve_list is only valid for {CATEGORY_IP!r}")
    preserve_list = tuple(str(entry) for entry in preserve_raw)
    return CategoryPolicy(
        name=name,
        detection=detection,
        handling=handling,
        confidence=confidence,
        classes=classes,
        preserve_list=preserve_list,
        default_action=default_action,
    )


def _exceptions_from_raw(raw: object | None) -> tuple[ExceptionPolicy, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise DataProtectionConfigError("data_protection.exceptions must be a list")
    exceptions: list[ExceptionPolicy] = []
    for index, entry in enumerate(raw):
        where = f"data_protection.exceptions[{index}]"
        if not isinstance(entry, dict):
            raise DataProtectionConfigError(f"{where} must be a mapping")
        _reject_unknown_keys(entry, {"category", "rule", "match", "scope", "reason"}, where)
        category = entry.get("category")
        if not isinstance(category, str) or not category:
            raise DataProtectionConfigError(f"{where}.category is required")
        if category == CATEGORY_CREDENTIALS:
            raise DataProtectionConfigError(
                f"{where}: credentials can never be excepted — the secrets scanner "
                "is fail-closed and independent of data_protection"
            )
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DataProtectionConfigError(f"{where}.reason is required and must be non-empty")
        rule = entry.get("rule")
        if rule is not None and not isinstance(rule, str):
            raise DataProtectionConfigError(f"{where}.rule must be a string")
        match_value = entry.get("match")
        if match_value is not None and not isinstance(match_value, str):
            raise DataProtectionConfigError(f"{where}.match must be a string")
        scope = entry.get("scope", "*")
        if not isinstance(scope, str) or not scope:
            raise DataProtectionConfigError(f"{where}.scope must be a non-empty glob")
        exceptions.append(
            ExceptionPolicy(
                category=category,
                reason=reason.strip(),
                rule=rule,
                match=match_value,
                scope=scope,
            )
        )
    return tuple(exceptions)


def _scope_matches(scope: str, path: str | None) -> bool:
    if path is None:
        # No file context (arbitrary output text): only a catch-all scope
        # applies, so a file-scoped exception cannot silently leak elsewhere.
        return scope in ("*", "**")
    return fnmatch.fnmatch(path, scope)


def _address_in_preserve_list(value: str, preserve_list: tuple[str, ...]) -> bool:
    """True when *value* (address or CIDR) is covered by a preserved range."""
    try:
        if "/" in value:
            network = ipaddress.ip_network(value, strict=False)
            return any(
                network.subnet_of(candidate)
                for candidate in _parse_networks(preserve_list, network.version)
            )
        address = ipaddress.ip_address(value)
        return any(
            address in candidate for candidate in _parse_networks(preserve_list, address.version)
        )
    except ValueError:
        return False


def _parse_networks(entries: tuple[str, ...], version: int) -> list:
    networks = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if network.version == version:
            networks.append(network)
    return networks


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _apply_redactions(text: str, findings: Iterable[Finding]) -> str:
    """Rewrite *text* in place for every active, handled finding."""
    active = [
        f
        for f in findings
        if not f.suppressed and f.handling in (HANDLING_REDACT, HANDLING_REPLACE)
    ]
    if not active:
        return text
    output = text
    for finding in sorted(active, key=lambda f: f.start, reverse=True):
        output = output[: finding.start] + finding.redacted_value + output[finding.end :]
    return output
