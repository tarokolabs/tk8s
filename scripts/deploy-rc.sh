#!/usr/bin/env bash

cn=$(kubectl config view --minify -o jsonpath='{.clusters[].name}')
[ "$cn" == "" ] && echo "can not find any K8S cluster" && exit 1

cl=$(sudo podman ps -a | grep -o -e "${cn}-[a-z]*[-]*[a-z]*[1-9]*" | tr '\n' ' ')

for nn in $cl 
do 
   sudo podman exec ${nn} bash -c "apt-get update &>/dev/null && apt-get upgrade -y &>/dev/null && echo $nn update ok"
   sudo podman exec ${nn} bash -c "apt-get install -y gpg inotify-tools wget &>/dev/null"

   # install crun (https://github.com/containers/crun/releases)
   sudo podman exec ${nn} which crun &>/dev/null
   if [ "$?" != "0" ]; then
      sudo podman exec ${nn} wget https://github.com/containers/crun/releases/download/1.21/crun-1.21-linux-amd64 -O /usr/bin/crun &>/dev/null
      [ "$?" == "0" ] && sudo podman exec ${nn} chmod +x /usr/bin/crun && echo "$nn crun ok"
   fi
   #[ "$?" != "0" ] && sudo podman exec ${nn} bash -c 'apt install -y nano crun &>/dev/null' && echo "$nn crun ok" 

   # install gVisor
   sudo podman exec ${nn} which runsc &>/dev/null
   if [ "$?" != "0" ]; then
      sudo podman exec -e ARCH=$(uname -m) -e URL=https://storage.googleapis.com/gvisor/releases/release/latest/`uname -m` \
      ${nn} bash -c 'wget ${URL}/runsc ${URL}/containerd-shim-runsc-v1 &>/dev/null && \
      chmod a+rx runsc containerd-shim-runsc-v1 && mv runsc containerd-shim-runsc-v1 /usr/local/bin'
      echo "$nn runsc ok"
   fi 
done 
