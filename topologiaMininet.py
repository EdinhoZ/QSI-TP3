#!/usr/bin/env python
# -*- coding: utf-8 -*-

from mininet.net import Mininet
from mininet.node import Controller, RemoteController, OVSController
from mininet.node import CPULimitedHost, Host, Node
from mininet.node import OVSKernelSwitch, UserSwitch
from mininet.node import IVSSwitch
from mininet.log import setLogLevel, info
from mininet.link import TCLink, Intf
from mininet.cli import CLI
from subprocess import call
import time
import sys

def myNetwork():

    net = Mininet(topo=None,
                  build=False,
                  ipBase='10.0.0.0/8')

    info('*** Adding controller\n')
    info('*** Add switches\n')
    s2 = net.addSwitch('s2', cls=OVSKernelSwitch, failMode='standalone')
    s4 = net.addSwitch('s4', cls=OVSKernelSwitch, failMode='standalone')
    r5 = net.addHost('r5', cls=Node, ip='0.0.0.0')
    r5.cmd('sysctl -w net.ipv4.ip_forward=1')
    r8 = net.addHost('r8', cls=Node, ip='0.0.0.0')
    r8.cmd('sysctl -w net.ipv4.ip_forward=1')
    r10 = net.addHost('r10', cls=Node, ip='0.0.0.0')
    r10.cmd('sysctl -w net.ipv4.ip_forward=1')
    r6 = net.addHost('r6', cls=Node, ip='0.0.0.0')
    r6.cmd('sysctl -w net.ipv4.ip_forward=1')
    s3 = net.addSwitch('s3', cls=OVSKernelSwitch, failMode='standalone')
    r9 = net.addHost('r9', cls=Node, ip='0.0.0.0')
    r9.cmd('sysctl -w net.ipv4.ip_forward=1')
    r7 = net.addHost('r7', cls=Node, ip='0.0.0.0')
    r7.cmd('sysctl -w net.ipv4.ip_forward=1')
    s1 = net.addSwitch('s1', cls=OVSKernelSwitch, failMode='standalone')

    info('*** Add hosts\n')
    h11 = net.addHost('h11', cls=Host, ip='10.0.0.11', defaultRoute=None)
    h9  = net.addHost('h9',  cls=Host, ip='10.0.0.9',  defaultRoute=None)
    h5  = net.addHost('h5',  cls=Host, ip='10.0.0.5',  defaultRoute=None)
    h7  = net.addHost('h7',  cls=Host, ip='10.0.0.7',  defaultRoute=None)
    h6  = net.addHost('h6',  cls=Host, ip='10.0.0.6',  defaultRoute=None)
    h1  = net.addHost('h1',  cls=Host, ip='10.0.0.1',  defaultRoute=None)
    h8  = net.addHost('h8',  cls=Host, ip='10.0.0.8',  defaultRoute=None)
    h10 = net.addHost('h10', cls=Host, ip='10.0.0.10', defaultRoute=None)
    h2  = net.addHost('h2',  cls=Host, ip='10.0.0.2',  defaultRoute=None)
    h3  = net.addHost('h3',  cls=Host, ip='10.0.0.3',  defaultRoute=None)
    h4  = net.addHost('h4',  cls=Host, ip='10.0.0.4',  defaultRoute=None)

    info('*** Add links\n')
    net.addLink(h1, s1)
    net.addLink(h2, s1)
    net.addLink(h3, s1)
    net.addLink(h11, s2)
    net.addLink(h10, s2)
    net.addLink(h9, s2)
    net.addLink(s2, r7)
    net.addLink(r7, r8)
    net.addLink(r8, r9)
    net.addLink(r9, r6)
    net.addLink(r6, r7)
    net.addLink(r7, r10)
    net.addLink(h8, r10)
    net.addLink(r10, r6)
    net.addLink(s1, r8)
    net.addLink(r9, s4)
    net.addLink(s4, h4)
    net.addLink(s4, h5)
    net.addLink(r6, r5)
    net.addLink(r5, s3)
    net.addLink(s3, h6)
    net.addLink(s3, h7)

    info('*** Starting network\n')
    net.build()
    for controller in net.controllers:
        controller.start()

    info('*** Starting switches\n')
    net.get('s2').start([])
    net.get('s4').start([])
    net.get('s3').start([])
    net.get('s1').start([])

    info('*** Starting traffic generation\n')

    voip_src = net.get('h1')
    voip_dst = net.get('h11')
    video_src = net.get('h2')
    video_dst = net.get('h10')
    data_src  = net.get('h3')
    data_dst  = net.get('h9')

    info('*** Starting iperf servers\n')
    voip_dst.cmd('iperf -s -u > voip_server.log &')
    video_dst.cmd('iperf -s -u > video_server.log &')
    data_dst.cmd('iperf -s > data_server.log &')

    info('*** Starting traffic clients\n')
    voip_src.cmd('iperf -c 10.0.0.11 -u -b 100K -t 20 -i 1 > voip_client.log &')
    video_src.cmd('iperf -c 10.0.0.10 -u -b 5M -t 20 -i 1 > video_client.log &')
    data_src.cmd('iperf -c 10.0.0.9 -t 20 -i 1 > data_client.log &')

    info('*** Traffic started. Logs will be saved in each host.\n')

    if len(sys.argv) > 1 and sys.argv[1] == 'cli':
        info('*** Entrando no modo interativo do Mininet\n')
        CLI(net)
    else:
        info('*** Aguardando fim dos testes...\n')
        time.sleep(10)

    info('*** Encerrando rede...\n')
    net.stop()


if __name__ == '__main__':
    setLogLevel('info')
    myNetwork()
