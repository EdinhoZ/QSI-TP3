#!/usr/bin/env python3
import time
import argparse
import subprocess
import logging
from collections import defaultdict
from prometheus_client import start_http_server, Gauge, Counter

# =========================
# CONFIG
# =========================
POLL_INTERVAL = 1.0
PROM_PORT = 9100

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("monitor")

# =========================
# PROMETHEUS METRICS
# =========================

link_bytes = Counter(
    "link_bytes_total",
    "Total bytes per link",
    ["switch", "port", "direction"]
)

link_packets = Counter(
    "link_packets_total",
    "Total packets per link",
    ["switch", "port", "direction"]
)

link_throughput = Gauge(
    "link_throughput_bps",
    "Link throughput (bps)",
    ["switch", "port"]
)

link_packet_rate = Gauge(
    "link_packet_rate_pps",
    "Packet rate (pps)",
    ["switch", "port"]
)

qos_bytes = Counter(
    "qos_bytes_total",
    "Bytes per DSCP class",
    ["switch", "port", "dscp"]
)

qos_packets = Counter(
    "qos_packets_total",
    "Packets per DSCP class",
    ["switch", "port", "dscp"]
)

queue_backlog = Gauge(
    "queue_backlog_bytes",
    "Queue backlog in bytes",
    ["dev"]
)

queue_drops = Counter(
    "queue_drops_total",
    "Queue drops",
    ["dev"]
)

# =========================
# MONITOR IMPLEMENTATION
# =========================

class MonitorVNF:
    def __init__(self, switches):
        self.switches = switches
        self.prev = {}
        self.prev_time = time.time()

    def run(self, cmd):
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=2
        )

    # -------- OVS PORT STATS --------
    def collect_ports(self):
        stats = {}

        for sw in self.switches:
            res = self.run(["ovs-ofctl", "dump-ports", sw])
            if res.returncode != 0:
                continue

            port = None
            for line in res.stdout.splitlines():
                line = line.strip()

                if line.startswith("port"):
                    port = line.split()[1].strip(":")
                    stats[(sw, port)] = {
                        "rx_bytes": 0,
                        "tx_bytes": 0,
                        "rx_pkts": 0,
                        "tx_pkts": 0
                    }

                elif "rx pkts" in line and port:
                    parts = line.split(",")
                    for p in parts:
                        if "pkts=" in p:
                            stats[(sw, port)]["rx_pkts"] += int(p.split("=")[1])
                        if "bytes=" in p:
                            stats[(sw, port)]["rx_bytes"] += int(p.split("=")[1])

                elif "tx pkts" in line and port:
                    parts = line.split(",")
                    for p in parts:
                        if "pkts=" in p:
                            stats[(sw, port)]["tx_pkts"] += int(p.split("=")[1])
                        if "bytes=" in p:
                            stats[(sw, port)]["tx_bytes"] += int(p.split("=")[1])

        return stats

    # -------- TC QUEUE STATS --------
    def collect_tc(self):
        res = self.run(["tc", "-s", "qdisc", "show"])
        if res.returncode != 0:
            return

        for line in res.stdout.splitlines():
            if "dev" in line and "backlog" in line:
                parts = line.split()
                dev = parts[parts.index("dev") + 1]
                for p in parts:
                    if p.startswith("backlog"):
                        backlog = int(p.split("b")[0].split()[-1])
                        queue_backlog.labels(dev=dev).set(backlog)
                    if p.startswith("drops"):
                        drops = int(p.split()[1])
                        queue_drops.labels(dev=dev).inc(drops)

    # -------- MAIN LOOP --------
    def collect(self):
        now = time.time()
        cur = self.collect_ports()

        if self.prev:
            dt = now - self.prev_time
            if dt <= 0:
                return

            for key, st in cur.items():
                if key not in self.prev:
                    continue

                sw, port = key
                prev = self.prev[key]

                d_rx = st["rx_bytes"] - prev["rx_bytes"]
                d_tx = st["tx_bytes"] - prev["tx_bytes"]
                d_pkts = (
                    (st["rx_pkts"] - prev["rx_pkts"]) +
                    (st["tx_pkts"] - prev["tx_pkts"])
                )

                if d_rx >= 0:
                    link_bytes.labels(sw, port, "rx").inc(d_rx)
                if d_tx >= 0:
                    link_bytes.labels(sw, port, "tx").inc(d_tx)

                link_packets.labels(sw, port, "rx").inc(
                    max(0, st["rx_pkts"] - prev["rx_pkts"])
                )
                link_packets.labels(sw, port, "tx").inc(
                    max(0, st["tx_pkts"] - prev["tx_pkts"])
                )

                link_throughput.labels(sw, port).set(
                    ((d_rx + d_tx) * 8) / dt
                )
                link_packet_rate.labels(sw, port).set(
                    max(0, d_pkts / dt)
                )

        self.prev = cur
        self.prev_time = now

        self.collect_tc()

# =========================
# MAIN
# =========================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--switches", nargs="+",
        default=["s1", "s2", "s3", "s4"]
    )
    args = parser.parse_args()

    log.info("Starting Monitor VNF")
    log.info(f"Switches: {args.switches}")

    start_http_server(PROM_PORT)
    log.info(f"Exporter on :{PROM_PORT}/metrics")

    mon = MonitorVNF(args.switches)

    while True:
        mon.collect()
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
