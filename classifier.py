#!/usr/bin/env python3
import subprocess
import time

BRIDGE = "br0"

def run(cmd):
    print("[Classifier]", cmd)
    subprocess.run(cmd, shell=True, check=False)

def install_flows():
    run(f"ovs-ofctl del-flows {BRIDGE}")

    # HTTP (port 80) -> AF41 (DSCP 34)
    run(f"ovs-ofctl add-flow {BRIDGE} \"ip,nw_proto=6,tp_dst=80,actions=set_field:34->ip_dscp,normal\"")

    # RTP / VoIP (UDP 5004, 5005) -> EF (DSCP 46)
    run(f"ovs-ofctl add-flow {BRIDGE} \"udp,tp_dst=5004,actions=set_field:46->ip_dscp,normal\"")
    run(f"ovs-ofctl add-flow {BRIDGE} \"udp,tp_dst=5005,actions=set_field:46->ip_dscp,normal\"")

    # DNS -> low priority (DSCP 8)
    run(f"ovs-ofctl add-flow {BRIDGE} \"udp,tp_dst=53,actions=set_field:8->ip_dscp,normal\"")

    # SSH -> medium (DSCP 16)
    run(f"ovs-ofctl add-flow {BRIDGE} \"tcp,tp_dst=22,actions=set_field:16->ip_dscp,normal\"")

    # Default -> best effort
    run(f"ovs-ofctl add-flow {BRIDGE} \"ip,actions=set_field:0->ip_dscp,normal\"")

def main():
    print("[Classifier] Installing QoS flows into OVS...")
    install_flows()
    print("[Classifier] Rules installed. Sleeping forever.")
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
