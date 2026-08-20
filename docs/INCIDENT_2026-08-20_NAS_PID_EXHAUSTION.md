# Incident 2026-08-20 — the scraper's zombies took the whole NAS down (twice)

**Status:** root cause fixed (`b75c0e5` here, `d5bde29` in scraper-service).
**Scope of this document:** the scraper leak and its blast radius only.
**Deploy state at time of writing:** NOT deployed — the NAS was being
rebooted. The fix exists in git and nowhere else until both services are
redeployed.

## What the operator saw

`driftking.braindeadbot.com` returned 502. So did every other NAS-hosted
service. DSM's Container Manager showed as crashed, and neither its restart
button nor `sudo synopkg restart ContainerManager` would bring it back
(`Failed to restart package [ContainerManager], err=[0]`). It was the second
such outage.

`npm run deploy` from any repo ended in:

```
✔ SSH access to nas confirmed
Cannot connect to the Docker daemon at unix:///var/run/docker.sock
✖ Image transfer failed (exit 1) — check NAS Docker daemon health
```

## Root cause

**20,179 zombie `chrome-headless` processes**, every one a child of this
service's scraper sibling (`uvicorn scraper_main:app --port 8001`, container
`bdc6a81f65c0…`). Against `kernel.pid_max = 32768`:

| measured on the box | value |
|---|---|
| processes | 21,024 |
| threads (`/proc/loadavg` field 4) | 22,087 |
| last pid issued (field 5) | 32,447 — at the ceiling |
| `chrome-headless` share | 20,179 of 21,024 |

With the pid space full, **`fork()` returned `EAGAIN` system-wide**. The
kernel log is unambiguous:

```
proc_fork.c:189 Failed to fork(). errno=[11/Resource temporarily unavailable]
disk_monitor.c:385 Fail to fork
```

Two independent faults had to line up to produce this, and **either one
alone is harmless**:

1. **A failed scrape orphaned its browser.** `PlaywrightEngine.fetch()`
   called `await browser.close()` on the happy path only. Its
   `except Exception` returns a `ScrapeResult` — so every timeout, every
   Cloudflare interstitial, every `raise eval_err` from the extract path
   leaked a Chromium. `health_check()` had the same shape.
2. **Nothing reaped the orphans.** `uvicorn` is pid 1 inside that container
   and is not an init, so it never `wait()`s. An orphan reparented to it
   could only ever become a **permanent zombie** — unkillable by
   definition, because it is already dead.

Fault 1 supplies the orphans; fault 2 makes them immortal. Days of scraping
accumulated 20k.

## Why every symptom pointed somewhere else

Once `fork()` fails, nothing that reports a failure can be trusted, because
reporting a failure often requires forking:

- **sshd authenticated, then `exec request failed on channel 0`.** This
  reads as a key or permissions problem. It is not — auth had already
  passed; sshd simply could not fork a session. A **shell** channel
  (`ssh -T`, stdin piped) still worked when `exec` did not, and that is how
  the box was diagnosed at all.
- **systemd lost its D-Bus name.** `systemctl` reported `Failed to execute
  program org.freedesktop.systemd1: No such file or directory` — that is
  D-Bus failing to *spawn* an activation helper, not a missing file.
  (`Exec=/bin/false` in `org.freedesktop.systemd1.service` is normal;
  systemd claims the name itself.) With systemd off the bus, no unit could
  start, which is exactly why DSM's button and `synopkg` did nothing.
- **dockerd died, but containers kept running** under their containerd
  shims. Their published ports died with docker-proxy, so every endpoint
  502'd while `ping`, DSM on :5000, and postgres/mongo/redis were all fine.
  "The host is up and the databases are healthy" was true and irrelevant.
- Disk (10% used) and memory (15 GB free) were both fine, and both were
  checked first. **The limit that was hit was neither.**

## The fix

- `app/scraper/engines/playwright_engine.py` — `fetch()` and
  `health_check()` now close the browser in a `finally`, placed **inside**
  the `async_playwright()` block. Closing after the driver context exits is
  too late to help: the driver is already gone. The diff is mostly
  re-indentation; `git diff -w` is 12 lines.
- `docker-compose.yml` — `init: true` (tini as pid 1, which reaps) and
  `pids_limit: 2048`. This service delegates scraping to scraper-service
  (`SCRAPER_SERVICE_URL`), but `app/scraper` and a baked Chromium ship in
  this image too, so the same path is reachable from here.

**`pids_limit` is the containment, and it is the part that matters.** A leak
of this shape should cost a container, not the machine. Note the pids cgroup
counts **threads**, and one headless Chromium is ~100 tasks — hence 1024/2048
rather than something tight.

## Recovery performed (for the next time, before it is fixed everywhere)

Killing the zombies is pointless; they are already dead. Kill the **live**
processes in the offending container's cgroup — the orphans then reparent to
pid 1 and are reaped. Measured: **21,043 → 862 processes**.

On a fork-starved box, `ps`/`pgrep` do not work. Read `/proc` with bash
builtins only (`read -r x < "$p/comm"`; `kill` is a builtin):

```sh
for p in /proc/[0-9]*; do read -r c < "$p/comm" 2>/dev/null && echo "$c"; done \
  | sort | uniq -c | sort -rn | head
```

`/proc/loadavg` field 4 is `running/TOTAL_THREADS`, field 5 the last pid
issued — the fastest read on whether the pid space is the problem. A sample
process's `stat` field 3 of `Z` says "zombie": do not bother killing it,
take field 4 (its parent) and read that parent's `/proc/PID/cgroup` to learn
which container is responsible.

⚠ **Freeing the pids is not full recovery.** Once systemd has lost its bus
name it does not get it back: `kill -USR1 1` (reconnect to D-Bus) and
`kill -TERM 1` (daemon-reexec) were both tried *after* pids were free and
both failed. **A reboot was still required** to restore dockerd and DSM.
Clearing the pids first is what makes that reboot clean rather than forced.

## Open items

- [ ] **Not deployed.** Both services must be redeployed for any of this to
      be live; until then the leak is still in the running image.
- [ ] **The `finally` is unverified under a real failing scrape.** It is
      pinned by no test. Worth watching zombie counts after deploy — tini
      will now reap regardless, which could mask a still-leaking `close()`.
- [ ] **Why scrapes fail often enough to leak 20k** is unexamined. The leak
      is fixed; the failure rate that fed it is not. See the interstitial
      detection work in `0677ae9`.
- [ ] **No other container was audited for the same shape.** Any image whose
      pid 1 is an app (not an init) and that spawns subprocesses has this
      exposure. A repo-wide sweep for `init:` in compose files is unwritten.
- [ ] **Nothing alerts on host pid pressure.** The first signal both times
      was a user reporting a dead website.
