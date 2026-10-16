interfaces {
    ethernet eth0 {
        address "$router_ip/24"
    }
    loopback lo {
    }
}
protocols {
    bfd {
$bfd_peers
    }
    bgp {
        address-family {
            ipv4-unicast {
                maximum-paths {
                    ebgp "8"
                }
            }
        }
$neighbors
        system-as "65000"
    }
    static {
        route 0.0.0.0/0 {
            next-hop $gateway {
            }
        }
        route $vip/32 {
            reject {
                distance "254"
            }
        }
    }
}
system {
    config-management {
        commit-revisions "20"
    }
    host-name "$cluster-router"
    login {
        user vyos {
            authentication {
                encrypted-password "$$6$$QxPS.uk6mfo$$9QBSo8u1FkH16gMyAVhus6fU3LOzvLR9Z9.82m3tiHFAxTtIkhaZSWssSgzt4v4dGAL8rhVQxTg0oAG9/q11h/"
                plaintext-password ""
            }
        }
    }
}
