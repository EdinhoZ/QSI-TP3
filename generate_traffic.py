#!/usr/bin/env python3
"""
Simple script to generate traffic on running Mininet topology.
Uses the existing topology that should be running as a subprocess.
"""
import subprocess
import time
import sys
import os

def run_on_host(host_name: str, cmd: str, bg=False) -> str:
    """
    Run a command on a Mininet host.
    Uses the fact that Mininet interfaces are named like 'h1-eth0' on the data plane.
    """
    # For now, we'll just try to ping from h4 to h1 which should work if routing is set up
    # h1 is on LAN1 (10.1.0.0/24): IP 10.1.0.2
    # h4 is on LAN2 (10.2.0.0/24): IP 10.2.0.2
    # h7 is on LAN3 (10.3.0.0/24): IP 10.3.0.2
    # h10 is on LAN4 (10.4.0.0/24): IP 10.4.0.2
    pass

def main():
    """Generate UDP traffic for 30 seconds"""
    # Host IPs (from topology comments)
    h1_ip = "10.1.0.2"
    h4_ip = "10.2.0.2"
    h7_ip = "10.3.0.2"
    h10_ip = "10.4.0.2"
    
    print("[Traffic] Starting iperf server on h1...")
    # Start iperf server on h1
    proc_server = subprocess.Popen(
        f"iperf -s -u -p 5201 -B {h1_ip}",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(1)
    
    print(f"[Traffic] Starting iperf UDP client on h4 -> {h1_ip}:5201 for 15s at 5Mbps...")
    # Run iperf client from h4 to h1 (UDP, 5 Mbps for 15 seconds)
    proc_client = subprocess.Popen(
        f"iperf -c {h1_ip} -u -p 5201 -b 5M -t 15",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    
    try:
        proc_client.wait(timeout=20)
        print("[Traffic] iperf client finished")
    except subprocess.TimeoutExpired:
        proc_client.kill()
        print("[Traffic] iperf client timeout")
    finally:
        proc_server.terminate()
        print("[Traffic] Stopped iperf server")
    
    print("[Traffic] Done. Check monitor logs for metrics.")

if __name__ == "__main__":
    main()
