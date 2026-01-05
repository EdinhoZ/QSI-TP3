#!/usr/bin/env python3
import time
import subprocess
import logging
import signal
import sys
from prometheus_client import start_http_server, Gauge, Counter


POLL_INTERVAL = 1.0
PROM_PORT = 9100

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger("monitor")

# Prometheus metrics

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

class MonitorVNF:
    def __init__(self):
        pass

    def run(self, cmd):
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=2
        )

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

    def collect(self):
        self.collect_tc()

def main():
    log.info("Starting Monitor VNF")
    log.info("Monitoring tc queue statistics")

    start_http_server(PROM_PORT)
    log.info(f"Prometheus exporter on :{PROM_PORT}/metrics")

    mon = MonitorVNF()

    def signal_handler(sig, frame):
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while True:
        mon.collect()
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
