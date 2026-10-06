# Tier-1 Data Protection

GitReins' Tier 1 already fails closed on **credentials** (the `secrets` lane:
gitleaks plus the built-in cross-check). This document covers the *opt-in*
second filter that catches the other classes an agent can leak into a
repository: **personally identifying information (PII)** and **network
identifiers** (IPv4/IPv6 literals and CIDR ranges).

Everything here is driven by one policy object,
`engine.data_protection.DataProtectionPolicy`, built from a single top-level
`data_protection:` block in `.gitreins/config.yaml`. The commit-time guard
(`engine.guard_manager`) and the Tier-1 judge pipeline
(`engine.pipeline.tier1_plan`) both build the policy through
`engine.data_protection.build_policy`, so a green guard and a green verdict can
never disagree about what a policy means.

> **Off by default.** Omitting the `data_protection:` block — or setting
> `enabled: false` — is a no-op. Existing repositories are byte-for-byte
> unchanged until an operator opts in.

## Sensitive-data category inventory

| Category | What it covers | Detector | Configurable here? | Notes |
|---|---|---|---|---|
| `credentials` | API keys, tokens, passwords, private keys | `secrets` lane (gitleaks + built-in cross-check) | **No** | Always on, fail-closed. The data-protection policy cannot filter, except or weaken it — any attempt is rejected at config load. |
| `pii` | emails, phone numbers, SSNs, passport numbers, medical identifiers, financial identifiers (cards/IBANs), labelled personal names | `data_protection` | Yes | Regex + validator based; confidence-filtered per class. |
| `ip_addresses` | IPv4 and IPv6 literals, IPv4/IPv6 CIDR ranges | `data_protection` | Yes | Stdlib-validated, with an explicit `preserve_list` allowlist. |

The inventory itself is machine-readable (`CATEGORY_INVENTORY` in
`engine/data_protection.py`), and a test asserts the documented classes match
the implemented rules — the table above cannot drift from the code.

## Example configuration

```yaml
data_protection:
  # Master switch. Absent/false = the lane does not run at all.
  enabled: true

  categories:
    pii:
      detection: block      # off | warn | block
      handling: redact      # preserve | redact | replace
      confidence: 0.7       # class floor; lower it to opt into noisier classes
      classes:              # per-class overrides (optional)
        names: { enabled: false }
        email: { confidence: 0.95 }

    ip_addresses:
      detection: warn
      handling: replace     # [IP:IPV4] / [IP:IPV6] / [IP:CIDR]
      # Applies to ranges not matched by preserve_list. Defaults to `handling`.
      default_action: redact
      # CIDRs that are explicitly shared/documented and may stay visible.
      preserve_list:
        - 10.0.0.0/8
        - 2001:db8::/32

  # Narrow, auditable false positives. `reason` is required; `match` and/or
  # `rule` must narrow the exception — a category-wide exception is rejected.
  exceptions:
    - category: ip_addresses
      rule: ipv4
      match: 198.51.100.7
      scope: "docs/*"       # path glob; default "*"
      reason: "RFC 5737 documentation address"
```

## Detection levels and handling modes

`detection` decides whether a finding gates the commit:

| `detection` | Effect |
|---|---|
| `off` | The category is not scanned. |
| `warn` | Findings are reported (a `⚠` note) but do not fail the lane. |
| `block` | Any finding fails the lane (and therefore the commit / the Tier-1 stage). |

`handling` decides what happens to a detected value in output and artifacts:

| `handling` | Replacement |
|---|---|
| `preserve` | The raw value stays visible — **report-only** by explicit policy. |
| `redact` | `[REDACTED:<category>]` (e.g. `[REDACTED:pii]`). |
| `replace` | A class-specific placeholder (e.g. `[PII:EMAIL]`, `[IP:IPV4]`). |

## Rule precedence

For every detected value, first match wins:

1. **Exceptions** — a matching `(category, rule, value, scope)` suppresses the
   finding (it is still counted and named in `suppressed`, so an exception can
   never hide its own existence).
2. **IP `preserve_list`** — an address or range covered by a preserved CIDR is
   handled as `preserve`.
3. **Class override → category default** — `classes.<rule>.<field>` beats the
   category field; for an unmatched IP the category `default_action` applies if
   set, otherwise the category `handling`.

CIDR ranges are matched before the literal inside them, so `192.168.1.0/24`
is one `cidr` finding, not a `cidr` plus an `ipv4`.

## Confidence / sensitivity

Each PII class carries a base confidence. A class is reported only when its
confidence is at or above the effective floor: the class override, else the
category `confidence`, else the built-in default (`pii: 0.7`, `ip_addresses:
0.0`). The default PII floor deliberately leaves the noisy, context-free classes
quiet:

| Class | Base confidence | Fires at default 0.7? |
|---|---|---|
| `email` | 0.95 | yes |
| `ssn` | 0.90 | yes |
| `financial` | 0.85 | yes |
| `phone` | 0.60 | no — lower `confidence` to enable |
| `medical` | 0.55 | no |
| `passport` | 0.45 | no |
| `names` | 0.40 | no |

Lowering `confidence` (or a class override) is the documented sensitivity knob.

## Validation

A malformed or ambiguous policy is **rejected**, never silently broadened. The
loader raises `DataProtectionConfigError` naming the offending key for, among
others: an unknown category or class; a bad `detection`/`handling`/
`default_action`; a confidence outside `[0, 1]`; an invalid `preserve_list`
entry; **any** `preserve_list` entry with a `/0` prefix (`0.0.0.0/0` or `::/0`
would allow every address); a `preserve_list` or `default_action` on a
non-IP category; an exception with no `reason`; an exception that narrows
nothing (`match` and `rule` both absent); and **any** attempt to configure or
except the `credentials` category. In the guard the malformed policy surfaces as
a FAIL naming the key, so it blocks the commit instead of running with defaults.

## Output redaction

When a category's handling is `redact` or `replace`, detected values are
scrubbed from:

* the lane's own console output (values print as placeholders),
* the run log written by the guard (`_redacted_for_artifact`),
* the verdict history artifacts `verdict.json` / `summary.md`
  (`VerdictPersister._scrub`),
* the structured findings carried in `Tier1Result.extra["data_protection"]`.

The redaction is applied to *every* lane's text before the run log and verdict
are written, so a value echoed by an unrelated lane cannot survive there. A
`preserve` category is intentionally left visible (report-only). Tests assert
that the fixture canary strings never appear in scrubbed output.

## Measured results (synthetic fixtures)

The labeled corpus under `tests/fixtures/data_protection/` is synthetic only.
Numbers are produced by `engine.data_protection.measure_fixtures` over that
corpus at maximum sensitivity (confidence `0.0`), not written by hand:

| Corpus | True positives | Missed | False positives |
|---|---|---|---|
| `pii_labeled.json` (9 fixtures) | 9 rules across email/phone/ssn/passport/medical/financial/names | 0 | 0 |
| `network_labeled.json` (6 fixtures) | ipv4 ×2, ipv6 ×2, cidr ×2 | 0 | 0 |
| `benign_near_matches.json` (11 fixtures) | — | — | 1 (`passport` on order code `AB123456`) |

Measured false positives: one — a 2-letter + 6-9-digit order code is
passport-shaped. This is the documented reason `passport` ships below the
default confidence floor.

### Known limits / missed detections

* **Label-anchored classes** (`medical`, `names`) only fire on an explicit label
  (`MRN:`, `diagnosis:`, `patient name:`). An unlabelled medical code or bare
  personal name is not detected.
* **Phone** is precision-first: a separator-laden run of 12+ digits is treated as
  card/account-shaped and skipped, so a number written with an unusual grouping
  may be missed.
* **SSN** requires the dashed form; a separator-free 9-digit run is not
  classified.
* **Financial** requires a scheme checksum (Luhn for cards, mod-97 for IBANs);
  account numbers without one are out of scope.
* **IPv4** is validated (each octet 0-255) but a four-component version string
  is syntactically an address — use `preserve_list` for such cases.
* **Obfuscated values** (`alice at example dot com`) are not detected; regex
  cannot reconstruct them, and no attempt is made to guess.

## Extensibility

Adding a filter is a local change: add a `DetectionRule` and its iterator to
`PII_RULES`/`IP_RULES` (or a new category to `CATEGORY_INVENTORY` plus
`RULES_BY_CATEGORY`), and it appears in validation, `preserve_list` precedence,
the confidence table and the docs test automatically. There is no global
allowlist to edit and no way to add a category-wide exception — exceptions are
always per-finding and scoped.
