#!/usr/bin/env python3
"""
Stress test script for VNFs: congestion, variable load, and failure scenarios.
- Uses iperf (v2) for traffic generation inside Mininet namespaces.
- Uses tc/netem to inject delay/loss (failures) on client interface.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional

HOST_PID_FILE = "/tmp/mininet_hosts.json"
LOG_DIR_NAME = "traffic_logs"
STREAM_PORTS = [5004, 5005]  # UDP streaming
HTTP_PORT = 80               # TCP medium priority
BULK_PORT = 9000             # TCP bulk

processes: List[subprocess.Popen] = []
stop_flag = False


def log(msg: str):
    print(f"[StressTest {time.strftime('%H:%M:%S')}] {msg}")


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


def load_host_pids() -> Dict[str, int]:
    if not os.path.exists(HOST_PID_FILE):
        raise FileNotFoundError(f"Host PID file not found: {HOST_PID_FILE}")
    with open(HOST_PID_FILE, "r") as f:
        return json.load(f)


def get_host_ip(host_pid: int, iface: str = "eth0") -> Optional[str]:
    res = exec_in_host(host_pid, f"ip -4 addr show {iface}")
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        if "inet " in line:
            return line.strip().split()[1].split('/')[0]
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
    cmd = f"iperf -c {server_ip} -p {port} -u -b {bw} -t {duration} > {logfile_ns} 2>&1"
    proc = exec_in_host(client_pid, cmd, background=True)
    proc.log_info = (client_pid, logfile_ns, f"{log_dir}/{label}.log")
    log(f"Started UDP flow {label} -> {server_ip}:{port} @ {bw} for {duration}s")
    return proc


def start_tcp_flow(client_pid: int, server_ip: str, port: int, duration: int, label: str, log_dir: str, parallel: int = 1):
    logfile_ns = f"/tmp/{label.replace(' ', '_')}.log"
    par_flag = f"-P {parallel}" if parallel > 1 else ""
    cmd = f"iperf -c {server_ip} -p {port} {par_flag} -t {duration} > {logfile_ns} 2>&1"
    proc = exec_in_host(client_pid, cmd, background=True)
    proc.log_info = (client_pid, logfile_ns, f"{log_dir}/{label}.log")
    log(f"Started TCP flow {label} -> {server_ip}:{port} for {duration}s (P={parallel})")
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


def cleanup(server_pid: int, client_pid: int, client_iface: str):
    global stop_flag
    stop_flag = True
    clear_netem(client_pid, client_iface)
    kill_iperf(server_pid)
    kill_iperf(client_pid)
    time.sleep(0.2)


def print_results(log_dir: str):
    log("\n" + "=" * 70)
    log("STRESS TEST RESULTS")
    log("=" * 70)
    files = [
        ("stream_5004.log", "Streaming 5004"),
        ("stream_5005.log", "Streaming 5005"),
        ("http.log", "HTTP"),
        ("bulk.log", "Bulk"),
        ("var_load.log", "Variable Load"),
        ("failure_udp.log", "Failure UDP"),
        ("failure_tcp.log", "Failure TCP"),
    ]
    for fname, title in files:
        path = os.path.join(log_dir, fname)
        log(f"\n{title} ({path}):")
        log("-" * 70)
        if os.path.exists(path):
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
        else:
            log("No log file")


def run_scenarios(server_pid: int, client_pid: int, server_ip: str, client_iface: str, log_dir: str, args):
    # Phase 1: Congestion (sustained streams + bulk + http)
    log("=" * 70)
    log("PHASE 1: Sustained congestion")
    phase1 = [
        start_udp_flow(client_pid, server_ip, STREAM_PORTS[0], args.stream_bw, args.phase1_duration, "stream_5004", log_dir),
        start_udp_flow(client_pid, server_ip, STREAM_PORTS[1], args.stream_bw, args.phase1_duration, "stream_5005", log_dir),
        start_tcp_flow(client_pid, server_ip, HTTP_PORT, args.phase1_duration, "http", log_dir, parallel=2),
        start_tcp_flow(client_pid, server_ip, BULK_PORT, args.phase1_duration, "bulk", log_dir, parallel=3),
    ]
    time.sleep(args.phase1_duration)

    # Phase 2: Variable load (bursty UDP)
    log("=" * 70)
    log("PHASE 2: Variable load (bursty)")
    var_label = "var_load"
    logfile_ns = f"/tmp/{var_label}.log"
    seq = [("2M", 8), ("8M", 8), ("1M", 8), ("6M", 8), ("10M", 8)]
    with open(logfile_ns, "w") as _:
        pass
    for bw, dur in seq:
        cmd = f"iperf -c {server_ip} -p {STREAM_PORTS[0]} -u -b {bw} -t {dur} >> {logfile_ns} 2>&1"
        exec_in_host(client_pid, cmd, background=False)
        time.sleep(1)
    proc = subprocess.CompletedProcess([], 0)
    proc.log_info = (client_pid, logfile_ns, f"{log_dir}/{var_label}.log")
    processes.append(proc)

    # Phase 3: Failures (delay + loss) during traffic
    log("=" * 70)
    log("PHASE 3: Failure injection (delay+loss)")
    apply_netem(client_pid, client_iface, delay_ms=args.failure_delay, loss_pct=args.failure_loss)
    failure_udp = start_udp_flow(client_pid, server_ip, STREAM_PORTS[0], args.stream_bw, args.failure_duration, "failure_udp", log_dir)
    failure_tcp = start_tcp_flow(client_pid, server_ip, BULK_PORT, args.failure_duration, "failure_tcp", log_dir, parallel=2)
    time.sleep(args.failure_duration)
    clear_netem(client_pid, client_iface)


def main():
    parser = argparse.ArgumentParser(description="Stress test VNFs with congestion, variable load, and induced failures")
    parser.add_argument("--server", default="h1", help="Server host (default: h1)")
    parser.add_argument("--client", default="h4", help="Client host (default: h4)")
    parser.add_argument("--stream-bw", default="5M", help="Streaming UDP bandwidth (each stream)")
    parser.add_argument("--phase1-duration", type=int, default=30, help="Duration for congestion phase")
    parser.add_argument("--failure-duration", type=int, default=20, help="Duration for failure phase")
    parser.add_argument("--failure-delay", type=int, default=50, help="Netem delay ms during failure phase")
    parser.add_argument("--failure-loss", type=float, default=5.0, help="Netem loss %% during failure phase")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(script_dir, LOG_DIR_NAME)
    os.makedirs(log_dir, exist_ok=True)

    host_pids = load_host_pids()
    if args.server not in host_pids or args.client not in host_pids:
        log(f"Hosts not found. Available: {list(host_pids.keys())}")
        sys.exit(1)

    server_pid = host_pids[args.server]
    client_pid = host_pids[args.client]
    client_iface = f"{args.client}-eth0"

    if not check_iperf(server_pid) or not check_iperf(client_pid):
        log("iperf not found in namespace; install with apt-get install iperf")
        sys.exit(1)

    server_ip = get_host_ip(server_pid, f"{args.server}-eth0")
    if not server_ip:
        log("Could not determine server IP")
        sys.exit(1)

    log(f"Server {args.server} IP: {server_ip}")
    log(f"Client: {args.client} iface: {client_iface}")

    # Ensure clean state
    kill_iperf(server_pid)
    kill_iperf(client_pid)
    clear_netem(client_pid, client_iface)

    # Start servers
    for p in STREAM_PORTS:
        start_iperf_server(server_pid, p, udp=True)
    start_iperf_server(server_pid, HTTP_PORT, udp=False)
    start_iperf_server(server_pid, BULK_PORT, udp=False)

    def handle_sig(signum, frame):
        cleanup(server_pid, client_pid, client_iface)
        retrieve_logs()
        print_results(log_dir)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        run_scenarios(server_pid, client_pid, server_ip, client_iface, log_dir, args)
        retrieve_logs()
        print_results(log_dir)
    finally:
        cleanup(server_pid, client_pid, client_iface)


if __name__ == "__main__":
    main()
