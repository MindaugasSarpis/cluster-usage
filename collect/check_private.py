#!/usr/bin/python3
"""Refuse to publish if any file in the repo names a local account or leaks host details.

Checks every file git would publish (tracked or staged) for:
  - any login name with uid 1000-59999 (people and service accounts), as a whole
    word, case-insensitive;
  - any other login name (system accounts such as root or slurm, whose names are
    also ordinary words) as a quoted string in the data files, where an
    un-anonymised key would show up;
  - home directory paths, IPv4 addresses and the sampler's "uid<n>" fallback names.
Exits 1 and prints file:line for each hit.
"""
import pwd
import re
import subprocess
import sys

files = subprocess.run(["git", "ls-files", "--cached", "-z"], capture_output=True,
                       check=True).stdout.decode().split("\0")
files = [f for f in files if f]

people, system = [], []
for p in pwd.getpwall():
    (people if 1000 <= p.pw_uid < 60000 else system).append(p.pw_name)

checks = [(re.compile(r"(?<![\w-])(%s)(?![\w-])" % "|".join(map(re.escape, people)), re.I), None)]
checks.append((re.compile(r'"(%s)"' % "|".join(map(re.escape, system))), "docs/data/"))
checks.append((re.compile(r"/home/\w"), None))
checks.append((re.compile(r"(?<![\d.])(\d{1,3}\.){3}\d{1,3}(?![\d.])"), None))
checks.append((re.compile(r"\buid\d+\b"), None))

hits = 0
for path in files:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        continue
    for n, line in enumerate(lines, 1):
        for rx, only in checks:
            if only and not path.startswith(only):
                continue
            m = rx.search(line)
            if m:
                hits += 1
                print("%s:%d: %r" % (path, n, m.group(0)), file=sys.stderr)
if hits:
    print("check_private: %d hit(s), not publishing" % hits, file=sys.stderr)
    sys.exit(1)
print("check_private: %d files clean" % len(files))
