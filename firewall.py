#!/usr/bin/env python3
import argparse
import logging
import os
import shlex
import signal
import subprocess
import sys
from typing import List, Tuple, Optional

LOGFILE = "vnf_logs/firewall.log"
CHAIN_NAME = "VNF-FW"
TABLE = "filter"
HOOK_CHAIN = "forward"

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

def run_cmd(cmd, dry_run=False):
    if dry_run:
        print(f"[DRY RUN] {cmd}")
        return
    proc = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)
    return proc.stdout.strip()

def nft_hook_chain():
        try:
            # Create forward chain if it doesn't exist
            try:
                run_cmd(f"nft list chain inet {TABLE} {HOOK_CHAIN}")
            except subprocess.CalledProcessError:
                print(f"[INFO] Forward chain '{HOOK_CHAIN}' not found, creating...")
                run_cmd(f'nft add chain inet {TABLE} {HOOK_CHAIN} "{{ type filter hook forward priority 0 ; policy accept ; }}"')

            # Create VNF-FW chain if it doesn't exist
            try:
                run_cmd(f"nft list chain inet {TABLE} {CHAIN_NAME}")
            except subprocess.CalledProcessError:
                print(f"[INFO] VNF chain '{CHAIN_NAME}' not found, creating...")
                run_cmd(f"nft add chain inet {TABLE} {CHAIN_NAME} {{ type filter hook none ; }}")

            # Hook VNF-FW into forward chain if not already hooked
            try:
                output = run_cmd(f"nft list chain inet {TABLE} {HOOK_CHAIN}")
                if f"jump {CHAIN_NAME}" not in output:
                    print(f"[INFO] Hooking '{CHAIN_NAME}' into '{HOOK_CHAIN}'")
                    run_cmd(f"nft insert rule inet {TABLE} {HOOK_CHAIN} jump {CHAIN_NAME}")
            except subprocess.CalledProcessError as e:
                print(f"[ERROR] Could not hook VNF-FW: {e}")
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] nft command failed: {e}")


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

    def nft_create_table_and_chain(self, table="filter", chain=CHAIN_NAME):
        # Create table if missing
        run_cmd(f"nft list tables | grep -q 'inet {table}' || nft add table inet {table}", dry_run=self.dry_run)

        # Create chain if missing
        try:
            run_cmd(f"nft list chain inet {table} {chain}", dry_run=self.dry_run)
        except subprocess.CalledProcessError:
            # Old nft versions: create chain without hook first
            run_cmd(f"nft add chain inet {table} {chain}", dry_run=self.dry_run)
            logging.info(f"nft chain {chain} created in table {table}")

            # Then insert hook rule separately
            nft_hook_chain()

    def nft_add_rule(self, rule: Rule):
        name, action, proto, src, dst, sport, dport, dscp = rule
        chain = "VNF-FW"

        cmd = f"nft add rule inet filter {chain} "

        if proto != "all":
            cmd += f"{proto} "
        if sport:
            cmd += f"sport {sport} "
        if dport:
            cmd += f"dport {dport} "
        if dscp is not None:
            cmd += f"ip dscp set {dscp} "

        # Use accept/drop/etc. in lowercase
        cmd += f"counter {action.lower()}"

        run_cmd(cmd, dry_run=self.dry_run)


    def nft_cleanup(self):
        # Delete jump rule if exists
        try:
            run_cmd(f"nft list chain inet filter forward | grep 'jump {CHAIN_NAME}'", dry_run=self.dry_run)
            run_cmd(f"nft delete rule inet filter forward jump {CHAIN_NAME}", fail_ok=True, dry_run=self.dry_run)
        except Exception:
            pass
        # Delete chain
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
            nft_hook_chain()
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
