#!/usr/bin/env python3
"""
Simple network metrics collector for QSI-TP3.
Collects interface statistics and exports Prometheus metrics.
"""

import time
import logging
import argparse
import subprocess
from prometheus_client import start_http_server, Gauge

# ============================================================
#                       CONFIGURATION
# ============================================================

SCRAPE_PORT = 9100
POLL_INTERVAL = 1.0  # seconds
LOG_LEVEL = logging.INFO

logging.basicConfig(
    level=LOG_LEVEL,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============================================================
#                   PROMETHEUS METRICS
# ============================================================

throughput_g = Gauge('network_throughput_mbps', 'Network throughput in Mbps')
packet_rate_g = Gauge('network_packet_rate_pps', 'Packet rate in packets/sec')
byte_count_g = Gauge('network_bytes_total', 'Total bytes transferred')
packet_count_g = Gauge('network_packets_total', 'Total packets transferred')

# ============================================================
#                    METRIC COLLECTOR
# ============================================================

class Monitor:
    """Collect network metrics from OVS switches."""
    
    def __init__(self, switches=None):
        self.switches = switches or ['s1', 's2', 's3', 's4']
        # Track previous stats per-port to avoid negative deltas when ports reset/disappear
        self.prev_ports = {}
        self.prev_time = time.time()
        logger.info(f"Monitoring switches: {', '.join(self.switches)}")
    
    def get_ovs_port_stats(self):
        """Return per-port stats across switches: {key: {rx_bytes, tx_bytes, rx_packets, tx_packets}}.
        Key format: '<switch>:<port_index>'
        """
        ports = {}
        for switch in self.switches:
            try:
                cmd = ['sudo', 'ovs-ofctl', 'dump-ports', switch]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                if result.returncode != 0:
                    continue
                current_port = None
                for line in result.stdout.split('\n'):
                    line = line.strip()
                    if line.startswith('port '):
                        # Extract port index
                        try:
                            # e.g., 'port 1: rx pkts=..., bytes=..., ...'
                            prefix, rest = line.split(':', 1)
                            _, idx_str = prefix.split(' ', 1)
                            current_port = f"{switch}:{idx_str.strip()}"
                        except Exception:
                            current_port = None
                        # Initialize dict for port if not exists
                        if current_port and current_port not in ports:
                            ports[current_port] = {'rx_bytes': 0, 'tx_bytes': 0, 'rx_packets': 0, 'tx_packets': 0}
                        # Parse rx metrics on the same line
                        if current_port and 'rx pkts' in line:
                            parts = [p.strip() for p in rest.split(',')]
                            for part in parts:
                                if part.startswith('rx pkts='):
                                    try:
                                        ports[current_port]['rx_packets'] += int(part.split('=')[1])
                                    except Exception:
                                        pass
                                elif part.startswith(' bytes=') or part.startswith('bytes='):
                                    try:
                                        ports[current_port]['rx_bytes'] += int(part.split('=')[1])
                                    except Exception:
                                        pass
                    elif 'tx pkts' in line and current_port:
                        # Parse tx metrics for the last seen port
                        try:
                            _, rest = line.split(':', 1)
                        except Exception:
                            rest = line
                        parts = [p.strip() for p in rest.split(',')]
                        for part in parts:
                            if part.startswith('tx pkts='):
                                try:
                                    ports[current_port]['tx_packets'] += int(part.split('=')[1])
                                except Exception:
                                    pass
                            elif part.startswith(' bytes=') or part.startswith('bytes='):
                                try:
                                    ports[current_port]['tx_bytes'] += int(part.split('=')[1])
                                except Exception:
                                    pass
            except Exception as e:
                logger.warning(f"Error reading stats from {switch}: {e}")
                continue
        return ports
    
    def collect_and_export(self):
        """Collect metrics and export to Prometheus."""
        try:
            ports = self.get_ovs_port_stats()
            current_time = time.time()
            
            # Calculate deltas per-port to avoid negative totals on resets/disappearing ports
            total_bytes = 0
            total_packets = 0
            for key, st in ports.items():
                total_bytes += (st['rx_bytes'] + st['tx_bytes'])
                total_packets += (st['rx_packets'] + st['tx_packets'])

            if self.prev_ports:
                interval = current_time - self.prev_time
                if interval > 0:
                    delta_bytes = 0
                    delta_packets = 0
                    for key, st in ports.items():
                        cur_b = st['rx_bytes'] + st['tx_bytes']
                        cur_p = st['rx_packets'] + st['tx_packets']
                        prev = self.prev_ports.get(key)
                        if prev:
                            prev_b = prev['rx_bytes'] + prev['tx_bytes']
                            prev_p = prev['rx_packets'] + prev['tx_packets']
                            # Only add positive deltas; treat resets/wraps as zero delta
                            if cur_b >= prev_b:
                                delta_bytes += (cur_b - prev_b)
                            if cur_p >= prev_p:
                                delta_packets += (cur_p - prev_p)
                    
                    # Calculate rates
                    throughput_mbps = (delta_bytes * 8) / (interval * 1e6)
                    packet_rate_pps = delta_packets / interval
                    
                    # Update metrics
                    thr = max(0, throughput_mbps)
                    pps = max(0, packet_rate_pps)
                    throughput_g.set(thr)
                    packet_rate_g.set(pps)
                    byte_count_g.set(total_bytes)
                    packet_count_g.set(total_packets)
                    
                    logger.info(
                        f"Throughput: {thr:.3f} Mbps | "
                        f"Packet rate: {pps:.1f} pps | "
                        f"Total: {total_bytes} bytes"
                    )
            else:
                logger.info("First collection - establishing baseline")
            
            # Store current stats for next iteration
            self.prev_ports = ports
            self.prev_time = current_time
        
        except Exception as e:
            logger.error(f"Error collecting metrics: {e}")

# ============================================================
#                         MAIN
# ============================================================

def main(switches=None):
    """Main monitoring loop."""
    monitor = Monitor(switches)
    
    # Start Prometheus HTTP server
    try:
        start_http_server(SCRAPE_PORT)
        logger.info(f"Prometheus endpoint started on http://0.0.0.0:{SCRAPE_PORT}/metrics")
    except Exception as e:
        logger.error(f"Failed to start Prometheus server: {e}")
        return
    
    logger.info(f"Starting metric collection (polling every {POLL_INTERVAL}s)")
    
    try:
        while True:
            monitor.collect_and_export()
            time.sleep(POLL_INTERVAL)
    
    except KeyboardInterrupt:
        logger.info("Shutting down")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Network metrics collector")
    parser.add_argument("--switches", nargs='+', default=['s1', 's2', 's3', 's4'],
                        help="List of OVS switches to monitor")
    args = parser.parse_args()
    
    main(args.switches)
