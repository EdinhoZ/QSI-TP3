#!/usr/bin/env python3
import subprocess
import re

RTP_DSCP = 46  # EF
AF_DSCP = 34   # AF41
RTP_PORT_RANGE = "16384:32767" # Common RTP port range

def run(cmd):
    print(f"[Classifier] {cmd}")
    subprocess.run(cmd, shell=True, check=False)

def list_lan_interfaces():
    # Get all IPv4 interfaces
    output = subprocess.check_output("ip -o -4 addr show", shell=True, text=True)
    lan_ifaces = []

    for line in output.splitlines():
        parts = line.split()
        iface = parts[1]
        ip = parts[3]

        # Exclude loopback interface
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

def install_http_rules(iface):
    # HTTP/HTTPS traffic as AF41
    for port in (80, 443):
        run(
            f"iptables -t mangle -A PREROUTING "
            f"-i {iface} -p tcp --dport {port} "
            f"-j DSCP --set-dscp {AF_DSCP}"
        )

def main():
    print("[Classifier] Initializing RTP classifier VNF")
    reset_mangle()

    lan_ifaces = list_lan_interfaces()
    if not lan_ifaces:
        print("[Classifier] WARNING: No LAN interfaces detected")
        return

    for iface in lan_ifaces:
        print(f"[Classifier] Installing RTP rules on {iface}")
        install_rtp_rules(iface)
        install_http_rules(iface)

    print("[Classifier] RTP and HTTP classification active (DSCP EF/AF41)")

if __name__ == "__main__":
    main()
