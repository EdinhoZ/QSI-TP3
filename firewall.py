#!/usr/bin/env python3
import subprocess
import time
import signal
import sys

CHAIN = "VNF_FW"
RTP_DSCP = 46
RTP_PORT_RANGE = "16384:32767"

def run(cmd):
    print(f"[Firewall] {cmd}")
    subprocess.run(cmd, shell=True, check=False)

def detect_lan_interfaces():
    out = subprocess.check_output("ip -o -4 addr show", shell=True, text=True)
    ifaces = []
    for ln in out.splitlines():
        _, iface, _, addr, *_ = ln.split()
        if iface == "lo":
            continue
        if addr.startswith("192.168."):
            continue
        ifaces.append(iface)
    return ifaces

def reset_fw():
    run(f"iptables -D FORWARD -j {CHAIN}")
    run(f"iptables -F {CHAIN}")
    run(f"iptables -X {CHAIN}")

def setup_chain():
    run(f"iptables -N {CHAIN}")
    run(f"iptables -I FORWARD -j {CHAIN}")

def allow_established():
    run(
        f"iptables -A {CHAIN} "
        f"-m conntrack --ctstate ESTABLISHED,RELATED "
        f"-j ACCEPT"
    )

def allow_rtp(iface):
    # DSCP-based
    run(
        f"iptables -A {CHAIN} -i {iface} "
        f"-m dscp --dscp {RTP_DSCP} "
        f"-j ACCEPT"
    )

    # Port-based fallback
    run(
        f"iptables -A {CHAIN} -i {iface} "
        f"-p udp --dport {RTP_PORT_RANGE} "
        f"-j ACCEPT"
    )

def drop_suspicious_udp(iface):
    # Drop high-rate non-RTP UDP entering the core
    run(
        f"iptables -A {CHAIN} -i {iface} "
        f"-p udp ! --dport {RTP_PORT_RANGE} "
        f"-m conntrack --ctstate NEW "
        f"-m limit --limit 50/second --limit-burst 100 "
        f"-j ACCEPT"
    )
    run(
        f"iptables -A {CHAIN} -i {iface} "
        f"-p udp "
        f"-j DROP"
    )

def default_accept():
    run(f"iptables -A {CHAIN} -j ACCEPT")

def main():
    print("[Firewall] Initializing edge firewall VNF")

    reset_fw()
    setup_chain()
    allow_established()

    lan_ifaces = detect_lan_interfaces()
    if not lan_ifaces:
        print("[Firewall] WARNING: no LAN interfaces detected")

    for iface in lan_ifaces:
        print(f"[Firewall] Protecting core ingress on {iface}")
        allow_rtp(iface)
        drop_suspicious_udp(iface)

    default_accept()
    print("[Firewall] Firewall active")

    def signal_handler(sig, frame):
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # keep alive
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
