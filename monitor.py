#!/usr/bin/env python3
import time
import logging
import argparse
import socket
import struct
from prometheus_client import Gauge, start_http_server

SCRAPE_PORT = 9100
STATS_INTERVAL = 1
LOGFILE = f"vnf_logs/monitor.log{time.strftime('%Y%m%d')}"

throughput_g = Gauge("vnf_throughput_mbps", "Total throughput (Mbps)")
flow_count_g = Gauge("vnf_flow_count", "Number of active flows")
avg_packet_size_g = Gauge("vnf_avg_packet_size_bytes", "Average packet size in bytes")

logging.basicConfig(filename=LOGFILE, level=logging.INFO,
                    format='[%(asctime)s] %(message)s', datefmt='%H:%M:%S')


# ============================================================
#                 Minimal sFlow PARSER
# ============================================================

class SFlowParser:
    """
    Minimal sFlow v5 parser:
      - extracts samples
      - extracts flow records
      - extracts src/dst IP and header length
    Enough for your monitoring logic.
    """

    FLOW_SAMPLE = 1
    FLOW_SAMPLE_EXPANDED = 3
    HEADER_PROTOCOL_ETHERNET = 1

    def parse(self, data: bytes):
        """
        Returns a list of parsed flow samples.
        """
        samples = []
        offset = 0

        # sFlow v5 header is fixed: version, agent, sub-agent, sequence, uptime, num samples
        if len(data) < 28:
            return samples

        version, = struct.unpack_from("!I", data, offset)
        offset += 4
        if version != 5:
            return samples

        # skip agent address type + address + sub-agent ID + sequence + uptime
        offset += 4 + 4 + 4 + 4 + 4

        num_samples, = struct.unpack_from("!I", data, offset)
        offset += 4

        for _ in range(num_samples):
            if offset + 8 > len(data):
                break

            sample_type, sample_len = struct.unpack_from("!II", data, offset)
            offset += 8

            sample_end = offset + sample_len
            if sample_end > len(data):
                break

            # Tag is high 20 bits
            format_type = sample_type & 0x0FFF_FFFF

            if format_type in (self.FLOW_SAMPLE, self.FLOW_SAMPLE_EXPANDED):
                sample = self._parse_flow_sample(data[offset:sample_end])
                if sample:
                    samples.append(sample)

            offset = sample_end

        return samples

    def _parse_flow_sample(self, buf: bytes):
        """
        Extracts flow records from a flow sample.
        """
        off = 0
        if len(buf) < 16:
            return None

        # skip ingress/egress, sampling rate, etc.
        off += 16

        # number of flow records
        if off + 4 > len(buf):
            return None
        num_records, = struct.unpack_from("!I", buf, off)
        off += 4

        records = []

        for _ in range(num_records):
            if off + 8 > len(buf):
                break

            rec_type, rec_len = struct.unpack_from("!II", buf, off)
            off += 8

            rec_end = off + rec_len
            if rec_end > len(buf):
                break

            # Only handle flow sample "raw header" records (type 1)
            if (rec_type & 0x0FFF_FFFF) == 1:
                rec = self._parse_header_record(buf[off:rec_end])
                if rec:
                    records.append(rec)

            off = rec_end

        return records

    def _parse_header_record(self, buf: bytes):
        """
        Extract IP header information from HEADER protocol record.
        """
        off = 0
        if len(buf) < 16:
            return None

        proto, header_len, frame_len, stripped = struct.unpack_from("!IIII", buf, off)
        off += 16

        if proto != self.HEADER_PROTOCOL_ETHERNET:
            return None

        # skip next fields to reach Ethernet + IP header
        if off + header_len > len(buf):
            return None

        eth = buf[off:off + header_len]

        # Parse Ethernet + IP
        # Ethernet header = 14 bytes
        if len(eth) < 34:
            return None

        eth_type = struct.unpack_from("!H", eth, 12)[0]
        if eth_type != 0x0800:  # IPv4
            return None

        # IPv4 header: bytes 26-29 src, 30-33 dst
        src = socket.inet_ntoa(eth[26:30])
        dst = socket.inet_ntoa(eth[30:34])

        return {
            "header_len": frame_len,
            "src_ip": src,
            "dst_ip": dst
        }


# ============================================================
#                       MONITOR LOGIC
# ============================================================

class Monitor:
    def __init__(self):
        self.total_bytes = 0
        self.total_packets = 0
        self.flow_counts = {}
        self.last_time = time.time()
        self.parser = SFlowParser()

    def handle(self, data: bytes):
        samples = self.parser.parse(data)
        for records in samples:
            for rec in records:
                self.total_bytes += rec["header_len"]
                self.total_packets += 1
                key = (rec["src_ip"], rec["dst_ip"])
                self.flow_counts[key] = self.flow_counts.get(key, 0) + 1

    def export_metrics(self):
        now = time.time()
        interval = now - self.last_time
        if interval <= 0:
            interval = 1

        throughput_mbps = (self.total_bytes * 8) / (interval * 1e6)
        throughput_g.set(throughput_mbps)
        flow_count_g.set(len(self.flow_counts))
        avg_size = self.total_bytes / max(self.total_packets, 1)
        avg_packet_size_g.set(avg_size)

        logging.info(f"Throughput: {throughput_mbps:.3f} Mbps | "
                     f"Flows: {len(self.flow_counts)} | "
                     f"Avg pkt: {avg_size:.1f} bytes")

        self.total_bytes = 0
        self.total_packets = 0
        self.flow_counts.clear()
        self.last_time = now


# ============================================================
#                          MAIN
# ============================================================

def main(addr: str, port: int):
    monitor = Monitor()

    start_http_server(SCRAPE_PORT)
    logging.info(f"[Monitor] Prometheus exposed on {SCRAPE_PORT}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((addr, port))

    logging.info(f"[Monitor] Listening for sFlow datagrams on {addr}:{port}")

    sock.setblocking(False)

    try:
        while True:
            try:
                data, src = sock.recvfrom(65535)
                monitor.handle(data)
            except BlockingIOError:
                pass

            time.sleep(STATS_INTERVAL)
            monitor.export_metrics()

    except KeyboardInterrupt:
        logging.info("[Monitor] Shutting down...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="sFlow-based monitor VNF")
    parser.add_argument("--addr", default="0.0.0.0", help="IP address to listen for sFlow")
    parser.add_argument("--port", default=6343, type=int, help="UDP port for sFlow datagrams")
    args = parser.parse_args()
    main(args.addr, args.port)
