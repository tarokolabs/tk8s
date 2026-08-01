#!/bin/bash

[ "$1" == "" ] && echo "mkistio <cluser>" && exit 1

which istioctl &>/dev/null
[ "$?" != "0" ] && echo  "pls install istioctl" &&  exit 1

node="${1}-control-plane ${1}-worker ${1}-worker2"

for n in $node
do
   sudo podman exec $n cat /etc/sysctl.conf | grep 'fs.inotify.max_user_watches=200000' &>/dev/null
   if [ "$?" != "0" ]; then
      sudo podman exec $n bash -c 'echo fs.inotify.max_user_watches=200000 >> /etc/sysctl.conf'
      sudo podman exec $n bash -c 'echo fs.inotify.max_user_instances=200000 >> /etc/sysctl.conf' 
      sudo podman exec $n bash -c 'sysctl -p'
      sudo podman exec $n bash -c 'echo ip_tables >> /etc/modules'
   fi
done
 
