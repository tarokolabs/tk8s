interfaces {
    ethernet eth0 {
        address "$router_ip/24"
    }
    loopback lo {
    }
}
load-balancing {
    haproxy {
        backend api {
            mode "tcp"
$servers
        }
        service api {
            backend "api"
            mode "tcp"
            port "6443"
        }
        timeout {
            client "600"
            server "600"
        }
    }
}
protocols {
    static {
        route 0.0.0.0/0 {
            next-hop $gateway {
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
