#!/usr/bin/env python3
import argparse
import subprocess
import sys
import os

def run(cmd: str):
    print(f"[Classifier] Running: {cmd}")
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[Classifier] ERROR running '{cmd}': {proc.stderr.strip()}")
    return proc.returncode, proc.stdout, proc.stderr

def detect_bridge_name() -> str:
    bridges = [dev for dev in os.listdir("/sys/class/net") if dev.startswith("s") and dev[1:].isdigit()]

    if not bridges:
        raise RuntimeError("Could not auto-detect any Mininet OVS bridge.")

    if len(bridges) == 1:
        print(f"[Classifier] Auto-detected bridge: {bridges[0]}")
        return bridges[0]

    print(f"[Classifier] Multiple bridges detected: {bridges}. Using 's1' by default.")
    return "s1"

def install_flows(bridge: str):
    print(f"[Classifier] Clearing existing flows...")
    run(f"ovs-ofctl del-flows {bridge}")

    # DSCP rules
    dscp_policies = [
        ("HTTP",    "ip,nw_proto=6,tp_dst=80",    34),  # AF41
        ("RTP1",    "udp,tp_dst=5004",            46),  # EF (streaming)
        ("RTP2",    "udp,tp_dst=5005",            46),  # EF (streaming)
        ("DNS",     "udp,tp_dst=53",               8),
        ("SSH",     "tcp,tp_dst=22",              16),
        ("DEFAULT", "ip",                           0),
    ]

    print(f"[Classifier] Installing DSCP classifier rules...")
    for name, match, dscp in dscp_policies:
        flow = f"{match},actions=set_field:{dscp}->ip_dscp,normal"
        ret, _, _ = run(f'ovs-ofctl add-flow {bridge} "{flow}"')
        if ret == 0:
            print(f"[Classifier] Installed {name} rule with DSCP {dscp}")

    print(f"[Classifier] Flow installation complete.")

def main():
    parser = argparse.ArgumentParser(description="OVS QoS Classifier (DSCP setter)")
    parser.add_argument("-b", "--bridge", help="OVS bridge name (default: auto-detect)")
    args = parser.parse_args()

    bridge = args.bridge or detect_bridge_name()
    print(f"[Classifier] Installing flows on bridge: {bridge}")
    install_flows(bridge)
    print(f"[Classifier] DSCP classification rules successfully installed.")

if __name__ == "__main__":
    main()