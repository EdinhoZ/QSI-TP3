#!/usr/bin/env python3
"""
Traffic Generator for QSI-TP3 VNF Testing

Generates saturating network traffic to demonstrate VNF chain prioritization:
- High-priority streaming traffic (RTP on UDP 5004/5005, DSCP 46)
- Medium-priority HTTP traffic (TCP 80, DSCP 34)  
- Low-priority bulk traffic (generic TCP, DSCP 0)

The goal is to saturate the network and show that streaming traffic 
is prioritized by the VNF chain even under heavy load.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, List, Tuple

# Configuration
HOST_PID_FILE = "/tmp/mininet_hosts.json"
DURATION = 60  # seconds
STREAMING_PORT_1 = 5004
STREAMING_PORT_2 = 5005
HTTP_PORT = 80
BULK_PORT = 9000

# Traffic generation parameters
STREAMING_BANDWIDTH = "5M"   # High-quality video stream
HTTP_BANDWIDTH = "10M"       # Web traffic
BULK_BANDWIDTH = "50M"       # Saturating bulk transfer

# Process tracking
processes = []
stop_flag = threading.Event()


def log(msg: str):
    """Timestamped logging."""
    print(f"[TrafficGen {time.strftime('%H:%M:%S')}] {msg}")


def run_cmd(cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Execute shell command."""
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check)


def load_host_pids(path: str = HOST_PID_FILE) -> Dict[str, int]:
    """Load Mininet host PID mapping."""
    if not os.path.exists(path):
        log(f"ERROR: Host PID file not found: {path}")
        log("Please start the Mininet topology first (topologiaMininet.py)")
        sys.exit(1)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        log(f"ERROR: Failed to load host PIDs: {e}")
        sys.exit(1)


def exec_in_host(host_pid: int, cmd: str, background: bool = False) -> subprocess.Popen:
    """Execute command inside Mininet host namespace."""
    is_root = os.geteuid() == 0
    mnexec = "mnexec" if is_root else "sudo mnexec"
    
    full_cmd = f'{mnexec} -a {host_pid} sh -c "{cmd}"'
    
    if background:
        proc = subprocess.Popen(full_cmd, shell=True, stdout=subprocess.DEVNULL, 
                               stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
        processes.append(proc)
        return proc
    else:
        return subprocess.run(full_cmd, shell=True, capture_output=True, text=True)


def start_iperf_server(host_pid: int, port: int, host_name: str, udp: bool):
    """Start iperf server on specified port (UDP or TCP)."""
    mode = "UDP" if udp else "TCP"
    log(f"Starting iperf {mode} server on {host_name} port {port}")
    flag = "-u" if udp else ""
    cmd = f"nohup iperf -s -p {port} {flag} > /dev/null 2>&1 &"
    exec_in_host(host_pid, cmd, background=False)
    time.sleep(0.5)

def retrieve_logs_from_namespace():
    """Retrieve iperf logs written in /tmp inside host namespaces to workspace traffic_logs."""
    log("Retrieving log files from namespace...")
    for proc in processes:
        if hasattr(proc, 'log_info'):
            client_pid, src_log, dst_log = proc.log_info
            res = exec_in_host(client_pid, f"cat {src_log}", background=False)
            if getattr(res, 'returncode', 1) == 0 and getattr(res, 'stdout', None):
                try:
                    with open(dst_log, 'w') as f:
                        f.write(res.stdout)
                    log(f"Retrieved: {dst_log}")
                except Exception as e:
                    log(f"Failed to write {dst_log}: {e}")
            else:
                log(f"No output from {src_log}")


def generate_streaming_traffic(client_pid: int, server_ip: str, port: int, 
                               duration: int, bandwidth: str, name: str, log_dir: str):
    """Generate UDP streaming traffic (simulates RTP)."""
    log(f"Starting STREAMING traffic: {name} -> {server_ip}:{port} @ {bandwidth}")
    # Use /tmp inside namespace, then copy results out
    logfile = f"/tmp/stream_{port}.log"
    cmd = f"iperf -c {server_ip} -p {port} -u -b {bandwidth} -t {duration} > {logfile} 2>&1"
    proc = exec_in_host(client_pid, cmd, background=True)
    # Store mapping for later retrieval
    proc.log_info = (client_pid, logfile, f"{log_dir}/stream_{port}.log")
    return proc


def generate_http_traffic(client_pid: int, server_ip: str, duration: int, 
                         bandwidth: str, name: str, log_dir: str):
    """Generate TCP traffic on HTTP port (simulates web traffic)."""
    log(f"Starting HTTP traffic: {name} -> {server_ip}:80 @ {bandwidth}")
    logfile = f"/tmp/http.log"
    cmd = f"iperf -c {server_ip} -p {HTTP_PORT} -t {duration} > {logfile} 2>&1"
    proc = exec_in_host(client_pid, cmd, background=True)
    proc.log_info = (client_pid, logfile, f"{log_dir}/http.log")
    return proc


def generate_bulk_traffic(client_pid: int, server_ip: str, duration: int, 
                         bandwidth: str, name: str, log_dir: str):
    """Generate high-volume TCP bulk traffic."""
    log(f"Starting BULK traffic: {name} -> {server_ip}:9000 @ {bandwidth}")
    logfile = f"/tmp/bulk.log"
    cmd = f"iperf -c {server_ip} -p {BULK_PORT} -t {duration} > {logfile} 2>&1"
    proc = exec_in_host(client_pid, cmd, background=True)
    proc.log_info = (client_pid, logfile, f"{log_dir}/bulk.log")
    return proc


def get_host_ip(host_pid: int, iface: str = "eth0") -> str:
    """Get IP address of host interface."""
    result = exec_in_host(host_pid, f"ip -4 addr show {iface}")
    if result.returncode != 0:
        return None
    
    for line in result.stdout.splitlines():
        if "inet " in line:
            return line.strip().split()[1].split('/')[0]
    return None


def kill_iperf_processes(host_pid: int):
    """Kill any existing iperf processes in host."""
    exec_in_host(host_pid, "pkill -9 iperf", background=False)


def check_iperf3_installed(host_pid: int) -> bool:
    """Check if iperf is installed in the namespace."""
    result = exec_in_host(host_pid, "which iperf", background=False)
    return result.returncode == 0


def cleanup():
    """Cleanup all traffic generation processes."""
    log("Stopping all traffic generators...")
    stop_flag.set()
    
    for proc in processes:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
    
    # Give processes time to terminate
    time.sleep(1)
    
    for proc in processes:
        try:
            proc.kill()
        except Exception:
            pass


def display_results(log_dir: str):
    """Display traffic generation results."""
    log("\n" + "="*70)
    log("TRAFFIC GENERATION RESULTS")
    log("="*70)
    
    logs = [
        (f"{log_dir}/stream_5004.log", "STREAMING (RTP Port 5004) - HIGH PRIORITY"),
        (f"{log_dir}/stream_5005.log", "STREAMING (RTP Port 5005) - HIGH PRIORITY"),
        (f"{log_dir}/http.log", "HTTP (Port 80) - MEDIUM PRIORITY"),
        (f"{log_dir}/bulk.log", "BULK (Port 9000) - LOW PRIORITY"),
    ]
    
    for logfile, title in logs:
        log(f"\n{title}:")
        log("-" * 70)
        if os.path.exists(logfile):
            try:
                with open(logfile, 'r') as f:
                    content = f.read()
                    lines = content.splitlines()
                    # Try to extract meaningful iperf v2 or v3 lines
                    printed = False
                    for line in lines:
                        low = line.lower()
                        if ("sender" in low or "receiver" in low or "lost" in low or "jitter" in low or "mbits/sec" in low):
                            log(line.strip())
                            printed = True
                    # Fallback: print the last 3 lines
                    if not printed and lines:
                        for l in lines[-3:]:
                            log(l.strip())
            except Exception as e:
                log(f"Could not read log: {e}")
        else:
            log("No log file generated")
    
    log("\n" + "="*70)
    log("INTERPRETATION:")
    log("  • Streaming traffic should show low packet loss and good throughput")
    log("  • HTTP traffic may show moderate performance")  
    log("  • Bulk traffic should be throttled/degraded when network is saturated")
    log("  • This demonstrates VNF chain prioritization of streaming (DSCP 46)")
    log("="*70 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Generate saturating traffic to test VNF prioritization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Traffic Types:
  STREAMING (RTP):  UDP ports 5004, 5005 → DSCP 46 (highest priority)
  HTTP:             TCP port 80 → DSCP 34 (medium priority)
  BULK:             TCP port 9000 → DSCP 0 (lowest priority)

The VNF chain should prioritize streaming traffic even under saturation.
        """
    )
    parser.add_argument("-d", "--duration", type=int, default=DURATION,
                       help=f"Traffic duration in seconds (default: {DURATION})")
    parser.add_argument("--streaming-bw", default=STREAMING_BANDWIDTH,
                       help=f"Streaming bandwidth (default: {STREAMING_BANDWIDTH})")
    parser.add_argument("--http-bw", default=HTTP_BANDWIDTH,
                       help=f"HTTP bandwidth (default: {HTTP_BANDWIDTH})")
    parser.add_argument("--bulk-bw", default=BULK_BANDWIDTH,
                       help=f"Bulk bandwidth (default: {BULK_BANDWIDTH})")
    parser.add_argument("--server", default="h1", help="Server host (default: h1)")
    parser.add_argument("--client", default="h4", help="Client host (default: h4)")
    
    args = parser.parse_args()
    
    # Setup log directory (use workspace traffic_logs)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(script_dir, "traffic_logs")
    os.makedirs(log_dir, exist_ok=True)
    
    # Load host PIDs
    host_pids = load_host_pids()
    
    if args.server not in host_pids or args.client not in host_pids:
        log(f"ERROR: Hosts {args.server} or {args.client} not found in topology")
        log(f"Available hosts: {list(host_pids.keys())}")
        sys.exit(1)
    
    server_pid = host_pids[args.server]
    client_pid = host_pids[args.client]
    
    # Get server IP
    server_iface = f"{args.server}-eth0"
    server_ip = get_host_ip(server_pid, server_iface)
    
    if not server_ip:
        log(f"ERROR: Could not determine IP for {args.server}")
        sys.exit(1)
    
    log(f"Server {args.server}: {server_ip}")
    log(f"Client: {args.client}")
    log(f"Duration: {args.duration}s")
    log("")
    
    # Check if iperf3 is installed
    if not check_iperf3_installed(client_pid):
        log("ERROR: iperf not found in client namespace")
        log("Please install iperf: sudo apt-get install iperf")
        sys.exit(1)
    if not check_iperf3_installed(server_pid):
        log("ERROR: iperf not found in server namespace")
        log("Please install iperf: sudo apt-get install iperf")
        sys.exit(1)
    
    log("iperf found in both hosts ✓")
    log("")
    
    # Cleanup any existing iperf processes
    log("Cleaning up any existing iperf processes...")
    kill_iperf_processes(server_pid)
    kill_iperf_processes(client_pid)
    time.sleep(1)
    
    # Setup signal handlers
    def signal_handler(signum, frame):
        cleanup()
        display_results(log_dir)
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Start iperf3 servers
        log("="*70)
        log("PHASE 1: Starting iperf servers")
        log("="*70)
        start_iperf_server(server_pid, STREAMING_PORT_1, args.server, udp=True)
        start_iperf_server(server_pid, STREAMING_PORT_2, args.server, udp=True)
        start_iperf_server(server_pid, HTTP_PORT, args.server, udp=False)
        start_iperf_server(server_pid, BULK_PORT, args.server, udp=False)
        
        time.sleep(2)
        
        # Start traffic generators
        log("\n" + "="*70)
        log("PHASE 2: Starting traffic generators (SATURATING NETWORK)")
        log("="*70)
        
        # High-priority streaming traffic
        proc1 = generate_streaming_traffic(client_pid, server_ip, STREAMING_PORT_1,
                                   args.duration, args.streaming_bw, 
                                   f"{args.client}->Stream1", log_dir)
        proc2 = generate_streaming_traffic(client_pid, server_ip, STREAMING_PORT_2,
                                   args.duration, args.streaming_bw,
                                   f"{args.client}->Stream2", log_dir)
        
        time.sleep(1)
        
        # Medium-priority HTTP traffic
        proc3 = generate_http_traffic(client_pid, server_ip, args.duration,
                            args.http_bw, f"{args.client}->HTTP", log_dir)
        
        time.sleep(1)
        
        # Low-priority bulk traffic (this should saturate the network)
        proc4 = generate_bulk_traffic(client_pid, server_ip, args.duration,
                            args.bulk_bw, f"{args.client}->Bulk", log_dir)
        
        log("\n" + "="*70)
        log(f"PHASE 3: Traffic generation in progress ({args.duration}s)")
        log("="*70)
        log("Network is now saturated with multiple traffic classes.")
        log("VNF chain should prioritize streaming (DSCP 46) over HTTP (DSCP 34) and bulk (DSCP 0).")
        log("\nPress Ctrl+C to stop early...\n")
        
        # Wait for traffic to complete
        for i in range(args.duration):
            if stop_flag.is_set():
                break
            remaining = args.duration - i
            if remaining % 10 == 0 or remaining <= 5:
                log(f"Time remaining: {remaining}s")
            time.sleep(1)
        
        log("\nTraffic generation complete. Collecting results...\n")
        time.sleep(2)
        
        # Retrieve logs from namespace
        retrieve_logs_from_namespace()
        
        # Cleanup and show results
        cleanup()
        time.sleep(1)
        display_results(log_dir)
        
        # Cleanup servers
        log("Stopping iperf servers...")
        kill_iperf_processes(server_pid)
        
    except Exception as e:
        log(f"ERROR: {e}")
        cleanup()
        sys.exit(1)


if __name__ == "__main__":
    main()
