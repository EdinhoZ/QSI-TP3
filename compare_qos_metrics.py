#!/usr/bin/env python3
"""
QoS Metrics Comparison Script
Compares metrics from VNF-disabled vs VNF-enabled scenarios and calculates improvement/degradation.
"""
import argparse
import json
import sys
from typing import Dict, Optional, Tuple
from datetime import datetime


class MetricsComparator:
    def __init__(self, baseline_file: str, vnf_file: str):
        self.baseline_file = baseline_file
        self.vnf_file = vnf_file
        self.baseline_metrics = None
        self.vnf_metrics = None
        
    def load_metrics(self) -> bool:
        """Load metrics from both JSON files."""
        try:
            with open(self.baseline_file, 'r') as f:
                self.baseline_metrics = json.load(f)
            print(f"Loaded baseline metrics from {self.baseline_file}")
        except Exception as e:
            print(f"Error loading baseline metrics: {e}")
            return False
        
        try:
            with open(self.vnf_file, 'r') as f:
                self.vnf_metrics = json.load(f)
            print(f"Loaded VNF metrics from {self.vnf_file}")
        except Exception as e:
            print(f"Error loading VNF metrics: {e}")
            return False
        
        return True
    
    def calculate_change(self, baseline: Optional[float], vnf: Optional[float], 
                        metric_name: str, lower_is_better: bool = False) -> Dict:
        """
        Calculate the change between baseline and VNF metrics.
        
        Args:
            baseline: Baseline value (without VNF)
            vnf: VNF value (with VNF)
            metric_name: Name of the metric
            lower_is_better: If True, lower values are improvements
        
        Returns:
            Dictionary with change information
        """
        if baseline is None or vnf is None:
            return {
                'baseline': baseline,
                'vnf': vnf,
                'change': None,
                'change_pct': None,
                'improvement': None,
                'status': 'N/A'
            }
        
        # Avoid division by zero
        if baseline == 0:
            if vnf == 0:
                change_pct = 0.0
            else:
                change_pct = float('inf') if vnf > 0 else float('-inf')
        else:
            change_pct = ((vnf - baseline) / baseline) * 100
        
        change = vnf - baseline
        
        # Determine if it's an improvement
        if lower_is_better:
            improvement = change < 0  # Lower is better
        else:
            improvement = change > 0  # Higher is better
        
        # Determine status
        if abs(change_pct) < 1:
            status = 'UNCHANGED'
        elif improvement:
            status = 'IMPROVED'
        else:
            status = 'DEGRADED'
        
        return {
            'baseline': baseline,
            'vnf': vnf,
            'change': change,
            'change_pct': change_pct,
            'improvement': improvement,
            'status': status
        }
    
    def compare_traffic_class(self, class_name: str, baseline_data: Dict, 
                             vnf_data: Dict) -> Dict:
        """Compare metrics for a specific traffic class."""
        comparison = {}
        
        # Throughput (higher is better)
        if 'throughput_mbps' in baseline_data or 'throughput_mbps' in vnf_data:
            comparison['throughput'] = self.calculate_change(
                baseline_data.get('throughput_mbps'),
                vnf_data.get('throughput_mbps'),
                'throughput',
                lower_is_better=False
            )
        
        # Packet loss (lower is better)
        for loss_key in ['packet_loss_avg_pct', 'packet_loss_pct', 'total_packet_loss_pct']:
            if loss_key in baseline_data or loss_key in vnf_data:
                comparison['packet_loss'] = self.calculate_change(
                    baseline_data.get(loss_key),
                    vnf_data.get(loss_key),
                    'packet_loss',
                    lower_is_better=True
                )
                break
        
        # Jitter (lower is better)
        for jitter_key in ['jitter_avg_ms', 'jitter_ms']:
            if jitter_key in baseline_data or jitter_key in vnf_data:
                comparison['jitter'] = self.calculate_change(
                    baseline_data.get(jitter_key),
                    vnf_data.get(jitter_key),
                    'jitter',
                    lower_is_better=True
                )
                break
        
        # RTT/Latency (lower is better)
        if 'rtt_avg_ms' in baseline_data or 'rtt_avg_ms' in vnf_data:
            comparison['latency'] = self.calculate_change(
                baseline_data.get('rtt_avg_ms'),
                vnf_data.get('rtt_avg_ms'),
                'latency',
                lower_is_better=True
            )
        
        return comparison
    
    def compare_all_metrics(self) -> Dict:
        """Compare all metrics between baseline and VNF scenarios."""
        comparison = {}
        
        # Compare each traffic class
        traffic_classes = set(list(self.baseline_metrics.keys()) + list(self.vnf_metrics.keys()))
        
        for class_name in traffic_classes:
            if class_name == 'failure':
                # Handle failure scenarios separately (they have nested structure)
                comparison['failure'] = {}
                baseline_failure = self.baseline_metrics.get('failure', {})
                vnf_failure = self.vnf_metrics.get('failure', {})
                
                for proto in ['udp', 'tcp']:
                    if proto in baseline_failure or proto in vnf_failure:
                        comparison['failure'][proto] = self.compare_traffic_class(
                            f'failure_{proto}',
                            baseline_failure.get(proto, {}),
                            vnf_failure.get(proto, {})
                        )
            else:
                baseline_data = self.baseline_metrics.get(class_name, {})
                vnf_data = self.vnf_metrics.get(class_name, {})
                
                if baseline_data or vnf_data:
                    comparison[class_name] = self.compare_traffic_class(
                        class_name,
                        baseline_data,
                        vnf_data
                    )
        
        return comparison
    
    def print_comparison(self, comparison: Dict):
        """Print comparison results in a readable format."""
        print("\n" + "=" * 80)
        print("QoS METRICS COMPARISON: VNF Impact Analysis")
        print("=" * 80)
        print(f"Baseline (without VNF): {self.baseline_file}")
        print(f"With VNF: {self.vnf_file}")
        print("=" * 80)
        
        def print_metric(name: str, data: Dict, indent: int = 0):
            """Print a single metric comparison."""
            prefix = "  " * indent
            
            if data['status'] == 'N/A':
                print(f"{prefix}{name}: N/A (missing data)")
                return
            
            status_symbol = {
                'IMPROVED': '✓',
                'DEGRADED': '✗',
                'UNCHANGED': '='
            }.get(data['status'], '?')
            
            # Format change percentage
            if data['change_pct'] is not None and abs(data['change_pct']) != float('inf'):
                change_str = f"{data['change_pct']:+.2f}%"
            else:
                change_str = "N/A"
            
            # Format values with appropriate precision
            if data['baseline'] is not None:
                baseline_str = f"{data['baseline']:.3f}"
            else:
                baseline_str = "N/A"
            
            if data['vnf'] is not None:
                vnf_str = f"{data['vnf']:.3f}"
            else:
                vnf_str = "N/A"
            
            print(f"{prefix}{status_symbol} {name:20s}: {baseline_str:>10s} → {vnf_str:>10s} ({change_str:>10s}) [{data['status']}]")
        
        for class_name, class_data in comparison.items():
            if class_name == 'failure':
                print(f"\n--- Failure Scenarios ---")
                for proto, proto_data in class_data.items():
                    print(f"\n  {proto.upper()}:")
                    for metric_name, metric_data in proto_data.items():
                        print_metric(metric_name.capitalize(), metric_data, indent=2)
            else:
                print(f"\n--- {class_name.replace('_', ' ').title()} ---")
                for metric_name, metric_data in class_data.items():
                    print_metric(metric_name.capitalize(), metric_data, indent=1)
        
        # Summary
        print("\n" + "=" * 80)
        print("SUMMARY")
        print("=" * 80)
        
        total_metrics = 0
        improved = 0
        degraded = 0
        unchanged = 0
        
        def count_metrics(data: Dict):
            nonlocal total_metrics, improved, degraded, unchanged
            for key, value in data.items():
                if isinstance(value, dict):
                    if 'status' in value:
                        total_metrics += 1
                        if value['status'] == 'IMPROVED':
                            improved += 1
                        elif value['status'] == 'DEGRADED':
                            degraded += 1
                        elif value['status'] == 'UNCHANGED':
                            unchanged += 1
                    else:
                        count_metrics(value)
        
        count_metrics(comparison)
        
        print(f"Total metrics compared: {total_metrics}")
        print(f"  Improved:   {improved:3d} ({improved/total_metrics*100:.1f}%)" if total_metrics > 0 else "  Improved:   0")
        print(f"  Degraded:   {degraded:3d} ({degraded/total_metrics*100:.1f}%)" if total_metrics > 0 else "  Degraded:   0")
        print(f"  Unchanged:  {unchanged:3d} ({unchanged/total_metrics*100:.1f}%)" if total_metrics > 0 else "  Unchanged:  0")
        
        if total_metrics > 0:
            overall_score = (improved - degraded) / total_metrics * 100
            print(f"\nOverall VNF Impact Score: {overall_score:+.1f}%")
            
            if overall_score > 10:
                print("Assessment: VNFs provide SIGNIFICANT IMPROVEMENT")
            elif overall_score > 0:
                print("Assessment: VNFs provide MODERATE IMPROVEMENT")
            elif overall_score > -10:
                print("Assessment: VNFs have MINIMAL IMPACT")
            else:
                print("Assessment: VNFs cause DEGRADATION")
    
    def save_comparison(self, comparison: Dict, output_file: str):
        """Save comparison results to a JSON file."""
        output = {
            'timestamp': datetime.now().isoformat(),
            'baseline_file': self.baseline_file,
            'vnf_file': self.vnf_file,
            'comparison': comparison
        }
        
        with open(output_file, 'w') as f:
            json.dump(output, f, indent=2)
        
        print(f"\nComparison results saved to {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Compare QoS metrics from VNF-disabled vs VNF-enabled scenarios"
    )
    parser.add_argument(
        "baseline_file",
        help="JSON file with baseline metrics (without VNF)"
    )
    parser.add_argument(
        "vnf_file",
        help="JSON file with VNF metrics (with VNF enabled)"
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output file for comparison results (JSON format)"
    )
    
    args = parser.parse_args()
    
    # Create comparator
    comparator = MetricsComparator(args.baseline_file, args.vnf_file)
    
    # Load metrics
    if not comparator.load_metrics():
        print("Failed to load metrics files")
        sys.exit(1)
    
    # Compare metrics
    comparison = comparator.compare_all_metrics()
    
    # Print comparison
    comparator.print_comparison(comparison)
    
    # Save comparison if output file specified
    if args.output:
        comparator.save_comparison(comparison, args.output)


if __name__ == "__main__":
    main()
