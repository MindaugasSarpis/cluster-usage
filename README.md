# cluster-usage

A public, anonymised report of who uses **gluon**, the head node of the
*mathematica* Slurm cluster: CPU, memory, logins and Slurm jobs per user,
with graphs.

**Report:** https://mindaugassarpis.github.io/cluster-usage/

Users are shown as letters (A, B, C …). The key from letter to account stays
on gluon and is never committed. Root, system daemons and the cluster's
service accounts are grouped as *system*. Each graph colours the six largest
users individually and folds the rest into *other users*; the table under
every graph lists each letter separately.

## Why it is built this way

Slurm accounting is switched off on this cluster
(`AccountingStorageType=accounting_storage/none`, `JobCompType=jobcomp/none`,
`JobAcctGatherType=jobacct_gather/none`), so `sacct` and `sreport` return
nothing. There is no sysstat and no process accounting either. Per-user CPU
history therefore does not exist anywhere that an ordinary account can read:
**the per-user history starts on the day the sampler started**. Two older
sources are shown separately as *bootstrap datasets* (below).

Everything runs as an ordinary user from cron, reads only world-readable files,
uses `/usr/bin/python3` with the standard library only, and needs no
administrator rights.

## What is measured, and how

### The sampler (`collect/sample.py`, every minute)

Each run reads:

| Source | Gives |
|---|---|
| `/proc/<pid>/stat` for every process, plus the owner of `/proc/<pid>` | CPU time, resident memory (RSS), thread count, parent process |
| `/proc/stat` | CPU time of the whole machine |
| `/proc/loadavg`, `/proc/meminfo` | load average, memory in use |
| `squeue -h -o '%u\|%C\|%T'` | running and pending Slurm jobs and their CPUs, per user |

**Cores in use per user** is the growth of `utime + stime` of each of that
user's processes since the previous sample, divided by the time between
samples. Processes are matched across samples by PID and start time, so a
reused PID is not mistaken for the old process. A process that started and
ended between two samples is never seen directly, but its CPU time is added to
its parent's `cutime + cstime` when the parent collects it. The sampler credits
that growth to the parent's user, after subtracting what it already counted for
that user's processes that disappeared. Root processes collect the processes
of every user, so they get two narrower rules instead: a root process whose
children all belong to one user (such as `slurmstepd` under a job) passes its
surplus to that user; and init (pid 1), which collects orphans, splits its
surplus between the owners of the orphans that vanished, in proportion to what
each had used. The result is checked against `/proc/stat` on every sample: the
*attributed* column in the load table shows how much of the machine's busy CPU
was assigned to a user. In a test of eight one-minute intervals, seven agreed
to within 0.2 cores and one fell 2 cores (9 %) short; the remainder is work
that started and ended between two samples in a way none of the rules can
trace.

**Memory per user** is the sum of RSS over that user's processes. Pages shared
between processes count once per process, so the figure is an upper bound.

**Load average** counts tasks running *and* tasks waiting on disk or NFS.
On gluon it sits far above the cores in use, so the report shows both.

**Slurm** numbers are what `squeue` reports: CPUs *allocated* to running jobs,
not CPUs used. Slurm jobs run inside `slurmd.service` and are already included
in each user's CPU, because their processes belong to that user.

The sampler takes about 0.1 s, never runs twice at once (it holds a `flock`
on a lock file and exits if the previous run is still going), and skips
processes that exit while it reads them. If more than five minutes pass
between samples (a reboot, cron stopped), that interval is not counted; the
tables show how many hours each day was actually sampled.

Raw samples are written to `~/.local/share/cluster_usage/raw/<date>.jsonl`
on gluon (mode 0600, compressed after a week). They hold real account names
and never leave the machine.

### The report builder (`collect/build.py`, daily)

1. Rolls the raw samples up per hour and caches the roll-ups.
2. Writes small JSON files to `docs/data/`: daily totals for every sampled day,
   hourly values for the last 7 days, an hour-of-week profile, and the two
   bootstrap datasets. Only letters appear in them.
3. `collect/check_private.py` then scans every file to be published for any
   account name on the machine (every account with uid 1000–59999 as a whole
   word, case-insensitive; standard system accounts as quoted names in the
   data files), home directory paths and IP addresses. Any hit stops the
   publish.
4. `collect/publish.sh` commits and pushes; GitHub Pages serves `docs/` from
   the `main` branch. There is no build step and no GitHub Actions.

The page (`docs/`) is static HTML with Chart.js 4.5.1 loaded from cdnjs
(pinned, with a subresource-integrity hash). It follows the system's light or
dark theme and can be switched by hand.

## Bootstrap datasets

These reach back before the sampler, but measure different things and are
labelled as such in the report.

- **Hours logged in per month, since November 2023**, from the binary login
  records `/var/log/wtmp` and `/var/log/wtmp.1`, parsed directly (not via
  `last`, which shortens long account names). A month's figure for a user is
  the number of hours in which they had at least one SSH or console session
  open; overlapping sessions count once. A session whose logout record is
  missing is not counted. The parsed sessions are cached on gluon, so rotating
  `wtmp` loses nothing from then on. Being logged in says nothing about how
  much CPU someone used.
- **CPU-hours since the last boot**, from the kernel's cgroup v2 counters
  (`cpu.stat`, `usage_usec`): one counter per user who has a session open
  (`user.slice/user-<uid>.slice`), one for all users whose sessions have ended
  (the rest of `user.slice`), one for `slurmd.service` (all Slurm jobs
  together, not split by user) and one for other services. A user's counter
  disappears when they log out completely, and their usage moves to *ended
  sessions*. The builder takes a new snapshot every day.

## Upgrade path: real job history from Slurm

If the cluster's administrator enables Slurm accounting, the report could show
real per-job history (who ran what, for how long, on how many CPUs, with how
much memory), including the photon node and including jobs from before the
sampler started on any node that is added later. In `slurm.conf`:

- **Full accounting (best):** run `slurmdbd` with a MariaDB/MySQL database and set
  ```
  AccountingStorageType=accounting_storage/slurmdbd
  JobAcctGatherType=jobacct_gather/cgroup    # or jobacct_gather/linux
  JobAcctGatherFrequency=30
  ```
  Then `sacct` gives per-job CPU time, memory peak and elapsed time, and
  `sreport cluster AccountUtilizationByUser` gives per-user totals over any
  period.
- **Minimum (no database):** set
  ```
  JobCompType=jobcomp/filetxt
  JobCompLoc=/var/log/slurm/job_completions.log
  ```
  This writes one line per finished job (user, start, end, nodes, CPUs). If
  that file is made readable to users, the builder can read it to add Slurm
  job history per user.

Either way the sampler stays useful for work done outside Slurm (interactive
Mathematica, scripts run directly on gluon), which accounting never sees.

## Layout

```
collect/sample.py         sampler, run every minute by cron
collect/build.py          roll-up and anonymisation, writes docs/data/
collect/check_private.py  pre-publish scan for account names and host details
collect/publish.sh        build + check + commit + push, run daily by cron
docs/                     the static site served by GitHub Pages
```

Private, on gluon only, under `~/.local/share/cluster_usage/`: `raw/`
(samples), `rollup/`, `state.json` (previous sample), `key.json` (letter key),
`config.json` (accounts grouped as *system*), `logins.json` (cached sessions).

## Crontab

```
* * * * *   /usr/bin/python3 $HOME/cluster_usage/collect/sample.py
17 3 * * *  $HOME/cluster_usage/collect/publish.sh >> $HOME/.local/state/cluster_usage/publish.log 2>&1
```

Not covered yet: **photon**, the second node, which is drained. It needs its own
sampler once it is back in service.
