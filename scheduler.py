#!/usr/bin/env python3
import subprocess
import time
import signal
import sys

RTP_DSCP = 46
AF_DSCP  = 34

ROOT_HANDLE = "1:"
RTP_CLASS   = "1:10"
AF_CLASS    = "1:20"
BE_CLASS    = "1:30"

def run(cmd):
    print(f"[Scheduler] {cmd}")
    subprocess.run(cmd, shell=True, check=False)

def detect_core_interfaces():
    out = subprocess.check_output("ip -o -4 addr show", shell=True, text=True)
    ifaces = []
    for ln in out.splitlines():
        _, iface, _, addr, *_ = ln.split()
        if addr.startswith("192.168."):
            ifaces.append(iface)
    return ifaces

def clear_qdisc(dev):
    run(f"tc qdisc del dev {dev} root")

def setup_htb(dev):
    # Root HTB
    run(
        f"tc qdisc add dev {dev} root handle 1: htb default 30"
    )

    # Root class (100%)
    run(
        f"tc class add dev {dev} parent 1: classid 1:1 "
        f"htb rate 100mbit ceil 100mbit"
    )

    # RTP class (guaranteed + strict priority)
    run(
        f"tc class add dev {dev} parent 1:1 classid {RTP_CLASS} "
        f"htb rate 30mbit ceil 100mbit prio 0"
    )
    run(
        f"tc qdisc add dev {dev} parent {RTP_CLASS} handle 10: prio bands 3"
    )

    # AF class
    run(
        f"tc class add dev {dev} parent 1:1 classid {AF_CLASS} "
        f"htb rate 20mbit ceil 80mbit prio 1"
    )

    # Best effort
    run(
        f"tc class add dev {dev} parent 1:1 classid {BE_CLASS} "
        f"htb rate 10mbit ceil 100mbit prio 2"
    )

def add_filters(dev):
    # DSCP -> class mapping
    run(
        f"tc filter add dev {dev} protocol ip parent 1: prio 1 "
        f"u32 match ip tos {(RTP_DSCP<<2)} 0xff flowid {RTP_CLASS}"
    )

    run(
        f"tc filter add dev {dev} protocol ip parent 1: prio 2 "
        f"u32 match ip tos {(AF_DSCP<<2)} 0xff flowid {AF_CLASS}"
    )

def main():
    print("[Scheduler] Starting core egress scheduler VNF")

    core_ifaces = detect_core_interfaces()
    if not core_ifaces:
        print("[Scheduler] WARNING: no core interfaces detected")
        sys.exit(1)

    for iface in core_ifaces:
        print(f"[Scheduler] Installing scheduler on {iface}")
        clear_qdisc(iface)
        setup_htb(iface)
        add_filters(iface)

    def stop(*_):
        print("[Scheduler] Cleaning up")
        for iface in core_ifaces:
            clear_qdisc(iface)
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
