#!/usr/bin/env python3

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import Node, OVSBridge
from mininet.cli import CLI
from mininet.log import setLogLevel, info
import ipaddress
import time
import sys
from collections import deque, defaultdict

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
            self.addLink(routers[a], routers[b])

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
            if r_intf is None:
                info(f"WARNING: router {rname} has no connection to switch {switch_name}; using defaultintf()\n")
                r_intf_name = router.defaultintf().name
            else:
                r_intf_name = r_intf.name
            r_ip = next(ip_iter)
            router.setIP(str(r_ip), prefixLen=24, intf=r_intf_name)

            hosts_on_sw = []
            for h in net.hosts:
                conns = h.connectionsTo(sw)
                if conns:
                    hosts_on_sw.append(h)

            for h in hosts_on_sw:
                hip = next(ip_iter)
                h.setIP(str(hip), prefixLen=24)
                h.cmd(f"ip route add default via {r_ip}")

        else:
            # caso do r5 (nenhum switch)
            found = False
            for h in net.hosts:
                intf_r, intf_h = connected_intf(router, h)
                if intf_r is not None:
                    r_ip = next(ip_iter)
                    router.setIP(str(r_ip), prefixLen=24, intf=intf_r.name)
                    hip = next(ip_iter)
                    h.setIP(str(hip), prefixLen=24)
                    h.cmd(f"ip route add default via {r_ip}")
                    found = True
                    break
            if not found:
                info(f"WARNING: r5 had no direct host connection found; assigned IP to defaultintf()\n")
                r_ip = next(ip_iter)
                router.setIP(str(r_ip), prefixLen=24, intf=router.defaultintf().name)

    info("*** Assigning p2p links (/30)\n")

    base = ipaddress.ip_network("192.168.0.0/16")
    sub_iter = base.subnets(new_prefix=30)

    r_adj = defaultdict(set)
    link_ip = {}   # (r1, r2) -> (ip1, ip2, subnet)

    topo = net.topo

    for (a, b) in topo.p2p_links:
        subnet = next(sub_iter)
        hosts = list(subnet.hosts())
        ipA, ipB = hosts[0], hosts[1]

        ra = net.get(a)
        rb = net.get(b)

        intfA, intfB = connected_intf(ra, rb)
        if intfA is None or intfB is None:
            info(f"ERROR: no intf between {a} and {b} detected\n")
            continue

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
            cur = path[-1]
            if cur == dst:
                return path
            for nb in r_adj[cur]:
                if nb not in seen:
                    seen.add(nb)
                    q.append(path + [nb])
        return None

    # Add routes to all LAN subnets AND all p2p /30s
    for rname in ["r1", "r2", "r3", "r4", "r5"]:
        r = net.get(rname)

        for target in ["r1", "r2", "r3", "r4", "r5"]:
            if target == rname:
                continue

            # 1. Route to target LAN (10.x.0.0/24)
            path = bfs(rname, target)
            if not path:
                continue

            next_hop = path[1]
            nh_entry = link_ip.get((rname, next_hop))
            if not nh_entry:
                continue
            nh_ip = nh_entry[1]

            # LAN route
            r.cmd(f"ip route add {router_subnet[target]} via {nh_ip}")

            # 2. Route to every p2p /30 subnet attached to 'target'
            for (a, b), (ipa, ipb, subnet) in link_ip.items():
                if a == target:          # subnets connected to 'target'
                    r.cmd(f"ip route add {subnet} via {nh_ip}")

    info("*** Routing installed\n")

def run(flag):
    topo = Topology()
    net = Mininet(topo=topo, controller=None)
    net.start()

    ip_assign(net)

    info("\n*** intf dump\n")
    for r in ["r1", "r2", "r3", "r4", "r5"]:
        info(f"\n=== {r} ===\n")
        info(net.get(r).cmd("ip -4 addr show"))
        info(net.get(r).cmd("ip route show"))

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
