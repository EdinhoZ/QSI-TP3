#!/usr/bin/env python3
import subprocess
import os
import sys
import time
import signal
import argparse
from typing import List

MONITOR = "monitor.py"
FIREWALL = "firewall.py"

# HTB+prio configuration for host interfaces
RATE_LIMITS = {
    46: "20mbit",  # RTP/VoIP (DSCP EF)
    34: "10mbit",  # HTTP (DSCP AF41)
    0: "5mbit",    # Bulk/Default
}

# Default hosts to apply VNF configuration (can be overridden via args)
DEFAULT_TARGET_HOSTS = ["h1", "h4"]

VNFS: List[subprocess.Popen] = []
TARGET_HOSTS: List[str] = DEFAULT_TARGET_HOSTS
ENABLE_CLASSIFIER = True
ENABLE_POLICER = True
ENABLE_SCHEDULER = True
ENABLE_FIREWALL = True
ENABLE_MONITOR = True

def log(msg):
    print(f"[ORC] {msg}")

def run(cmd: str) -> subprocess.Popen:
    log(cmd)
    return subprocess.Popen(cmd, shell=True)

def run_blocking(cmd: str):
    log(cmd)
    subprocess.check_call(cmd, shell=True)

def get_mininet_hosts() -> List[str]:
    """Get list of Mininet hosts from running processes"""
    try:
        # Check for Mininet host processes
        result = subprocess.run(
            "ps aux | grep 'mininet:h' | grep -v grep | awk '{print $NF}'",
            shell=True,
            capture_output=True,
            text=True
        )
        # Extract host names like h1, h2, h3 from "mininet:h1" format
        hosts = []
        for line in result.stdout.splitlines():
            if 'mininet:h' in line:
                host = line.split(':')[-1].strip()
                if host.startswith('h') and host[1:].replace('h', '').isdigit():
                    hosts.append(host)
        
        if hosts:
            return sorted(set(hosts))
        
        # Fallback: check for host interfaces in root namespace
        result = subprocess.run(
            "ip link show | grep -oE 'h[0-9]+-eth0'",
            shell=True,
            capture_output=True,
            text=True
        )
        hosts = [iface.replace('-eth0', '') for iface in result.stdout.splitlines()]
        return sorted(set(hosts))
    except Exception as e:
        log(f"Error detecting hosts: {e}")
        return []

def cleanup():
    log("Stopping VNFs...")
    for p in VNFS:
        try:
            p.terminate()
        except Exception:
            pass

    # Clean up host interface tc rules
    for host in TARGET_HOSTS:
        iface = f"{host}-eth0"
        pid = get_host_pid(host)
        if pid:
            subprocess.run(
                f"mnexec -a {pid} tc qdisc del dev {iface} root",
                shell=True,
                stderr=subprocess.DEVNULL
            )
            subprocess.run(
                f"mnexec -a {pid} iptables -t mangle -F",
                shell=True,
                stderr=subprocess.DEVNULL
            )

    log("Cleanup complete")

def handle_signal(sig, frame):
    cleanup()
    sys.exit(0)


def get_host_pid(host: str) -> str:
    try:
        result = subprocess.run(
            f"pgrep -f 'mininet:{host}$'",
            shell=True,
            capture_output=True,
            text=True
        )
        pid = result.stdout.strip().split('\n')[0]  # Get first PID if multiple
        if pid:
            return pid
        return None
    except Exception:
        return None

def apply_host_classifier(host: str):
    if not ENABLE_CLASSIFIER:
        return
        
    log(f"Configuring classifier on {host}")
    
    pid = get_host_pid(host)
    if not pid:
        log(f"Error: Cannot find PID for {host}")
        return
    
    # Clear existing mangle rules
    subprocess.run(
        f"mnexec -a {pid} iptables -t mangle -F",
        shell=True,
        stderr=subprocess.DEVNULL
    )
    
    # Mark RTP traffic (ports 5004, 5005 and RTP range) with DSCP EF (46)
    subprocess.run(
        f"mnexec -a {pid} iptables -t mangle -A OUTPUT -p udp --dport 16384:32767 -j DSCP --set-dscp 46",
        shell=True
    )
    subprocess.run(
        f"mnexec -a {pid} iptables -t mangle -A OUTPUT -p udp --dport 5004 -j DSCP --set-dscp 46",
        shell=True
    )
    subprocess.run(
        f"mnexec -a {pid} iptables -t mangle -A OUTPUT -p udp --dport 5005 -j DSCP --set-dscp 46",
        shell=True
    )
    
    # Mark HTTP traffic (port 80) with DSCP AF41 (34)
    subprocess.run(
        f"mnexec -a {pid} iptables -t mangle -A OUTPUT -p tcp --dport 80 -j DSCP --set-dscp 34",
        shell=True
    )
    
    log(f"Classifier configured on {host}")

def apply_host_policing(host: str):
    """Apply HTB+prio qdisc hierarchy on host interface"""
    if not ENABLE_POLICER:
        return
        
    log(f"Configuring policer+scheduler on {host}")
    iface = f"{host}-eth0"
    
    pid = get_host_pid(host)
    if not pid:
        log(f"Error: Cannot find PID for {host}")
        return
    
    # Remove existing qdiscs
    subprocess.run(
        f"mnexec -a {pid} tc qdisc del dev {iface} root",
        shell=True,
        stderr=subprocess.DEVNULL
    )
    
    # Create HTB root qdisc
    subprocess.run(
        f"mnexec -a {pid} tc qdisc add dev {iface} root handle 1: htb default 30",
        shell=True,
        stderr=subprocess.DEVNULL
    )
    
    # Create root class
    subprocess.run(
        f"mnexec -a {pid} tc class add dev {iface} parent 1: classid 1:1 htb rate 100mbit ceil 100mbit",
        shell=True,
        stderr=subprocess.DEVNULL
    )
    
    # Create rate-limited classes
    subprocess.run(
        f"mnexec -a {pid} tc class add dev {iface} parent 1:1 classid 1:10 htb rate 20mbit ceil 20mbit prio 0",
        shell=True,
        stderr=subprocess.DEVNULL
    )
    subprocess.run(
        f"mnexec -a {pid} tc class add dev {iface} parent 1:1 classid 1:20 htb rate 10mbit ceil 10mbit prio 1",
        shell=True,
        stderr=subprocess.DEVNULL
    )
    subprocess.run(
        f"mnexec -a {pid} tc class add dev {iface} parent 1:1 classid 1:30 htb rate 5mbit ceil 5mbit prio 2",
        shell=True,
        stderr=subprocess.DEVNULL
    )
    
    # Add prio qdisc to RTP class (scheduler)
    if ENABLE_SCHEDULER:
        subprocess.run(
            f"mnexec -a {pid} tc qdisc add dev {iface} parent 1:10 handle 10: prio bands 3",
            shell=True,
            stderr=subprocess.DEVNULL
        )
    
    # Add filters
    subprocess.run(
        f"mnexec -a {pid} tc filter add dev {iface} protocol ip parent 1: prio 1 u32 match ip tos 0xb8 0xff flowid 1:10",
        shell=True,
        stderr=subprocess.DEVNULL
    )
    subprocess.run(
        f"mnexec -a {pid} tc filter add dev {iface} protocol ip parent 1: prio 2 u32 match ip tos 0x88 0xff flowid 1:20",
        shell=True,
        stderr=subprocess.DEVNULL
    )
    
    log(f"Policer+scheduler configured on {host}")

def verify_config(host: str):
    print(f"\n=== Verifying {host} ===")
    iface = f"{host}-eth0"
    
    pid = get_host_pid(host)
    if not pid:
        print(f"Error: Cannot find PID for {host}")
        return
    
    print(f"\nTC Classes on {iface}:")
    result = subprocess.run(
        f"mnexec -a {pid} tc class show dev {iface}",
        shell=True,
        capture_output=True,
        text=True
    )
    print(result.stdout)
    
    print(f"\nIPTables mangle rules:")
    result = subprocess.run(
        f"mnexec -a {pid} iptables -t mangle -L OUTPUT -n -v",
        shell=True,
        capture_output=True,
        text=True
    )
    print(result.stdout)

def launch_firewall():
    if ENABLE_FIREWALL and os.path.exists(FIREWALL):
        log("Launching firewall")
        VNFS.append(run(f"python3 {FIREWALL}"))
    elif ENABLE_FIREWALL:
        log(f"Warning: Firewall enabled but {FIREWALL} not found")

def launch_monitor():
    if ENABLE_MONITOR and os.path.exists(MONITOR):
        log("Launching monitor")
        VNFS.append(run(f"python3 {MONITOR}"))
    elif ENABLE_MONITOR:
        log(f"Warning: Monitor enabled but {MONITOR} not found")

def main():
    global TARGET_HOSTS, ENABLE_CLASSIFIER, ENABLE_POLICER, ENABLE_SCHEDULER, ENABLE_FIREWALL, ENABLE_MONITOR
    
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description="VNF orchestrator: Apply classifier, policer, and scheduler to Mininet hosts"
    )
    parser.add_argument(
        "hosts",
        nargs="*",
        default=DEFAULT_TARGET_HOSTS,
        help=f"Target hosts to configure (default: {' '.join(DEFAULT_TARGET_HOSTS)})"
    )
    parser.add_argument("--no-classifier", action="store_true", help="Disable DSCP classifier")
    parser.add_argument("--no-policer", action="store_true", help="Disable rate limiting policer")
    parser.add_argument("--no-scheduler", action="store_true", help="Disable prio scheduler")
    parser.add_argument("--no-firewall", action="store_true", help="Disable firewall VNF")
    parser.add_argument("--no-monitor", action="store_true", help="Disable monitor VNF")
    args = parser.parse_args()
    
    TARGET_HOSTS = args.hosts if args.hosts else DEFAULT_TARGET_HOSTS
    ENABLE_CLASSIFIER = not args.no_classifier
    ENABLE_POLICER = not args.no_policer
    ENABLE_SCHEDULER = not args.no_scheduler
    ENABLE_FIREWALL = not args.no_firewall
    ENABLE_MONITOR = not args.no_monitor
    
    if os.geteuid() != 0:
        log("Must be run as root")
        sys.exit(1)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Check if Mininet is running
    hosts = get_mininet_hosts()
    if not hosts:
        log("No Mininet hosts detected. Is Mininet running?")
        sys.exit(1)

    log(f"Detected Mininet hosts: {hosts}")
    log(f"Configuring VNFs on: {TARGET_HOSTS}")

    # Apply VNF configuration to target hosts
    for host in TARGET_HOSTS:
        if host not in hosts:
            log(f"Warning: {host} not found in running Mininet topology")
            continue
            
        log(f"Configuring {host}...")
        apply_host_classifier(host)
        apply_host_policing(host)
    
    time.sleep(1)

    # Verify configuration
    print("\n" + "="*50)
    print("VERIFICATION")
    print("="*50)
    
    for host in TARGET_HOSTS:
        verify_config(host)

    # Launch background VNFs
    launch_firewall()
    time.sleep(0.5)
    
    launch_monitor()

    log("All VNFs configured and running")
    log("Press Ctrl+C to stop")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
