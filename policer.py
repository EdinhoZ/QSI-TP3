#!/usr/bin/env python3
import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from typing import Dict, Tuple, List

DEFAULT_RATE = "5mbit"
DEFAULT_BURST = "10kb"
LOGFILE = "policer.log"

logging.basicConfig(
    filename=LOGFILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%H:%M:%S"))
logging.getLogger().addHandler(console)

def run_cmd(cmd: str, fail_ok: bool = False) -> Tuple[int, str, str]:
    logging.debug(f"[cmd] {cmd}")
    try:
        proc = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        logging.debug(proc.stdout.strip())
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed: {cmd}\nrc={e.returncode} stdout={e.stdout.strip()} stderr={e.stderr.strip()}")
        if fail_ok:
            return e.returncode, e.stdout or "", e.stderr or ""
        raise

def detect_interface() -> str:
    for interface in os.listdir("/sys/class/net"):
        if interface.endswith("-eth0"):
            logging.info(f"auto-detected interface {interface}")
            return interface
    for interface in os.listdir("/sys/class/net"):
        if interface != "lo":
            logging.info(f"fallback detected interface {interface}")
            return interface
    raise RuntimeError("No network interface found")

def ensure_ingress_qdisc(dev: str):
    # Clean up any existing filters and qdiscs first
    run_cmd(f"tc filter del dev {dev} parent ffff:", fail_ok=True)
    run_cmd(f"tc qdisc del dev {dev} ingress", fail_ok=True)
    # Now add fresh ingress qdisc
    run_cmd(f"tc qdisc add dev {dev} handle ffff: ingress")
    logging.info(f"ingress qdisc added on {dev}")

def add_ingress_police(dev: str, dscp: int, rate: str, burst: str, prio: int = 1):
    tos_val = (dscp << 2) & 0xff
    tos_hex = hex(tos_val)
    police_action = f"police rate {rate} burst {burst} drop flowid :1"
    
    # Use u32 directly (more reliable in Mininet than flower)
    cmd_u32 = (
        f"tc filter add dev {dev} parent ffff: protocol ip prio {prio} "
        f"u32 match ip tos {tos_val} 0xff action {police_action}"
    )
    run_cmd(cmd_u32)
    logging.info(f"Installed ingress police (u32) on {dev} for DSCP={dscp} (tos={tos_hex}) rate={rate} burst={burst}")

def setup_egress_shaper(dev: str, default_rate: str = DEFAULT_RATE, default_burst: str = DEFAULT_BURST):
    run_cmd(f"tc qdisc del dev {dev} root", fail_ok=True)
    run_cmd(f"tc qdisc add dev {dev} root handle 1: htb default 10")
    run_cmd(f"tc class add dev {dev} parent 1: classid 1:10 htb rate {default_rate} burst {default_burst}")
    logging.info(f"egress HTB installed on {dev}, class 1:10 rate={default_rate} burst={default_burst}")

def clear_qdiscs(dev: str):
    run_cmd(f"tc qdisc del dev {dev} ingress", fail_ok=True)
    run_cmd(f"tc qdisc del dev {dev} root", fail_ok=True)
    logging.info(f"cleared tc qdiscs on {dev}")

def parse_class_arg(arg: str) -> Tuple[str, int, str, str]:
    parts = arg.split(":")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("class must be NAME:DSCP:RATE:BURST")
    name, dscp_s, rate, burst = parts
    dscp = int(dscp_s)
    return name, dscp, rate, burst

def install_policers(dev: str, classes: List[Tuple[str, int, str, str]], do_egress_shaping: bool):
    ensure_ingress_qdisc(dev)

    if do_egress_shaping:
        setup_egress_shaper(dev)

    prio = 1
    for name, dscp, rate, burst in classes:
        try:
            add_ingress_police(dev, dscp, rate, burst, prio=prio)
        except Exception as e:
            logging.error(f"failed to install policer for class {name}: {e}")
        prio += 1

def main():
    parser = argparse.ArgumentParser(description="DSCP-aware policer VNF (uses tc)")
    parser.add_argument("--interface", "-i", help="interface to police (default: auto-detect)", default=None)
    parser.add_argument("--class", "-c", dest="classes", action="append",
                        help="class definition NAME:DSCP:RATE:BURST (repeatable)", required=True,
                        type=parse_class_arg)
    parser.add_argument("--shaping", action="store_true",
                        help="also install a simple egress HTB shaper (optional)")
    args = parser.parse_args()

    dev = args.interface or detect_interface()
    classes = args.classes

    logging.info(f"Starting policer on interface {dev}")
    logging.info(f"Classes: {classes}")
    logging.info(f"Enable egress shaping: {args.shaping}")

    def handle_sig(signum, frame):
        logging.info("Received stop signal, cleaning up...")
        clear_qdiscs(dev)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    try:
        install_policers(dev, classes, do_egress_shaping=args.shaping)
        logging.info("Policer installed. Running...")
        while True:
            time.sleep(1)
    except Exception as e:
        logging.exception(f"Unhandled error: {e}")
        clear_qdiscs(dev)
        sys.exit(1)

if __name__ == "__main__":
    main()
