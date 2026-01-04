#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
import time
import signal
import json
from typing import Optional

# Import the metrics extraction module
import qos_metrics


class QoSTestRunner:
    def __init__(self, script_dir: str):
        self.script_dir = script_dir
        self.stress_test_script = os.path.join(script_dir, "stress_test.py")
        self.apply_vnf_script = os.path.join(script_dir, "apply_and_verify.py")
        self.traffic_logs_dir = os.path.join(script_dir, "traffic_logs")
        self.vnf_process = None
        
    def log(self, msg: str):
        print(f"[QoSTestRunner {time.strftime('%H:%M:%S')}] {msg}")
    
    def check_mininet_running(self) -> bool:
        result = subprocess.run(
            "pgrep -f 'mininet'",
            shell=True,
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    
    def start_vnfs(self, hosts: str) -> Optional[subprocess.Popen]:
        if not os.path.exists(self.apply_vnf_script):
            self.log(f"Error: VNF script not found: {self.apply_vnf_script}")
            return None
        
        self.log(f"Starting VNFs on hosts: {hosts}")
        cmd = f"sudo python3 {self.apply_vnf_script} {hosts}"
        
        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid
            )
            # Give VNFs time to initialize
            time.sleep(5)
            
            # Check if process is still running
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                self.log(f"VNF process exited early. stdout: {stdout.decode()}, stderr: {stderr.decode()}")
                return None
            
            self.log("VNFs started successfully")
            return proc
        except Exception as e:
            self.log(f"Error starting VNFs: {e}")
            return None
    
    def stop_vnfs(self):
        if self.vnf_process:
            self.log("Stopping VNFs...")
            try:
                # Send SIGTERM to the process group
                os.killpg(os.getpgid(self.vnf_process.pid), signal.SIGTERM)
                self.vnf_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Force kill if it doesn't stop
                os.killpg(os.getpgid(self.vnf_process.pid), signal.SIGKILL)
            except Exception as e:
                self.log(f"Error stopping VNFs: {e}")
            
            self.vnf_process = None
            time.sleep(2)
            self.log("VNFs stopped")
    
    def cleanup_tc_rules(self, hosts: str):
        self.log("Cleaning up TC rules...")
        for host in hosts.split(','):
            host = host.strip()
            # Try to get host PID and clean up
            result = subprocess.run(
                f"pgrep -f 'mininet:{host}$'",
                shell=True,
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                pid = result.stdout.strip().split('\n')[0]
                subprocess.run(
                    f"sudo mnexec -a {pid} tc qdisc del dev {host}-eth0 root",
                    shell=True,
                    stderr=subprocess.DEVNULL
                )
                subprocess.run(
                    f"sudo mnexec -a {pid} iptables -t mangle -F",
                    shell=True,
                    stderr=subprocess.DEVNULL
                )
    
    def run_stress_test(self, args: argparse.Namespace) -> bool:
        if not os.path.exists(self.stress_test_script):
            self.log(f"Error: Stress test script not found: {self.stress_test_script}")
            return False
        
        self.log("Running stress test...")
        
        # Build command
        cmd_parts = [
            f"sudo python3 {self.stress_test_script}",
            f"--server {args.server}",
            f"--clients {args.clients}",
            f"--stream-bw {args.stream_bw}",
            f"--phase1-duration {args.duration}",
            "--phase1-only"
        ]
        
        if args.ping:
            cmd_parts.append("--ping")
        
        cmd = " ".join(cmd_parts)
        
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=args.duration + 60
            )
            
            if result.returncode != 0:
                self.log(f"Stress test failed with return code {result.returncode}")
                self.log(f"stdout: {result.stdout}")
                self.log(f"stderr: {result.stderr}")
                return False
            
            self.log("Stress test completed successfully")
            return True
        except subprocess.TimeoutExpired:
            self.log("Stress test timed out")
            return False
        except Exception as e:
            self.log(f"Error running stress test: {e}")
            return False
    
    def extract_and_save_metrics(self, output_file: str) -> bool:
        if not os.path.isdir(self.traffic_logs_dir):
            self.log(f"Error: Traffic logs directory not found: {self.traffic_logs_dir}")
            return False
        
        self.log(f"Extracting metrics from {self.traffic_logs_dir}...")
        
        try:
            metrics = qos_metrics.extract_all_metrics(self.traffic_logs_dir)
            qos_metrics.save_metrics_to_json(metrics, output_file)
            
            # Fix permissions if running as root (drop to user who invoked sudo)
            if os.geteuid() == 0:
                sudo_uid = int(os.environ.get('SUDO_UID', -1))
                sudo_gid = int(os.environ.get('SUDO_GID', -1))
                if sudo_uid != -1 and sudo_gid != -1:
                    os.chown(output_file, sudo_uid, sudo_gid)
                    self.log(f"Changed ownership of {output_file} to user {sudo_uid}:{sudo_gid}")
            
            self.log(f"Metrics saved to {output_file}")
            return True
        except Exception as e:
            self.log(f"Error extracting metrics: {e}")
            return False
    
    def run_test_scenario(self, args: argparse.Namespace, vnf_enabled: bool, output_file: str) -> bool:
        scenario_name = "WITH VNFs" if vnf_enabled else "WITHOUT VNFs"
        self.log("=" * 70)
        self.log(f"RUNNING SCENARIO: {scenario_name}")
        self.log("=" * 70)
        
        # Clean up any existing TC rules
        self.cleanup_tc_rules(args.clients)
        
        # Start VNFs if needed
        if vnf_enabled:
            self.vnf_process = self.start_vnfs(args.clients)
            if not self.vnf_process:
                self.log("Failed to start VNFs, aborting scenario")
                return False
        
        # Run stress test
        success = self.run_stress_test(args)
        
        if success:
            # Extract and save metrics
            success = self.extract_and_save_metrics(output_file)
        
        # Stop VNFs if they were started
        if vnf_enabled:
            self.stop_vnfs()
            self.cleanup_tc_rules(args.clients)
        
        return success


def main():
    parser = argparse.ArgumentParser(
        description="Run QoS tests with VNFs enabled/disabled and extract metrics"
    )
    parser.add_argument(
        "--server",
        default="h1",
        help="Server host (default: h1)"
    )
    parser.add_argument(
        "--clients",
        default="h4",
        help="Comma-separated client hosts (default: h4)"
    )
    parser.add_argument(
        "--stream-bw",
        default="5M",
        help="Streaming UDP bandwidth per flow (default: 5M)"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=30,
        help="Test duration in seconds (default: 30)"
    )
    parser.add_argument(
        "--ping",
        action="store_true",
        help="Include ping tests"
    )
    parser.add_argument(
        "--output-dir",
        default="qos_results",
        help="Output directory for metrics JSON files (default: qos_results)"
    )
    parser.add_argument(
        "--vnf-only",
        action="store_true",
        help="Run only with VNFs enabled"
    )
    parser.add_argument(
        "--no-vnf-only",
        action="store_true",
        help="Run only without VNFs"
    )
    
    args = parser.parse_args()
    
    # Check if running as root
    if os.geteuid() != 0:
        print("Error: This script must be run as root (sudo)")
        sys.exit(1)
    
    # Get script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Create test runner
    runner = QoSTestRunner(script_dir)
    
    # Check if Mininet is running
    if not runner.check_mininet_running():
        runner.log("Error: Mininet is not running. Please start the topology first.")
        sys.exit(1)
    
    # Create output directory with proper permissions
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Fix directory permissions if running as root
    if os.geteuid() == 0:
        sudo_uid = int(os.environ.get('SUDO_UID', -1))
        sudo_gid = int(os.environ.get('SUDO_GID', -1))
        if sudo_uid != -1 and sudo_gid != -1:
            os.chown(args.output_dir, sudo_uid, sudo_gid)
    
    results = {}
    
    # Run scenario without VNFs
    if not args.vnf_only:
        output_file = os.path.join(args.output_dir, "metrics_without_vnf.json")
        success = runner.run_test_scenario(args, vnf_enabled=False, output_file=output_file)
        results['without_vnf'] = success
        
        if success:
            runner.log("Scenario WITHOUT VNFs completed successfully")
        else:
            runner.log("Scenario WITHOUT VNFs failed")
        
        # Wait between tests
        runner.log("Waiting 10 seconds before next scenario...")
        time.sleep(10)
    
    # Run scenario with VNFs
    if not args.no_vnf_only:
        output_file = os.path.join(args.output_dir, "metrics_with_vnf.json")
        success = runner.run_test_scenario(args, vnf_enabled=True, output_file=output_file)
        results['with_vnf'] = success
        
        if success:
            runner.log("Scenario WITH VNFs completed successfully")
        else:
            runner.log("Scenario WITH VNFs failed")
    
    # Summary
    runner.log("=" * 70)
    runner.log("TEST SUMMARY")
    runner.log("=" * 70)
    
    for scenario, success in results.items():
        status = "SUCCESS" if success else "FAILED"
        runner.log(f"{scenario}: {status}")
    
    all_success = all(results.values())
    
    if all_success and len(results) == 2:
        runner.log("\nBoth scenarios completed successfully!")
        runner.log(f"Metrics saved in: {args.output_dir}/")
        runner.log("Run the comparison script to analyze the differences:")
        runner.log(f"  python3 compare_qos_metrics.py {args.output_dir}/metrics_without_vnf.json {args.output_dir}/metrics_with_vnf.json")
    
    sys.exit(0 if all_success else 1)


if __name__ == "__main__":
    main()
