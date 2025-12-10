#!/usr/bin/env python3
import subprocess
import os
import signal
import time
import sys

# =========================
# CONFIGURATION (adjust as needed)
# =========================
FIREWALL_RULES_FILE = "firewall_rules/default.txt"
SFLOW_LISTEN_IP = "0.0.0.0"                     # <-- update if needed
SFLOW_PORT = 6343
POLICER_CLASSES = [
    # Example: ("classname", DSCP, rate, burst)
    ("HTTP", 34, "5mbit", "10kb"),
    ("RTP", 46, "10mbit", "10kb"),
    ("DNS", 8, "1mbit", "5kb"),
    ("SSH", 16, "2mbit", "5kb")
]
ENABLE_EGRESS_SHAPING = True

VNFS = []

# =========================
# UTILS
# =========================
def run(cmd):
    print("[ORC]", cmd)
    return subprocess.Popen(cmd, shell=True)

def detect_bridges():
    return [dev for dev in os.listdir("/sys/class/net") if dev.startswith("s") and dev[1:].isdigit()]

def detect_host_ifaces():
    # returns dict: {bridge: [host_if1, host_if2, ...]}
    bridge_ifaces = {}
    bridges = detect_bridges()
    for br in bridges:
        ports = []
        path = f"/sys/class/net/{br}/brif"
        if os.path.exists(path):
            ports = [p for p in os.listdir(path) if not p.startswith("lo")]
        bridge_ifaces[br] = ports
    return bridge_ifaces

def terminate_vnfs():
    for p in VNFS:
        try:
            p.terminate()
        except Exception:
            pass
    print("[ORC] All VNFs terminated")

# =========================
# LAUNCH VNFS
# =========================
def main():
    bridge_ifaces = detect_host_ifaces()
    print("[ORC] Detected bridges and interfaces:", bridge_ifaces)

    # --- CLASSIFIER ---
    for br in bridge_ifaces.keys():
        cmd = f"python3 classifier.py --bridge {br}"
        p = run(cmd)
        VNFS.append(p)

    # --- POLICER ---
    # we attach to all host-facing ports for simplicity
    for br, ifaces in bridge_ifaces.items():
        for iface in ifaces:
            class_args = " ".join([f"--class {c[0]}:{c[1]}:{c[2]}:{c[3]}" for c in POLICER_CLASSES])
            shaping_arg = "--shaping" if ENABLE_EGRESS_SHAPING else ""
            cmd = f"python3 policer.py --interface {iface} {class_args} {shaping_arg}"
            p = run(cmd)
            VNFS.append(p)

    # --- MONITOR ---
    cmd = f"python3 monitor.py --addr {SFLOW_LISTEN_IP} --port {SFLOW_PORT}"
    p = run(cmd)
    VNFS.append(p)

    # --- KEEP ORCHESTRATOR RUNNING ---
    def handle_sig(signum, frame):
        print("[ORC] Stopping all VNFs...")
        terminate_vnfs()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        handle_sig(None, None)

if __name__ == "__main__":
    main()
