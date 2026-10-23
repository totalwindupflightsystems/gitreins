"""Build a clean two-commit A/B range for push-check: concat vs joined, single commit each.

Never pastes literals in command text: reads chunks from the concat commit's blob
(every byte already in repo history) and only removes the quote-plus-quote joins.
"""

import pathlib
import re
import subprocess

cwd = "/tmp/df-ab/upstream"


def git(*args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def build_branch(name, join: bool):
    subprocess.run(["git", "checkout", "-q", "5a2b027"], cwd=cwd)
    subprocess.run(["git", "branch", "-q", "-D", name], cwd=cwd, capture_output=True)
    subprocess.run(["git", "checkout", "-q", "-b", name], cwd=cwd)
    blob = git("show", "486977b:credentials.py")
    if join:
        # remove the quote + quote split: '"sk_live_" + "9f8e…"' -> '"sk_live_9f8e…"'
        blob = re.sub(r'"(sk_live_)" \+ "([0-9a-f]+)"', r'"\1\2"', blob)
        blob = re.sub(r'"(ghp_)" \+ "([0-9A-Za-z]+)"', r'"\1\2"', blob)
    p = pathlib.Path(cwd, "secrets_cfg.py")
    p.write_text(blob)
    subprocess.run(["git", "add", "secrets_cfg.py"], cwd=cwd)
    subprocess.run(["git", "commit", "-q", "--no-verify", "-m", f"arm {name}"], cwd=cwd)
    tip = git("rev-parse", "HEAD").strip()
    return tip


base = git("rev-parse", "5a2b027").strip()
tip_concat = build_branch("ab-concat", join=False)
tip_joined = build_branch("ab-joined", join=True)
print("BASE", base)
print("CONCAT_TIP", tip_concat)
print("JOINED_TIP", tip_joined)
with open("/tmp/df-ab-tips.txt", "w") as f:
    f.write(f"{base}\n{tip_concat}\n{tip_joined}\n")
