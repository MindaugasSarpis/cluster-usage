#!/usr/bin/python3
"""Roll the private samples up into the anonymised JSON files under docs/data/.

Run by cron once a day (via publish.sh). Reads ~/.local/share/cluster_usage/
(raw samples, the private letter key, config) plus two bootstrap sources that
predate the sampler: the since-boot cgroup CPU counters and the login history
in /var/log/wtmp*. Writes only letters, never login names or uids.
"""
import datetime
import glob
import gzip
import json
import os
import pwd
import random
import string
import struct
import time

DATA = os.path.expanduser("~/.local/share/cluster_usage")
RAW = os.path.join(DATA, "raw")
ROLLUP = os.path.join(DATA, "rollup")
KEY = os.path.join(DATA, "key.json")
CONFIG = os.path.join(DATA, "config.json")
LOGIN_CACHE = os.path.join(DATA, "logins.json")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "data")
CGROUP = "/sys/fs/cgroup"
HZ = os.sysconf("SC_CLK_TCK")
MAX_GAP = 300
FEATURED = 6       # users with their own colour; the rest fold into "Other users"
SYSTEM = "system"
OTHER = "other"
GIB_KB = 1024 * 1024


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj, private=False):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"), sort_keys=not private)
    os.replace(tmp, path)


def day_of(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def day_start(day):
    return time.mktime(time.strptime(day, "%Y-%m-%d"))


# ---------------------------------------------------------------- anonymising

class Anon:
    """Maps login names to letters (A, B, ... Z, AA, ...) or to 'system'."""

    def __init__(self):
        cfg = load_json(CONFIG, {})
        self.system_names = set(cfg.get("system_accounts", []))
        self.key = load_json(KEY, {})
        self.pending = set()

    def is_system(self, name):
        if name in self.system_names:
            return True
        try:
            uid = pwd.getpwnam(name).pw_uid
        except KeyError:
            return True  # unknown or deleted account: do not single it out
        return uid < 1000 or uid >= 60000

    def group(self, name):
        if self.is_system(name):
            return SYSTEM
        if name not in self.key:
            self.pending.add(name)
            return "?" + name  # replaced in assign()
        return self.key[name]

    def assign(self):
        """Give new names the next free letters in random order, so letters don't follow names."""
        if not self.pending:
            return
        used = set(self.key.values())
        new = sorted(self.pending)
        random.SystemRandom().shuffle(new)
        n = 0
        for name in new:
            while True:
                letter = letters(n)
                n += 1
                if letter not in used:
                    break
            self.key[name] = letter
            used.add(letter)
        self.pending.clear()
        write_json(KEY, self.key, private=True)

    def resolve(self, g):
        return self.key[g[1:]] if g.startswith("?") else g


def letters(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = string.ascii_uppercase[r] + s
    return s


# ---------------------------------------------------------------- raw samples

def read_samples(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue  # a line cut short by a crash


def compress_old_raw(today):
    for path in glob.glob(os.path.join(RAW, "*.jsonl")):
        day = os.path.basename(path)[:10]
        if (day_start(today) - day_start(day)) / 86400 > 7:
            with open(path, "rb") as src, gzip.open(path + ".gz", "wb") as dst:
                dst.write(src.read())
            os.remove(path)


def new_hour():
    return {"cov": 0.0, "cpu_cov": 0.0, "sq_cov": 0.0, "busy_s": 0.0, "attr_s": 0.0,
            "load_s": 0.0, "used_kb_s": 0.0, "ncpu": 0, "users": {}}


def roll_day(path):
    """Hourly sums of one raw file: {hour: {...}} keyed by login name (private)."""
    hours = {}
    for s in read_samples(path):
        h = hours.setdefault(time.strftime("%H", time.localtime(s["t"])), new_hour())
        dur = s["dt"] if s["dt"] and s["dt"] <= MAX_GAP else 60.0
        h["cov"] += dur
        h["ncpu"] = s["ncpu"]
        h["load_s"] += s["load"][0] * dur
        h["used_kb_s"] += (s["mem"]["total_kb"] - s["mem"]["avail_kb"]) * dur
        has_cpu = s["dt"] is not None and s["host_busy"] is not None
        if has_cpu:
            h["cpu_cov"] += dur
            h["busy_s"] += s["host_busy"] * dur
        if s.get("slurm_ok"):
            h["sq_cov"] += dur
        for name, u in s["users"].items():
            r = h["users"].setdefault(name, {"cpu_s": 0.0, "rss_s": 0.0, "rss_max": 0,
                                             "nproc_s": 0.0, "run_cpus_s": 0.0,
                                             "run_jobs_s": 0.0, "pend_jobs_s": 0.0})
            if has_cpu and u["cpu"] is not None:
                r["cpu_s"] += u["cpu"] * dur
                h["attr_s"] += u["cpu"] * dur
            r["rss_s"] += u["rss_kb"] * dur
            r["rss_max"] = max(r["rss_max"], u["rss_kb"])
            r["nproc_s"] += u["nproc"] * dur
            q = u.get("slurm")
            if q:
                r["run_cpus_s"] += q["run_cpus"] * dur
                r["run_jobs_s"] += q["run_jobs"] * dur
                r["pend_jobs_s"] += q["pend_jobs"] * dur
    return hours


def rollups(today):
    """Return {day: hours}, re-rolling days whose raw file is newer than the cached rollup."""
    os.makedirs(ROLLUP, exist_ok=True)
    days = {}
    for path in sorted(glob.glob(os.path.join(RAW, "*.jsonl*"))):
        day = os.path.basename(path)[:10]
        cache = os.path.join(ROLLUP, day + ".json")
        if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(path):
            days[day] = load_json(cache, {})
        else:
            days[day] = roll_day(path)
            write_json(cache, days[day], private=True)
    return days


# ---------------------------------------------------------------- bootstrap: logins

UTMP = struct.Struct("<h2xi32s4s32s256shhiii4i20s")
RUN_LVL, BOOT_TIME, USER_PROCESS, DEAD_PROCESS = 1, 2, 7, 8


def utmp_records(path):
    try:
        with open(path, "rb") as f:
            buf = f.read()
    except OSError:
        return
    for off in range(0, len(buf) - UTMP.size + 1, UTMP.size):
        r = UTMP.unpack_from(buf, off)
        yield (r[0], r[1], r[2].split(b"\0")[0].decode(errors="replace"),
               r[4].split(b"\0")[0].decode(errors="replace"), r[9])


def wtmp_sessions(now):
    """Login sessions [name, start, end] from wtmp.1 + wtmp, as `last` would pair them."""
    live = {(line, pid) for typ, pid, line, _, _ in utmp_records("/var/run/utmp")
            if typ == USER_PROCESS}
    sessions, open_ = [], {}
    first = None
    for path in ("/var/log/wtmp.1", "/var/log/wtmp"):
        for typ, pid, line, user, t in utmp_records(path):
            first = t if first is None else min(first, t)
            if typ == USER_PROCESS and user:
                if line in open_:
                    s = open_.pop(line)
                    sessions.append([s[0], s[1], t])
                open_[line] = (user, t, pid)
            elif typ == DEAD_PROCESS and line in open_:
                s = open_.pop(line)
                sessions.append([s[0], s[1], t])
            elif typ == BOOT_TIME or (typ == RUN_LVL and user == "shutdown"):
                for s in open_.values():
                    sessions.append([s[0], s[1], t])
                open_.clear()
    for line, s in open_.items():
        if (line, s[2]) in live:  # still logged in
            sessions.append([s[0], s[1], now])
        # otherwise the logout record is missing ("gone - no logout"): not counted
    return sessions, first


def merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def month_starts(t0, t1):
    tm = time.localtime(t0)
    y, m = tm.tm_year, tm.tm_mon
    while True:
        start = time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))
        if start > t1:
            return
        yield "%04d-%02d" % (y, m), start
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def login_hours(now):
    """{name: {month: hours with at least one session open}}, cached so wtmp rotation loses nothing."""
    sessions, first = wtmp_sessions(now)
    cache = load_json(LOGIN_CACHE, {"first": first, "intervals": {}})
    per = cache["intervals"]
    for name, s, e in sessions:
        if e > s:
            per.setdefault(name, []).append([s, e])
    per = {n: merge(iv) for n, iv in per.items()}
    first = min(x for x in (cache.get("first"), first) if x is not None)
    write_json(LOGIN_CACHE, {"first": first, "intervals": per}, private=True)
    months = list(month_starts(first, now))
    bounds = [(m, s, months[i + 1][1] if i + 1 < len(months) else now)
              for i, (m, s) in enumerate(months)]
    out = {}
    for name, iv in per.items():
        row = out.setdefault(name, {})
        for s, e in iv:
            for m, ms, me in bounds:
                o = min(e, me) - max(s, ms)
                if o > 0:
                    row[m] = row.get(m, 0) + o / 3600
    return out, [m for m, _, _ in bounds], first


# ---------------------------------------------------------------- bootstrap: since boot

def cg_usage(rel):
    try:
        with open(os.path.join(CGROUP, rel, "cpu.stat")) as f:
            for line in f:
                k, v = line.split()
                if k == "usage_usec":
                    return int(v) / 1e6 / 3600
    except OSError:
        return None


def since_boot():
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime"):
                btime = int(line.split()[1])
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    host = (v[0] + v[1] + v[2] + v[5] + v[6] + v[7]) / HZ / 3600
    users = {}
    for path in glob.glob(os.path.join(CGROUP, "user.slice", "user-*.slice")):
        uid = int(os.path.basename(path)[5:-6])
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            name = "uid%d" % uid
        h = cg_usage(os.path.relpath(path, CGROUP))
        if h is not None:
            users[name] = h
    return {"boot": btime, "host": host, "users": users,
            "user_slice": cg_usage("user.slice"), "system_slice": cg_usage("system.slice"),
            "slurmd": cg_usage("system.slice/slurmd.service")}


# ---------------------------------------------------------------- output

def series_dict():
    return {"cpu_h": 0.0, "mem_gb": 0.0, "mem_max_gb": 0.0, "slurm_cpus": 0.0,
            "slurm_jobs": 0.0, "pend_jobs": 0.0, "procs": 0.0}


def summarise(buckets, anon):
    """buckets: list of lists of hour dicts -> per-bucket host + per-group values."""
    rows = []
    for hours in buckets:
        cov = sum(h["cov"] for h in hours)
        cpu_cov = sum(h["cpu_cov"] for h in hours)
        sq_cov = sum(h["sq_cov"] for h in hours)
        ncpu = max([h["ncpu"] for h in hours] or [0])
        groups = {}
        for h in hours:
            for name, u in h["users"].items():
                g = groups.setdefault(anon.group(name), series_dict())
                g["cpu_h"] += u["cpu_s"] / 3600
                g["mem_gb"] += u["rss_s"] / GIB_KB
                g["mem_max_gb"] = max(g["mem_max_gb"], u["rss_max"] / GIB_KB)  # max per account
                g["procs"] += u["nproc_s"]
                g["slurm_cpus"] += u["run_cpus_s"]
                g["slurm_jobs"] += u["run_jobs_s"]
                g["pend_jobs"] += u["pend_jobs_s"]
        for g in groups.values():
            for k in ("mem_gb", "procs"):
                g[k] = g[k] / cov if cov else None
            for k in ("slurm_cpus", "slurm_jobs", "pend_jobs"):
                g[k] = g[k] / sq_cov if sq_cov else None
        busy = sum(h["busy_s"] for h in hours) / cpu_cov if cpu_cov else None
        rows.append({
            "coverage_h": cov / 3600,
            "busy_cores": busy,
            "attributed_cores": sum(h["attr_s"] for h in hours) / cpu_cov if cpu_cov else None,
            "load": sum(h["load_s"] for h in hours) / cov if cov else None,
            "mem_used_gb": sum(h["used_kb_s"] for h in hours) / cov / GIB_KB if cov else None,
            "util_pct": busy / ncpu * 100 if busy is not None and ncpu else None,
            "ncpu": ncpu,
            "groups": groups,
        })
    return rows


def columns(rows, labels, members):
    """Turn summarise() rows into column arrays keyed by member letter."""
    def r(x, n=3):
        return None if x is None else round(x, n)
    host = {k: [r(row[k]) for row in rows]
            for k in ("coverage_h", "busy_cores", "attributed_cores", "load",
                      "mem_used_gb", "util_pct", "ncpu")}
    series = {}
    for m in members:
        series[m] = {k: [r(row["groups"].get(m, {}).get(k)) if m in row["groups"]
                         else (0.0 if k in ("cpu_h", "mem_gb", "mem_max_gb", "procs")
                               and row["coverage_h"] else None)
                         for row in rows]
                     for k in series_dict()}
    return {"labels": labels, "host": host, "series": series}


def fold(groups_rows, featured):
    """Fold every non-featured letter into OTHER; keep SYSTEM separate."""
    for row in groups_rows:
        merged = {}
        for g, v in row["groups"].items():
            key = g if g in featured or g == SYSTEM else OTHER
            if key not in merged:
                merged[key] = dict(v)
            else:
                d = merged[key]
                for k in v:
                    if k == "mem_max_gb":
                        d[k] = max(d[k] or 0, v[k] or 0)
                    elif v[k] is not None:
                        d[k] = (d[k] or 0) + v[k]
        row["full_groups"] = row["groups"]
        row["groups"] = merged


def full_table(rows, labels):
    """Per-letter CPU-hours for every letter (no folding), for the table view."""
    letters_ = sorted({g for row in rows for g in row["full_groups"]}, key=sort_key)
    return {"labels": labels, "letters": letters_,
            "cpu_h": {g: [round(row["full_groups"].get(g, {}).get("cpu_h", 0.0), 3)
                          for row in rows] for g in letters_},
            "mem_gb": {g: [None if row["full_groups"].get(g, {}).get("mem_gb") is None else
                           round(row["full_groups"][g]["mem_gb"], 3) for row in rows]
                       for g in letters_}}


def sort_key(g):
    return (g in (OTHER, SYSTEM), g == SYSTEM, len(g), g)


def main():
    os.umask(0o077)
    os.makedirs(DATA, exist_ok=True)
    now = time.time()
    today = day_of(now)
    anon = Anon()
    compress_old_raw(today)
    days = rollups(today)

    logins, months, wtmp_first = login_hours(now)
    boot = since_boot()
    for name in list(logins) + list(boot["users"]):
        anon.group(name)
    for hours in days.values():
        for h in hours.values():
            for name in h["users"]:
                anon.group(name)
    anon.assign()

    # Daily, from the first sampled day to today.
    day_labels = []
    if days:
        t = day_start(min(days))
        while day_of(t) <= today:
            day_labels.append(day_of(t))
            t += 86400 + 3600  # step past DST changes, then snap to the day
            t = day_start(day_of(t))
    daily_rows = summarise([list(days.get(d, {}).values()) for d in day_labels], anon)

    # Hourly, the last 7 days (168 hours ending with the current hour).
    hour_now = time.mktime(time.localtime(now)[:4] + (0, 0, 0, 0, -1))
    hour_ts = [hour_now - 3600 * i for i in range(167, -1, -1)]
    hourly_buckets, hour_labels = [], []
    for t in hour_ts:
        lt = time.localtime(t)
        hour_labels.append(time.strftime("%Y-%m-%dT%H:00", lt))
        h = days.get(time.strftime("%Y-%m-%d", lt), {}).get(time.strftime("%H", lt))
        hourly_buckets.append([h] if h else [])
    hourly_rows = summarise(hourly_buckets, anon)

    # Featured letters: the biggest by sampled CPU-hours, then login hours, then since-boot.
    score = {}
    total_cpu = sum(g["cpu_h"] for row in daily_rows for g in row["groups"].values()) or 1
    total_login = sum(sum(m.values()) for m in logins.values()) or 1
    total_boot = sum(boot["users"].values()) or 1
    for row in daily_rows:
        for g, v in row["groups"].items():
            score[g] = score.get(g, 0) + v["cpu_h"] / total_cpu
    for name, m in logins.items():
        g = anon.group(name)
        score[g] = score.get(g, 0) + sum(m.values()) / total_login
    for name, h in boot["users"].items():
        g = anon.group(name)
        score[g] = score.get(g, 0) + h / total_boot
    score.pop(SYSTEM, None)
    featured = sorted(sorted(score, key=lambda g: -score[g])[:FEATURED], key=sort_key)
    members = featured + [OTHER, SYSTEM]

    fold(daily_rows, featured)
    fold(hourly_rows, featured)

    # Hour-of-week heatmap of busy cores, over every sampled hour.
    heat_sum = [[0.0] * 24 for _ in range(7)]
    heat_cov = [[0.0] * 24 for _ in range(7)]
    for day, hours in days.items():
        wd = time.strptime(day, "%Y-%m-%d").tm_wday
        for hh, h in hours.items():
            heat_sum[wd][int(hh)] += h["busy_s"]
            heat_cov[wd][int(hh)] += h["cpu_cov"]
    heat = [[round(heat_sum[d][h] / heat_cov[d][h], 2) if heat_cov[d][h] else None
             for h in range(24)] for d in range(7)]

    # Logins per letter per month.
    login_groups = {}
    for name, m in logins.items():
        g = anon.group(name)
        g = g if g in featured or g == SYSTEM else OTHER
        row = login_groups.setdefault(g, {})
        for k, v in m.items():
            row[k] = row.get(k, 0) + v
    login_all = {}
    for name, m in logins.items():
        row = login_all.setdefault(anon.group(name), {})
        for k, v in m.items():
            row[k] = row.get(k, 0) + v

    # Since boot.
    boot_users = {}
    for name, h in boot["users"].items():
        g = anon.group(name)
        boot_users[g] = boot_users.get(g, 0) + h
    live = sum(boot["users"].values())
    boot_parts = [{"label": g, "core_h": round(h, 1)}
                  for g, h in sorted(boot_users.items(), key=lambda x: sort_key(x[0]))]
    if boot["user_slice"] is not None:
        boot_parts.append({"label": "ended sessions", "core_h": round(max(boot["user_slice"] - live, 0), 1)})
    if boot["slurmd"] is not None:
        boot_parts.append({"label": "Slurm jobs", "core_h": round(boot["slurmd"], 1)})
    if boot["system_slice"] is not None and boot["slurmd"] is not None:
        boot_parts.append({"label": "other services", "core_h": round(boot["system_slice"] - boot["slurmd"], 1)})

    first_sample = None
    for d in sorted(days):
        if days[d]:
            first_sample = d
            break

    os.makedirs(OUT, exist_ok=True)
    stamp = datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
    write_json(os.path.join(OUT, "meta.json"), {
        "generated": stamp, "generated_t": int(now), "host": "gluon",
        "ncpu": os.cpu_count(), "first_sample_day": first_sample,
        "members": members, "featured": featured,
        "wtmp_first": int(wtmp_first) if wtmp_first else None,
        "timezone": time.strftime("%Z", time.localtime(now)),
    })
    write_json(os.path.join(OUT, "daily.json"),
               dict(columns(daily_rows, day_labels, members), full=full_table(daily_rows, day_labels)))
    write_json(os.path.join(OUT, "hourly.json"),
               dict(columns(hourly_rows, hour_labels, members), full=full_table(hourly_rows, hour_labels)))
    write_json(os.path.join(OUT, "heatmap.json"), {"busy_cores": heat, "days": len(days)})
    write_json(os.path.join(OUT, "logins.json"), {
        "months": months, "members": members,
        "hours": {g: [round(login_groups.get(g, {}).get(m, 0), 2) for m in months] for g in members},
        "full": {g: [round(login_all[g].get(m, 0), 2) for m in months]
                 for g in sorted(login_all, key=sort_key)},
    })
    write_json(os.path.join(OUT, "sinceboot.json"), {
        "boot": boot["boot"], "snapshot": stamp, "host_core_h": round(boot["host"], 1),
        "parts": boot_parts,
    })


if __name__ == "__main__":
    main()
