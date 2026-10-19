#!/bin/bash
# Rebuild the report, check it for private data, commit and push. Run by cron daily.
set -euo pipefail
cd "$(dirname "$0")/.."

/usr/bin/python3 collect/build.py
git add -A
if ! /usr/bin/python3 collect/check_private.py; then
    git reset -q
    exit 1
fi
if git diff --cached --quiet; then
    exit 0
fi
git commit -q -m "Update report $(date +%F)"
git push -q origin main
