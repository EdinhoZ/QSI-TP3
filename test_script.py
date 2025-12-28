#!/usr/bin/env python3
import subprocess
import os
import signal
import time
import sys
import threading
import urllib.request
import json

# Host PID map file exported by topologiaMininet.py
HOST_PID_FILE = "/tmp/mininet_hosts.json"

# =========================
# CONFIGURATION (adjust as needed)
# =========================
POLICER_CLASSES = [
    # Example: ("classname", DSCP, rate, burst)
    ("HTTP", 34, "5mbit", "10kb"),
    ("RTP", 46, "10mbit", "10kb"),
    ("DNS", 8, "1mbit", "5kb"),
    ("SSH", 16, "2mbit", "5kb")
]
ENABLE_EGRESS_SHAPING = True

# Firewall (access control) configuration
ENABLE_FIREWALL = True
FIREWALL_RULES_FILE = "firewall_rules/default.txt"

# Scheduler (traffic prioritization) configuration
ENABLE_SCHEDULER = True
SCHEDULER_BANDS = 3
SCHEDULER_CLASSES = [
    ("voip", 46, 0),      # RTP (DSCP 46) -> band 0 (highest priority)
    ("video", 34, 1),     # HTTP (DSCP 34) -> band 1
    ("bulk", 0, 2),       # Default (DSCP 0) -> band 2 (lowest priority)
]

VNFS = []

# Valid VNFs for CLI toggles
VALID_VNFS = {"monitor", "firewall", "classifier", "policer", "scheduler"}

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


def parse_vnf_args(argv):
    """Parse CLI args to select which VNFs to run.
    No args => all VNFs.
    Args without '-' => include only those VNFs.
    Args prefixed with '-' => exclude those VNFs from the full set.
    Unknown names are ignored with a hint.
    """
    selects = set()
    excludes = set()
    for raw in argv:
        if not raw:
            continue
        is_excl = raw.startswith("-")
        name = raw[1:] if is_excl else raw
        name = name.lower()
        if name in VALID_VNFS:
            (excludes if is_excl else selects).add(name)
        else:
            print(f"[ORC] Ignoring unknown VNF '{raw}'. Valid: {', '.join(sorted(VALID_VNFS))}")
    if selects:
        return selects
    if excludes:
        return VALID_VNFS - excludes
    return set(VALID_VNFS)


def load_host_pids(path: str = HOST_PID_FILE):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def apply_host_policing(pids: dict):
    """Apply per-host tc inside namespaces to prioritize streaming (DSCP 46)."""
    if not pids:
        print("[ORC] Policer skipped: no host PID map found (run topology first)")
        return

    # Ensure mnexec is available; otherwise skip safely
    if not _has_command("mnexec"):
        print("[ORC] Policer skipped: 'mnexec' not found on system")
        return

    # Drop sudo if already running as root
    is_root = (os.geteuid() == 0)

    tos_stream = (46 << 2)  # DSCP to TOS value
    for host, pid in pids.items():
        # Commands run inside the host namespace via mnexec -a <pid>
        cmds = [
            "tc qdisc del dev {iface} root || true",
            "tc qdisc del dev {iface} ingress || true",
            "tc qdisc add dev {iface} root handle 1: htb default 20",
            "tc class add dev {iface} parent 1: classid 1:10 htb rate 50mbit ceil 50mbit",
            "tc class add dev {iface} parent 1: classid 1:20 htb rate 10mbit ceil 10mbit",
            f"tc filter add dev {{iface}} parent 1: protocol ip prio 1 u32 match ip tos {tos_stream} 0xfc flowid 1:10",
            "tc qdisc add dev {iface} handle ffff: ingress",
            f"tc filter add dev {{iface}} parent ffff: protocol ip prio 1 u32 match ip tos {tos_stream} 0xfc police rate 50mbit burst 100kb drop flowid :1",
        ]

        iface = f"{host}-eth0"
        joined = "; ".join(cmds).format(iface=iface)
        base = "mnexec" if is_root else "sudo mnexec"
        full_cmd = f"{base} -a {pid} sh -c \"{joined}\""
        print(f"[ORC] Applying policing on {host} ({iface})")
        try:
            proc = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=10)
            if proc.returncode != 0:
                print(f"[ORC] Policing failed on {host}: {proc.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print(f"[ORC] Policing timed out on {host}")

def cleanup_host_policing(pids: dict):
    """Remove tc settings inside host namespaces to restore defaults."""
    if not pids:
        return
    if not _has_command("mnexec"):
        return
    is_root = (os.geteuid() == 0)
    for host, pid in pids.items():
        iface = f"{host}-eth0"
        cmds = [
            "tc qdisc del dev {iface} root || true",
            "tc qdisc del dev {iface} ingress || true",
        ]
        joined = "; ".join(cmds).format(iface=iface)
        base = "mnexec" if is_root else "sudo mnexec"
        full_cmd = f"{base} -a {pid} sh -c \"{joined}\""
        try:
            subprocess.run(full_cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except subprocess.TimeoutExpired:
            pass

def apply_host_scheduling(pids: dict, policer_enabled: bool):
    """Apply priority scheduling inside host namespaces for traffic prioritization.
    Integrates with policer HTB if both are enabled, otherwise uses standalone prio qdisc."""
    if not pids:
        print("[ORC] Scheduler skipped: no host PID map found (run topology first)")
        return

    if not _has_command("mnexec"):
        print("[ORC] Scheduler skipped: 'mnexec' not found on system")
        return

    is_root = (os.geteuid() == 0)
    tos_stream = (46 << 2)  # DSCP 46 for RTP/VoIP

    for host, pid in pids.items():
        iface = f"{host}-eth0"
        
        if policer_enabled:
            # Policer already set up HTB - just add prio scheduling to the high-priority class
            # Replace the default pfifo_fast on class 1:10 (streaming) with prio qdisc
            cmds = [
                # Add prio qdisc to the high-priority streaming class (1:10)
                f"tc qdisc add dev {{iface}} parent 1:10 handle 10: prio bands {SCHEDULER_BANDS}",
            ]
            
            # Add DSCP filters to map traffic to priority bands within the high-priority class
            prio_counter = 1
            for name, dscp, band in SCHEDULER_CLASSES:
                tos_val = (dscp << 2) & 0xff
                flowid = f"10:{band + 1}"
                cmds.append(
                    f"tc filter add dev {{iface}} parent 10: protocol ip prio {prio_counter} "
                    f"u32 match ip tos {tos_val} 0xff flowid {flowid}"
                )
                prio_counter += 1
            
            print(f"[ORC] Applying scheduling on {host} ({iface}) - integrated with policer")
        else:
            # No policer - set up standalone prio qdisc on root
            cmds = [
                "tc qdisc del dev {iface} root || true",
                f"tc qdisc add dev {{iface}} root handle 1: prio bands {SCHEDULER_BANDS}",
            ]
            
            # Add DSCP filters to map traffic to priority bands
            prio_counter = 1
            for name, dscp, band in SCHEDULER_CLASSES:
                tos_val = (dscp << 2) & 0xff
                flowid = f"1:{band + 1}"
                cmds.append(
                    f"tc filter add dev {{iface}} parent 1: protocol ip prio {prio_counter} "
                    f"u32 match ip tos {tos_val} 0xff flowid {flowid}"
                )
                prio_counter += 1
            
            print(f"[ORC] Applying scheduling on {host} ({iface}) - standalone mode")
        
        joined = "; ".join(cmds).format(iface=iface)
        base = "mnexec" if is_root else "sudo mnexec"
        full_cmd = f"{base} -a {pid} sh -c \"{joined}\""
        try:
            proc = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=10)
            if proc.returncode != 0:
                print(f"[ORC] Scheduling failed on {host}: {proc.stderr.strip()}")
        except subprocess.TimeoutExpired:
            print(f"[ORC] Scheduling timed out on {host}")

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
    NOTE: Returns empty list for now - Policer/Scheduler are disabled to avoid breaking OVS forwarding.
    TC modifications to OVS bridge ports can break packet forwarding.
    """
    bridge_ifaces = {}
    bridges = detect_bridges()

    if _has_command("ovs-vsctl"):
        for br in bridges:
            # Disabled: returning empty port lists to avoid applying tc rules to OVS ports
            # TC modifications break OVS forwarding (deleting noqueue qdisc)
            bridge_ifaces[br] = []
        return bridge_ifaces

    # Fallback to Linux bridge sysfs (may not work for OVS bridges)
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
def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    enabled = parse_vnf_args(argv)

    bridge_ifaces = detect_host_ifaces()
    print("[ORC] Detected bridges and interfaces:", bridge_ifaces)
    print("[ORC] VNFs enabled:", ", ".join(sorted(enabled)))

    # Apply host-level policing inside namespaces (safe for OVS) only if policer enabled
    host_pids = load_host_pids()
    policer_enabled = "policer" in enabled
    
    if policer_enabled:
        apply_host_policing(host_pids)
    
    # Apply host-level scheduling inside namespaces for traffic prioritization
    # Integrates with policer if both are enabled
    if "scheduler" in enabled and ENABLE_SCHEDULER:
        apply_host_scheduling(host_pids, policer_enabled)

    # --- CLASSIFIER (DSCP Marking) ---
    if "classifier" in enabled:
        for br in bridge_ifaces.keys():
            cmd = f"python3 classifier.py --bridge {br}"
            p = run(cmd)
            VNFS.append(p)

    # --- FIREWALL (Access Control) ---
    if "firewall" in enabled:
        if ENABLE_FIREWALL and os.path.exists(FIREWALL_RULES_FILE):
            cmd = f"python3 firewall.py --rule-file {FIREWALL_RULES_FILE}"
            print(f"[ORC] Launching firewall with rules from {FIREWALL_RULES_FILE}")
            p = run(cmd)
            VNFS.append(p)
        elif ENABLE_FIREWALL:
            print(f"[ORC] Firewall enabled but rules file not found: {FIREWALL_RULES_FILE}")

    # --- SCHEDULER (Priority Queueing) ---
    # Scheduler is now applied directly to host interfaces via apply_host_scheduling()
    # No separate scheduler.py VNF process needed when using host namespace approach

    # --- POLICER (Rate Limiting) ---
    # Only run if we have host-facing interfaces; skip silently otherwise to avoid breaking OVS forwarding.
    if "policer" in enabled:
        any_ifaces = any(bool(ifaces) for ifaces in bridge_ifaces.values())
        if any_ifaces:
            for br, ifaces in bridge_ifaces.items():
                for iface in ifaces:
                    class_args = " ".join([f"--class {c[0]}:{c[1]}:{c[2]}:{c[3]}" for c in POLICER_CLASSES])
                    shaping_arg = "--shaping" if ENABLE_EGRESS_SHAPING else ""
                    cmd = f"python3 policer.py --interface {iface} {class_args} {shaping_arg}"
                    p = run(cmd)
                    VNFS.append(p)
        else:
            print("[ORC] Policer skipped: no host-facing interfaces detected (avoiding OVS bridge ports)")

    # --- MONITOR (OVS stats → Prometheus) ---
    if "monitor" in enabled:
        cmd = "python3 monitor.py"
        p = run(cmd)
        VNFS.append(p)

    # --- METRICS POLLING (optional) ---
    def poll_metrics():
        port = globals().get("SCRAPE_PORT", 9100)
        url = f"http://127.0.0.1:{port}/metrics"
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

    if ENABLE_METRICS_POLL and "monitor" in enabled:
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
        # Clean host policing/scheduling to restore link defaults
        pids = load_host_pids()
        if "policer" in enabled or "scheduler" in enabled:
            try:
                cleanup_host_policing(pids)
            except Exception:
                pass
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
    main(sys.argv[1:])
