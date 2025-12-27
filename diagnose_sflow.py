#!/usr/bin/env python3
"""
Diagnostic script for sFlow and Monitor VNF.

Run this while Mininet and the orchestrator are running to verify:
1. OVS is configured with sFlow
2. Monitor is listening
3. Packets are being received
4. Parsing is working
"""

import subprocess
import socket
import time
import sys

def run_cmd(cmd):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except Exception as e:
        return "", str(e), 1

def check_ovs_sflow():
    print("\n" + "="*70)
    print("1. CHECK OVS SFLOW CONFIGURATION")
    print("="*70)
    
    stdout, stderr, rc = run_cmd("sudo ovs-vsctl list-br")
    if rc != 0:
        print("✗ FAIL: Could not list OVS bridges")
        print(f"  Error: {stderr}")
        return False
    
    bridges = stdout.split('\n')
    print(f"Bridges found: {bridges}")
    
    all_good = True
    for br in bridges:
        if not br.strip():
            continue
        print(f"\n  Bridge: {br}")
        
        # Check if sFlow is configured
        stdout, _, rc = run_cmd(f"sudo ovs-vsctl get bridge {br} sflow")
        if rc == 0 and stdout and stdout != "[]":
            print(f"    ✓ sFlow configured: {stdout}")
        else:
            print(f"    ✗ No sFlow configuration on {br}")
            all_good = False
    
    # Get detailed sFlow config
    print("\n  sFlow details:")
    stdout, _, _ = run_cmd("sudo ovs-vsctl list sflow")
    if stdout:
        print(f"    {stdout}")
    else:
        print("    (no sFlow configured)")
    
    return all_good

def check_monitor_listening():
    print("\n" + "="*70)
    print("2. CHECK MONITOR LISTENING")
    print("="*70)
    
    stdout, stderr, rc = run_cmd("ss -ulpn | grep 6343")
    if rc == 0 and stdout:
        print(f"✓ Monitor listening on port 6343:")
        print(f"  {stdout}")
        return True
    else:
        print("✗ Monitor NOT listening on port 6343")
        print("  Ensure test_script.py is running: python3 test_script.py")
        return False

def check_prometheus():
    print("\n" + "="*70)
    print("3. CHECK PROMETHEUS ENDPOINT")
    print("="*70)
    
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:9100/metrics", timeout=2) as resp:
            body = resp.read().decode()
            metrics = {}
            for line in body.splitlines():
                if line.startswith("vnf_"):
                    parts = line.split()
                    if len(parts) >= 2:
                        metrics[parts[0]] = parts[1]
            
            print("✓ Prometheus endpoint responding:")
            for k, v in metrics.items():
                print(f"  {k} = {v}")
            return True
    except Exception as e:
        print(f"✗ Could not reach Prometheus: {e}")
        return False

def check_monitor_logs():
    print("\n" + "="*70)
    print("4. CHECK MONITOR LOGS")
    print("="*70)
    
    import glob
    logs = sorted(glob.glob("vnf_logs/monitor.log*"))
    if not logs:
        print("✗ No monitor logs found")
        return False
    
    latest_log = logs[-1]
    print(f"Latest log: {latest_log}")
    
    # Read last 20 lines
    stdout, _, rc = run_cmd(f"tail -20 {latest_log}")
    if rc == 0:
        print("\nRecent entries:")
        for line in stdout.split('\n'):
            if line:
                print(f"  {line}")
        
        # Check if we see zeros or actual data
        if "0.000 Mbps" in stdout and "Flows: 0" in stdout:
            print("\n✗ Monitor showing all zeros (no traffic detected)")
            return False
        else:
            print("\n✓ Monitor showing non-zero metrics")
            return True
    else:
        print(f"✗ Could not read logs: {stderr}")
        return False

def manual_sflow_test():
    print("\n" + "="*70)
    print("5. MANUAL NETWORK DIAGNOSTIC")
    print("="*70)
    
    print("\nTesting if packets can reach the monitor:")
    print("  Starting a test listener on port 6343...")
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 6344))  # Use different port to avoid conflicts
    sock.settimeout(10)
    
    print("  Waiting 10 seconds for any UDP packets on 127.0.0.1:6344...")
    print("  (Note: may not receive if OVS targets 6343, not 6344)")
    
    try:
        data, addr = sock.recvfrom(1024)
        print(f"  ✓ Received {len(data)} bytes from {addr}")
        return True
    except socket.timeout:
        print("  (No packets received on test port)")
        return False
    finally:
        sock.close()

def main():
    print("\n" + "="*70)
    print("QSI-TP3 sFlow & Monitor Diagnostic")
    print("="*70)
    
    results = []
    
    results.append(("OVS sFlow Config", check_ovs_sflow()))
    results.append(("Monitor Listening", check_monitor_listening()))
    results.append(("Prometheus Endpoint", check_prometheus()))
    results.append(("Monitor Logs", check_monitor_logs()))
    
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    
    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{name:30} {status}")
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    print(f"\nResult: {passed}/{total} checks passed")
    
    if passed == total:
        print("\n✓ Everything looks good! Try generating traffic:")
        print("  In Mininet CLI:")
        print("    h1 iperf3 -s -p 5004 -D")
        print("    h4 iperf3 -c 10.1.0.2 -p 5004 -u -b 5M -t 10")
        return 0
    else:
        print("\n✗ Some checks failed. Review output above for fixes.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
