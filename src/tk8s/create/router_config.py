"""The blocks tkctl renders into each router's tkctl.boot, in config.boot's own layout: the
haproxy servers (proxy), the BGP neighbors and BFD peers (bgp), and the VRRP group of a
two-router cluster. Templates have no loops; these strings fill their $servers, $neighbors,
$bfd_peers and $vrrp slots."""

from .. import model


def servers(c: model.Cluster) -> str:
    """The backend's server blocks, one per control plane, in config.boot's own layout."""
    return "\n".join(
        f"            server {n.name} {{\n"
        f'                address "{n.ip}"\n'
        f"                check {{\n"
        f"                }}\n"
        f'                port "6443"\n'
        f"            }}"
        for n in c.control_planes
    )


def neighbors(c: model.Cluster) -> str:
    """One eBGP neighbor per control plane (AS 65001, BFD on, timers 3/9), in config.boot's own
    layout, for the bgp template."""
    return "\n".join(
        f"        neighbor {n.ip} {{\n"
        f"            address-family {{\n"
        f"                ipv4-unicast {{\n"
        f"                }}\n"
        f"            }}\n"
        f"            bfd {{\n"
        f"            }}\n"
        f'            remote-as "65001"\n'
        f"            timers {{\n"
        f'                holdtime "9"\n'
        f'                keepalive "3"\n'
        f"            }}\n"
        f"        }}"
        for n in c.control_planes
    )


def bfd_peers(c: model.Cluster) -> str:
    """One BFD peer per control plane, 300 ms x 3: a silent control plane is dead in about a
    second (the spike measured 1.1 s against 8.8 s on BGP timers alone)."""
    return "\n".join(
        f"        peer {n.ip} {{\n"
        f"            interval {{\n"
        f'                multiplier "3"\n'
        f'                receive "300"\n'
        f'                transmit "300"\n'
        f"            }}\n"
        f"        }}"
        for n in c.control_planes
    )


def vrrp_block(c: model.Cluster, r: model.Router) -> str:
    """The high-availability block of a two-router cluster, in config.boot's own layout (captured
    from a `save` on VyOS 2026.10.07): group tk8s floats the cluster's router address between the
    two. router-1 (priority 200) waits preempt-delay seconds after boot before taking the address
    back, so its BGP sessions have learnt the VIP by then. Empty with one router."""
    if r.priority is None:
        return ""
    delay = '            preempt-delay "10"\n' if r.priority == 200 else ""
    return (
        "high-availability {\n"
        "    vrrp {\n"
        "        group tk8s {\n"
        f"            address {c.floating_ip}/24 {{\n"
        "            }\n"
        '            interface "eth0"\n'
        f"{delay}"
        f'            priority "{r.priority}"\n'
        '            vrid "10"\n'
        "        }\n"
        "    }\n"
        "}"
    )
