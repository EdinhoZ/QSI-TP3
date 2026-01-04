#!/usr/bin/env python3

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import Node, OVSBridge
from mininet.cli import CLI
from mininet.log import setLogLevel, info
from mininet.link import TCLink
import ipaddress
import time
import sys
from collections import deque, defaultdict
import json
import os


class Router(Node):
    def config(self, **params):
        super().config(**params)
        self.cmd("sysctl net.ipv4.ip_forward=1")

    def terminate(self):
        self.cmd("sysctl net.ipv4.ip_forward=0")
        super().terminate()


class Topology(Topo):

    def build(self):
        routers = {}
        for r in ["r1", "r2", "r3", "r4", "r5"]:
            routers[r] = self.addNode(r, cls=Router)

        # r1 LAN
        s1 = self.addSwitch("s1", cls=OVSBridge)
        h1 = self.addHost("h1")
        h2 = self.addHost("h2")
        h3 = self.addHost("h3")

        # r2 LAN
        s2 = self.addSwitch("s2", cls=OVSBridge)
        h4 = self.addHost("h4")
        h5 = self.addHost("h5")

        # r3 LAN
        s3 = self.addSwitch("s3", cls=OVSBridge)
        h6 = self.addHost("h6")
        h7 = self.addHost("h7")
        h8 = self.addHost("h8")

        # r4 LAN
        s4 = self.addSwitch("s4", cls=OVSBridge)
        h9  = self.addHost("h9")
        h10 = self.addHost("h10")

        # r5 LAN
        h11 = self.addHost("h11")

        for h in [h1, h2, h3]:
            self.addLink(s1, h)

        for h in [h4, h5]:
            self.addLink(s2, h)

        for h in [h6, h7, h8]:
            self.addLink(s3, h)

        for h in [h9, h10]:
            self.addLink(s4, h)

        self.addLink(routers["r5"], h11)

        # =========================
        # CORE ROUTER LINKS (CAPPED)
        # =========================
        CORE_BW = 35      # Mbps
        CORE_DELAY = "5ms"

        router_links = [
            ("r1", "r2"),
            ("r1", "r3"),
            ("r2", "r4"),
            ("r3", "r4"),
            ("r3", "r5"),
            ("r4", "r5")
        ]

        self.p2p_links = []
        for (a, b) in router_links:
            self.p2p_links.append((a, b))
            self.addLink(
                routers[a],
                routers[b],
                cls=TCLink,
                bw=CORE_BW,
                delay=CORE_DELAY
            )

        # Router–LAN links (UNCAPPED)
        self.addLink(routers["r1"], s1)
        self.addLink(routers["r2"], s2)
        self.addLink(routers["r3"], s3)
        self.addLink(routers["r4"], s4)


topos = {"topology": (lambda: Topology())}


def ip_assign(net):
    info("*** Assigning IPs\n")

    lan_map = {
        "r1": ("s1", net.get("r1"), "10.1.0.0/24"),
        "r2": ("s2", net.get("r2"), "10.2.0.0/24"),
        "r3": ("s3", net.get("r3"), "10.3.0.0/24"),
        "r4": ("s4", net.get("r4"), "10.4.0.0/24"),
        "r5": (None, net.get("r5"), "10.5.0.0/24"),
    }

    def connected_intf(a, b):
        conns = a.connectionsTo(b)
        if not conns:
            return None, None
        intfA, intfB = conns[0]
        return intfA, intfB

    for rname, (switch_name, router, subnet) in lan_map.items():
        net_sub = ipaddress.ip_network(subnet)
        ip_iter = iter(net_sub.hosts())

        if switch_name:
            sw = net.get(switch_name)
            r_intf, sw_intf = connected_intf(router, sw)
            r_ip = next(ip_iter)
            router.setIP(str(r_ip), prefixLen=24, intf=r_intf.name)

            for h in net.hosts:
                if h.connectionsTo(sw):
                    hip = next(ip_iter)
                    h.setIP(str(hip), prefixLen=24)
                    h.cmd(f"ip route add default via {r_ip}")

        else:
            for h in net.hosts:
                intf_r, intf_h = connected_intf(router, h)
                if intf_r:
                    r_ip = next(ip_iter)
                    router.setIP(str(r_ip), prefixLen=24, intf=intf_r.name)
                    hip = next(ip_iter)
                    h.setIP(str(hip), prefixLen=24)
                    h.cmd(f"ip route add default via {r_ip}")
                    break

    info("*** Assigning p2p links (/30)\n")

    base = ipaddress.ip_network("192.168.0.0/16")
    sub_iter = base.subnets(new_prefix=30)

    r_adj = defaultdict(set)
    link_ip = {}

    topo = net.topo

    for (a, b) in topo.p2p_links:
        subnet = next(sub_iter)
        ipA, ipB = list(subnet.hosts())

        ra = net.get(a)
        rb = net.get(b)

        intfA, intfB = connected_intf(ra, rb)
        ra.setIP(str(ipA), prefixLen=30, intf=intfA.name)
        rb.setIP(str(ipB), prefixLen=30, intf=intfB.name)

        r_adj[a].add(b)
        r_adj[b].add(a)
        link_ip[(a, b)] = (str(ipA), str(ipB), str(subnet))
        link_ip[(b, a)] = (str(ipB), str(ipA), str(subnet))

    info("*** Installing static routes\n")

    router_subnet = {r: lan_map[r][2] for r in lan_map}

    def bfs(src, dst):
        q = deque([[src]])
        seen = {src}
        while q:
            path = q.popleft()
            if path[-1] == dst:
                return path
            for nb in r_adj[path[-1]]:
                if nb not in seen:
                    seen.add(nb)
                    q.append(path + [nb])
        return None

    for rname in router_subnet:
        r = net.get(rname)
        for target in router_subnet:
            if target == rname:
                continue

            path = bfs(rname, target)
            if not path:
                continue

            nh = path[1]
            nh_ip = link_ip[(rname, nh)][1]
            r.cmd(f"ip route add {router_subnet[target]} via {nh_ip}")

            for (a, _), (_, _, subnet) in link_ip.items():
                if a == target:
                    r.cmd(f"ip route add {subnet} via {nh_ip}")

    info("*** Routing installed\n")


def run(flag):
    topo = Topology()
    net = Mininet(topo=topo, controller=None)
    net.start()

    ip_assign(net)

    if flag == "test":
        info("\n*** Running pingAll\n")
        loss = net.pingAll()
        info(f"PingAll loss = {loss}%\n")

    CLI(net)
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    flag = sys.argv[1] if len(sys.argv) > 1 else None
    run(flag)
