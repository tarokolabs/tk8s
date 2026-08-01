#!/bin/bash

VER="24.01"

sudo podman rmi dkreg.kube-system:5000/alp.jkagent:${VER} &>/dev/null
[ "$?" == "0" ] && echo "dkreg.kube-system/alp.jkagent:${VER} removed"
sudo podman rmi alp.jkagent:${VER} &>/dev/null
[ "$?" == "0" ] && echo "alp.jkagent:${VER} removed"
sudo podman build --format=docker --build-arg="VER=3.20.0" --no-cache --force-rm --squash-all -t dkreg.kube-system:5000/alp.jkagent:${VER} ~/wulin/images/jkagent &>/dev/null
[ "$?" == "0" ] && echo -e "dkreg.kube-system:5000/alp.jkagent:${VER} image ok\n"

sudo podman login --tls-verify=false dkreg.kube-system:5000 -u bigred -p bigred &>/dev/null
[ "$?" != "0" ] && echo "docker registry not exist" && exit 1

im="dkreg.kube-system:5000/alp.jkagent:${VER}"
echo "[CI/CD images deploy]"
for n in $im
do
  sudo podman push --tls-verify=false $n &>/dev/null
  [ "$?" == "0" ] && echo "$n push ok"
done
sudo podman logout dkreg.kube-system:5000 &>/dev/null
echo ""
