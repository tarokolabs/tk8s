#!/bin/bash

# install etcdctl
if [ -f /etc/kubernetes/pki/etcd/ca.crt ]; then
   which etcdctl &>/dev/null
   if [ "$?" != "0" ]; then
      ETCD_RELEASE=$(curl -s https://api.github.com/repos/etcd-io/etcd/releases/latest|grep tag_name | cut -d '"' -f 4)
      wget https://github.com/etcd-io/etcd/releases/download/${ETCD_RELEASE}/etcd-${ETCD_RELEASE}-linux-amd64.tar.gz &>/dev/null
      tar zxvf etcd-${ETCD_RELEASE}-linux-amd64.tar.gz &>/dev/null
      cp -rp etcd-${ETCD_RELEASE}-linux-amd64/etcdctl /usr/local/bin

      echo '#!/bin/bash' > /usr/local/sbin/etcdctl
      echo '/usr/local/bin/etcdctl --endpoints=${SRVIP}:2379 --cacert=/etc/kubernetes/pki/etcd/ca.crt \
      --cert=/etc/kubernetes/pki/apiserver-etcd-client.crt \
      --key=/etc/kubernetes/pki/apiserver-etcd-client.key $@' >> /usr/local/sbin/etcdctl
      chmod +x /usr/local/sbin/etcdctl
      echo "etcdctl ok"
   fi
fi

