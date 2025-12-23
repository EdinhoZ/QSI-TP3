#!/usr/bin/env python3
import subprocess
import os
import signal
import time
import sys
import threading
import urllib.request

# =========================
# CONFIGURATION (adjust as needed)
# =========================
FIREWALL_RULES_FILE = "firewall_rules/default.txt"
SFLOW_LISTEN_IP = "0.0.0.0"                     # <-- update if needed
SFLOW_PORT = 6343
# sFlow export target (OVS -> monitor). If monitor runs locally, use 127.0.0.1
ENABLE_SFLOW = True
SFLOW_TARGET_IP = "127.0.0.1"
SFLOW_TARGET_PORT = SFLOW_PORT
SFLOW_SAMPLING = 64
SFLOW_POLLING = 1
POLICER_CLASSES = [
    # Example: ("classname", DSCP, rate, burst)
    ("HTTP", 34, "5mbit", "10kb"),
    ("RTP", 46, "10mbit", "10kb"),
    ("DNS", 8, "1mbit", "5kb"),
    ("SSH", 16, "2mbit", "5kb")
]
ENABLE_EGRESS_SHAPING = True

VNFS = []

# Smoke test/metrics polling
ENABLE_METRICS_POLL = True
METRICS_POLL_SECONDS = 30

# =========================
# UTILS
# =========================
def run(cmd):
    print("[ORC]", cmd)
    return subprocess.Popen(cmd, shell=True)

def _has_command(cmd: str) -> bool:
    return subprocess.run(f"which {cmd}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

def _run_capture(cmd: str) -> str:
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""

def detect_bridges():
    """
    Prefer OVS bridges via ovs-vsctl; fallback to sysfs heuristic (s[0-9]+).
    """
    if _has_command("ovs-vsctl"):
        out = _run_capture("ovs-vsctl list-br")
        bridges = [line.strip() for line in out.splitlines() if line.strip()]
        if bridges:
            return bridges
    # Fallback: heuristic based on Mininet naming
    return [dev for dev in os.listdir("/sys/class/net") if dev.startswith("s") and dev[1:].isdigit()]

def detect_host_ifaces():
    """
    Return dict { bridge: [port1, port2, ...] }.
    Uses `ovs-vsctl list-ports <bridge>` when available; otherwise falls back to Linux bridge sysfs.
    Filters obvious non-data ports when possible.
    """
    bridge_ifaces = {}
    bridges = detect_bridges()

    if _has_command("ovs-vsctl"):
        for br in bridges:
            out = _run_capture(f"ovs-vsctl list-ports {br}")
            ports = [p.strip() for p in out.splitlines() if p.strip()]
            # Heuristic: prefer Mininet data ports like sX-ethY / hX-ethY / rX-ethY; drop patch/int ports
            filtered = [p for p in ports if "-eth" in p]
            bridge_ifaces[br] = filtered if filtered else ports
        return bridge_ifaces

    # Fallback to Linux bridge sysfs (may not work for OVS bridges)
    for br in bridges:
        ports = []
        path = f"/sys/class/net/{br}/brif"
        if os.path.exists(path):
            ports = [p for p in os.listdir(path) if not p.startswith("lo")]
        bridge_ifaces[br] = ports
    return bridge_ifaces

def _pick_sflow_agent_port(ports):
    """
    Choose a reasonable OVS port to use as the sFlow agent interface.
    Prefer *-eth* data-plane ports; otherwise first available.
    """
    for p in ports:
        if "-eth" in p:
            return p
    return ports[0] if ports else None

def setup_sflow_for_bridge(br: str, ports: list):
    if not _has_command("ovs-vsctl"):
        print(f"[ORC] ovs-vsctl not found; skipping sFlow setup for {br}")
        return
    agent = _pick_sflow_agent_port(ports)
    if not agent:
        print(f"[ORC] No ports found on {br}; skipping sFlow setup")
        return
    # Clear any existing sFlow ref first
    _run_capture(f"ovs-vsctl -- --if-exists clear Bridge {br} sflow")
    cmd = (
        "ovs-vsctl -- "
        "--id=@sflow create sflow "
        f"agent={agent} "
        f"target=\"udp:{SFLOW_TARGET_IP}:{SFLOW_TARGET_PORT}\" "
        f"sampling={SFLOW_SAMPLING} polling={SFLOW_POLLING} "
        f"-- set bridge {br} sflow=@sflow"
    )
    print(f"[ORC] Enabling sFlow on {br} (agent={agent} -> {SFLOW_TARGET_IP}:{SFLOW_TARGET_PORT})")
    _run_capture(cmd)

def clear_sflow_for_bridge(br: str):
    if not _has_command("ovs-vsctl"):
        return
    _run_capture(f"ovs-vsctl -- --if-exists clear Bridge {br} sflow")
    print(f"[ORC] Cleared sFlow on {br}")

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

    # --- sFlow setup (optional) ---
    if ENABLE_SFLOW and _has_command("ovs-vsctl"):
        for br, ifaces in bridge_ifaces.items():
            setup_sflow_for_bridge(br, ifaces)

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

    # --- METRICS POLLING (optional) ---
    def poll_metrics():
        url = f"http://127.0.0.1:{SCRAPE_PORT if 'SCRAPE_PORT' in globals() else 9100}/metrics"
        # monitor.py uses SCRAPE_PORT=9100; keep fallback consistent
        end = time.time() + METRICS_POLL_SECONDS
        last_vals = {}
        print(f"[ORC] Polling metrics for ~{METRICS_POLL_SECONDS}s from {url}")
        while time.time() < end:
            try:
                with urllib.request.urlopen(url, timeout=1.5) as resp:
                    body = resp.read().decode()
                def get_val(metric):
                    for ln in body.splitlines():
                        if ln.startswith(metric + " "):
                            try:
                                return float(ln.split()[1])
                            except Exception:
                                return None
                    return None
                thr = get_val("vnf_throughput_mbps")
                flows = get_val("vnf_flow_count")
                avg = get_val("vnf_avg_packet_size_bytes")
                cur = {"thr": thr, "flows": flows, "avg": avg}
                if cur != last_vals:
                    print(f"[ORC] metrics: throughput={thr} Mbps, flows={flows}, avg_pkt={avg} bytes")
                    last_vals = cur
            except Exception:
                pass
            time.sleep(1)

    if ENABLE_METRICS_POLL:
        t = threading.Thread(target=poll_metrics, daemon=True)
        t.start()

    # --- SMOKE TEST INSTRUCTIONS ---
    print("[ORC] Smoke test (run in Mininet CLI):")
    print("    mininet> h1 iperf3 -s -D")
    print("    mininet> h1 ip -4 addr show")
    print("    mininet> h4 iperf3 -c <h1-ip> -t 5")
    print("    mininet> h4 ping -c 2 <h1-ip>")

    # --- KEEP ORCHESTRATOR RUNNING ---
    def handle_sig(signum, frame):
        print("[ORC] Stopping all VNFs...")
        terminate_vnfs()
        if ENABLE_SFLOW and _has_command("ovs-vsctl"):
            for br in bridge_ifaces.keys():
                clear_sflow_for_bridge(br)
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
