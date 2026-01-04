#!/usr/bin/env python3
import subprocess
import time
import os
import re
import sys
import shutil

SCENARIOS = [
    {
        "name": "1_DEFAULT",
        "p_rate": "20mbit", "p_burst": "100kb",
        "s_rtp": "30mbit",  "s_be": "10mbit",
        "desc": "Base: Configuração inicial do enunciado."
    },
    {
        "name": "2_BURST_HUGE",
        "p_rate": "20mbit", "p_burst": "2mbit",
        "s_rtp": "30mbit",  "s_be": "10mbit",
        "desc": "Teste de Stress: Provoca Bufferbloat (Jitter alto)?"
    },
    {
        "name": "3_BURST_TINY",
        "p_rate": "20mbit", "p_burst": "10kb",
        "s_rtp": "30mbit",  "s_be": "10mbit",
        "desc": "Teste de Stress: O Policer corta pacotes legítimos?"
    },
    {
        "name": "4_PRIO_MAX",
        "p_rate": "15mbit", "p_burst": "100kb",
        "s_rtp": "70mbit",  "s_be": "5mbit",
        "desc": "Otimização: Máxima prioridade possível ao RTP."
    },
    {
        "name": "5_JUST_ENOUGH",
        "p_rate": "20mbit", "p_burst": "100kb",
        "s_rtp": "6mbit",   "s_be": "30mbit",
        "desc": "Limite: Dá apenas a banda estritamente necessária ao vídeo."
    },
    {
        "name": "6_POLICER_OPEN",
        "p_rate": "100mbit", "p_burst": "100kb",
        "s_rtp": "40mbit",   "s_be": "20mbit",
        "desc": "Controlo: Deixa tudo passar na entrada, só o Scheduler filtra."
    },
    {
        "name": "7_LATENCY_KING",
        "p_rate": "20mbit", "p_burst": "50kb",
        "s_rtp": "80mbit",  "s_be": "5mbit",
        "desc": "Tentativa: Combinar burst baixo com tubo largo."
    },
    {
        "name": "8_BROKEN",
        "p_rate": "20mbit", "p_burst": "100kb",
        "s_rtp": "1mbit",   "s_be": "90mbit",
        "desc": "Falha Intencional: RTP deve ter perdas massivas."
    }
]

FILE_POLICER = "policer.py"
FILE_SCHEDULER = "scheduler.py"
LOG_DIR = "traffic_logs"

def get_policer_code(rate, burst):
    return f"""#!/usr/bin/env python3
import subprocess
import time
import signal
import sys

DEFAULT_RATE = "{rate}"
DEFAULT_BURST = "{burst}"

RTP_DSCP = 46

def run(cmd):
    subprocess.run(cmd, shell=True, check=False)

def detect_lan_interfaces():
    out = subprocess.check_output("ip -o -4 addr show", shell=True, text=True)
    ifaces = []
    for ln in out.splitlines():
        _, iface, _, addr, *_ = ln.split()
        if iface == "lo": continue
        if addr.startswith("192.168."): continue
        ifaces.append(iface)
    return ifaces

def clear_ingress(dev):
    run(f"tc qdisc del dev {{dev}} ingress")

def setup_ingress(dev):
    run(f"tc qdisc add dev {{dev}} handle ffff: ingress")

def install_policer(dev):
    tos_ef = (RTP_DSCP << 2) & 0xff
    run(f"tc filter add dev {{dev}} parent ffff: protocol ip prio 1 u32 match ip tos {{tos_ef}} 0xff action pass")
    run(f"tc filter add dev {{dev}} parent ffff: protocol ip prio 10 u32 match ip protocol 17 0xff action police rate {{DEFAULT_RATE}} burst {{DEFAULT_BURST}} drop")

def main():
    lan_ifaces = detect_lan_interfaces()
    for iface in lan_ifaces:
        clear_ingress(iface)
        setup_ingress(iface)
        install_policer(iface)

    def stop(*_):
        for iface in lan_ifaces: clear_ingress(iface)
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while True: time.sleep(60)

if __name__ == "__main__":
    main()
"""

def get_scheduler_code(rtp_rate, be_rate):
    return f"""#!/usr/bin/env python3
import subprocess
import time
import signal
import sys

RTP_BW = "{rtp_rate}"
BE_BW  = "{be_rate}"

RTP_DSCP = 46
AF_DSCP  = 34
RTP_CLASS = "1:10"
AF_CLASS  = "1:20"
BE_CLASS  = "1:30"

def run(cmd):
    subprocess.run(cmd, shell=True, check=False)

def detect_core_interfaces():
    out = subprocess.check_output("ip -o -4 addr show", shell=True, text=True)
    ifaces = []
    for ln in out.splitlines():
        _, iface, _, addr, *_ = ln.split()
        if addr.startswith("192.168."):
            ifaces.append(iface)
    return ifaces

def clear_qdisc(dev):
    run(f"tc qdisc del dev {{dev}} root")

def setup_htb(dev):
    run(f"tc qdisc add dev {{dev}} root handle 1: htb default 30")
    run(f"tc class add dev {{dev}} parent 1: classid 1:1 htb rate 100mbit ceil 100mbit")
    
    run(f"tc class add dev {{dev}} parent 1:1 classid {{RTP_CLASS}} htb rate {{RTP_BW}} ceil 100mbit prio 0")
    run(f"tc qdisc add dev {{dev}} parent {{RTP_CLASS}} handle 10: prio bands 3")
    
    run(f"tc class add dev {{dev}} parent 1:1 classid {{AF_CLASS}} htb rate 20mbit ceil 80mbit prio 1")
    
    run(f"tc class add dev {{dev}} parent 1:1 classid {{BE_CLASS}} htb rate {{BE_BW}} ceil 100mbit prio 2")

def add_filters(dev):
    run(f"tc filter add dev {{dev}} protocol ip parent 1: prio 1 u32 match ip tos {{(RTP_DSCP<<2)}} 0xff flowid {{RTP_CLASS}}")
    run(f"tc filter add dev {{dev}} protocol ip parent 1: prio 2 u32 match ip tos {{(AF_DSCP<<2)}} 0xff flowid {{AF_CLASS}}")

def main():
    core_ifaces = detect_core_interfaces()
    for iface in core_ifaces:
        clear_qdisc(iface)
        setup_htb(iface)
        add_filters(iface)

    def stop(*_):
        for iface in core_ifaces: clear_qdisc(iface)
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while True: time.sleep(60)

if __name__ == "__main__":
    main()
"""

def backup_files():
    for f in [FILE_POLICER, FILE_SCHEDULER]:
        if os.path.exists(f):
            shutil.copy(f, f"{f}.bak")

def restore_files():
    print("\n[Otimizador] A restaurar ficheiros originais...")
    for f in [FILE_POLICER, FILE_SCHEDULER]:
        if os.path.exists(f"{f}.bak"):
            shutil.move(f"{f}.bak", f)

def write_vnf_config(scenario):
    with open(FILE_POLICER, "w") as f:
        f.write(get_policer_code(scenario["p_rate"], scenario["p_burst"]))
    
    with open(FILE_SCHEDULER, "w") as f:
        f.write(get_scheduler_code(scenario["s_rtp"], scenario["s_be"]))
    
    os.chmod(FILE_POLICER, 0o755)
    os.chmod(FILE_SCHEDULER, 0o755)

def restart_vnfs():
    subprocess.run("pkill -f policer.py", shell=True)
    subprocess.run("pkill -f scheduler.py", shell=True)
    time.sleep(1)
    
    subprocess.Popen([sys.executable, FILE_POLICER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.Popen([sys.executable, FILE_SCHEDULER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)

def parse_logs():
    results = {"jitter": "N/A", "http": "N/A", "bulk": "N/A"}
    
    try:
        stream_logs = [f for f in os.listdir(LOG_DIR) if "stream_" in f and f.endswith(".log")]
        if stream_logs:
            with open(os.path.join(LOG_DIR, stream_logs[0]), 'r') as f:
                content = f.read()
                match = re.search(r'(\d+\.\d+)\s+ms', content)
                if match:
                    results["jitter"] = float(match.group(1))
        
        http_logs = [f for f in os.listdir(LOG_DIR) if "http_" in f and f.endswith(".log")]
        if http_logs:
            with open(os.path.join(LOG_DIR, http_logs[0]), 'r') as f:
                content = f.read()
                matches = re.findall(r'\s(\d+(\.\d+)?)\s+Mbits/sec', content)
                if matches:
                    results["http"] = float(matches[-1][0])

        bulk_logs = [f for f in os.listdir(LOG_DIR) if "bulk_" in f and f.endswith(".log")]
        if bulk_logs:
            with open(os.path.join(LOG_DIR, bulk_logs[0]), 'r') as f:
                content = f.read()
                matches = re.findall(r'\s(\d+(\.\d+)?)\s+Mbits/sec', content)
                if matches:
                    results["bulk"] = float(matches[-1][0])

    except Exception as e:
        print(f"Erro a ler logs: {e}")
    
    return results

def run_optimization():
    print("="*80)
    print(" INICIANDO PROCESSO DE OTIMIZAÇÃO AUTOMÁTICA DE VNF ")
    print("="*80)
    print("NOTA: O Mininet (topologiaMininet.py) e o classifier.py JÁ DEVEM estar a correr.")
    
    backup_files()
    results_table = []

    try:
        for sc in SCENARIOS:
            print(f"\n---> TESTANDO CENÁRIO: {sc['name']}")
            print(f"     [Config] Policer Burst: {sc['p_burst']} | Scheduler RTP: {sc['s_rtp']}")
            
            write_vnf_config(sc)
            restart_vnfs()
            
            print("     [Exec] Gerando tráfego de stress (20s)...")
            subprocess.run([sys.executable, "stress_test.py", "--phase1-only", "--phase1-duration", "20"], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            res = parse_logs()
            print(f"     [Result] Jitter: {res['jitter']} ms | HTTP: {res['http']} Mbps | Bulk: {res['bulk']} Mbps")
            
            results_table.append({
                "name": sc["name"],
                "jitter": res["jitter"],
                "http": res["http"],
                "bulk": res["bulk"],
                "desc": sc["desc"]
            })
            
    except KeyboardInterrupt:
        print("\nInterrompido pelo utilizador.")
    finally:
        restore_files()
        restart_vnfs()

    print("\n" + "="*115)
    print(f"{'CENÁRIO':<20} | {'JITTER (ms)':<12} | {'HTTP (Mbps)':<12} | {'BULK (Mbps)':<12} | {'OBSERVAÇÃO'}")
    print("="*115)
    
    best_jitter = float('inf')
    best_sc = None

    for r in results_table:
        j = r['jitter'] if r['jitter'] != "N/A" else 999
        
        if j < best_jitter:
            best_jitter = j
            best_sc = r['name']

        print(f"{r['name']:<20} | {r['jitter']:<12} | {r['http']:<12} | {r['bulk']:<12} | {r['desc']}")
    
    print("="*115)
    print(f"\n>>> VENCEDOR: '{best_sc}' (Menor Jitter para VoIP).")

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("ERRO: Executa com 'sudo python3 otimizador.py'")
        sys.exit(1)
    run_optimization()
