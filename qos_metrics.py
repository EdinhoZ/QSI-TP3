#!/usr/bin/env python3
"""
QoS Metrics Extraction Module
Extracts throughput, latency, packet loss, and jitter from iperf and ping logs.
"""
import os
import re
import json
from typing import Dict, List, Optional, Tuple
import statistics


def parse_iperf_udp_receiver(log_content: str) -> Optional[Dict]:
    """
    Parse iperf UDP receiver summary to extract throughput, packet loss, and jitter.
    
    Expected format:
    [  3]  0.0-30.1 sec   18.0 MBytes  5.03 Mbits/sec   0.123 ms  123/12820 (0.96%)
    """
    result = {
        'throughput_mbps': None,
        'packet_loss_pct': None,
        'jitter_ms': None,
        'packets_lost': None,
        'packets_total': None
    }
    
    for line in log_content.splitlines():
        line = line.strip()
        # Look for receiver summary line
        if 'receiver' in line.lower() or (re.search(r'\d+/\d+\s+\([\d.]+%\)', line) and 'sec' in line):
            # Extract throughput
            throughput_match = re.search(r'([\d.]+)\s+([KMG]?)bits/sec', line)
            if throughput_match:
                value = float(throughput_match.group(1))
                unit = throughput_match.group(2)
                if unit == 'K':
                    value /= 1000
                elif unit == 'G':
                    value *= 1000
                result['throughput_mbps'] = value
            
            # Extract jitter
            jitter_match = re.search(r'([\d.]+)\s+ms\s+\d+/\d+', line)
            if jitter_match:
                result['jitter_ms'] = float(jitter_match.group(1))
            
            # Extract packet loss
            loss_match = re.search(r'(\d+)/(\d+)\s+\(([\d.]+)%\)', line)
            if loss_match:
                result['packets_lost'] = int(loss_match.group(1))
                result['packets_total'] = int(loss_match.group(2))
                result['packet_loss_pct'] = float(loss_match.group(3))
    
    return result if result['throughput_mbps'] is not None else None


def parse_iperf_tcp(log_content: str) -> Optional[Dict]:
    """
    Parse iperf TCP summary to extract throughput.
    
    Expected format:
    [SUM]  0.0-30.0 sec   150 MBytes  41.9 Mbits/sec
    or
    [  3]  0.0-30.0 sec   150 MBytes  41.9 Mbits/sec
    """
    result = {
        'throughput_mbps': None
    }
    
    # Try to find the last line with bandwidth info (usually the summary)
    throughput_lines = []
    for line in log_content.splitlines():
        line = line.strip()
        # Look for lines with time interval and bandwidth
        if 'sec' in line and re.search(r'([\d.]+)\s+([KMG]?)bits/sec', line):
            # Prefer SUM lines or sender/receiver lines
            if '[SUM]' in line or 'sender' in line.lower() or 'receiver' in line.lower():
                throughput_match = re.search(r'([\d.]+)\s+([KMG]?)bits/sec', line)
                if throughput_match:
                    value = float(throughput_match.group(1))
                    unit = throughput_match.group(2)
                    if unit == 'K':
                        value /= 1000
                    elif unit == 'G':
                        value *= 1000
                    result['throughput_mbps'] = value
                    return result
            else:
                throughput_lines.append(line)
    
    # If no SUM/sender/receiver line found, use the last line with bandwidth
    if throughput_lines:
        line = throughput_lines[-1]
        throughput_match = re.search(r'([\d.]+)\s+([KMG]?)bits/sec', line)
        if throughput_match:
            value = float(throughput_match.group(1))
            unit = throughput_match.group(2)
            if unit == 'K':
                value /= 1000
            elif unit == 'G':
                value *= 1000
            result['throughput_mbps'] = value
    
    return result if result['throughput_mbps'] is not None else None


def parse_ping_log(log_content: str) -> Optional[Dict]:
    """
    Parse ping output to extract RTT statistics.
    
    Expected format:
    rtt min/avg/max/mdev = 0.123/0.456/0.789/0.012 ms
    """
    result = {
        'rtt_min_ms': None,
        'rtt_avg_ms': None,
        'rtt_max_ms': None,
        'rtt_mdev_ms': None,
        'packets_transmitted': None,
        'packets_received': None,
        'packet_loss_pct': None
    }
    
    for line in log_content.splitlines():
        line = line.strip()
        
        # Extract RTT statistics
        rtt_match = re.search(r'rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms', line)
        if rtt_match:
            result['rtt_min_ms'] = float(rtt_match.group(1))
            result['rtt_avg_ms'] = float(rtt_match.group(2))
            result['rtt_max_ms'] = float(rtt_match.group(3))
            result['rtt_mdev_ms'] = float(rtt_match.group(4))
        
        # Extract packet statistics
        # Format: 100 packets transmitted, 98 received, 2% packet loss
        packet_match = re.search(r'(\d+)\s+packets transmitted,\s+(\d+)\s+received,\s+([\d.]+)%\s+packet loss', line)
        if packet_match:
            result['packets_transmitted'] = int(packet_match.group(1))
            result['packets_received'] = int(packet_match.group(2))
            result['packet_loss_pct'] = float(packet_match.group(3))
    
    return result if result['rtt_avg_ms'] is not None else None


def extract_metrics_from_log_file(log_file: str, log_type: str) -> Optional[Dict]:
    """
    Extract metrics from a single log file.
    
    Args:
        log_file: Path to the log file
        log_type: Type of log ('udp', 'tcp', or 'ping')
    
    Returns:
        Dictionary with extracted metrics or None if parsing failed
    """
    if not os.path.exists(log_file):
        return None
    
    try:
        with open(log_file, 'r') as f:
            content = f.read()
        
        if log_type == 'udp':
            return parse_iperf_udp_receiver(content)
        elif log_type == 'tcp':
            return parse_iperf_tcp(content)
        elif log_type == 'ping':
            return parse_ping_log(content)
        else:
            return None
    except Exception as e:
        print(f"Error parsing {log_file}: {e}")
        return None


def aggregate_flow_metrics(log_dir: str, flow_prefix: str, log_type: str) -> Optional[Dict]:
    """
    Aggregate metrics from multiple flows with the same prefix.
    
    Args:
        log_dir: Directory containing log files
        flow_prefix: Prefix of log files to aggregate (e.g., 'stream_')
        log_type: Type of logs ('udp', 'tcp', or 'ping')
    
    Returns:
        Aggregated metrics dictionary
    """
    if not os.path.isdir(log_dir):
        return None
    
    metrics_list = []
    for filename in os.listdir(log_dir):
        if filename.startswith(flow_prefix) and filename.endswith('.log'):
            log_path = os.path.join(log_dir, filename)
            metrics = extract_metrics_from_log_file(log_path, log_type)
            if metrics:
                metrics_list.append(metrics)
    
    if not metrics_list:
        return None
    
    # Aggregate metrics
    aggregated = {}
    
    # For throughput, sum all flows
    if log_type in ['udp', 'tcp']:
        throughputs = [m['throughput_mbps'] for m in metrics_list if m.get('throughput_mbps') is not None]
        if throughputs:
            aggregated['throughput_mbps'] = sum(throughputs)
            aggregated['throughput_avg_per_flow'] = statistics.mean(throughputs)
            aggregated['throughput_min'] = min(throughputs)
            aggregated['throughput_max'] = max(throughputs)
    
    # For UDP-specific metrics, average them
    if log_type == 'udp':
        jitters = [m['jitter_ms'] for m in metrics_list if m.get('jitter_ms') is not None]
        losses = [m['packet_loss_pct'] for m in metrics_list if m.get('packet_loss_pct') is not None]
        
        if jitters:
            aggregated['jitter_avg_ms'] = statistics.mean(jitters)
            aggregated['jitter_min_ms'] = min(jitters)
            aggregated['jitter_max_ms'] = max(jitters)
        
        if losses:
            aggregated['packet_loss_avg_pct'] = statistics.mean(losses)
            aggregated['packet_loss_min_pct'] = min(losses)
            aggregated['packet_loss_max_pct'] = max(losses)
        
        # Sum total packets
        total_lost = sum(m.get('packets_lost', 0) for m in metrics_list)
        total_packets = sum(m.get('packets_total', 0) for m in metrics_list)
        if total_packets > 0:
            aggregated['total_packet_loss_pct'] = (total_lost / total_packets) * 100
    
    # For ping metrics, average RTT values
    if log_type == 'ping':
        rtt_avgs = [m['rtt_avg_ms'] for m in metrics_list if m.get('rtt_avg_ms') is not None]
        rtt_mins = [m['rtt_min_ms'] for m in metrics_list if m.get('rtt_min_ms') is not None]
        rtt_maxs = [m['rtt_max_ms'] for m in metrics_list if m.get('rtt_max_ms') is not None]
        losses = [m['packet_loss_pct'] for m in metrics_list if m.get('packet_loss_pct') is not None]
        
        if rtt_avgs:
            aggregated['rtt_avg_ms'] = statistics.mean(rtt_avgs)
        if rtt_mins:
            aggregated['rtt_min_ms'] = min(rtt_mins)
        if rtt_maxs:
            aggregated['rtt_max_ms'] = max(rtt_maxs)
        if losses:
            aggregated['packet_loss_avg_pct'] = statistics.mean(losses)
    
    aggregated['num_flows'] = len(metrics_list)
    return aggregated


def extract_all_metrics(log_dir: str) -> Dict:
    """
    Extract all QoS metrics from a stress test run.
    
    Returns a structured dictionary with metrics for different traffic types:
    - streaming (UDP)
    - http (TCP)
    - bulk (TCP)
    - ping (ICMP)
    - variable_load (UDP)
    - failure scenarios
    """
    metrics = {
        'streaming': {},
        'http': {},
        'bulk': {},
        'ping': {},
        'variable_load': {},
        'failure': {}
    }
    
    # Streaming (UDP ports 5004, 5005)
    streaming = aggregate_flow_metrics(log_dir, 'stream_', 'udp')
    if streaming:
        metrics['streaming'] = streaming
    
    # HTTP (TCP port 80)
    http = aggregate_flow_metrics(log_dir, 'http_', 'tcp')
    if http:
        metrics['http'] = http
    
    # Bulk (TCP port 9000)
    bulk = aggregate_flow_metrics(log_dir, 'bulk_', 'tcp')
    if bulk:
        metrics['bulk'] = bulk
    
    # Ping
    ping = aggregate_flow_metrics(log_dir, 'ping_', 'ping')
    if ping:
        metrics['ping'] = ping
    
    # Variable load
    var_load = aggregate_flow_metrics(log_dir, 'var_load_', 'udp')
    if var_load:
        metrics['variable_load'] = var_load
    
    # Failure scenarios
    failure_udp = aggregate_flow_metrics(log_dir, 'failure_udp_', 'udp')
    failure_tcp = aggregate_flow_metrics(log_dir, 'failure_tcp_', 'tcp')
    if failure_udp or failure_tcp:
        metrics['failure'] = {
            'udp': failure_udp or {},
            'tcp': failure_tcp or {}
        }
    
    return metrics


def save_metrics_to_json(metrics: Dict, output_file: str):
    """Save metrics dictionary to a JSON file."""
    os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else '.', exist_ok=True)
    with open(output_file, 'w') as f:
        json.dump(metrics, f, indent=2)
    print(f"Metrics saved to {output_file}")


def load_metrics_from_json(json_file: str) -> Dict:
    """Load metrics dictionary from a JSON file."""
    with open(json_file, 'r') as f:
        return json.load(f)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python3 qos_metrics.py <log_directory> [output_json]")
        print("Example: python3 qos_metrics.py traffic_logs metrics.json")
        sys.exit(1)
    
    log_dir = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else "qos_metrics.json"
    
    print(f"Extracting metrics from {log_dir}...")
    metrics = extract_all_metrics(log_dir)
    
    save_metrics_to_json(metrics, output_file)
    
    # Print summary
    print("\n=== Metrics Summary ===")
    print(json.dumps(metrics, indent=2))
