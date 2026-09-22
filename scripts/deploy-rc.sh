#!/usr/bin/env bash

cn=$(kubectl config view --minify -o jsonpath='{.clusters[].name}')
[ "$cn" == "" ] && echo "can not find any K8S cluster" && exit 1

cl=$(sudo podman ps -a | grep -o -e "${cn}-[a-z]*[-]*[a-z]*[1-9]*" | tr '\n' ' ')

for nn in $cl 
do 
   sudo podman exec ${nn} bash -c "apt-get update &>/dev/null && apt-get upgrade -y &>/dev/null && echo $nn update ok"
   sudo podman exec ${nn} bash -c "apt-get install -y gpg inotify-tools wget zstd &>/dev/null"

   # install crun (https://github.com/containers/crun/releases)
   sudo podman exec ${nn} which crun &>/dev/null
   if [ "$?" != "0" ]; then
      sudo podman exec ${nn} wget https://github.com/containers/crun/releases/download/1.29.1/crun-1.29.1-linux-amd64 -O /usr/bin/crun &>/dev/null
      [ "$?" == "0" ] && sudo podman exec ${nn} chmod +x /usr/bin/crun && echo "$nn crun ok"
   fi
   #[ "$?" != "0" ] && sudo podman exec ${nn} bash -c 'apt install -y nano crun &>/dev/null' && echo "$nn crun ok" 

   # install gVisor（https://gvisor.dev/docs/user_guide/install/）
   # 官方發佈自 2026 起改為單一 gvisor.tar.zstd（含 runsc、containerd-shim-runsc-v1、gvisor-bin/），
   # 舊的 ${URL}/runsc 單檔路徑已 404。釘版本而非 latest，GVISOR_REL 可覆寫；解包需節點內有 zstd
   sudo podman exec ${nn} which runsc &>/dev/null
   if [ "$?" != "0" ]; then
      GVISOR_REL=${GVISOR_REL:-20260914.0}
      sudo podman exec -e URL=https://storage.googleapis.com/gvisor/releases/release/${GVISOR_REL}/$(uname -m) \
      ${nn} bash -c 'set -e; cd /tmp
         wget -q ${URL}/gvisor.tar.zstd ${URL}/gvisor.tar.zstd.sha512
         sha512sum -c gvisor.tar.zstd.sha512 >/dev/null
         tar --zstd -xf gvisor.tar.zstd -C /usr/local/bin
         rm -f gvisor.tar.zstd gvisor.tar.zstd.sha512' &>/tmp/${nn}-gvisor.out
      sudo podman exec ${nn} which runsc &>/dev/null \
         && echo "$nn runsc ok ($(sudo podman exec ${nn} runsc --version | head -1 | awk '{print $3}'))" \
         || echo "$nn runsc FAILED (see /tmp/${nn}-gvisor.out)"
   fi 
done 
