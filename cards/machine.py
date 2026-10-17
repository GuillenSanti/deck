"""Machine: disk, memory and CPU, the apps using the most of each, docker and the local apps you list."""

import json
import os
import time

from dash_common import CACHE_DIR, NOTICE, read_config
from dash_machine import docker, listening_ports, machine
from dash_ui import BLUE, DIM, GREEN, ICON, MUTED, RED, RESET, TEXT, chip, fit, gauge, level, paint, size


TITLE, TITLE_ICON = "Machine", ICON["chart"]
SOURCES = {"machine": machine}
SLOW_SOURCES = {"ports": listening_ports, "docker": docker}
SIDE = True
ALERT_ORDER = 5
ACKED_PATH = os.path.join(CACHE_DIR, "acked.json")  # alerts you clicked away: {"docker": [container, ...]}
SICK = []  # the unhealthy containers in the last frame


def load_acked():
    try:
        with open(ACKED_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


ACKED = load_acked()


def save_acked(kind, names):
    ACKED[kind] = list(names)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(ACKED_PATH, "w") as f:
            json.dump(ACKED, f)
    except OSError:
        pass  # it's only a convenience: the alert comes back on the next start


def render(data, inner, rows=None, keys=""):
    return {"lines": machine_lines(data["machine"], data["ports"], data["docker"], inner)}


def alerts(data):
    """Docker in trouble and a filling disk."""
    dk, mc = data["docker"], data["machine"]
    out = []
    if dk is None:
        out.append(chip(RED, f"{ICON['docker']} docker not responding"))
    elif dk:  # False: docker isn't installed
        SICK[:] = dk["sick"]
        seen = [c for c in ACKED.get("docker", []) if c in dk["sick"]]  # forgotten once they recover
        if seen != ACKED.get("docker", []):
            save_acked("docker", seen)
        new = [c for c in dk["sick"] if c not in seen]
        if new:
            sick = new[0] if len(new) == 1 else f"{len(new)} containers"
            out.append((chip(RED, f"{ICON['docker']} {sick} unhealthy"), "ack:docker"))
    if mc["disk_used"] >= 0.85:
        out.append(chip(level(mc["disk_used"], 0.85, 0.95), f"{ICON['disk']} {size(mc['disk_free'])} free"))
    return out


def act(target):
    if target == "ack:docker":
        save_acked("docker", SICK)
        NOTICE[:] = [f"{', '.join(SICK)}: hidden up top until it recovers (Machine still shows it)", time.time() + 5]


def ranking(title, items, fmt, w):
    """A column of app name, bar relative to the top one, and value."""
    bar_w = min(w - 10 - 6, 16)
    peak = max((v for _, v in items), default=0) or 1
    lines = [paint(DIM, title)]
    for name, value in items:
        n = max(1, round(value / peak * bar_w))
        lines.append(
            f"{fit(paint(TEXT, name), 10)}{BLUE}{'━' * n}{RESET}{' ' * (bar_w - n)}{paint(MUTED, fmt(value).rjust(6))}"
        )
    return [fit(line, w) for line in lines]


def machine_lines(mc, ports, dk, inner):
    bar_w = min(inner - 11 - 1 - 2 - 11, 32)
    mem = mc["mem_used"] / mc["mem_total"] if mc["mem_total"] else 0
    rows = [
        ("disk", "disk", mc["disk_used"], level(mc["disk_used"], 0.85, 0.95), f"{size(mc['disk_free'])} free"),
        ("memory", "memory", mem, level(mem, 0.85, 0.95), f"{size(mc['mem_used'])} of {size(mc['mem_total'])}"),
        ("cpu", "cpu", mc["cpu"], level(mc["cpu"], 0.7, 0.9), f"{mc['cpu'] * 100:.0f}% used"),
    ]
    lines = [
        f"{MUTED}{ICON[icon]}  {label:<8}{RESET} {gauge(frac, bar_w, color)}  {paint(TEXT, value)}"
        for icon, label, frac, color, value in rows
    ]
    col_w = (inner - 2) // 2
    by_cpu = ranking("top cpu", mc["top_cpu"], lambda v: f"{v:.0f}%", col_w)
    by_mem = ranking("top memory", mc["top_mem"], size, col_w)
    lines.append("")
    lines += [a + "  " + b for a, b in zip(by_cpu, by_mem)]
    lines.append("")
    if dk is not False:
        if dk is None:
            state = paint(RED, "not responding")
        elif dk["sick"]:
            state = paint(RED, f"{ICON['cross']} " + ", ".join(dk["sick"]))
        else:
            state = f"{GREEN}●{RESET} {TEXT}{dk['running']} running{RESET}" + (
                f"{DIM} · {dk['stopped']} stopped{RESET}" if dk["stopped"] else ""
            )
        lines.append(f"{MUTED}{ICON['docker']}  {'docker':<8}{RESET} {state}")
    for app in apps():
        up = app["port"] in ports
        if up or (app.get("path") and os.path.isdir(os.path.expanduser(app["path"]))):
            icon = ICON.get(app.get("icon"), app.get("icon") or ICON["board"])
            lines.append(
                f"{MUTED}{icon}  {app['name']:<8.8}{RESET} "
                + (f"{GREEN}●{RESET} {TEXT}localhost:{app['port']}{RESET}" if up else paint(DIM, "○ stopped"))
            )
    return lines


def apps():
    """The local apps the config lists, each shown while its port listens, or as stopped while its folder
    exists: {"apps": [{"name": "ngrok", "port": 4040, "icon": "tunnel"}, ...]}, with an optional "path"."""
    found = read_config().get("apps")
    return [
        app for app in (found if isinstance(found, list) else [])
        if isinstance(app, dict) and isinstance(app.get("name"), str) and isinstance(app.get("port"), int)
    ]
