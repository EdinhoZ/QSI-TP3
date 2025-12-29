#!/usr/bin/env python3
import sys
import subprocess
import os
import importlib.util

def install_package(package):
    if importlib.util.find_spec(package) is None:
        print(f"[SETUP] O módulo '{package}' não foi encontrado. A instalar...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", package])
            print(f"[SETUP] '{package}' instalado com sucesso.")
        except subprocess.CalledProcessError as e:
            print(f"[SETUP] ERRO: Falha ao instalar '{package}'. Erro: {e}")
            sys.exit(1)
    else:
        print(f"[SETUP] O módulo '{package}' já está instalado.")

def create_directory(path):
    if not os.path.exists(path):
        print(f"[SETUP] A criar diretoria: {path}")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            print(f"[SETUP] Erro ao criar diretoria {path}: {e}")
            sys.exit(1)
    else:
        print(f"[SETUP] A diretoria '{path}' já existe.")

def create_default_file(path, content):
    if not os.path.exists(path):
        print(f"[SETUP] A criar ficheiro default: {path}")
        with open(path, "w") as f:
            f.write(content)

def main():
    print("=== A iniciar configuração de dependências ===")
    install_package("prometheus_client")
    create_directory("vnf_logs")
    create_directory("firewall_rules")
    create_default_file("firewall_rules/default.txt", "# Default allow all\n")

    print("=== Configuração concluída com sucesso ===\n")

if __name__ == "__main__":
    main()
