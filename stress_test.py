#!/usr/bin/env python3
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


# Track server log files for retrieval
server_logs = []

def kill_iperf(host_pid: int):
    exec_in_host(host_pid, "pkill -9 iperf", background=False)


def start_iperf_server(host_pid: int, port: int, udp: bool, log_dir: str = None):
    flag = "-u" if udp else ""
    mode = "UDP" if udp else "TCP"
    log(f"Starting iperf {mode} server on port {port}")
    
    # For UDP servers, log output to capture jitter and packet loss
    if udp and log_dir:
        logfile_ns = f"/tmp/server_{port}.log"
        cmd = f"nohup iperf -s -p {port} {flag} > {logfile_ns} 2>&1 &"
        server_logs.append((host_pid, logfile_ns, f"{log_dir}/server_{port}.log"))
    else:
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
    
    # Retrieve server logs
    for server_pid, src_log, dst_log in server_logs:
        res = exec_in_host(server_pid, f"cat {src_log}")
        if res.returncode == 0:
            try:
                with open(dst_log, "w") as f:
                    f.write(res.stdout)
                log(f"Saved {dst_log}")
            except Exception as e:
                log(f"Failed to save {dst_log}: {e}")


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
    
    # Clear any existing qdisc first
    del_res = exec_in_host(host_pid, f"tc qdisc del dev {iface} root 2>/dev/null || true")
    
    # Add netem qdisc
    add_res = exec_in_host(host_pid, f"tc qdisc add dev {iface} root netem {spec}")
    if add_res.returncode != 0:
        log(f"ERROR: Failed to add netem on {iface}")
        log(f"  Command stderr: {add_res.stderr.strip()}")
        return
    
    # Verify netem was applied and log the configuration
    res = exec_in_host(host_pid, f"tc qdisc show dev {iface}")
    if res.returncode == 0 and res.stdout.strip():
        log(f"Applied netem on {iface}: {spec}")
        log(f"  Verified qdisc config: {res.stdout.strip()}")
    else:
        log(f"ERROR: Failed to verify netem on {iface}")
        log(f"  tc qdisc output: '{res.stdout.strip()}'")
        if res.stderr:
            log(f"  tc stderr: {res.stderr.strip()}")


def verify_netem(host_pid: int, iface: str) -> bool:
    """Verify that netem is actually applied to the interface."""
    res = exec_in_host(host_pid, f"tc qdisc show dev {iface}")
    if res.returncode == 0 and "netem" in res.stdout:
        log(f"Netem verification for {iface}: {res.stdout.strip()}")
        return True
    else:
        log(f"WARNING: Netem NOT found on {iface}. tc output: {res.stdout}")
        return False


def get_interface_stats(host_pid: int, iface: str, label: str = ""):
    """Get interface packet/loss statistics."""
    res = exec_in_host(host_pid, f"ip -s link show {iface}")
    if res.returncode == 0:
        lines = res.stdout.splitlines()
        for i, line in enumerate(lines):
            if "RX" in line or "TX" in line or "dropped" in line.lower():
                log(f"  {label} {iface} stats: {line.strip()}")
    return res.returncode == 0


def clear_netem(host_pid: int, iface: str):
    """Remove netem qdisc from interface."""
    exec_in_host(host_pid, f"tc qdisc del dev {iface} root")
    log(f"Cleared netem from {iface}")


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


def parse_metrics_from_logs(log_dir: str) -> Dict[str, Dict[str, str]]:
    """
    Extract and compare baseline (Phase 1/2) vs failure (Phase 3) metrics.
    Returns dict with RTP loss, HTTP throughput/times, Bulk throughput, jitter stats, and recovery time.
    """
    metrics = {
        "baseline_rtp_loss": {},
        "baseline_http": {},
        "baseline_bulk": {},
        "baseline_jitter": {},
        "failure_rtp_loss": {},
        "failure_http": {},
        "failure_bulk": {},
        "failure_jitter_range": {},
        "failure_recovery_time": {},
    }
    
    try:
        names = sorted(os.listdir(log_dir))
    except FileNotFoundError:
        return metrics
    
    # Extract RTP loss from baseline and failure
    for fname in names:
        if fname.startswith("stream_"):
            host = fname.replace("stream_", "").replace(".log", "")
            path = os.path.join(log_dir, fname)
            try:
                with open(path) as f:
                    content = f.read()
                    for line in content.splitlines():
                        if "datagrams received" in line.lower() and "lost" in line.lower():
                            metrics["baseline_rtp_loss"][host] = line.strip()
                            break
            except Exception:
                pass
        
        elif fname.startswith("failure_udp_"):
            host = fname.replace("failure_udp_", "").replace(".log", "")
            path = os.path.join(log_dir, fname)
            try:
                with open(path) as f:
                    content = f.read()
                    for line in content.splitlines():
                        if "datagrams received" in line.lower() and "lost" in line.lower():
                            metrics["failure_rtp_loss"][host] = line.strip()
                            break
            except Exception:
                pass
    
    # Extract HTTP metrics (throughput and connection times)
    for fname in names:
        if fname.startswith("http_"):
            host = fname.replace("http_", "").replace(".log", "")
            path = os.path.join(log_dir, fname)
            try:
                with open(path) as f:
                    lines = f.read().splitlines()
                    # Find total throughput line (ends with receiver)
                    for line in reversed(lines):
                        if "0.0000-" in line and "sec" in line and "Mbits" in line:
                            metrics["baseline_http"][host] = line.strip()
                            break
                    # Find connection times
                    for line in lines:
                        if "[ CT]" in line and "final" in line:
                            metrics["baseline_http"][host + "_CT"] = line.strip()
                            break
            except Exception:
                pass
        
        elif fname.startswith("failure_tcp_"):
            host = fname.replace("failure_tcp_", "").replace(".log", "")
            path = os.path.join(log_dir, fname)
            try:
                with open(path) as f:
                    lines = f.read().splitlines()
                    # Find total throughput
                    for line in reversed(lines):
                        if "0.0000-" in line and "sec" in line and "Mbits" in line:
                            metrics["failure_http"][host] = line.strip()
                            break
                    # Find connection times under failure
                    for line in lines:
                        if "[ CT]" in line and "final" in line:
                            metrics["failure_http"][host + "_CT"] = line.strip()
                            break
            except Exception:
                pass
    
    # Extract Bulk metrics (average throughput)
    for fname in names:
        if fname.startswith("bulk_"):
            host = fname.replace("bulk_", "").replace(".log", "")
            path = os.path.join(log_dir, fname)
            try:
                with open(path) as f:
                    total = 0.0
                    count = 0
                    for line in f:
                        if line.startswith("[SUM]"):
                            parts = line.split()
                            # Extract bandwidth (7th column in [SUM] lines)
                            if len(parts) >= 7:
                                try:
                                    bw = float(parts[6])
                                    total += bw
                                    count += 1
                                except ValueError:
                                    pass
                    if count > 0:
                        avg = total / count
                        metrics["baseline_bulk"][host] = f"{avg:.2f} Mbits/sec avg ({count} intervals)"
            except Exception:
                pass
    
    # Extract Jitter and Recovery Time from RTP failure logs
    for fname in names:
        if fname.startswith("failure_udp_"):
            host = fname.replace("failure_udp_", "").replace(".log", "")
            path = os.path.join(log_dir, fname)
            try:
                with open(path) as f:
                    lines = f.read().splitlines()
                    jitters = []
                    # Extract all jitter values from per-second intervals
                    for line in lines:
                        if "0.0000-" in line and "ms" in line and "Lost" in line:
                            parts = line.split()
                            for i, part in enumerate(parts):
                                if "ms" in part and i > 0:
                                    try:
                                        jitter = float(parts[i-1])
                                        jitters.append(jitter)
                                    except (ValueError, IndexError):
                                        pass
                    if jitters:
                        min_j = min(jitters)
                        max_j = max(jitters)
                        avg_j = sum(jitters) / len(jitters)
                        metrics["failure_jitter_range"][host] = f"min={min_j:.3f}ms, avg={avg_j:.3f}ms, max={max_j:.3f}ms"
                    
                    # Estimate recovery time: find when throughput stabilizes at high rate
                    throughs = []
                    for line in lines:
                        if "0.0000-" in line and "Mbits" in line and "[" not in line:
                            parts = line.split()
                            for i, part in enumerate(parts):
                                if "Mbits/sec" in part and i > 0:
                                    try:
                                        through = float(parts[i-1])
                                        throughs.append(through)
                                    except (ValueError, IndexError):
                                        pass
                    if len(throughs) > 2:
                        baseline_through = throughs[-1]  # Final throughput (stable)
                        # Find first interval that reaches 80% of baseline
                        recovery_intervals = 0
                        for t in throughs:
                            if t >= baseline_through * 0.8:
                                break
                            recovery_intervals += 1
                        recovery_time = recovery_intervals * 1.0  # 1 second per interval
                        metrics["failure_recovery_time"][host] = f"{recovery_time:.0f}s to 80% throughput"
            except Exception:
                pass
    
    return metrics


def print_comparison_table(metrics: Dict[str, Dict[str, str]]):
    """Print a comparison table of baseline vs failure metrics."""
    log("\n" + "=" * 90)
    log("BASELINE vs FAILURE COMPARISON")
    log("=" * 90)
    
    # RTP Loss Comparison
    log("\nRTP (Streaming) - Packet Loss:")
    log("-" * 90)
    hosts_with_baseline_rtp = set(metrics.get("baseline_rtp_loss", {}).keys())
    hosts_with_failure_rtp = set(metrics.get("failure_rtp_loss", {}).keys())
    hosts_rtp = hosts_with_baseline_rtp | hosts_with_failure_rtp
    
    if hosts_rtp:
        for host in sorted(hosts_rtp):
            baseline = metrics.get("baseline_rtp_loss", {}).get(host, "No data")
            failure = metrics.get("failure_rtp_loss", {}).get(host, "No data")
            log(f"  {host} (baseline): {baseline}")
            log(f"  {host} (failure):  {failure}")
            log("")
    else:
        log("  No RTP data collected")
    
    # HTTP Throughput Comparison
    log("\nHTTP (Medium Priority) - Throughput & Connection Times:")
    log("-" * 90)
    hosts_http = set()
    for key in metrics.get("baseline_http", {}).keys():
        if not key.endswith("_CT"):
            hosts_http.add(key)
    
    if hosts_http:
        for host in sorted(hosts_http):
            baseline_bw = metrics.get("baseline_http", {}).get(host, "No data")
            failure_bw = metrics.get("failure_http", {}).get(host, "No data")
            baseline_ct = metrics.get("baseline_http", {}).get(host + "_CT", "No data")
            failure_ct = metrics.get("failure_http", {}).get(host + "_CT", "No data")
            
            log(f"  {host} (baseline throughput): {baseline_bw}")
            log(f"  {host} (failure throughput):  {failure_bw}")
            log(f"  {host} (baseline conn times): {baseline_ct}")
            log(f"  {host} (failure conn times):  {failure_ct}")
            log("")
    else:
        log("  No HTTP data collected")
    
    # Bulk Throughput Comparison
    log("\nBulk (Background Traffic) - Throughput:")
    log("-" * 90)
    hosts_bulk = set(metrics.get("baseline_bulk", {}).keys())
    
    if hosts_bulk:
        for host in sorted(hosts_bulk):
            baseline = metrics.get("baseline_bulk", {}).get(host, "No data")
            log(f"  {host} (baseline): {baseline}")
    else:
        log("  No Bulk data collected")
    
    # RTP Jitter and Recovery Time Under Failure
    log("\nRTP (Streaming) - Jitter Range & Recovery Under Failure:")
    log("-" * 90)
    hosts_jitter = set(metrics.get("failure_jitter_range", {}).keys())
    
    if hosts_jitter:
        for host in sorted(hosts_jitter):
            jitter = metrics.get("failure_jitter_range", {}).get(host, "No data")
            recovery = metrics.get("failure_recovery_time", {}).get(host, "No data")
            log(f"  {host} (jitter range):  {jitter}")
            log(f"  {host} (recovery time): {recovery}")
            log("")
    else:
        log("  No jitter/recovery data collected")
    
    log("\n" + "=" * 90)


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


def run_phase2_alternating(client_pid: int, server_ip: str, log_dir: str, duration: int = 60):
    """Phase 2a: Alternating RTP bursts (2M ↔ 10M) + concurrent HTTP & Bulk"""
    log("PHASE 2a: Alternating bursts (2M ↔ 10M) with concurrent HTTP & Bulk")
    
    # Start HTTP and Bulk in background for the entire duration
    start_tcp_flow(client_pid, server_ip, HTTP_PORT, duration, f"http_phase2a", log_dir, parallel=2)
    start_tcp_flow(client_pid, server_ip, BULK_PORT, duration, f"bulk_phase2a", log_dir, parallel=3)
    
    # Run variable RTP load
    label = f"var_load_alternating"
    logfile_ns = f"/tmp/{label}_{int(time.time())}.log"
    with open(logfile_ns, "w") as _:
        pass
    
    start_time = time.time()
    burst_duration = 8  # 8 seconds per burst
    is_high = True
    
    while time.time() - start_time < duration:
        bw = "10M" if is_high else "2M"
        cmd = f"iperf -c {server_ip} -p {STREAM_PORTS[0]} -u -b {bw} -t {burst_duration} >> {logfile_ns} 2>&1"
        log(f"  RTP: {bw} for {burst_duration}s")
        exec_in_host(client_pid, cmd, background=False)
        is_high = not is_high
        time.sleep(1)
    
    proc = subprocess.CompletedProcess([], 0)
    proc.log_info = (client_pid, logfile_ns, f"{log_dir}/{label}.log")
    processes.append(proc)


def run_phase2_progressive(client_pid: int, server_ip: str, log_dir: str, duration: int = 30):
    """Phase 2b: Progressive RTP ramp (1M → 10M) + concurrent HTTP & Bulk"""
    log(f"PHASE 2b: Progressive ramp (1M → 10M over {duration}s) with concurrent HTTP & Bulk")
    
    # Start HTTP and Bulk in background for the entire duration
    start_tcp_flow(client_pid, server_ip, HTTP_PORT, duration, f"http_phase2b", log_dir, parallel=2)
    start_tcp_flow(client_pid, server_ip, BULK_PORT, duration, f"bulk_phase2b", log_dir, parallel=3)
    
    # Run variable RTP load
    label = f"var_load_progressive"
    logfile_ns = f"/tmp/{label}_{int(time.time())}.log"
    with open(logfile_ns, "w") as _:
        pass
    
    step_duration = 2  # 2 seconds per step
    num_steps = int(duration / step_duration)
    bw_min, bw_max = 1, 10
    
    for i in range(num_steps):
        # Linear interpolation from 1M to 10M
        bw = bw_min + (bw_max - bw_min) * (i / max(1, num_steps - 1))
        cmd = f"iperf -c {server_ip} -p {STREAM_PORTS[0]} -u -b {bw:.1f}M -t {step_duration} >> {logfile_ns} 2>&1"
        log(f"  RTP: {bw:.1f}M for {step_duration}s")
        exec_in_host(client_pid, cmd, background=False)
        time.sleep(0.5)
    
    proc = subprocess.CompletedProcess([], 0)
    proc.log_info = (client_pid, logfile_ns, f"{log_dir}/{label}.log")
    processes.append(proc)


def run_phase2_sinusoidal(client_pid: int, server_ip: str, log_dir: str, duration: int = 60):
    """Phase 2c: Sinusoidal RTP load (2M ↔ 8M) + concurrent HTTP & Bulk"""
    import math
    log(f"PHASE 2c: Sinusoidal load (2M ↔ 8M over {duration}s) with concurrent HTTP & Bulk")
    
    # Start HTTP and Bulk in background for the entire duration
    start_tcp_flow(client_pid, server_ip, HTTP_PORT, duration, f"http_phase2c", log_dir, parallel=2)
    start_tcp_flow(client_pid, server_ip, BULK_PORT, duration, f"bulk_phase2c", log_dir, parallel=3)
    
    # Run variable RTP load
    label = f"var_load_sinusoidal"
    logfile_ns = f"/tmp/{label}_{int(time.time())}.log"
    with open(logfile_ns, "w") as _:
        pass
    
    step_duration = 2  # 2 seconds per step
    num_steps = int(duration / step_duration)
    bw_min, bw_max = 2, 8
    center = (bw_min + bw_max) / 2
    amplitude = (bw_max - bw_min) / 2
    
    for i in range(num_steps):
        # Sinusoidal: center + amplitude * sin(phase)
        phase = (i / num_steps) * 2 * math.pi
        bw = center + amplitude * math.sin(phase)
        cmd = f"iperf -c {server_ip} -p {STREAM_PORTS[0]} -u -b {bw:.1f}M -t {step_duration} >> {logfile_ns} 2>&1"
        log(f"  RTP: {bw:.1f}M for {step_duration}s")
        exec_in_host(client_pid, cmd, background=False)
        time.sleep(0.5)
    
    proc = subprocess.CompletedProcess([], 0)
    proc.log_info = (client_pid, logfile_ns, f"{log_dir}/{label}.log")
    processes.append(proc)


def run_scenarios(server_pid: int, client_entries: List[Dict], server_ip: str, log_dir: str, args):
    """Execute phases 1-3 of stress testing."""
    # Phase 1: Congestion (sustained streams + bulk + http) from all clients
    if not args.skip_phase1 and not args.phase3_only:
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
        
        # Give iperf time to write final summary lines (increased wait)
        log("Waiting for iperf to write final summaries...")
        time.sleep(15)  # Longer wait to let iperf finish gracefully
    elif args.phase3_only:
        log("=" * 70)
        log("PHASE 1: Skipped (--phase3-only flag set)")
    else:
        log("=" * 70)
        log("PHASE 1: Skipped (--skip-phase1 flag set)")

    if args.phase1_only:
        log("Phase1-only flag set; skipping variable/failure phases")
        return

    # Phase 2: Variable load patterns
    if not args.phase3_only:
        log("=" * 70)
        first = client_entries[0]
        
        if args.phase2_alternating:
            run_phase2_alternating(first["pid"], server_ip, log_dir, duration=args.phase2_duration)
            time.sleep(15)  # Wait for iperf to finish
        
        if args.phase2_progressive:
            run_phase2_progressive(first["pid"], server_ip, log_dir, duration=args.phase2_duration)
            time.sleep(15)
        
        if args.phase2_sinusoidal:
            run_phase2_sinusoidal(first["pid"], server_ip, log_dir, duration=args.phase2_duration)
            time.sleep(15)
        
        if not (args.phase2_alternating or args.phase2_progressive or args.phase2_sinusoidal):
            log("No Phase 2 patterns selected; skipping Phase 2")
            # If phase3_only is set, continue to Phase 3; otherwise return
            if not args.phase3_only:
                return
    else:
        log("=" * 70)
        log("PHASE 2: Skipped (--phase3-only flag set)")

    # Phase 3: Failures (delay + loss) during traffic on first client
    log("=" * 70)
    log("PHASE 3: Failure injection (delay+loss) on first client")
    first = client_entries[0]
    
    # Capture baseline stats before netem
    log("Baseline interface stats (before netem):")
    get_interface_stats(first["pid"], first["iface"], "BEFORE")
    
    apply_netem(first["pid"], first["iface"], delay_ms=args.failure_delay, loss_pct=args.failure_loss)
    
    # Verify netem was actually applied
    time.sleep(0.5)
    if not verify_netem(first["pid"], first["iface"]):
        log("WARNING: Netem verification failed; loss/delay may not be applied!")
    
    log(f"Starting traffic with netem active (delay={args.failure_delay}ms, loss={args.failure_loss}%)")
    start_udp_flow(first["pid"], server_ip, STREAM_PORTS[0], args.stream_bw, args.failure_duration, f"failure_udp_{first['name']}", log_dir)
    start_tcp_flow(first["pid"], server_ip, BULK_PORT, args.failure_duration, f"failure_tcp_{first['name']}", log_dir, parallel=2)
    time.sleep(args.failure_duration)
    
    # Capture stats after traffic
    log("Interface stats (after traffic with netem):")
    get_interface_stats(first["pid"], first["iface"], "AFTER")
    
    # Give iperf time to write final summaries
    log("Waiting for iperf to write final summaries...")
    time.sleep(15)  # Longer wait to let iperf finish gracefully
    
    clear_netem(first["pid"], first["iface"])


def main():
    parser = argparse.ArgumentParser(description="Stress test VNFs with congestion, variable load, and induced failures")
    parser.add_argument("--server", default="h1", help="Server host (default: h1)")
    parser.add_argument("--clients", default="h4", help="Comma-separated client hosts (default: h4)")
    parser.add_argument("--stream-bw", default="5M", help="Streaming UDP bandwidth (each stream)")
    parser.add_argument("--phase1-duration", type=int, default=30, help="Duration for congestion phase")
    parser.add_argument("--phase1-only", action="store_true", help="Run only congestion phase (skip variable/failure)")
    parser.add_argument("--skip-phase1", action="store_true", help="Skip congestion phase (jump directly to phase 2 or 3)")
    parser.add_argument("--phase3-only", action="store_true", help="Run only failure injection phase (skip congestion/variable)")
    parser.add_argument("--ping", action="store_true", help="Also run pings from each client to server during phase 1")
    parser.add_argument("--ping-interval", type=float, default=0.2, help="Ping interval seconds (default 0.2)")
    parser.add_argument("--phase2-duration", type=int, default=60, help="Duration for each Phase 2 pattern (default 60s)")
    parser.add_argument("--phase2-alternating", action="store_true", help="Run Phase 2a: Alternating bursts (2M ↔ 10M)")
    parser.add_argument("--phase2-progressive", action="store_true", help="Run Phase 2b: Progressive ramp (1M → 10M)")
    parser.add_argument("--phase2-sinusoidal", action="store_true", help="Run Phase 2c: Sinusoidal load (2M ↔ 8M)")
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
    client_entries = [{"name": c, "pid": host_pids[c], "iface": f"{c}-eth0"} for c in client_names]

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
        start_iperf_server(server_pid, p, udp=True, log_dir=log_dir)
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
        # Parse and display metrics comparison
        metrics = parse_metrics_from_logs(log_dir)
        print_comparison_table(metrics)
        print_results(log_dir)
    finally:
        cleanup(server_pid, [e["pid"] for e in client_entries], [e["iface"] for e in client_entries])


if __name__ == "__main__":
    main()
