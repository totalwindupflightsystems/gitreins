import re

# NOTE: the 40-char body is redacted (GitHub push protection blocks the real
# bytes in-tree). The finding does not depend on the body's VALUE: any
# 28+ contiguous A-Za-z0-9 body after a split `"ghp_" + "…"` prefix defeats the
# same three regex classes. Reproduce by regenerating the body locally
# (see df-ab-build-branches.py, which reads the byte chunks from repo history).

p = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")
line = 'STRIPE_KEY = "sk_live_" + "<40-hex-body-redacted-for-push>"'
print("sk- matches concat line:", bool(p.search(line)))

p2 = re.compile(
    r"(?im)^\s*(?:[A-Z0-9_]*(?:PASSWORD|TOKEN|SECRET|API_KEY)[A-Z0-9_]*)\s*=\s*[^\s#]{12,}"
)
print("assignment matches concat line:", bool(p2.search(line)))

joined = 'STRIPE_KEY = "sk_live_<40-hex-body-redacted-for-push>"'
print("assignment matches joined line:", bool(p2.search(joined)))
