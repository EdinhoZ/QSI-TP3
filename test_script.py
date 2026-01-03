#!/usr/bin/env python3
import subprocess
import os
import sys
import time
import signal
from typing import List

# =========================
# CONFIG
# =========================

CLASSIFIER = "classifier.py"
POLICER = "policer.py"
SCHEDULER = "scheduler.py"
MONITOR = "monitor.py"
FIREWALL = "firewall.py"

# Scheduler
SCHEDULER_BANDS = 3
SCHEDULER_CLASSES = [
    ("voip", 46, 0),
    ("video", 34, 1),
    ("bulk", 0, 2),
]

# Policer
POLICER_CLASSES = [
    ("RTP", 46, "20mbit", "50kb"),
    ("HTTP", 34, "10mbit", "50kb"),
    ("BULK", 0, "5mbit", "50kb"),
]

ENABLE_EGRESS_SHAPING = False

# =========================
# GLOBAL STATE
# =========================

VNFS: List[subprocess.Popen] = []

# =========================
# UTILS
# =========================

def log(msg):
    print(f"[ORC] {msg}")

def run(cmd: str) -> subprocess.Popen:
    log(cmd)
    return subprocess.Popen(cmd, shell=True)

def run_blocking(cmd: str):
    log(cmd)
    subprocess.check_call(cmd, shell=True)

def has_cmd(cmd: str) -> bool:
    return subprocess.run(
        f"which {cmd}",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    ).returncode == 0

# =========================
# TOPOLOGY DISCOVERY
# =========================

def detect_bridges() -> List[str]:
    if has_cmd("ovs-vsctl"):
        out = subprocess.run(
            "ovs-vsctl list-br",
            shell=True,
            capture_output=True,
            text=True
        ).stdout
        bridges = [b.strip() for b in out.splitlines() if b.strip()]
        if bridges:
            return bridges

    return [
        d for d in os.listdir("/sys/class/net")
        if d.startswith("s") and d[1:].isdigit()
    ]

# =========================
# CLEANUP
# =========================

def cleanup():
    log("Stopping VNFs...")
    for p in VNFS:
        try:
            p.terminate()
        except Exception:
            pass

    for br in detect_bridges():
        subprocess.run(f"tc qdisc del dev {br} root", shell=True, stderr=subprocess.DEVNULL)
        subprocess.run(f"tc qdisc del dev {br} ingress", shell=True, stderr=subprocess.DEVNULL)

    log("Cleanup complete")

def handle_signal(sig, frame):
    cleanup()
    sys.exit(0)

# =========================
# VNF LAUNCHERS
# =========================

def launch_classifier(bridges):
    for br in bridges:
        VNFS.append(run(f"python3 {CLASSIFIER} -b {br}"))

def launch_firewall():
    if os.path.exists(FIREWALL):
        VNFS.append(run(f"python3 {FIREWALL}"))

def launch_policer(bridges):
    for br in bridges:
        args = " ".join(
            f"-c {n}:{d}:{r}:{b}"
            for n, d, r, b in POLICER_CLASSES
        )
        cmd = f"python3 {POLICER} -i {br} {args}"
        if ENABLE_EGRESS_SHAPING:
            cmd += " --shaping"
        VNFS.append(run(cmd))

def launch_scheduler(bridges):
    for br in bridges:
        args = " ".join(
            f"-c {n}:{d}:{b}"
            for n, d, b in SCHEDULER_CLASSES
        )
        VNFS.append(
            run(
                f"python3 {SCHEDULER} -i {br} -b {SCHEDULER_BANDS} {args}"
            )
        )

def launch_monitor():
    VNFS.append(run(f"python3 {MONITOR}"))

# =========================
# MAIN
# =========================

def main():
    if os.geteuid() != 0:
        log("Must be run as root")
        sys.exit(1)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    bridges = detect_bridges()
    if not bridges:
        log("No OVS bridges detected")
        sys.exit(1)

    log(f"Detected bridges: {bridges}")

    # ------------------------------------------------
    # ORDERED VNF CHAIN
    # ------------------------------------------------
    log("Launching classifier")
    launch_classifier(bridges)
    time.sleep(1)

    log("Launching firewall")
    launch_firewall()
    time.sleep(1)

    log("Launching policer (ingress)")
    launch_policer(bridges)
    time.sleep(1)

    log("Launching scheduler (egress)")
    launch_scheduler(bridges)
    time.sleep(1)

    log("Launching monitor")
    launch_monitor()

    log("All VNFs running")

    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
