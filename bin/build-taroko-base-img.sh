#!/bin/bash

VER="26.01"

sudo rm /usr/bin/mc &>/dev/null
sudo curl -s https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/bin/mc &>/dev/null
if [ "$?" == "0" ]; then
   sudo chmod +x /usr/bin/mc
   cp -rP /usr/bin/mc ~/tk/wulin/images/base/
   cp -rP /usr/bin/mc ~/tk/wulin/images/jkagent/
   echo "minio client ok"
fi

sudo podman rmi quay.io/cloudwalker/alp.base &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/alp.base removed"
sudo podman build --format=docker --build-arg="VER=3.22.1" --no-cache --force-rm -t quay.io/cloudwalker/alp.base ~/tk/wulin/images/base &>/tmp/alp.base
[ "$?" != "0" ] && echo -e "quay.io/cloudwalker/alp.base image error\n" && exit 1
echo -e "quay.io/cloudwalker/alp.base image ok\n"

sudo podman rmi quay.io/cloudwalker/alp.sshd &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/alp.sshd removed"
sudo podman build --format=docker --no-cache --force-rm -t quay.io/cloudwalker/alp.sshd ~/tk/wulin/images/sshd &>/tmp/alp.sshd
[ "$?" != "0" ] && echo "quay.io/cloudwalker/alp.sshd image error" && exit 1
echo -e "quay.io/cloudwalker/alp.sshd image ok\n"

sudo podman rmi quay.io/cloudwalker/alp.fbs &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/alp.fbs removed"
sudo podman build --format=docker --no-cache --force-rm -t quay.io/cloudwalker/alp.fbs ~/tk/wulin/images/fbs &>/tmp/alp.fbs
[ "$?" != "0" ] && echo "quay.io/cloudwalker/alp.fbs image error" && exit 1
echo -e "quay.io/cloudwalker/alp.fbs image ok\n"

sudo podman rmi quay.io/cloudwalker/alp.tkadm &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/alp.tkadm removed"
sudo podman build --format=docker --build-arg="VER=25.01" --no-cache --force-rm -t quay.io/cloudwalker/alp.tkadm ~/tk/wulin/images/tkadm &>/tmp/alp.tkadm
[ "$?" != "0" ] && echo "quay.io/cloudwalker/alp.tkadm image error" && exit 1 
echo -e "quay.io/cloudwalker/alp.tkadm image ok\n"

sudo podman rmi quay.io/cloudwalker/alp.podman &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/alp.podman removed"
sudo podman build --build-arg VER=${VER} --format=docker --no-cache --force-rm -t quay.io/cloudwalker/alp.podman ~/tk/wulin/images/alp.podman/ &>/tmp/alp.podman
[ "$?" != "0" ] && echo -e "quay.io/cloudwalker/alp.podman image error\n" && exit 1
echo -e "quay.io/cloudwalker/alp.podman image ok\n"

sudo podman rmi quay.io/cloudwalker/oracle.mysql &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/oracle.mysql removed"
#sudo podman build --format=docker --build-arg="VER=8.0.37" --no-cache --force-rm -t quay.io/cloudwalker/oracle.mysql ~/tk/wulin/images/mysql &>/tmp/oracle.mysql
sudo podman build --format=docker --build-arg="VER=8.4.5" --no-cache --force-rm -t quay.io/cloudwalker/oracle.mysql ~/tk/wulin/images/mysql &>/tmp/oracle.mysql
[ "$?" != "0" ] && echo -e "quay.io/cloudwalker/oracle.mysql image error\n" && exit 1
echo -e "quay.io/cloudwalker/oracle.mysql image ok\n"

sudo podman rmi docker.io/library/alpine &>/dev/null
[ "$?" == "0" ] && echo "docker.io/library/alpine removed"
sudo podman pull docker.io/library/alpine &>/dev/null
sudo podman tag docker.io/library/alpine quay.io/cloudwalker/alpine &>/dev/null
[ "$?" == "0" ] && echo -e "quay.io/cloudwalker/alpine image ok\n"

sudo podman rmi docker.io/library/nginx &>/dev/null
[ "$?" == "0" ] && echo "docker.io/library/nginx removed"
sudo podman pull docker.io/library/nginx &>/dev/null
sudo podman tag docker.io/library/nginx quay.io/cloudwalker/nginx &>/dev/null
[ "$?" == "0" ] && echo -e "quay.io/cloudwalker/nginx image ok\n"

echo ""
sudo podman login quay.io

for n in alp.base alp.tkadm alp.fbs alp.sshd alp.podman nginx alpine oracle.mysql 
do
  sudo podman push quay.io/cloudwalker/${n} &>/dev/null
  if [ "$?" == "0" ]; then
     sudo podman rmi quay.io/cloudwalker/${n}
     echo "quay.io/cloudwalker/${n} push ok"
  fi 
done

# build taroko images
#[ ! -d ~/tk ] && sudo podman logout quay.io && exit 1
#for n in 1.29.14 1.32.5 1.33.1
#do
#  sudo podman rmi quay.io/cloudwalker/taroko:v${n} &>/dev/null
#  sudo podman build --build-arg VER=${n} --no-cache --force-rm -t quay.io/cloudwalker/taroko:v${n} \
#  ~/tk/wulin/images/taroko/ &>/dev/null
#  if [ "$?" == "0" ]; then
#     sudo podman push quay.io/cloudwalker/taroko:v${n} &>/dev/null
#     [ "$?" == "0" ] && echo "quay.io/cloudwalker/taroko:v${n} ok"
#  fi
#done

sudo podman logout quay.io
