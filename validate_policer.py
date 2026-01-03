#!/usr/bin/env python3
"""
Validate whether traffic was policed by checking tc counters in host namespaces.
- Looks for ingress police filters (parent ffff:) and reports packet/byte counts and drops.
- Optionally shows HTB class counters (egress shaping) if present.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Tuple

HOST_PID_FILE = "/tmp/mininet_hosts.json"


def load_host_pids(path: str = HOST_PID_FILE) -> Dict[str, int]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Host PID file not found: {path}")
    with open(path) as f:
        return json.load(f)


def exec_in_ns(pid: int, cmd: str) -> str:
    mnexec = "mnexec" if os.geteuid() == 0 else "sudo mnexec"
    full = f"{mnexec} -a {pid} sh -c \"{cmd}\""
    proc = subprocess.run(full, shell=True, capture_output=True, text=True)
    if proc.returncode != 0:
        return proc.stderr.strip()
    return proc.stdout


def parse_police_counters(text: str) -> List[Tuple[int, int]]:
    # Returns list of (bytes, packets) seen by police filters
    hits = []
    for line in text.splitlines():
        m = re.search(r"Sent\s+(\d+) bytes\s+(\d+) pkt", line, re.IGNORECASE)
        if m:
            hits.append((int(m.group(1)), int(m.group(2))))
        m2 = re.search(r"conform.*?(\d+) bytes.*?(\d+) packets", line, re.IGNORECASE)
        if m2:
            hits.append((int(m2.group(1)), int(m2.group(2))))
    return hits


def parse_drop_counters(text: str) -> int:
    drops = 0
    for line in text.splitlines():
        m = re.search(r"dropped\s+(\d+)", line, re.IGNORECASE)
        if m:
            drops += int(m.group(1))
        m2 = re.search(r"overlimits\s+(\d+)", line, re.IGNORECASE)
        if m2:
            drops += int(m2.group(1))
    return drops


def check_host(host: str, pid: int, iface: str, show_htb: bool):
    print(f"== {host} ({iface}) ==")
    ingress = exec_in_ns(pid, f"tc -s filter show dev {iface} parent ffff:")
    if ingress.strip():
        hits = parse_police_counters(ingress)
        drops = parse_drop_counters(ingress)
        if hits:
            total_bytes = sum(b for b, _ in hits)
            total_pkts = sum(p for _, p in hits)
            print(f" ingress police hits: {len(hits)} filters, bytes={total_bytes}, pkts={total_pkts}, drops~{drops}")
        else:
            print(" ingress: no police counters with traffic")
        print(ingress.strip())
    else:
        print(" ingress: no filters or tc output empty")

    if show_htb:
        htb = exec_in_ns(pid, f"tc -s class show dev {iface} parent 1:")
        if htb.strip():
            print(" egress HTB (class stats):")
            print(htb.strip())
        else:
            print(" egress: no HTB classes")
    print()


def main():
    ap = argparse.ArgumentParser(description="Check tc police/HTB counters in host namespaces")
    ap.add_argument("--hosts", help="Comma-separated host names (default: all)")
    ap.add_argument("--iface-suffix", default="-eth0", help="Interface suffix (default: -eth0)")
    ap.add_argument("--htb", action="store_true", help="Also show egress HTB class stats (if present)")
    args = ap.parse_args()

    host_pids = load_host_pids()
    selected = list(host_pids.keys()) if not args.hosts else [h.strip() for h in args.hosts.split(',') if h.strip()]

    for h in selected:
        pid = host_pids.get(h)
        if not pid:
            print(f"[WARN] host {h} not found in PID map")
            continue
        iface = f"{h}{args.iface_suffix}"
        check_host(h, pid, iface, args.htb)


if __name__ == "__main__":
    main()
