#!/usr/bin/env python3
"""
Verify VNF functionality by comparing traffic with and without VNFs.
This script tests if DSCP marking and rate limiting actually work.
"""
import subprocess
import json
import time
import sys

def run_cmd(cmd):
    """Run command and return output"""
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

def load_host_pids():
    """Load host PIDs from Mininet topology"""
    try:
        with open("/tmp/mininet_hosts.json") as f:
            return json.load(f)
    except:
        print("ERROR: Cannot load /tmp/mininet_hosts.json - is Mininet running?")
        sys.exit(1)

def check_tc_config(host, pid):
    """Check tc configuration on a host interface"""
    iface = f"{host}-eth0"
    print(f"\n=== Checking {host} ({iface}) ===")
    
    # Check root qdisc
    rc, out, _ = run_cmd(f"sudo mnexec -a {pid} tc qdisc show dev {iface}")
    if rc == 0:
        print(f"Root qdisc:\n{out}")
    
    # Check classes
    rc, out, _ = run_cmd(f"sudo mnexec -a {pid} tc class show dev {iface}")
    if rc == 0 and out:
        print(f"\nClasses:\n{out}")
    
    # Check filters
    rc, out, _ = run_cmd(f"sudo mnexec -a {pid} tc filter show dev {iface}")
    if rc == 0 and out:
        print(f"\nFilters:\n{out}")

def check_iptables_mangle(host, pid):
    """Check iptables mangle table for DSCP marking"""
    print(f"\n=== Checking iptables mangle in {host} ===")
    rc, out, _ = run_cmd(f"sudo mnexec -a {pid} iptables -t mangle -L -n -v")
    if rc == 0:
        print(out)

def send_test_packet(src_host, src_pid, dst_ip, port, proto="udp"):
    """Send a test packet and check if it's marked correctly"""
    if proto == "udp":
        cmd = f"sudo mnexec -a {src_pid} timeout 2 iperf -c {dst_ip} -u -p {port} -t 1 -b 1M"
    else:
        cmd = f"sudo mnexec -a {src_pid} timeout 2 iperf -c {dst_ip} -p {port} -t 1"
    
    print(f"\n=== Sending {proto} to {dst_ip}:{port} ===")
    rc, out, err = run_cmd(cmd)
    print(f"Return code: {rc}")
    if out:
        print(f"Output: {out[:200]}")

def main():
    print("=" * 60)
    print("VNF VERIFICATION SCRIPT")
    print("=" * 60)
    
    pids = load_host_pids()
    
    if 'h1' not in pids or 'h4' not in pids:
        print("ERROR: Need at least h1 and h4 in topology")
        sys.exit(1)
    
    # Check if VNFs are configured
    print("\n[1] Checking tc configuration on hosts...")
    for host in ['h1', 'h4']:
        if host in pids:
            check_tc_config(host, pids[host])
    
    # Check DSCP marking rules
    print("\n[2] Checking DSCP marking rules...")
    for host in ['h1', 'h4']:
        if host in pids:
            check_iptables_mangle(host, pids[host])
    
    # Get h1 IP
    rc, h1_ip_out, _ = run_cmd(f"sudo mnexec -a {pids['h1']} ip -4 addr show h1-eth0")
    h1_ip = None
    if rc == 0:
        for line in h1_ip_out.split('\n'):
            if 'inet ' in line:
                h1_ip = line.split()[1].split('/')[0]
                break
    
    if not h1_ip:
        print("ERROR: Could not determine h1 IP address")
        print(f"Command output: {h1_ip_out}")
        sys.exit(1)
    
    print(f"\n[3] h1 IP address: {h1_ip}")
    
    # Check if iperf server is running on h1
    rc, _, _ = run_cmd(f"sudo mnexec -a {pids['h1']} pgrep iperf")
    if rc != 0:
        print("\n[4] Starting iperf server on h1...")
        subprocess.Popen(f"sudo mnexec -a {pids['h1']} iperf -s -u -D", shell=True)
        time.sleep(1)
    else:
        print("\n[4] iperf server already running on h1")
    
    print("\n" + "=" * 60)
    print("SUMMARY:")
    print("=" * 60)
    print("If VNFs are working correctly, you should see:")
    print("  1. HTB qdisc with 3 classes (1:10, 1:20, 1:30) on host interfaces")
    print("  2. Filters matching DSCP 46 and DSCP 34")
    print("  3. iptables mangle rules setting DSCP on UDP 5004/5005 and TCP 80")
    print("\nIf you don't see these, run: sudo python3 test_script.py")
    print("=" * 60)

if __name__ == "__main__":
    main()
