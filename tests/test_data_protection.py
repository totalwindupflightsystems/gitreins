"""Tests for the configurable Tier-1 data-protection filters (GR-146).

All fixture data is synthetic and labeled; the canary strings in
``tests/fixtures/data_protection/`` must never appear in scrub-able output
when the policy says redact/replace (AC5/AC6).
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from engine.config import GitReinsDefaults
from engine.data_protection import (
    ALL_RULES,
    CATEGORY_CREDENTIALS,
    CATEGORY_INVENTORY,
    CATEGORY_IP,
    CATEGORY_PII,
    DETECTION_BLOCK,
    DETECTION_OFF,
    DETECTION_WARN,
    DEFAULT_CATEGORY_CONFIDENCE,
    HANDLING_PRESERVE,
    HANDLING_REDACT,
    HANDLING_REPLACE,
    PII_RULES,
    RULES_BY_CATEGORY,
    DataProtectionConfigError,
    DataProtectionPolicy,
    build_policy,
    load_fixtures,
    measure_fixtures,
)
from engine.guard_manager import GuardManager
from engine.guards import check_data_protection
from engine.pipeline import _data_protection_enabled, tier1_plan

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "data_protection")


def _load(name: str) -> list[dict]:
    return load_fixtures(os.path.join(FIXTURE_DIR, name))


PII_FIXTURES = _load("pii_labeled.json")
NETWORK_FIXTURES = _load("network_labeled.json")
BENIGN_FIXTURES = _load("benign_near_matches.json")
ALL_FIXTURES = PII_FIXTURES + NETWORK_FIXTURES + BENIGN_FIXTURES


def _max_sensitivity_policy(**overrides) -> DataProtectionPolicy:
    """Policy that enables every class at confidence 0 (measurement policy)."""
    block = {
        "enabled": True,
        "categories": {
            CATEGORY_PII: {
                "detection": DETECTION_WARN,
                "handling": HANDLING_REDACT,
                "confidence": 0.0,
            },
            CATEGORY_IP: {
                "detection": DETECTION_WARN,
                "handling": HANDLING_REDACT,
                "confidence": 0.0,
            },
        },
    }
    block.update(overrides)
    return DataProtectionPolicy.from_dict(block)


# ── AC1: defaults / backwards compatibility ──────────────────────────────────


def test_absent_block_is_disabled_noop() -> None:
    policy = build_policy({})
    assert policy.enabled is False
    assert policy.active is False
    text = "canary.email.alpha@example.com 203.0.113.9"
    assert policy.scan(text).findings == ()
    assert policy.redact_text(text) == text


def test_default_policy_disabled() -> None:
    assert DataProtectionPolicy.default().enabled is False
    assert DataProtectionPolicy.default().active is False


def test_category_defaults_are_off() -> None:
    policy = DataProtectionPolicy.default()
    for name in (CATEGORY_PII, CATEGORY_IP):
        assert policy.category(name).detection == DETECTION_OFF


def test_enabled_but_all_categories_off_is_not_active() -> None:
    policy = DataProtectionPolicy.from_dict({"enabled": True})
    assert policy.enabled is True
    assert policy.active is False


def test_config_defaults_round_trip() -> None:
    cfg = GitReinsDefaults().overlay({})
    assert cfg.data_protection.enabled is False
    dumped = cfg.to_config_dict()
    assert "data_protection" in dumped
    assert dumped["data_protection"]["enabled"] is False


def test_config_overlay_reads_block() -> None:
    cfg = GitReinsDefaults().overlay(
        {
            "data_protection": {
                "enabled": True,
                "categories": {CATEGORY_IP: {"detection": DETECTION_BLOCK}},
            }
        }
    )
    assert cfg.data_protection.enabled is True
    assert cfg.data_protection.policy.category(CATEGORY_IP).detection == DETECTION_BLOCK


# ── AC7: category inventory + extensible model ───────────────────────────────


def test_category_inventory_covers_credentials_pii_and_network() -> None:
    names = {spec.name for spec in CATEGORY_INVENTORY}
    assert names == {CATEGORY_CREDENTIALS, CATEGORY_PII, CATEGORY_IP}


def test_credentials_owned_by_secrets_and_not_configurable() -> None:
    credentials = next(s for s in CATEGORY_INVENTORY if s.name == CATEGORY_CREDENTIALS)
    assert credentials.configurable is False
    assert "gitleaks" in credentials.detector


def test_inventory_classes_match_rule_registry() -> None:
    pii = next(s for s in CATEGORY_INVENTORY if s.name == CATEGORY_PII)
    ip = next(s for s in CATEGORY_INVENTORY if s.name == CATEGORY_IP)
    assert set(pii.classes) == {r.rule for r in PII_RULES}
    assert set(ip.classes) == {r.rule for r in RULES_BY_CATEGORY[CATEGORY_IP]}
    # every rule is documented with supported cases and limitations
    for rule in ALL_RULES:
        assert rule.supported and rule.limitations and rule.placeholder


# ── AC2/AC6: detection over the synthetic labeled corpus ─────────────────────


@pytest.mark.parametrize("fixture", PII_FIXTURES + NETWORK_FIXTURES, ids=lambda f: f["id"])
def test_labeled_fixture_detects_expected_rules(fixture) -> None:
    policy = _max_sensitivity_policy()
    detected = {f.rule for f in policy.scan(fixture["text"]).active}
    assert set(fixture["expected"]).issubset(detected), (
        f"{fixture['id']} expected {fixture['expected']} got {sorted(detected)}"
    )


@pytest.mark.parametrize("fixture", BENIGN_FIXTURES, ids=lambda f: f["id"])
def test_benign_near_matches_do_not_raise_other_classes(fixture) -> None:
    policy = _max_sensitivity_policy()
    detected = {f.rule for f in policy.scan(fixture["text"]).active}
    # Benign fixtures must never trip IP or the high-precision classes; the
    # measured low-precision FPs are asserted explicitly below.
    assert not (detected & {"ipv4", "ipv6", "cidr", "email", "ssn", "financial", "medical"})


def test_measured_false_positives_match_documented_classes() -> None:
    policy = _max_sensitivity_policy()
    measured = measure_fixtures(ALL_FIXTURES, policy)
    assert measured["missed"] == []
    # One measured false positive on the benign corpus, documented in
    # docs/data-protection.md: an order code (AB123456) is passport-shaped.
    assert measured["false_positives"] == ["known-fp-order-code:passport"]
    assert measured["per_rule"]["passport"]["false_positive"] == 1
    assert measured["per_rule"]["ipv4"]["false_positive"] == 0
    assert measured["per_rule"]["phone"]["false_positive"] == 0


def test_labeled_corpus_true_positive_counts() -> None:
    policy = _max_sensitivity_policy()
    measured = measure_fixtures(PII_FIXTURES + NETWORK_FIXTURES, policy)
    assert measured["missed"] == []
    assert measured["false_positives"] == []
    per_rule = measured["per_rule"]
    for rule in (
        "email",
        "phone",
        "ssn",
        "passport",
        "medical",
        "financial",
        "names",
        "ipv4",
        "ipv6",
        "cidr",
    ):
        assert per_rule[rule]["true_positive"] >= 1, rule


def test_every_canary_is_the_value_detected() -> None:
    policy = _max_sensitivity_policy()
    for fixture in PII_FIXTURES + NETWORK_FIXTURES:
        values = {f.value for f in policy.scan(fixture["text"]).active}
        assert fixture["canary"] in values, fixture["id"]


# ── AC2: IP precedence and defaults ──────────────────────────────────────────


def test_preserve_list_wins_over_default_action() -> None:
    policy = DataProtectionPolicy.from_dict(
        {
            "enabled": True,
            "categories": {
                CATEGORY_IP: {
                    "detection": DETECTION_WARN,
                    "handling": HANDLING_REDACT,
                    "default_action": HANDLING_REPLACE,
                    "preserve_list": ["10.0.0.0/8", "2001:db8::/32"],
                }
            },
        }
    )
    preserved = policy.scan("10.1.2.3").active[0]
    assert preserved.handling == HANDLING_PRESERVE
    replaced = policy.scan("203.0.113.9").active[0]
    assert replaced.handling == HANDLING_REPLACE
    v6_preserved = policy.scan("2001:db8::1").active[0]
    assert v6_preserved.handling == HANDLING_PRESERVE


def test_unmatched_default_action_falls_back_to_handling() -> None:
    policy = DataProtectionPolicy.from_dict(
        {
            "enabled": True,
            "categories": {CATEGORY_IP: {"detection": DETECTION_WARN, "handling": HANDLING_REDACT}},
        }
    )
    finding = policy.scan("203.0.113.9").active[0]
    assert finding.handling == HANDLING_REDACT


def test_cidr_wins_over_inner_literal() -> None:
    policy = _max_sensitivity_policy()
    findings = policy.scan("range 192.168.1.0/24 here").active
    rules = [f.rule for f in findings]
    assert rules.count("cidr") == 1
    assert "ipv4" not in rules


def test_invalid_octets_and_prefixes_are_not_detected() -> None:
    policy = _max_sensitivity_policy()
    assert policy.scan("999.999.999.999").active == ()
    assert policy.scan("192.168.1.0/40").active == ()
    assert policy.scan("2001:db8::/129").active == ()


def test_ipv6_requires_two_colon_groups() -> None:
    policy = _max_sensitivity_policy()
    assert policy.scan("finished at 12:30:45").active == ()
    assert [f.rule for f in policy.scan("host 2001:db8::1").active] == ["ipv6"]


# ── AC3: validation / confidence controls ────────────────────────────────────


@pytest.mark.parametrize(
    "block",
    [
        {"enabled": "yes"},
        {"enabled": True, "categories": {CATEGORY_PII: {"detection": "loud"}}},
        {"enabled": True, "categories": {CATEGORY_PII: {"handling": "delete"}}},
        {"enabled": True, "categories": {CATEGORY_PII: {"confidence": 1.5}}},
        {"enabled": True, "categories": {CATEGORY_PII: {"confidence": "high"}}},
        {"enabled": True, "categories": {"secrets": {}}},
        {"enabled": True, "categories": {CATEGORY_CREDENTIALS: {"detection": "warn"}}},
        {"enabled": True, "categories": {CATEGORY_PII: {"classes": {"not_a_class": {}}}}},
        {"enabled": True, "categories": {CATEGORY_IP: {"preserve_list": ["not-a-cidr"]}}},
        {"enabled": True, "categories": {CATEGORY_IP: {"preserve_list": ["0.0.0.0/0"]}}},
        {"enabled": True, "categories": {CATEGORY_IP: {"preserve_list": ["::/0"]}}},
        {"enabled": True, "categories": {CATEGORY_PII: {"preserve_list": ["10.0.0.0/8"]}}},
        {"enabled": True, "categories": {CATEGORY_PII: {"default_action": "redact"}}},
        {"enabled": True, "bogus_key": 1},
        {
            "enabled": True,
            "categories": {CATEGORY_IP: {"detection": DETECTION_BLOCK, "unknown": 1}},
        },
    ],
)
def test_malformed_policy_is_rejected(block) -> None:
    with pytest.raises(DataProtectionConfigError):
        DataProtectionPolicy.from_dict(block)


def test_valid_policy_is_accepted() -> None:
    policy = DataProtectionPolicy.from_dict(
        {
            "enabled": True,
            "categories": {
                CATEGORY_PII: {
                    "detection": DETECTION_BLOCK,
                    "handling": HANDLING_REDACT,
                    "confidence": 0.9,
                    "classes": {"email": {"confidence": 0.95}, "names": {"enabled": False}},
                },
                CATEGORY_IP: {
                    "detection": DETECTION_WARN,
                    "handling": HANDLING_REPLACE,
                    "default_action": HANDLING_REDACT,
                    "preserve_list": ["10.0.0.0/8"],
                },
            },
            "exceptions": [
                {
                    "category": CATEGORY_IP,
                    "rule": "ipv4",
                    "match": "198.51.100.7",
                    "scope": "docs/*",
                    "reason": "RFC 5737 documentation address",
                }
            ],
        }
    )
    policy.validate()  # no raise
    assert policy.rule_enabled("email") is True
    assert policy.rule_enabled("names") is False


def test_confidence_threshold_controls_class_selection() -> None:
    strict = DataProtectionPolicy.from_dict(
        {"enabled": True, "categories": {CATEGORY_PII: {"detection": DETECTION_WARN}}}
    )
    # Default PII floor is 0.7: only the high-precision classes fire.
    assert strict.confidence_threshold("email") == 0.7 == DEFAULT_CATEGORY_CONFIDENCE[CATEGORY_PII]
    detected = {
        f.rule for f in strict.scan("patient name: Alice Synthetic AB1234567 415 555 0132").active
    }
    assert detected == set()
    sensitive = _max_sensitivity_policy()
    assert {"names", "passport", "phone"} <= {
        f.rule
        for f in sensitive.scan("patient name: Alice Synthetic AB1234567 415 555 0132").active
    }


def test_class_confidence_override_beats_category() -> None:
    policy = DataProtectionPolicy.from_dict(
        {
            "enabled": True,
            "categories": {
                CATEGORY_PII: {
                    "detection": DETECTION_WARN,
                    "confidence": 0.9,
                    "classes": {"phone": {"confidence": 0.5}},
                }
            },
        }
    )
    assert policy.confidence_threshold("phone") == 0.5
    assert policy.rule_enabled("phone") is True


def test_unknown_class_in_exception_rejected() -> None:
    with pytest.raises(DataProtectionConfigError):
        DataProtectionPolicy.from_dict(
            {
                "enabled": True,
                "categories": {CATEGORY_PII: {"detection": DETECTION_WARN}},
                "exceptions": [
                    {"category": CATEGORY_PII, "rule": "nope", "reason": "x"},
                ],
            }
        )


# ── AC4: false-positive exceptions ───────────────────────────────────────────


def test_exception_requires_reason() -> None:
    with pytest.raises(DataProtectionConfigError):
        DataProtectionPolicy.from_dict(
            {
                "enabled": True,
                "categories": {CATEGORY_PII: {"detection": DETECTION_BLOCK}},
                "exceptions": [{"category": CATEGORY_PII, "rule": "email"}],
            }
        )


def test_exception_requires_narrowing_match_or_rule() -> None:
    with pytest.raises(DataProtectionConfigError):
        DataProtectionPolicy.from_dict(
            {
                "enabled": True,
                "categories": {CATEGORY_PII: {"detection": DETECTION_BLOCK}},
                "exceptions": [{"category": CATEGORY_PII, "reason": "too broad"}],
            }
        )


def test_exception_cannot_target_credentials() -> None:
    with pytest.raises(DataProtectionConfigError):
        DataProtectionPolicy.from_dict(
            {
                "enabled": True,
                "categories": {CATEGORY_PII: {"detection": DETECTION_BLOCK}},
                "exceptions": [
                    {"category": CATEGORY_CREDENTIALS, "match": "AKIA...", "reason": "nope"}
                ],
            }
        )


def _exception_policy(**exception_kwargs) -> DataProtectionPolicy:
    entry = {"category": CATEGORY_PII, "reason": "synthetic fixture value"}
    entry.update(exception_kwargs)
    return DataProtectionPolicy.from_dict(
        {
            "enabled": True,
            "categories": {
                CATEGORY_PII: {"detection": DETECTION_BLOCK, "handling": HANDLING_REDACT}
            },
            "exceptions": [entry],
        }
    )


def test_exception_suppresses_only_the_named_value_and_scope() -> None:
    policy = _exception_policy(rule="email", match="ok@example.com", scope="docs/*")
    in_scope = policy.scan("ok@example.com and other@example.com", path="docs/readme.md")
    assert [f.suppressed for f in in_scope.findings] == [True, False]
    assert len(in_scope.blocked) == 1
    # Same value OUTSIDE the scope is still a finding.
    out_of_scope = policy.scan("ok@example.com", path="src/app.py")
    assert out_of_scope.suppressed == ()
    assert len(out_of_scope.blocked) == 1


def test_exception_does_not_hide_unrelated_findings() -> None:
    policy = _exception_policy(rule="email", match="ok@example.com")
    result = policy.scan("ok@example.com 123-45-6789 203.0.113.9")
    blocked_rules = {f.rule for f in result.blocked}
    assert "ssn" in blocked_rules  # ssn is PII/block; unrelated finding survives
    assert "email" not in blocked_rules


def test_exception_applies_only_to_matching_value() -> None:
    policy = _exception_policy(rule="email", match="ok@example.com")
    result = policy.scan("ok@example.com other@example.com")
    assert sum(1 for f in result.findings if f.suppressed) == 1
    assert sum(1 for f in result.findings if f.blocked) == 1


# ── AC5: redaction in output/artifacts ───────────────────────────────────────


def _handling_policy(handling: str) -> DataProtectionPolicy:
    return DataProtectionPolicy.from_dict(
        {
            "enabled": True,
            "categories": {CATEGORY_PII: {"detection": DETECTION_BLOCK, "handling": handling}},
        }
    )


def test_redact_replaces_with_category_placeholder() -> None:
    policy = _handling_policy(HANDLING_REDACT)
    canary = "canary.email.alpha@example.com"
    out = policy.redact_text(f"leaked {canary} here")
    assert canary not in out
    assert "[REDACTED:pii]" in out


def test_replace_uses_class_placeholder() -> None:
    policy = _handling_policy(HANDLING_REPLACE)
    canary = "canary.email.alpha@example.com"
    out = policy.redact_text(f"leaked {canary} here")
    assert canary not in out
    assert "[PII:EMAIL]" in out


def test_preserve_leaves_value_visible() -> None:
    policy = _handling_policy(HANDLING_PRESERVE)
    canary = "canary.email.alpha@example.com"
    assert canary in policy.redact_text(canary)
    finding = policy.scan(canary).active[0]
    assert finding.safe_value == canary


def test_finding_dict_never_leaks_when_redact() -> None:
    policy = _handling_policy(HANDLING_REDACT)
    canary = "canary.email.alpha@example.com"
    for finding in policy.scan(canary).active:
        assert canary not in json.dumps(finding.to_dict())


def test_redact_text_scrubs_machine_readable_artifact() -> None:
    policy = DataProtectionPolicy.from_dict(
        {
            "enabled": True,
            "categories": {
                CATEGORY_PII: {
                    "detection": DETECTION_WARN,
                    "handling": HANDLING_REDACT,
                    "confidence": 0.0,
                },
                CATEGORY_IP: {
                    "detection": DETECTION_WARN,
                    "handling": HANDLING_REDACT,
                    "confidence": 0.0,
                },
            },
        }
    )
    canaries = [f["canary"] for f in PII_FIXTURES + NETWORK_FIXTURES]
    # Use the labeled fixture TEXTS (some rules are label-anchored, e.g.
    # medical/names), exactly as a leak in a file body would appear.
    artifact = json.dumps({"log": [f["text"] for f in PII_FIXTURES + NETWORK_FIXTURES]})
    scrubbed = policy.redact_text(artifact)
    for canary in canaries:
        assert canary not in scrubbed


# ── Guard-lane integration ───────────────────────────────────────────────────


def _git_repo(tmp_path) -> str:
    workdir = str(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    return workdir


def _tracked(workdir: str, relpath: str, content: str) -> None:
    absolute = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    with open(absolute, "w", encoding="utf-8") as handle:
        handle.write(content)


DP_CONFIG = {
    "enabled": True,
    "categories": {
        CATEGORY_PII: {
            "detection": DETECTION_BLOCK,
            "handling": HANDLING_REDACT,
            "confidence": 0.7,
        },
        CATEGORY_IP: {
            "detection": DETECTION_WARN,
            "handling": HANDLING_REPLACE,
            "default_action": HANDLING_REDACT,
            "preserve_list": ["10.0.0.0/8"],
        },
    },
}


def test_guard_lane_skips_when_disabled(tmp_path) -> None:
    workdir = _git_repo(tmp_path)
    _tracked(workdir, "a.py", "x = 'canary.email.alpha@example.com'\n")
    result = check_data_protection(workdir, ["a.py"], policy=DataProtectionPolicy.default())
    assert result.skipped is True
    assert result.passed is True
    assert result.findings == ()


def test_guard_lane_blocks_and_redacts(tmp_path) -> None:
    workdir = _git_repo(tmp_path)
    canary = "canary.email.alpha@example.com"
    _tracked(workdir, "a.py", f"CONTACT = {canary!r}\n")
    policy = DataProtectionPolicy.from_dict(DP_CONFIG)
    result = check_data_protection(workdir, ["a.py"], policy=policy)
    assert result.passed is False
    assert result.blocked_count == 1
    assert canary not in result.output
    assert "[REDACTED:pii]" in result.output


def test_guard_lane_warn_does_not_fail(tmp_path) -> None:
    workdir = _git_repo(tmp_path)
    _tracked(workdir, "a.py", "HOST = '203.0.113.9'\n")
    config = json.loads(json.dumps(DP_CONFIG))
    config["categories"][CATEGORY_IP]["detection"] = DETECTION_WARN
    config["categories"][CATEGORY_PII]["detection"] = DETECTION_OFF
    policy = DataProtectionPolicy.from_dict(config)
    result = check_data_protection(workdir, ["a.py"], policy=policy)
    assert result.passed is True
    assert result.warned_count == 1
    assert result.warning


def test_guard_manager_runs_lane_only_when_enabled(tmp_path) -> None:
    workdir = _git_repo(tmp_path)
    _tracked(workdir, "a.py", "x = 1\n")
    base = {"guards": {"secrets": False, "lint": False, "tests": False, "allow_skips": True}}
    off = GuardManager(workdir, config=base, scope="working-tree", persist_log=False)
    assert "data_protection" not in {r.name for r in off.run_all().results}

    on_cfg = dict(base, data_protection=DP_CONFIG)
    on = GuardManager(workdir, config=on_cfg, scope="working-tree", persist_log=False)
    assert "data_protection" in {r.name for r in on.run_all().results}


def test_guard_manager_lane_blocks_and_exposes_findings(tmp_path) -> None:
    workdir = _git_repo(tmp_path)
    canary = "canary.email.alpha@example.com"
    _tracked(workdir, "src/leak.py", f"CONTACT = {canary!r}\n")
    cfg = {
        "guards": {"secrets": False, "lint": False, "tests": False, "allow_skips": True},
        "data_protection": DP_CONFIG,
    }
    gm = GuardManager(workdir, config=cfg, scope="working-tree", persist_log=False)
    result = gm.run_all()
    assert result.passed is False
    lane = next(r for r in result.results if r.name == "data_protection")
    assert lane.passed is False
    assert canary not in lane.output
    dp_extra = result.extra["data_protection"]
    assert dp_extra["blocked"] >= 1
    assert canary not in json.dumps(dp_extra)


def test_guard_manager_data_protection_uses_explicit_scan_root(tmp_path, monkeypatch) -> None:
    workdir = _git_repo(tmp_path)
    source_root = tmp_path / "project"
    source_root.mkdir()
    outside = "canary.email.outside@example.com"
    _tracked(workdir, "outside.py", f"CONTACT = {outside!r}\\n")
    _tracked(workdir, "project/clean.py", "VALUE = 1\\n")
    config = {
        "guards": {"secrets": False, "lint": False, "tests": False, "allow_skips": True},
        "data_protection": DP_CONFIG,
    }
    gm = GuardManager(
        str(workdir),
        config=config,
        scope="working-tree",
        persist_log=False,
        scan_root=str(source_root),
    )

    original_open = open
    opened: list[str] = []

    def tracking_open(file, *args, **kwargs):
        opened.append(os.path.realpath(os.fspath(file)))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", tracking_open)
    result = gm._check_data_protection()

    assert result.passed is True
    assert os.path.realpath(os.path.join(workdir, "outside.py")) not in opened


def test_tier1_data_protection_step_uses_explicit_scan_root(tmp_path) -> None:
    control_root = tmp_path / "control"
    source_root = control_root / "project"
    source_root.mkdir(parents=True)
    config = {"data_protection": {"enabled": True, "categories": DP_CONFIG["categories"]}}

    steps, _ = tier1_plan(str(control_root), config, str(source_root))

    step = next(s for s in steps if s["id"] == "data_protection")
    assert "GITREINS_SCAN_ROOT" in step["run"]
    assert str(source_root) in step["run"]


def test_guard_manager_malformed_policy_fails_loud(tmp_path) -> None:
    workdir = _git_repo(tmp_path)
    _tracked(workdir, "a.py", "x = 1\n")
    cfg = {
        "guards": {"secrets": False, "lint": False, "tests": False, "allow_skips": True},
        "data_protection": {"enabled": True, "categories": {CATEGORY_PII: {"detection": "loud"}}},
    }
    gm = GuardManager(workdir, config=cfg, scope="working-tree", persist_log=False)
    result = gm.run_all()
    lane = next(r for r in result.results if r.name == "data_protection")
    assert lane.passed is False
    assert "invalid data_protection policy" in lane.error


def test_guard_manager_non_bool_enabled_still_fails_loud(tmp_path) -> None:
    workdir = _git_repo(tmp_path)
    _tracked(workdir, "a.py", "x = 1\n")
    cfg = {
        "guards": {"secrets": False, "lint": False, "tests": False, "allow_skips": True},
        "data_protection": {"enabled": "yes"},
    }
    gm = GuardManager(workdir, config=cfg, scope="working-tree", persist_log=False)
    result = gm.run_all()
    lane = next(r for r in result.results if r.name == "data_protection")
    assert lane.passed is False
    assert "enabled must be true or false" in lane.error


def test_run_log_is_scrubbed(tmp_path) -> None:
    workdir = _git_repo(tmp_path)
    canary = "canary.email.alpha@example.com"
    _tracked(workdir, "src/leak.py", f"CONTACT = {canary!r}\n")
    cfg = {
        "guards": {"secrets": False, "lint": False, "tests": False, "allow_skips": True},
        "data_protection": DP_CONFIG,
    }
    gm = GuardManager(workdir, config=cfg, scope="working-tree", persist_log=True)
    result = gm.run_all()
    log_path = result.extra.get("guard_log")
    assert log_path and os.path.isfile(log_path)
    with open(log_path, encoding="utf-8") as handle:
        content = handle.read()
    assert canary not in content
    assert "[REDACTED:pii]" in content


def test_guard_lane_never_raises_on_binary(tmp_path) -> None:
    workdir = _git_repo(tmp_path)
    os.makedirs(os.path.join(workdir, "src"))
    with open(os.path.join(workdir, "src", "blob.bin"), "wb") as handle:
        handle.write(b"\x00\x01canary.email.alpha@example.com\x00")
    policy = DataProtectionPolicy.from_dict(DP_CONFIG)
    result = check_data_protection(workdir, ["src/blob.bin"], policy=policy)
    assert result.scanned_files == 0


# ── Secrets independence (AC4/AC7) ───────────────────────────────────────────


def test_data_protection_cannot_disable_secrets_lane(tmp_path) -> None:
    workdir = _git_repo(tmp_path)
    _tracked(workdir, "a.py", "x = 1\n")
    cfg = {
        "guards": {"secrets": True, "lint": False, "tests": False, "allow_skips": True},
        "data_protection": DP_CONFIG,
    }
    gm = GuardManager(workdir, config=cfg, scope="working-tree", persist_log=False)
    result = gm.run_all()
    names = [r.name for r in result.results]
    assert "secrets" in names
    assert "data_protection" in names
    # The secrets lane result is untouched by the data-protection policy.
    secrets_lane = next(r for r in result.results if r.name == "secrets")
    assert secrets_lane.error == ""


def test_secrets_config_is_never_read_by_data_protection() -> None:
    policy = DataProtectionPolicy.from_dict(
        {"enabled": True, "categories": DP_CONFIG["categories"]}
    )
    # No exception surface can name a secret scanner or its patterns.
    assert all(e.category != CATEGORY_CREDENTIALS for e in policy.exceptions)
    assert CATEGORY_CREDENTIALS not in policy.categories


# ── Tier-1 judge surface (AC1) ───────────────────────────────────────────────


def test_tier1_plan_includes_data_protection_step_when_enabled() -> None:
    steps, marker = tier1_plan(
        ".", {"data_protection": {"enabled": True, "categories": DP_CONFIG["categories"]}}
    )
    ids = [s["id"] for s in steps]
    assert "data_protection" in ids
    assert ids.index("data_protection") == ids.index("secrets") + 1
    assert "data_protection" in marker["coverage"]


def test_tier1_plan_omits_step_when_absent() -> None:
    steps, marker = tier1_plan(".", {})
    assert "data_protection" not in {s["id"] for s in steps}
    assert "data_protection" not in marker["coverage"]


def test_tier1_data_protection_step_uses_guard_manager() -> None:
    steps, _ = tier1_plan(".", {"data_protection": {"enabled": True}})
    step = next(s for s in steps if s["id"] == "data_protection")
    assert "GuardManager" in step["run"]
    assert "_check_data_protection" in step["run"]


def test_data_protection_enabled_helper_requires_literal_true() -> None:
    assert _data_protection_enabled({"data_protection": {"enabled": True}}) is True
    assert _data_protection_enabled({"data_protection": {"enabled": False}}) is False
    assert _data_protection_enabled({"data_protection": "yes"}) is False
    assert _data_protection_enabled({}) is False


# ── serialization ────────────────────────────────────────────────────────────


def test_policy_to_dict_round_trips() -> None:
    policy = DataProtectionPolicy.from_dict(
        {
            "enabled": True,
            "categories": DP_CONFIG["categories"],
            "exceptions": [
                {
                    "category": CATEGORY_IP,
                    "match": "198.51.100.7",
                    "scope": "docs/*",
                    "reason": "documentation address",
                }
            ],
        }
    )
    clone = DataProtectionPolicy.from_dict(policy.to_dict())
    assert clone.to_dict() == policy.to_dict()


def test_findings_are_serializable_and_line_numbered() -> None:
    policy = _max_sensitivity_policy()
    result = policy.scan("first line\nsecond canary.email.alpha@example.com\n")
    finding = result.active[0]
    assert finding.line == 2
    json.dumps(finding.to_dict())
    assert "value_redacted" in finding.to_dict()


def test_judge_status_dict_surfaces_data_protection_evidence() -> None:
    from types import SimpleNamespace

    from engine.judge import judge_result_to_dict
    from engine.types import Tier1Result

    canary = "canary.email.alpha@example.com"
    status = {
        "blocked": 1,
        "warned": 0,
        "suppressed": 0,
        "scanned_files": 1,
        "findings": [
            {
                "category": CATEGORY_PII,
                "rule": "email",
                "line": 1,
                "confidence": 0.95,
                "detection": DETECTION_BLOCK,
                "handling": HANDLING_REDACT,
                "suppressed": False,
                "value_redacted": "[REDACTED:pii]",
            }
        ],
    }
    result = SimpleNamespace(
        passed=False,
        tier1=Tier1Result(passed=False, results=[], extra={"data_protection": status}),
        tier2=None,
        pipeline_result=None,
        quality_snapshot=None,
    )
    payload = judge_result_to_dict("GR-146", "/tmp/repo", result)
    assert payload["data_protection"] == status
    assert canary not in json.dumps(payload)
