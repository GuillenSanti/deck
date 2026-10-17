"""What runs on this machine: agent panes and sessions, CPU, memory, disk, docker, ports."""

import ctypes
import os
import re
import shutil
import subprocess
import time

import agent_sessions  # next to this file (Python puts a symlinked script's real folder on sys.path)
from dash_common import HOME, PANE, SESSION_DAYS, cards_with, run
from dash_ui import ICON


HOSTNAMES = {os.uname().nodename, os.uname().nodename.split(".")[0], os.environ.get("USER", "")}
AGENT_CMD = re.compile(r"^(\d+\.\d+\.\d+|claude|opencode|codex)$")  # Claude Code shows up as its version
PANE_FORMAT = "\t".join(
    (
        "#{session_name}",
        "#{window_index}",
        "#{window_name}",
        "#{pane_current_command}",
        "#{@attention}",
        "#{pane_title}",
        "#{pane_current_path}",
        "#{pane_id}",
    )
)


def place(path):
    """A folder's short name: what a card calls it, ~ for home, else its name."""
    named = next((name for name in (c.place(path) for c in cards_with("place")) if name), None)
    if named:
        return named
    return "~" if path == HOME else os.path.basename(path)


def agents():
    """Claude Code, opencode and codex panes, the ones waiting on you first."""
    current = run("tmux", "display", "-p", "-t", PANE, "#{session_name}").strip() if PANE else ""
    found = []
    for line in run("tmux", "list-panes", "-a", "-F", PANE_FORMAT).splitlines():
        parts = line.split("\t")
        if len(parts) != 8 or not AGENT_CMD.match(parts[3]):
            continue
        session, index, window, cmd, flag, title, path, pane_id = parts
        state = "ask" if flag == ICON["ask"] else "done" if flag == ICON["done"] else ""
        tool = "claude" if cmd[0].isdigit() or cmd == "claude" else cmd
        title = re.sub(r"^\W+", "", title).strip()  # drop Claude's ✳ / spinner prefix
        title = re.sub(r"^OC \| ", "", title)  # and opencode's "OC | " one
        found.append(
            {
                "tool": tool,
                "cwd": path,
                "state": state,
                "label": index if session == current else f"{session}:{index}",
                "window": window,
                "title": "" if title in HOSTNAMES else title,
                "where": place(path),
                "pane": pane_id,
                "order": (session != current, session, int(index)),
            }
        )
    rank = {"ask": 0, "done": 1, "": 2}
    return sorted(found, key=lambda a: (rank[a["state"]], a["order"]))


def listening_ports():
    ports = set()
    for line in run("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fn").splitlines():
        port = line.rsplit(":", 1)[-1]
        if line.startswith("n") and port.isdigit():
            ports.add(int(port))
    return ports


def docker():
    """Container counts; None if docker doesn't respond, False if it isn't installed."""
    if not shutil.which("docker"):
        return False
    try:
        p = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    rows = [line.split("\t") for line in p.stdout.splitlines() if line.count("\t") == 2]
    return {
        "running": sum(1 for r in rows if r[1] == "running"),
        "stopped": sum(1 for r in rows if r[1] in ("exited", "created", "paused")),
        "sick": [r[0] for r in rows if "unhealthy" in r[2] or r[1] in ("restarting", "dead")],
    }


# Machine counters come from libSystem, the same ones Activity Monitor reads:
# CPU from the per-core tick counters, per-app CPU from each process's CPU
# time, per-app memory from its footprint (which counts compressed memory).
class RUsage(ctypes.Structure):  # struct rusage_info_v2
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "user_time", "system_time", "pkg_idle_wkups", "interrupt_wkups", "pageins", "wired_size",
            "resident_size", "phys_footprint", "proc_start_abstime", "proc_exit_abstime", "child_user_time",
            "child_system_time", "child_pkg_idle_wkups", "child_interrupt_wkups", "child_pageins",
            "child_elapsed_abstime", "diskio_bytesread", "diskio_byteswritten",
        )
    ]


class Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


LIBC = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
LIBC.mach_host_self.restype = ctypes.c_uint32
LIBC.host_statistics.argtypes = [
    ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)
]
LIBC.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
LIBC.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(RUsage)]
LIBC.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
HOST = LIBC.mach_host_self()
_timebase = Timebase()
LIBC.mach_timebase_info(ctypes.byref(_timebase))
TICK_NS = _timebase.numer / _timebase.denom  # CPU times are in mach ticks (125/3 ns on Apple Silicon)
HOST_CPU_LOAD_INFO, RUSAGE_INFO_V2 = 3, 2
LAST_SAMPLE = [None]  # (time, cpu ticks, processes) from the previous frame


def cpu_ticks():
    """user, system, idle, nice ticks summed over all cores."""
    info, count = (ctypes.c_uint32 * 4)(), ctypes.c_uint32(4)
    LIBC.host_statistics(HOST, HOST_CPU_LOAD_INFO, info, ctypes.byref(count))
    return list(info)


def processes():
    """pid -> (name, CPU time in ns, memory footprint) for the processes we can see."""
    n = LIBC.proc_listallpids(None, 0)
    pids = (ctypes.c_int * (n + 256))()
    n = LIBC.proc_listallpids(pids, ctypes.sizeof(pids))
    usage, path, found = RUsage(), ctypes.create_string_buffer(4096), {}
    for pid in pids[: max(n, 0)]:
        if pid <= 0 or LIBC.proc_pid_rusage(pid, RUSAGE_INFO_V2, ctypes.byref(usage)) != 0:
            continue
        full = path.value.decode(errors="replace") if LIBC.proc_pidpath(pid, path, 4096) > 0 else "?"
        # Claude Code's binary is named after its version (~/.local/share/claude/versions/2.1.288)
        name = "claude" if "/claude/versions/" in full else os.path.basename(full)
        found[pid] = (name, (usage.user_time + usage.system_time) * TICK_NS, usage.phys_footprint)
    return found


def app_name(process):
    if process.startswith("com.apple.Virtualization"):
        return "docker VM"
    word = process.split()[0] if process.strip() else "?"
    return {"Google": "Chrome", "Code": "VS Code", "Visual": "VS Code"}.get(word, word)


def memory_used():
    """Activity Monitor's "Memory Used": app memory + wired + compressed."""
    vm = run("vm_stat")
    m = re.search(r"page size of (\d+)", vm)
    page = int(m.group(1)) if m else 16384

    def pages(label):
        m = re.search(rf"^{label}:\s+(\d+)", vm, re.M)
        return int(m.group(1)) * page if m else 0

    app = pages("Anonymous pages") - pages("Pages purgeable")
    return app + pages("Pages wired down") + pages("Pages occupied by compressor")


def machine():
    prev = LAST_SAMPLE[0]
    if prev is None or time.time() - prev[0] > 30:  # too old to tell what's happening now
        prev = (time.time(), cpu_ticks(), processes())
        time.sleep(0.5)
    cur = (time.time(), cpu_ticks(), processes())
    LAST_SAMPLE[0] = cur
    ticks = [b - a for a, b in zip(prev[1], cur[1])]
    elapsed = cur[0] - prev[0]
    cpu_by_app, mem_by_app = {}, {}
    for pid, (name, cpu_ns, footprint) in cur[2].items():
        app = app_name(name)
        mem_by_app[app] = mem_by_app.get(app, 0) + footprint
        before = prev[2].get(pid)
        if before and before[0] == name:  # same process, not a reused pid
            cpu_by_app[app] = cpu_by_app.get(app, 0) + (cpu_ns - before[1]) / 1e9 / elapsed * 100
    data = "/System/Volumes/Data"
    disk = shutil.disk_usage(data if os.path.isdir(data) else "/")
    try:
        mem_total = int(run("sysctl", "-n", "hw.memsize"))
    except ValueError:
        mem_total = 0
    return {
        "disk_free": disk.free,
        "disk_used": 1 - disk.free / disk.total,
        "mem_used": memory_used(),
        "mem_total": mem_total,
        "cpu": (ticks[0] + ticks[1] + ticks[3]) / sum(ticks) if sum(ticks) else 0.0,
        "top_cpu": sorted(cpu_by_app.items(), key=lambda kv: -kv[1])[:4],
        "top_mem": sorted(mem_by_app.items(), key=lambda kv: -kv[1])[:4],
    }


def recent_sessions():
    return agent_sessions.sessions(time.time() - SESSION_DAYS * 86400)
