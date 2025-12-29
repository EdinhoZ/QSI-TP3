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
    """Parse CLI args to select which VNFs to run."""
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

    if not _has_command("mnexec"):
        print("[ORC] Policer skipped: 'mnexec' not found on system")
        return

    is_root = (os.geteuid() == 0)
    tos_stream = (46 << 2)  # DSCP to TOS value
    for host, pid in pids.items():
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
            subprocess.run(full_cmd, shell=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            print(f"[ORC] Policing timed out on {host}")

def cleanup_host_policing(pids: dict):
    """Remove tc settings inside host namespaces."""
    if not pids or not _has_command("mnexec"):
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
    """Apply priority scheduling inside host namespaces."""
    if not pids:
        print("[ORC] Scheduler skipped: no host PID map found")
        return
    if not _has_command("mnexec"):
        return

    is_root = (os.geteuid() == 0)
    
    for host, pid in pids.items():
        iface = f"{host}-eth0"
        
        if policer_enabled:
            cmds = [
                f"tc qdisc add dev {{iface}} parent 1:10 handle 10: prio bands {SCHEDULER_BANDS}",
            ]
            prio_counter = 1
            for name, dscp, band in SCHEDULER_CLASSES:
                tos_val = (dscp << 2) & 0xff
                flowid = f"10:{band + 1}"
                cmds.append(
                    f"tc filter add dev {{iface}} parent 10: protocol ip prio {prio_counter} "
                    f"u32 match ip tos {tos_val} 0xff flowid {flowid}"
                )
                prio_counter += 1
            print(f"[ORC] Applying scheduling on {host} ({iface}) - integrated")
        else:
            cmds = [
                "tc qdisc del dev {iface} root || true",
                f"tc qdisc add dev {{iface}} root handle 1: prio bands {SCHEDULER_BANDS}",
            ]
            prio_counter = 1
            for name, dscp, band in SCHEDULER_CLASSES:
                tos_val = (dscp << 2) & 0xff
                flowid = f"1:{band + 1}"
                cmds.append(
                    f"tc filter add dev {{iface}} parent 1: protocol ip prio {prio_counter} "
                    f"u32 match ip tos {tos_val} 0xff flowid {flowid}"
                )
                prio_counter += 1
            print(f"[ORC] Applying scheduling on {host} ({iface}) - standalone")
        
        joined = "; ".join(cmds).format(iface=iface)
        base = "mnexec" if is_root else "sudo mnexec"
        full_cmd = f"{base} -a {pid} sh -c \"{joined}\""
        try:
            subprocess.run(full_cmd, shell=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            print(f"[ORC] Scheduling timed out on {host}")

def detect_bridges():
    if _has_command("ovs-vsctl"):
        out = _run_capture("ovs-vsctl list-br")
        bridges = [line.strip() for line in out.splitlines() if line.strip()]
        if bridges:
            return bridges
    return [dev for dev in os.listdir("/sys/class/net") if dev.startswith("s") and dev[1:].isdigit()]

def detect_host_ifaces():
    bridge_ifaces = {}
    bridges = detect_bridges()
    if _has_command("ovs-vsctl"):
        for br in bridges:
            bridge_ifaces[br] = []
        return bridge_ifaces
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
    # ========================================================
    # 1. SETUP AUTOMÁTICO DE DEPENDÊNCIAS
    # ========================================================
    setup_script = "dependencies.py"
    if os.path.exists(setup_script):
        try:
            subprocess.check_call([sys.executable, setup_script])
        except subprocess.CalledProcessError:
            print(f"[ORC] ERRO: Falha ao executar {setup_script}")
            sys.exit(1)
    else:
        print(f"[ORC] AVISO: {setup_script} não encontrado.")
    # ========================================================

    if argv is None:
        argv = sys.argv[1:]
    enabled = parse_vnf_args(argv)

    bridge_ifaces = detect_host_ifaces()
    print("[ORC] Detected bridges and interfaces:", bridge_ifaces)
    print("[ORC] VNFs enabled:", ", ".join(sorted(enabled)))

    host_pids = load_host_pids()
    policer_enabled = "policer" in enabled
    
    if policer_enabled:
        apply_host_policing(host_pids)
    
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

    # --- POLICER (Rate Limiting) ---
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
            print("[ORC] Policer skipped: no host-facing interfaces detected")

    # --- MONITOR (OVS stats → Prometheus) ---
    if "monitor" in enabled:
        cmd = "python3 monitor.py"
        p = run(cmd)
        VNFS.append(p)

    # --- METRICS POLLING (optional) ---
    def poll_metrics():
        port = globals().get("SCRAPE_PORT", 9100)
        url = f"http://127.0.0.1:{port}/metrics"
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
                thr = get_val("network_throughput_mbps")
                if thr is not None:
                     print(f"[ORC] Metrics: throughput={thr} Mbps")
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
