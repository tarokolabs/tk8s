export POD_NAMESPACE=default
export KUBERNETES_SERVICE_PORT=443
export KUBERNETES_SERVICE_HOST=kubernetes.default
export KUBERNETES_SERVICE_PORT_HTTPS=443
export NOW="--force --grace-period 0"
export KUBE_EDITOR="nano"
export TZ=Asia/Taipei
export HOSTARG='reg=dkreg.taroko:5000,user=bigred,pass=bigred,tls=disabled'
export KCN=$(cat /opt/zfs/tkadm/cn.txt)
export TKWK=$(echo ~/wulin/wk)

if [ -f '/opt/zfs/tkadm/cn.txt' ]; then
   export PS1='[$(cat /opt/zfs/tkadm/cn.txt)]\u@\h:\w$ '
else
   export PS1='\u@\h:\w$ '
fi

alias kg='kubectl get'
alias ka='kubectl apply'
alias kd='kubectl delete'
alias kt='kubectl top'
alias ks='kubectl get all -n kube-system'
alias kt='kubectl top'
alias kk='kubectl krew'
alias kp="kubectl get pods -o wide -A | sed 's/(.*)//' | tr -s ' ' | cut -d ' ' -f 1-4,7,8 | column -t"
alias dkimg='curl -X GET -s -u bigred:bigred http://dkreg.taroko:5000/v2/_catalog | jq ".repositories[]"'
alias kgip="kubectl get pod --template '{{.status.podIP}}'"
alias pingdup='sudo arping -D -I eth0 -c 2 '
alias ping='ping -c 4'
alias dir='ls -alh'
alias docker='sudo /usr/bin/podman'

sudo mount --make-rshared /

if [ "$USER" == "bigred" ]; then
   which argo &>/dev/null
   if [ "$?" != "0" ]; then
      curl -sLO https://github.com/argoproj/argo-workflows/releases/latest/download/argo-linux-amd64.gz
      if [ "$?" == "0" ]; then
         gunzip argo-linux-amd64.gz &>/dev/null
         [ "$?" == "0" ] && chmod +x argo-linux-amd64 && sudo mv ./argo-linux-amd64 /usr/local/bin/argo
      fi
   fi

#   mc config host ls | grep mios &>/dev/null
#   if [ "$?" != "0" ]; then
#      mc config host add mios http://miniosnsd.s3-system:9000 minio minio123 &>/dev/null
#      [ "$?" == "0" ] && echo "mios ok"
#   fi

   kubectl delete pod -A --field-selector=status.phase==Succeeded | grep 'No resources found' &>/dev/null
   [ "$?" != "0" ] && echo "delete all completed pods"

   kubectl delete pod -A --field-selector=status.phase==Failed | grep 'No resources found' &>/dev/null
   [ "$?" != "0" ] && echo "delete all errored pods"
   echo ""

   #if [ -f wulin/bin/${KCN}-kpasswd ]; then
   #   n=$(cat wulin/bin/${KCN}-kpasswd | cut -d ':' -f 1)
   #   for u in $n
   #   do
   #     [ -d "/home/$u" ] && sudo chown -R $u:$u /home/$u
   #   done
   #fi

   sudo chmod 700 -R /home/bigred/.ssh
   sudo rm /home/bigred/.ssh/known_hosts.old &>/dev/null
   sudo rm /home/bigred/.ssh/known_hosts &>/dev/null

   sudo chmod u+s /usr/bin/newuidmap
   sudo chmod u+s /usr/bin/newgidmap

   export PATH=/home/bigred/wulin/wk/dt/bin/:/home/bigred/wulin/wk/dip/bin/:$PATH
fi

