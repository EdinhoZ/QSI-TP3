#!/usr/bin/env python3
import subprocess
import re

RTP_DSCP = 46  # EF
RTP_PORT_RANGE = "16384:32767"

def run(cmd):
    print(f"[Classifier] {cmd}")
    subprocess.run(cmd, shell=True, check=False)

def list_lan_interfaces():
    """
    Detect LAN-facing interfaces:
    - Exclude loopback
    - Exclude router-to-router /30 links (192.168.x.x)
    """
    output = subprocess.check_output("ip -o -4 addr show", shell=True, text=True)
    lan_ifaces = []

    for line in output.splitlines():
        parts = line.split()
        iface = parts[1]
        ip = parts[3]

        if iface == "lo":
            continue

        # Exclude inter-router links
        if ip.startswith("192.168."):
            continue

        lan_ifaces.append(iface)

    return lan_ifaces

def reset_mangle():
    run("iptables -t mangle -F")
    run("iptables -t mangle -X")

def install_rtp_rules(iface):
    # RTP port range
    run(
        f"iptables -t mangle -A PREROUTING "
        f"-i {iface} -p udp --dport {RTP_PORT_RANGE} "
        f"-j DSCP --set-dscp {RTP_DSCP}"
    )

    # Optional explicit RTP ports
    for port in (5004, 5005):
        run(
            f"iptables -t mangle -A PREROUTING "
            f"-i {iface} -p udp --dport {port} "
            f"-j DSCP --set-dscp {RTP_DSCP}"
        )

def main():
    print("[Classifier] Initializing RTP classifier VNF (router-based)")
    reset_mangle()

    lan_ifaces = list_lan_interfaces()
    if not lan_ifaces:
        print("[Classifier] WARNING: No LAN interfaces detected")
        return

    for iface in lan_ifaces:
        print(f"[Classifier] Installing RTP rules on {iface}")
        install_rtp_rules(iface)

    print("[Classifier] RTP classification active (DSCP EF)")

if __name__ == "__main__":
    main()
