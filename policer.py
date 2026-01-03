#!/usr/bin/env python3
import subprocess
import time
import signal
import sys

RTP_DSCP = 46
DEFAULT_RATE = "20mbit"
DEFAULT_BURST = "100kb"

def run(cmd):
    print(f"[Policer] {cmd}")
    subprocess.run(cmd, shell=True, check=False)

def detect_lan_interfaces():
    """
    LAN-facing = non-loopback, non-192.168.x.x
    """
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

def clear_ingress(dev):
    run(f"tc qdisc del dev {dev} ingress")

def setup_ingress(dev):
    run(f"tc qdisc add dev {dev} handle ffff: ingress")

def install_policer(dev):
    """
    Police non-EF UDP traffic entering the core.
    EF traffic is explicitly excluded.
    """
    tos_ef = (RTP_DSCP << 2) & 0xff

    # Allow EF (RTP) unconditionally
    run(
        f"tc filter add dev {dev} parent ffff: protocol ip prio 1 "
        f"u32 match ip tos {tos_ef} 0xff "
        f"action pass"
    )

    # Police all other UDP
    run(
        f"tc filter add dev {dev} parent ffff: protocol ip prio 10 "
        f"u32 match ip protocol 17 0xff "
        f"action police rate {DEFAULT_RATE} burst {DEFAULT_BURST} drop"
    )

def main():
    print("[Policer] Starting edge ingress policer VNF")

    lan_ifaces = detect_lan_interfaces()
    if not lan_ifaces:
        print("[Policer] WARNING: no LAN interfaces detected")
        sys.exit(1)

    for iface in lan_ifaces:
        print(f"[Policer] Installing policer on {iface}")
        clear_ingress(iface)
        setup_ingress(iface)
        install_policer(iface)

    def stop(*_):
        print("[Policer] Cleaning up")
        for iface in lan_ifaces:
            clear_ingress(iface)
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
