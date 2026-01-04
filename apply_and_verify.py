#!/usr/bin/env python3
import subprocess

TARGET_HOSTS = ["h1", "h4"]

def get_host_pid(host: str) -> str:
    """Get the PID of a Mininet host process"""
    result = subprocess.run(
        f"pgrep -f 'mininet:{host}$'",
        shell=True,
        capture_output=True,
        text=True
    )
    pid = result.stdout.strip().split('\n')[0]
    return pid if pid else None

def apply_host_classifier(host: str):
    """Apply DSCP marking in host namespace using iptables"""
    print(f"Configuring classifier on {host}")
    
    pid = get_host_pid(host)
    if not pid:
        print(f"Error: Cannot find PID for {host}")
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
    
    print(f"Classifier configured on {host}")

def apply_host_policing(host: str):
    """Apply HTB+prio qdisc hierarchy on host interface"""
    print(f"Configuring policer+scheduler on {host}")
    iface = f"{host}-eth0"
    
    pid = get_host_pid(host)
    if not pid:
        print(f"Error: Cannot find PID for {host}")
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
    
    # Add prio qdisc to RTP class
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
    
    print(f"Policer+scheduler configured on {host}")

def verify_config(host: str):
    """Verify VNF configuration"""
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

if __name__ == "__main__":
    print("Applying VNFs to hosts:", TARGET_HOSTS)
    
    for host in TARGET_HOSTS:
        apply_host_classifier(host)
        apply_host_policing(host)
    
    print("\n" + "="*50)
    print("VERIFICATION")
    print("="*50)
    
    for host in TARGET_HOSTS:
        verify_config(host)
    
    print("\n✓ VNFs applied and verified!")
    print("You can now run stress_test.py to test with VNFs enabled")
