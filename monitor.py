#!/usr/bin/env python3
import time
import logging
import argparse
from prometheus_client import Gauge, start_http_server
from pysflow import SFlowReceiver, SFlowDatagram

SCRAPE_PORT = 9100
STATS_INTERVAL = 1  # (segundos)
LOGFILE = "monitor.log"

throughput_g = Gauge("vnf_throughput_mbps", "Total throughput (Mbps)")
flow_count_g = Gauge("vnf_flow_count", "Number of active flows")
avg_packet_size_g = Gauge("vnf_avg_packet_size_bytes", "Average packet size in bytes")

logging.basicConfig(filename=LOGFILE, level=logging.INFO,
                    format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')


class Monitor:
    def __init__(self):
        self.total_bytes = 0
        self.total_packets = 0
        self.flow_counts = {}
        self.last_time = time.time()

    def handle_sflow(self, datagram: SFlowDatagram):
        for flow_sample in datagram.flow_samples:
            for record in flow_sample.flow_records:
                if hasattr(record, 'header_protocol'):
                    # contar pacotes IP
                    if record.header_protocol in ('IP', 'IPv4', 'IPv6'):
                        self.total_bytes += record.header_len
                        self.total_packets += 1
                        key = (record.src_ip, record.dst_ip)
                        self.flow_counts[key] = self.flow_counts.get(key, 0) + 1

    def export_metrics(self):
        now = time.time()
        interval = now - self.last_time
        if interval <= 0:
            interval = 1

        throughput_mbps = (self.total_bytes * 8) / (interval * 1e6)
        throughput_g.set(throughput_mbps)

        flow_count_g.set(len(self.flow_counts))

        avg_packet_size = self.total_bytes / max(self.total_packets, 1)
        avg_packet_size_g.set(avg_packet_size)

        logging.info(f"Throughput: {throughput_mbps:.3f} Mbps | "
                     f"Flows: {len(self.flow_counts)} | "
                     f"Avg pkt size: {avg_packet_size:.1f} bytes")

        self.total_bytes = 0
        self.total_packets = 0
        self.flow_counts.clear()
        self.last_time = now

def main(listen_addr: str, listen_port: int):
    monitor = Monitor()

    start_http_server(SCRAPE_PORT)
    logging.info(f"[Monitor] Prometheus metrics exposed on port {SCRAPE_PORT}")

    receiver = SFlowReceiver(host=listen_addr, port=listen_port, handler=monitor.handle_sflow)
    receiver.start()
    logging.info(f"[Monitor] Listening for sFlow datagrams on {listen_addr}:{listen_port}")

    try:
        while True:
            time.sleep(STATS_INTERVAL)
            monitor.export_metrics()
    except KeyboardInterrupt:
        logging.info("[Monitor] Stopping...")
        receiver.stop()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="sFlow-based monitor VNF")
    parser.add_argument("--addr", default="0.0.0.0", help="IP address to listen for sFlow")
    parser.add_argument("--port", default=6343, type=int, help="UDP port for sFlow datagrams")
    args = parser.parse_args()
    main(args.addr, args.port)
