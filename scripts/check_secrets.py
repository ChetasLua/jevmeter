"""Fail if anything that looks like an API key is about to be committed.

    python scripts/check_secrets.py            # scan tracked + staged files
    python scripts/check_secrets.py --install  # also run it automatically before every git commit
"""
import os
import re
import subprocess
import sys

PATTERNS = [
    re.compile(r"apikey_[0-9a-f]{20,}_[0-9a-f]{20,}"),              # TypeSafe
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}"),          # OpenAI / Anthropic style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),                      # GitHub tokens
    re.compile(r"TYPESAFE_API_KEY\s*=\s*['\"]?[A-Za-z0-9_]{12,}"),     # a key pasted into an env line
]


def files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], capture_output=True, text=True).stdout
    return [f for f in out.splitlines() if f and os.path.isfile(f)]


def main():
    if "--install" in sys.argv:
        hook = os.path.join(".git", "hooks", "pre-commit")
        with open(hook, "w") as h:
            h.write("#!/bin/sh\nexec python3 scripts/check_secrets.py\n")
        os.chmod(hook, 0o755)
        print("installed pre-commit hook")
    bad = []
    for f in files():
        if f.endswith((".png", ".jpg", ".gif", ".mp4", ".wav")) or f == "scripts/check_secrets.py":
            continue
        try:
            text = open(f, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for p in PATTERNS:
            for m in p.finditer(text):
                bad.append(f"{f}: {m.group(0)[:14]}…")
    if bad:
        print("Possible secrets found; remove them before committing:\n  " + "\n  ".join(bad))
        sys.exit(1)
    print("no secrets found")


if __name__ == "__main__":
    main()
