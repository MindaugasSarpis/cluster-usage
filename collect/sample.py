#!/usr/bin/python3
"""Take one usage sample of this host and append it as a JSON line.

Run by cron once a minute. Reads only world-readable sources: /proc/<pid>/stat
for every process, /proc/stat, /proc/loadavg, /proc/meminfo and squeue.
Output goes to ~/.local/share/cluster_usage/raw/<YYYY-MM-DD>.jsonl, which
contains real login names and never leaves this machine.

CPU per user is the change in utime+stime of each process between this sample
and the previous one (kept in state.json). Time of processes that exited is
recovered from the cutime+cstime their parent gains when it reaps them, minus
what was already counted for them while they were alive.
"""
import fcntl
import json
import os
import pwd
import subprocess
import sys
import time

DATA = os.path.expanduser("~/.local/share/cluster_usage")
RAW = os.path.join(DATA, "raw")
STATE = os.path.join(DATA, "state.json")
LOCK = os.path.join(DATA, "sample.lock")
HZ = os.sysconf("SC_CLK_TCK")
PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024
MAX_GAP = 300  # seconds; older state is not used for CPU deltas


def read(path):
    with open(path) as f:
        return f.read()


def scan_procs():
    """Return {pid: (starttime, uid, ppid, own_ticks, child_ticks, rss_kb, threads)}."""
    procs = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            uid = os.stat("/proc/" + name).st_uid
            stat = read("/proc/%s/stat" % name)
        except OSError:  # process exited mid-scan
            continue
        f = stat[stat.rfind(")") + 2:].split()
        procs[int(name)] = (
            int(f[19]),                  # starttime
            uid,
            int(f[1]),                   # ppid
            int(f[11]) + int(f[12]),     # utime + stime
            int(f[13]) + int(f[14]),     # cutime + cstime
            int(f[21]) * PAGE_KB,        # rss
            int(f[17]),                  # num_threads
        )
    return procs


def host_cpu():
    """Return (busy_ticks, total_ticks) summed over all CPUs."""
    f = [int(x) for x in read("/proc/stat").split("\n", 1)[0].split()[1:]]
    user, nice, system, idle, iowait, irq, softirq, steal = f[:8]
    busy = user + nice + system + irq + softirq + steal
    return busy, busy + idle + iowait


def meminfo():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0])
    return {"total_kb": m["MemTotal"], "avail_kb": m["MemAvailable"],
            "swap_total_kb": m["SwapTotal"], "swap_free_kb": m["SwapFree"]}


def squeue():
    """Return {user: {run_jobs, run_cpus, pend_jobs, pend_cpus}} or None on failure."""
    try:
        out = subprocess.run(["squeue", "-h", "-o", "%u|%C|%T"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    q = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        user, cpus, state = parts[0], int(parts[1]), parts[2]
        u = q.setdefault(user, {"run_jobs": 0, "run_cpus": 0, "pend_jobs": 0, "pend_cpus": 0})
        if state == "RUNNING":
            u["run_jobs"] += 1
            u["run_cpus"] += cpus
        elif state == "PENDING":
            u["pend_jobs"] += 1
            u["pend_cpus"] += cpus
    return q


_names = {}


def uname(uid):
    if uid not in _names:
        try:
            _names[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _names[uid] = "uid%d" % uid
    return _names[uid]


def cpu_ticks_by_uid(prev, procs):
    """CPU ticks used per uid since the previous scan."""
    ticks = {}
    child_pool = {}  # uid -> cutime gained by survivors minus totals of vanished procs
    for pid, p in procs.items():
        start, uid, _, own, child = p[:5]
        old = prev.get(pid)
        if old and old[0] == start:
            d_own, d_child = own - old[3], child - old[4]
        else:  # started since the previous scan
            d_own, d_child = own, child
        ticks[uid] = ticks.get(uid, 0) + d_own
        child_pool[uid] = child_pool.get(uid, 0) + d_child
    for pid, old in prev.items():
        p = procs.get(pid)
        if p is None or p[0] != old[0]:  # vanished: its counted time reappears in a reaper
            child_pool[old[1]] = child_pool.get(old[1], 0) - old[3] - old[4]
    for uid, extra in child_pool.items():
        # root reaps orphans and Slurm job steps of every user; its cutime would
        # double count them, so only non-root users get the reaped-child credit.
        if uid != 0 and extra > 0:
            ticks[uid] = ticks.get(uid, 0) + extra
    # A root process other than init whose children all belong to one user (such
    # as slurmstepd under a job) passes on what it reaped beyond the children
    # already counted: work that started and ended between two scans.
    kids, reaped = {}, {}
    for scan in (prev, procs):
        for p in scan.values():
            kids.setdefault(p[2], set()).add(p[1])
    for pid, old in prev.items():
        p = procs.get(pid)
        if p is None or p[0] != old[0]:
            reaped[old[2]] = reaped.get(old[2], 0) + old[3] + old[4]
    for pid, p in procs.items():
        users = kids.get(pid, ())
        if p[1] != 0 or pid == 1 or len(users) != 1 or 0 in users:
            continue
        old = prev.get(pid)
        d_child = p[4] - old[4] if old and old[0] == p[0] else p[4]
        extra = d_child - reaped.get(pid, 0)
        if extra > 0:
            uid = next(iter(users))
            ticks[uid] = ticks.get(uid, 0) + extra
    # init reaps orphans: vanished processes whose parent was init or is gone too.
    # Beyond what was already counted for them, its cutime growth is their final
    # stretch of work; split it between their owners by what each had used.
    init, old_init = procs.get(1), prev.get(1)
    if init and old_init and init[0] == old_init[0]:
        orphans = {}
        for pid, old in prev.items():
            p = procs.get(pid)
            parent = procs.get(old[2])
            gone_parent = parent is None or parent[0] != prev.get(old[2], (None,))[0]
            if (p is None or p[0] != old[0]) and (old[2] == 1 or gone_parent):
                orphans[old[1]] = orphans.get(old[1], 0) + old[3] + old[4]
        counted = sum(orphans.values())
        extra = init[4] - old_init[4] - counted
        if extra > 0 and counted > 0:
            for uid, used in orphans.items():
                ticks[uid] = ticks.get(uid, 0) + extra * used / counted
    return ticks


def main():
    os.umask(0o077)
    os.makedirs(RAW, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)  # previous run still going

    now = time.time()
    procs = scan_procs()
    busy, total = host_cpu()
    try:
        state = json.loads(read(STATE))
        prev_procs = {int(k): v for k, v in state["procs"].items()}
        dt = now - state["t"]
        if not 0 < dt <= MAX_GAP:
            state = None
    except (OSError, ValueError, KeyError):
        state = None

    users = {}
    for p in procs.values():
        u = users.setdefault(uname(p[1]), {"cpu": None, "rss_kb": 0, "nproc": 0, "nthr": 0})
        u["rss_kb"] += p[5]
        u["nproc"] += 1
        u["nthr"] += p[6]

    sample = {"t": round(now, 1), "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
              "dt": None, "ncpu": os.cpu_count(), "host_busy": None,
              "load": [float(x) for x in read("/proc/loadavg").split()[:3]], "mem": meminfo()}
    if state:
        sample["dt"] = round(dt, 2)
        d_total = total - state["cpu"][1]
        if d_total > 0:
            sample["host_busy"] = round((busy - state["cpu"][0]) / d_total * os.cpu_count(), 3)
        for uid, t in cpu_ticks_by_uid(prev_procs, procs).items():
            name = uname(uid)
            u = users.setdefault(name, {"cpu": None, "rss_kb": 0, "nproc": 0, "nthr": 0})
            u["cpu"] = round(t / HZ / dt, 3)
        for u in users.values():
            if u["cpu"] is None:
                u["cpu"] = 0.0

    q = squeue()
    sample["slurm_ok"] = q is not None
    for name, s in (q or {}).items():
        users.setdefault(name, {"cpu": 0.0 if state else None, "rss_kb": 0, "nproc": 0, "nthr": 0})["slurm"] = s
    sample["users"] = users

    day = time.strftime("%Y-%m-%d", time.localtime(now))
    with open(os.path.join(RAW, day + ".jsonl"), "a") as f:
        f.write(json.dumps(sample, separators=(",", ":")) + "\n")

    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"t": now, "cpu": [busy, total],
                   "procs": {str(k): v for k, v in procs.items()}}, f, separators=(",", ":"))
    os.replace(tmp, STATE)


if __name__ == "__main__":
    main()
