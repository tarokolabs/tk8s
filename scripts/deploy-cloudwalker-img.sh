#!/usr/bin/env bash

VER="26.01"

# sudo rm /usr/bin/mc &>/dev/null
# sudo curl -s https://dl.min.io/client/mc/release/linux-amd64/mc -o /usr/bin/mc &>/dev/null
# if [ "$?" == "0" ]; then
#   sudo chmod +x /usr/bin/mc
#   cp -rP /usr/bin/mc ${TK_HOME}/wulin/images/base/
#   cp -rP /usr/bin/mc ${TK_HOME}/wulin/images/jkagent/
#   echo "minio client ok"
#fi

sudo podman rmi quay.io/cloudwalker/alp.base &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/alp.base removed"
sudo podman build --format=docker --build-arg="VER=3.23.4" --no-cache --force-rm -t quay.io/cloudwalker/alp.base ${TK_HOME}/wulin/images/base &>/tmp/alp.base
[ "$?" != "0" ] && echo -e "quay.io/cloudwalker/alp.base image error\n" && exit 1
echo -e "quay.io/cloudwalker/alp.base image ok\n"

sudo podman rmi quay.io/cloudwalker/alp.sshd &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/alp.sshd removed"
sudo podman build --format=docker --no-cache --force-rm -t quay.io/cloudwalker/alp.sshd ${TK_HOME}/wulin/images/sshd &>/tmp/alp.sshd
[ "$?" != "0" ] && echo "quay.io/cloudwalker/alp.sshd image error" && exit 1
echo -e "quay.io/cloudwalker/alp.sshd image ok\n"

sudo podman rmi quay.io/cloudwalker/alp.fbs &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/alp.fbs removed"
sudo podman build --format=docker --no-cache --force-rm -t quay.io/cloudwalker/alp.fbs ${TK_HOME}/wulin/images/fbs &>/tmp/alp.fbs
[ "$?" != "0" ] && echo "quay.io/cloudwalker/alp.fbs image error" && exit 1
echo -e "quay.io/cloudwalker/alp.fbs image ok\n"

sudo podman rmi quay.io/cloudwalker/alp.tkadm &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/alp.tkadm removed"
sudo podman build --format=docker --build-arg="VER=26.01" --no-cache --force-rm -t quay.io/cloudwalker/alp.tkadm ${TK_HOME}/wulin/images/tkadm &>/tmp/alp.tkadm
[ "$?" != "0" ] && echo "quay.io/cloudwalker/alp.tkadm image error" && exit 1 
echo -e "quay.io/cloudwalker/alp.tkadm image ok\n"

sudo podman rmi quay.io/cloudwalker/usdip34 &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/usdip34 removed"
sudo podman build --build-arg VER=${VER} --format=docker --no-cache --force-rm -t quay.io/cloudwalker/usdip34 ${TK_HOME}/wulin/images/usdt.hdp341/ &>/tmp/usdip34
[ "$?" != "0" ] && echo -e "quay.io/cloudwalker/usdip34 image error\n" && exit 1
echo -e "quay.io/cloudwalker/usdip34 image ok\n"

sudo podman rmi quay.io/cloudwalker/alp.podman &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/alp.podman removed"
sudo podman build --build-arg VER=${VER} --format=docker --no-cache --force-rm -t quay.io/cloudwalker/alp.podman ${TK_HOME}/wulin/images/alp.podman/ &>/tmp/alp.podman
[ "$?" != "0" ] && echo -e "quay.io/cloudwalker/alp.podman image error\n" && exit 1
echo -e "quay.io/cloudwalker/alp.podman image ok\n"

sudo podman rmi quay.io/cloudwalker/oracle.mysql &>/dev/null
[ "$?" == "0" ] && echo "quay.io/cloudwalker/oracle.mysql removed"
#sudo podman build --format=docker --build-arg="VER=8.0.37" --no-cache --force-rm -t quay.io/cloudwalker/oracle.mysql ${TK_HOME}/wulin/images/mysql &>/tmp/oracle.mysql
sudo podman build --format=docker --build-arg="VER=8.4.5" --no-cache --force-rm -t quay.io/cloudwalker/oracle.mysql ${TK_HOME}/wulin/images/mysql &>/tmp/oracle.mysql
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

for n in alp.base alp.tkadm alp.fbs alp.sshd alp.podman nginx alpine oracle.mysql usdip34 
do
  sudo podman push quay.io/cloudwalker/${n} &>/dev/null
  if [ "$?" == "0" ]; then
     sudo podman rmi quay.io/cloudwalker/${n}
     echo "quay.io/cloudwalker/${n} push ok"
  fi 
done

sudo podman logout quay.io

