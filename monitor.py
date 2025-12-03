#!/usr/bin/env python3

import time
import threading
import os
import sys
from scapy.all import sniff
from collections import defaultdict
from prometheus_client import Gauge, start_http_server

# === Optional: Redirect logs to file ===
# LOGFILE = "monitor.log"
# log_file = open(LOGFILE, "a", buffering=1)  # line-buffered
# sys.stdout = log_file
# sys.stderr = log_file
# =======================================

# Configuration

def get_first_iface():
    for iface in os.listdir("/sys/class/net"):
        if iface != "lo":
            return iface
    return None

INTERFACE = get_first_iface()
SCRAPE_PORT = 9100
STATS_INTERVAL = 1

# Prometheus Gauges
throughput_g = Gauge("vnf_throughput_mbps", "Total throughput (Mbps)")
delay_g      = Gauge("vnf_delay_ms",        "Estimated packet delay (ms)")
jitter_g     = Gauge("vnf_jitter_ms",       "Estimated jitter (ms)")
loss_g       = Gauge("vnf_loss_rate",       "Estimated loss ratio")

# Internal monitoring storage
flow_last_timestamp = defaultdict(lambda: None)
flow_packet_count   = defaultdict(int)
flow_expected_count = defaultdict(int)
flow_last_delay = defaultdict(float)
flow_last_jitter = defaultdict(float)

def get_stats():
    stats = {}
    with open("/proc/net/dev") as f:
        for line in f:
            if INTERFACE in line:
                parts = line.split()
                stats["rx_bytes"] = int(parts[1])
                stats["rx_packets"] = int(parts[2])
                stats["tx_bytes"] = int(parts[9])
                stats["tx_packets"] = int(parts[10])
                return stats
    return None

# Packet processing (Scapy)
def process_packet(pkt):
    now = time.time()
    if "IP" in pkt:
        ip = pkt["IP"]
        flow_id = (ip.src, ip.dst, ip.proto)

        # Update packet counts
        flow_packet_count[flow_id] += 1
        flow_expected_count[flow_id] += 1  # Simplified for demo

        # Delay estimate

        last_timestamp = flow_last_timestamp[flow_id]

        if last_timestamp is not None:
            inter_arrival = now - last_timestamp # in seconds
            estimated_delay = inter_arrival * 1000  # ms

            flow_last_delay[flow_id] = estimated_delay
            flow_last_timestamp[flow_id] = now

            # Jitter estimate
            old_jitter = flow_last_jitter[flow_id]
            diff = abs(estimated_delay - old_jitter)
            new_jitter = old_jitter + (diff - old_jitter) / 16.0
            flow_last_jitter[flow_id] = new_jitter
        else:
            # First packet
            flow_last_timestamp[flow_id] = now

# Scapy sniffer thread
def start_sniffer():
    sniff(iface=INTERFACE, prn=process_packet, store=False)

def monitor_loop():
    prev = get_stats()
    print("[Monitor] Started monitoring loop")
    
    while True:
        time.sleep(STATS_INTERVAL)
        curr = get_stats()

        if not curr or not prev:
            continue

        # Throughput (Mbps)
        rx_rate = (curr["rx_bytes"] - prev["rx_bytes"]) * 8 / STATS_INTERVAL
        tx_rate = (curr["tx_bytes"] - prev["tx_bytes"]) * 8 / STATS_INTERVAL
        total_mbps = (rx_rate + tx_rate) / 1e6

        avg_delay = 0.0
        avg_jitter = 0.0
        avg_loss = 0.0

        flows = len(flow_packet_count)
        if flows > 0:
            avg_delay = sum(flow_last_delay.values()) / flows
            avg_jitter = sum(flow_last_jitter.values()) / flows

            delivered = sum(flow_packet_count.values())
            expected = sum(flow_expected_count.values())

            if expected > 0:
                avg_loss = 1 - (delivered / expected)

        # Prometheus exports
        throughput_g.set(total_mbps)
        delay_g.set(avg_delay)
        jitter_g.set(avg_jitter)
        loss_g.set(avg_loss)

        # Debug print
        print(
            f"[Monitor] Throughput: {total_mbps:.3f} Mbps, "
            f"Delay: {avg_delay:.2f} ms, Jitter: {avg_jitter:.2f} ms, "
            f"Loss: {avg_loss:.4f}"
        )

        prev = curr

if __name__ == "__main__":
    print("[Monitor] Initializing...")

    start_http_server(SCRAPE_PORT)
    sniffer_thread = threading.Thread(target=start_sniffer, daemon=True)
    sniffer_thread.start()
    
    monitor_loop()
