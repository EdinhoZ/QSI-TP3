#!/usr/bin/env python3
"""
Stress test script for VNFs: congestion, variable load, and failure scenarios.
- Uses iperf (v2) for traffic generation inside Mininet namespaces.
- Uses tc/netem to inject delay/loss (failures) on client interfaces.
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional
import psutil  # pip install psutil

LOG_DIR_NAME = "traffic_logs"
STREAM_PORTS = [5004, 5005]  # UDP streaming
HTTP_PORT = 80               # TCP medium priority
BULK_PORT = 9000             # TCP bulk

processes: List[subprocess.Popen] = []
stop_flag = False


def log(msg: str):
    print(f"[StressTest {time.strftime('%H:%M:%S')}] {msg}")


def purge_log_dir(log_dir: str):
    if not os.path.isdir(log_dir):
        return
    removed = 0
    for name in os.listdir(log_dir):
        if name.endswith(".log"):
            try:
                os.remove(os.path.join(log_dir, name))
                removed += 1
            except OSError:
                pass
    if removed:
        log(f"Cleared {removed} old log files in {log_dir}")


def exec_in_host(host_pid: int, cmd: str, background: bool = False):
    is_root = os.geteuid() == 0
    mnexec = "mnexec" if is_root else "sudo mnexec"
    full_cmd = f"{mnexec} -a {host_pid} sh -c \"{cmd}\""
    if background:
        proc = subprocess.Popen(full_cmd, shell=True, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
        processes.append(proc)
        return proc
    return subprocess.run(full_cmd, shell=True, capture_output=True, text=True)


def get_host_pid(hostname: str) -> int:
    """Return the PID of a running Mininet host process."""
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmdline = proc.info["cmdline"]
        if cmdline and any(f"mininet:{hostname}" in s for s in cmdline):
            return proc.info["pid"]
    raise RuntimeError(f"Host {hostname} is not running in Mininet or PID not found")


def get_host_ip(host_pid: int, iface: Optional[str] = None) -> Optional[str]:
    """
    Get the IPv4 address of a Mininet host.
    If iface is None, automatically detect the first usable interface.
    """
    if iface:
        cmd = f"ip -4 addr show {iface}"
    else:
        cmd = "ip -4 addr show | grep inet"
    res = exec_in_host(host_pid, cmd)
    if res.returncode != 0:
        return None

    # parse output for inet address
    for line in res.stdout.splitlines():
        line = line.strip()
        if "inet " in line:
            ip = line.split()[1].split("/")[0]
            # skip loopback
            if ip != "127.0.0.1":
                return ip
    return None



def check_iperf(host_pid: int) -> bool:
    res = exec_in_host(host_pid, "which iperf")
    return res.returncode == 0


def kill_iperf(host_pid: int):
    exec_in_host(host_pid, "pkill -9 iperf", background=False)


def start_iperf_server(host_pid: int, port: int, udp: bool):
    flag = "-u" if udp else ""
    mode = "UDP" if udp else "TCP"
    log(f"Starting iperf {mode} server on port {port}")
    cmd = f"nohup iperf -s -p {port} {flag} > /dev/null 2>&1 &"
    exec_in_host(host_pid, cmd, background=False)
    time.sleep(0.2)


def start_udp_flow(client_pid: int, server_ip: str, port: int, bw: str, duration: int, label: str, log_dir: str):
    logfile_ns = f"/tmp/{label.replace(' ', '_')}.log"
    cmd = f"iperf -c {server_ip} -p {port} -u -b {bw} -t {duration} -i 1"
    full_cmd = f"{cmd} | tee {logfile_ns}"
    proc = exec_in_host(client_pid, full_cmd, background=True)
    proc.log_info = (client_pid, logfile_ns, f"{log_dir}/{label}.log")
    log(f"Started UDP flow {label} -> {server_ip}:{port} @ {bw} for {duration}s (live console + log)")
    return proc


def start_tcp_flow(client_pid: int, server_ip: str, port: int, duration: int, label: str, log_dir: str, parallel: int = 1):
    logfile_ns = f"/tmp/{label.replace(' ', '_')}.log"
    par_flag = f"-P {parallel}" if parallel > 1 else ""
    cmd = f"iperf -c {server_ip} -p {port} {par_flag} -t {duration} -i 1"
    full_cmd = f"{cmd} | tee {logfile_ns}"
    proc = exec_in_host(client_pid, full_cmd, background=True)
    proc.log_info = (client_pid, logfile_ns, f"{log_dir}/{label}.log")
    log(f"Started TCP flow {label} -> {server_ip}:{port} for {duration}s (P={parallel}, live console + log)")
    return proc


def start_ping(client_pid: int, target_ip: str, duration: int, interval: float, label: str, log_dir: str):
    count = max(1, int(duration / interval) + 1)
    logfile_ns = f"/tmp/{label.replace(' ', '_')}.log"
    cmd = f"ping -i {interval} -c {count} {target_ip} > {logfile_ns} 2>&1"
    proc = exec_in_host(client_pid, cmd, background=True)
    proc.log_info = (client_pid, logfile_ns, f"{log_dir}/{label}.log")
    log(f"Started ping {label} -> {target_ip} for ~{duration}s (interval {interval}s)")
    return proc


def retrieve_logs():
    log("Retrieving logs from namespaces...")
    for proc in processes:
        if hasattr(proc, "log_info"):
            client_pid, src_log, dst_log = proc.log_info
            res = exec_in_host(client_pid, f"cat {src_log}")
            if res.returncode == 0:
                try:
                    with open(dst_log, "w") as f:
                        f.write(res.stdout)
                    log(f"Saved {dst_log}")
                except Exception as e:
                    log(f"Failed to save {dst_log}: {e}")
            else:
                log(f"No output for {src_log}")


def validate_logs(log_dir: str) -> bool:
    missing = False
    expected = []
    for proc in processes:
        if hasattr(proc, "log_info"):
            _, _, dst_log = proc.log_info
            expected.append(dst_log)
    if not expected:
        log("No expected logs recorded; nothing to validate")
        return False
    for path in expected:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            log(f"Missing or empty log: {path}")
            missing = True
    if missing:
        log("Some logs missing/empty; ensure iperf ran and sudo had no prompts")
    return not missing


def apply_netem(host_pid: int, iface: str, delay_ms: Optional[int] = None, loss_pct: Optional[float] = None):
    parts = []
    if delay_ms:
        parts.append(f"delay {delay_ms}ms")
    if loss_pct:
        parts.append(f"loss {loss_pct}%")
    if not parts:
        return
    spec = " ".join(parts)
    exec_in_host(host_pid, f"tc qdisc del dev {iface} root || true")
    exec_in_host(host_pid, f"tc qdisc add dev {iface} root netem {spec}")
    log(f"Applied netem on {iface}: {spec}")


def clear_netem(host_pid: int, iface: str):
    exec_in_host(host_pid, f"tc qdisc del dev {iface} root || true")
    log(f"Cleared netem on {iface}")


def cleanup(server_pid: int, client_pids: List[int], client_ifaces: List[str]):
    global stop_flag
    stop_flag = True
    for iface, pid in zip(client_ifaces, client_pids):
        clear_netem(pid, iface)
    kill_iperf(server_pid)
    for pid in client_pids:
        kill_iperf(pid)
    time.sleep(0.2)


def print_results(log_dir: str):
    log("\n" + "=" * 70)
    log("STRESS TEST RESULTS")
    log("=" * 70)
    try:
        names = sorted(os.listdir(log_dir))
    except FileNotFoundError:
        log(f"Log directory missing: {log_dir}")
        return

    categories = [
        ("Streaming", lambda n: n.startswith("stream_")),
        ("HTTP", lambda n: n.startswith("http_")),
        ("Bulk", lambda n: n.startswith("bulk_")),
        ("Variable Load", lambda n: n.startswith("var_load_")),
        ("Failure UDP", lambda n: n.startswith("failure_udp_")),
        ("Failure TCP", lambda n: n.startswith("failure_tcp_")),
    ]

    for title, pred in categories:
        matched = [n for n in names if pred(n)]
        if not matched:
            log(f"\n{title}: no log files")
            continue
        for fname in matched:
            path = os.path.join(log_dir, fname)
            log(f"\n{title} ({path}):")
            log("-" * 70)
            with open(path) as f:
                lines = f.read().splitlines()
            printed = False
            for line in lines:
                low = line.lower()
                if any(k in low for k in ["receiver", "sender", "mbits", "lost", "jitter"]):
                    log(line.strip())
                    printed = True
            if not printed and lines:
                for l in lines[-3:]:
                    log(l.strip())


def run_scenarios(server_pid: int, client_entries: List[Dict], server_ip: str, log_dir: str, args):
    # Phase 1: Congestion (sustained streams + bulk + http) from all clients
    log("=" * 70)
    log("PHASE 1: Sustained congestion (single or multi-host)")
    for entry in client_entries:
        cpid = entry["pid"]
        cname = entry["name"]
        start_udp_flow(cpid, server_ip, STREAM_PORTS[0], args.stream_bw, args.phase1_duration, f"stream_{cname}_5004", log_dir)
        start_udp_flow(cpid, server_ip, STREAM_PORTS[1], args.stream_bw, args.phase1_duration, f"stream_{cname}_5005", log_dir)
        start_tcp_flow(cpid, server_ip, HTTP_PORT, args.phase1_duration, f"http_{cname}", log_dir, parallel=2)
        start_tcp_flow(cpid, server_ip, BULK_PORT, args.phase1_duration, f"bulk_{cname}", log_dir, parallel=3)
        if args.ping:
            start_ping(cpid, server_ip, args.phase1_duration, args.ping_interval, f"ping_{cname}", log_dir)
    time.sleep(args.phase1_duration)

    if args.phase1_only:
        log("Phase1-only flag set; skipping variable load and failure phases")
        return

    # Phase 2: Variable load (bursty UDP) using first client only
    log("=" * 70)
    log("PHASE 2: Variable load (bursty)")
    first = client_entries[0]
    var_label = f"var_load_{first['name']}"
    logfile_ns = f"/tmp/{var_label}_{int(time.time())}.log"
    seq = [("2M", 8), ("8M", 8), ("1M", 8), ("6M", 8), ("10M", 8)]
    with open(logfile_ns, "w") as _:
        pass
    for bw, dur in seq:
        cmd = f"iperf -c {server_ip} -p {STREAM_PORTS[0]} -u -b {bw} -t {dur} >> {logfile_ns} 2>&1"
        exec_in_host(first["pid"], cmd, background=False)
        time.sleep(1)
    proc = subprocess.CompletedProcess([], 0)
    proc.log_info = (first["pid"], logfile_ns, f"{log_dir}/{var_label}.log")
    processes.append(proc)

    # Phase 3: Failures (delay + loss) during traffic on first client
    log("=" * 70)
    log("PHASE 3: Failure injection (delay+loss) on first client")
    apply_netem(first["pid"], first["iface"], delay_ms=args.failure_delay, loss_pct=args.failure_loss)
    start_udp_flow(first["pid"], server_ip, STREAM_PORTS[0], args.stream_bw, args.failure_duration, f"failure_udp_{first['name']}", log_dir)
    start_tcp_flow(first["pid"], server_ip, BULK_PORT, args.failure_duration, f"failure_tcp_{first['name']}", log_dir, parallel=2)
    time.sleep(args.failure_duration)
    clear_netem(first["pid"], first["iface"])


def main():
    parser = argparse.ArgumentParser(description="Stress test VNFs with congestion, variable load, and induced failures")
    parser.add_argument("--server", default="h1", help="Server host (default: h1)")
    parser.add_argument("--clients", default="h4", help="Comma-separated client hosts (default: h4)")
    parser.add_argument("--stream-bw", default="5M", help="Streaming UDP bandwidth (each stream)")
    parser.add_argument("--phase1-duration", type=int, default=30, help="Duration for congestion phase")
    parser.add_argument("--phase1-only", action="store_true", help="Run only congestion phase (skip variable/failure)")
    parser.add_argument("--ping", action="store_true", help="Also run pings from each client to server during phase 1")
    parser.add_argument("--ping-interval", type=float, default=0.2, help="Ping interval seconds (default 0.2)")
    parser.add_argument("--failure-duration", type=int, default=20, help="Duration for failure phase")
    parser.add_argument("--failure-delay", type=int, default=50, help="Netem delay ms during failure phase")
    parser.add_argument("--failure-loss", type=float, default=5.0, help="Netem loss %% during failure phase")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(script_dir, LOG_DIR_NAME)
    os.makedirs(log_dir, exist_ok=True)
    purge_log_dir(log_dir)

    client_names = [c.strip() for c in args.clients.split(',') if c.strip()]

    try:
        host_pids = {args.server: get_host_pid(args.server)}
        for c in client_names:
            host_pids[c] = get_host_pid(c)
    except RuntimeError as e:
        log(f"FATAL: {e}")
        sys.exit(1)

    server_pid = host_pids[args.server]
    client_entries = [{"name": c, "pid": host_pids[c], "iface": "eth0"} for c in client_names]

    # Check iperf presence
    if not check_iperf(server_pid) or any(not check_iperf(e["pid"]) for e in client_entries):
        log("iperf not found in namespace; install with apt-get install iperf")
        sys.exit(1)

    server_ip = get_host_ip(server_pid, iface=None)
    if not server_ip:
        log("Could not determine server IP on any interface")
        sys.exit(1)

    log(f"Server {args.server} IP: {server_ip}")
    log(f"Clients: {[e['name'] for e in client_entries]}")

    # Ensure clean state
    kill_iperf(server_pid)
    for e in client_entries:
        kill_iperf(e["pid"])
        clear_netem(e["pid"], e["iface"])

    # Start servers
    for p in STREAM_PORTS:
        start_iperf_server(server_pid, p, udp=True)
    start_iperf_server(server_pid, HTTP_PORT, udp=False)
    start_iperf_server(server_pid, BULK_PORT, udp=False)

    def handle_sig(signum, frame):
        cleanup(server_pid, [e["pid"] for e in client_entries], [e["iface"] for e in client_entries])
        retrieve_logs()
        print_results(log_dir)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        run_scenarios(server_pid, client_entries, server_ip, log_dir, args)
        retrieve_logs()
        if not validate_logs(log_dir):
            sys.exit(1)
        print_results(log_dir)
    finally:
        cleanup(server_pid, [e["pid"] for e in client_entries], [e["iface"] for e in client_entries])


if __name__ == "__main__":
    main()
