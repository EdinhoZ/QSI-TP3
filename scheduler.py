#!/usr/bin/env python3
"""
scheduler.py — Lightweight priority scheduler VNF

Installs a prio qdisc and maps DSCP values to priority bands.

Usage example:
  python3 scheduler.py -i vnf1-eth0 \
    -c voip:46:0 \
    -c video:34:1 \
    -c bulk:0:2

Each --class argument is NAME:DSCP:BAND, where BAND is 0 (highest) .. N-1 (lowest).
Default creates 3 bands (0..2). The qdisc will be `prio` with that many bands.
"""
import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from typing import List, Tuple

LOGFILE = "scheduler.log"
DEFAULT_BANDS = 3

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


def run(cmd: str, fail_ok: bool = False):
    logging.debug(f"[cmd] {cmd}")
    try:
        proc = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        if proc.stdout:
            logging.debug(proc.stdout.strip())
        if proc.stderr:
            logging.debug(proc.stderr.strip())
        return proc.returncode
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed: {cmd}\nrc={e.returncode}\nstdout={e.stdout}\nstderr={e.stderr}")
        if fail_ok:
            return e.returncode
        raise


def detect_interface() -> str:
    """Auto-detect first mininet-style -eth0 interface, else first non-loopback."""
    for iface in os.listdir("/sys/class/net"):
        if iface.endswith("-eth0"):
            logging.info(f"auto-detected interface {iface}")
            return iface
    for iface in os.listdir("/sys/class/net"):
        if iface != "lo":
            logging.info(f"fallback detected interface {iface}")
            return iface
    raise RuntimeError("No network interface found")


def clear_qdisc(dev: str):
    run(f"tc qdisc del dev {dev} root", fail_ok=True)
    logging.info(f"Cleared root qdisc on {dev}")


def setup_prio(dev: str, bands: int = DEFAULT_BANDS):
    """
    Install prio qdisc with `bands` bands.
    The Linux prio qdisc defaults to 3 bands; specifying bands>3 will set priomap accordingly.
    """
    # Delete existing root qdisc if any
    run(f"tc qdisc del dev {dev} root", fail_ok=True)
    # Create prio qdisc. If bands > 3 we can still use prio (it supports up to 16 bands),
    # but kernel maps priorities (0..15) to bands via priomap. For simplicity, we leave priomap default.
    run(f"tc qdisc add dev {dev} root handle 1: prio bands {bands}")
    logging.info(f"Installed prio qdisc on {dev} with {bands} bands")


def add_dscp_to_band(dev: str, dscp: int, band: int, prio: int = 10):
    """
    Map traffic with DSCP -> BAND using flower when available, else u32.
    For prio qdisc, flowid 1:<band+1> addresses band numbering starting at 1.
    """
    if band < 0:
        raise ValueError("band must be >= 0")
    flowid = f"1:{band + 1}"  # bands are referenced as 1:1,1:2,...

    # DSCP resides in IP TOS field as DSCP<<2
    tos_val = (dscp << 2) & 0xff
    tos_hex = hex(tos_val)

    # Try flower filter (preferred)
    cmd_flower = (
        f"tc filter add dev {dev} protocol ip parent 1: prio {prio} "
        f"flower ip_tos {tos_hex} action skbedit priority 0 flowid {flowid}"
    )

    try:
        run(cmd_flower)
        logging.info(f"Installed flower filter on {dev}: DSCP={dscp} -> band={band} (flowid={flowid})")
        return
    except Exception:
        logging.debug("flower not available or failed; falling back to u32")

    # Fallback: u32 match on ip tos and direct to flowid
    cmd_u32 = (
        f"tc filter add dev {dev} parent 1: protocol ip prio {prio} "
        f"u32 match ip tos {tos_val} 0xff flowid {flowid}"
    )
    run(cmd_u32)
    logging.info(f"Installed u32 filter on {dev}: DSCP={dscp} -> band={band} (flowid={flowid})")


def parse_class_arg(s: str) -> Tuple[str, int, int]:
    parts = s.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("class must be NAME:DSCP:BAND")
    name = parts[0]
    dscp = int(parts[1])
    band = int(parts[2])
    return name, dscp, band


def main():
    p = argparse.ArgumentParser(description="Lightweight priority scheduler VNF (prio qdisc)")
    p.add_argument("--interface", "-i", default=None, help="Interface to attach scheduler to")
    p.add_argument("--bands", "-b", type=int, default=DEFAULT_BANDS, help="Number of priority bands (default 3)")
    p.add_argument("--class", "-c", dest="classes", action="append", type=parse_class_arg, required=True,
                   help="CLASS mapping NAME:DSCP:BAND (repeatable). BAND 0 = highest priority")
    args = p.parse_args()

    dev = args.interface or detect_interface()
    bands = args.bands
    classes: List[Tuple[str, int, int]] = args.classes

    logging.info(f"Starting scheduler on {dev} with {bands} bands")
    logging.info(f"Class mappings: {classes}")

    def cleanup_and_exit(*_):
        logging.info("Stopping scheduler and cleaning qdisc...")
        clear_qdisc(dev)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup_and_exit)
    signal.signal(signal.SIGTERM, cleanup_and_exit)

    # Install qdisc and filters
    setup_prio(dev, bands=bands)

    # Install class filters (prio increments)
    prio_counter = 1
    for name, dscp, band in classes:
        if band >= bands:
            logging.warning(f"class {name}: band {band} >= bands {bands} -> mapping to last band {bands-1}")
            band = bands - 1
        try:
            add_dscp_to_band(dev, dscp, band, prio=prio_counter)
        except Exception as e:
            logging.exception(f"failed to map class {name} (DSCP {dscp}) to band {band}: {e}")
        prio_counter += 1

    logging.info("Scheduler installed. Running.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        cleanup_and_exit()


if __name__ == "__main__":
    main()
