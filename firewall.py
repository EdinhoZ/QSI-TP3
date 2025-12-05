#!/usr/bin/env python3
import argparse
import logging
import os
import shlex
import signal
import subprocess
import sys
from typing import List, Tuple, Optional

LOGFILE = "firewall.log"
CHAIN_NAME = "VNF-FW"  # custom chain name for iptables / nft

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


def run_cmd(cmd: str, fail_ok: bool = False, dry_run: bool = False):
    logging.debug(f"[cmd] {cmd}")
    if dry_run:
        print("[DRY RUN]", cmd)
        return 0
    try:
        proc = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
        if proc.stdout:
            logging.debug(proc.stdout.strip())
        return proc.returncode
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed: {cmd}\nrc={e.returncode} stdout={e.stdout.strip()} stderr={e.stderr.strip()}")
        if fail_ok:
            return e.returncode
        raise

def has_command(cmdname: str) -> bool:
    return subprocess.run(f"which {shlex.quote(cmdname)}", shell=True, stdout=subprocess.DEVNULL).returncode == 0

# Rule type: (name, action, proto, src, dst, sport, dport, dscp)
Rule = Tuple[str, str, str, str, str, str, str, Optional[int]]

def parse_rule_arg(s: str) -> Rule:
    parts = s.split(":")
    if len(parts) < 7:
        raise argparse.ArgumentTypeError("rule must be NAME:ACTION:PROTO:SRC:DST:SRC_PORT:DST_PORT[:DSCP]")
    # pad to 8
    while len(parts) < 8:
        parts.append("")
    name, action, proto, src, dst, sport, dport, dscp_s = parts[:8]
    proto = proto.lower()
    action = action.upper()
    dscp = int(dscp_s) if dscp_s.strip() else None
    return (name, action, proto, src or "0.0.0.0/0", dst or "0.0.0.0/0", sport or "", dport or "", dscp)

class FirewallVNF:
    def __init__(self, use_nft: bool, dry_run: bool = False):
        self.use_nft = use_nft
        self.dry_run = dry_run

    def nft_flush_table(self, table_name="filter"):
        run_cmd(f"nft flush table inet {table_name}", fail_ok=True, dry_run=self.dry_run)

    def nft_create_table_and_chain(self, table="filter", chain=CHAIN_NAME):
        # Ensure table exists and our chain exists; use family inet (works for IPv4 & IPv6)
        run_cmd(f"nft add table inet {table}", fail_ok=True, dry_run=self.dry_run)
        # Create a chain in table `inet filter` with type filter hook forward priority 0; policy accept default
        run_cmd(f"nft add chain inet {table} {chain} {{ type filter hook forward priority 0 ; policy accept ; }}",
                fail_ok=True, dry_run=self.dry_run)

    def nft_add_rule(self, rule: Rule):
        name, action, proto, src, dst, sport, dport, dscp = rule
        parts = []
        # family inet table filter chain VNF-FW
        if proto != "all":
            parts.append(proto)
        parts.append(f"ip saddr {src}")
        parts.append(f"ip daddr {dst}")
        if sport:
            parts.append(f"{proto} sport {sport}")
        if dport:
            parts.append(f"{proto} dport {dport}")
        if dscp is not None:
            parts.append(f"ip dscp {dscp}")

        # action mapping
        if action == "ACCEPT":
            act = "accept"
        elif action == "DROP":
            act = "drop"
        elif action == "REJECT":
            act = "reject"
        elif action == "LOG":
            # log then accept by default
            run_cmd(f"nft add rule inet filter {CHAIN_NAME} {' '.join(parts)} counter log prefix \"FW[{name}] \"", dry_run=self.dry_run)
            logging.info(f"nft rule added (LOG) {name}: {' '.join(parts)}")
            return
        else:
            raise ValueError(f"Unknown action {action}")

        cmd = f"nft add rule inet filter {CHAIN_NAME} {' '.join(parts)} counter {act}"
        run_cmd(cmd, dry_run=self.dry_run)
        logging.info(f"nft rule added {name}: {cmd}")

    def nft_hook_chain(self):
        # ensure FORWARD traffic jumps through our chain: add rule at top of forward chain to jump
        run_cmd(f"nft insert rule inet filter forward jump {CHAIN_NAME}", fail_ok=True, dry_run=self.dry_run)

    def nft_cleanup(self):
        # Remove jump rule(s) and delete our chain
        run_cmd(f"nft delete rule inet filter forward jump {CHAIN_NAME}", fail_ok=True, dry_run=self.dry_run)
        run_cmd(f"nft delete chain inet filter {CHAIN_NAME}", fail_ok=True, dry_run=self.dry_run)
        logging.info("nft cleanup completed")

    def ipt_create_chain(self):
        run_cmd(f"iptables -N {CHAIN_NAME}", fail_ok=True, dry_run=self.dry_run)
        # Insert jump in FORWARD to our chain (if not present)
        run_cmd(f"iptables -C FORWARD -j {CHAIN_NAME}", fail_ok=True, dry_run=self.dry_run)
        # If check failed (non-zero) then insert
        try:
            run_cmd(f"iptables -C FORWARD -j {CHAIN_NAME}", fail_ok=False, dry_run=self.dry_run)
        except Exception:
            run_cmd(f"iptables -I FORWARD -j {CHAIN_NAME}", dry_run=self.dry_run)

    def ipt_flush_chain(self):
        run_cmd(f"iptables -F {CHAIN_NAME}", fail_ok=True, dry_run=self.dry_run)

    def ipt_delete_chain(self):
        run_cmd(f"iptables -D FORWARD -j {CHAIN_NAME}", fail_ok=True, dry_run=self.dry_run)
        run_cmd(f"iptables -F {CHAIN_NAME}", fail_ok=True, dry_run=self.dry_run)
        run_cmd(f"iptables -X {CHAIN_NAME}", fail_ok=True, dry_run=self.dry_run)
        logging.info("iptables cleanup completed")

    def ipt_add_rule(self, rule: Rule):
        name, action, proto, src, dst, sport, dport, dscp = rule
        parts = []
        if proto != "all":
            parts.append(f"-p {proto}")
        if src:
            parts.append(f"-s {src}")
        if dst:
            parts.append(f"-d {dst}")
        if sport:
            parts.append(f"--sport {sport}")
        if dport:
            parts.append(f"--dport {dport}")
        if dscp is not None:
            # iptables dscp match via dscp module
            parts.append(f"-m dscp --dscp {dscp}")
        # action mapping
        if action == "ACCEPT":
            act = "-j ACCEPT"
        elif action == "DROP":
            act = "-j DROP"
        elif action == "REJECT":
            act = "-j REJECT"
        elif action == "LOG":
            # insert a LOG followed by accept
            cmd_log = f"iptables -A {CHAIN_NAME} {' '.join(parts)} -j LOG --log-prefix 'FW[{name}] '"
            run_cmd(cmd_log, dry_run=self.dry_run)
            logging.info(f"iptables LOG rule added {name}")
            return
        else:
            raise ValueError(f"Unknown action {action}")

        cmd = f"iptables -A {CHAIN_NAME} {' '.join(parts)} {act}"
        run_cmd(cmd, dry_run=self.dry_run)
        logging.info(f"iptables rule added {name}: {cmd}")

    def install_rules(self, rules: List[Rule]):
        logging.info(f"Installing {len(rules)} rules (use_nft={self.use_nft})")
        if self.use_nft:
            # Create table/chain and add jump rule
            self.nft_create_table_and_chain()
            # ensure forward hooks -> jump
            self.nft_hook_chain()
            for r in rules:
                self.nft_add_rule(r)
        else:
            # iptables path
            self.ipt_create_chain()
            self.ipt_flush_chain()  # start clean
            for r in rules:
                self.ipt_add_rule(r)

    def cleanup(self):
        logging.info("Cleaning firewall state")
        if self.use_nft:
            try:
                self.nft_cleanup()
            except Exception as e:
                logging.warning(f"nft cleanup warning: {e}")
        else:
            try:
                self.ipt_delete_chain()
            except Exception as e:
                logging.warning(f"iptables cleanup warning: {e}")


def load_rules_from_file(path: str) -> List[Rule]:
    rules = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            rules.append(parse_rule_arg(ln))
    return rules


def main():
    p = argparse.ArgumentParser(description="Firewall VNF (iptables / nftables)")

    p.add_argument("--rule", "-r", dest="rules", action="append",
                   help="rule NAME:ACTION:PROTO:SRC:DST:SRC_PORT:DST_PORT[:DSCP]",
                   type=parse_rule_arg)
    p.add_argument("--rule-file", help="file with rules one per line (same format, '#' comments)")
    p.add_argument("--nft", action="store_true", help="prefer nftables (if available)")
    p.add_argument("--dry-run", action="store_true", help="print commands without executing")
    args = p.parse_args()

    # gather rules
    rules: List[Rule] = []
    if args.rule_file:
        if not os.path.exists(args.rule_file):
            logging.error("rule-file not found")
            sys.exit(1)
        rules.extend(load_rules_from_file(args.rule_file))
    if args.rules:
        rules.extend(args.rules)
    if not rules:
        logging.error("No rules provided; nothing to install.")
        sys.exit(1)

    # choose backend
    use_nft = args.nft and has_command("nft")
    if args.nft and not has_command("nft"):
        logging.warning("nft requested but not available; using iptables")
    logging.info(f"Firewall starting, backend nft={use_nft}, dry_run={args.dry_run}")

    fw = FirewallVNF(use_nft=use_nft, dry_run=args.dry_run)

    def stop(signum=None, frame=None):
        logging.info("Stopping firewall and cleaning up...")
        try:
            fw.cleanup()
        except Exception as e:
            logging.exception(f"Cleanup error: {e}")
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    # Install rules
    try:
        fw.install_rules(rules)
        logging.info("Firewall rules installed and active.")
        # Keep process alive
        while True:
            signal.pause()
    finally:
        fw.cleanup()


if __name__ == "__main__":
    main()
