#!/bin/bash

nc -z -w 2 dkreg.kube-system 22100
[ "$?" != "0" ] && echo "Kube-kadm not exist" && exit 1

ssh localhost -p 22100 sudo chown -R bigred:wheel /home/bigred/wulin/
scp -r -P 22100 kind/bin bigred@dkreg.kube-system:/home/bigred/wulin &>/dev/null
[ "$?" == "0" ] && echo "kind/wulin/bin ok"

scp -r -P 22100 kind/wulin/images bigred@dkreg.kube-system:/home/bigred/wulin &>/dev/null
[ "$?" == "0" ] && echo "kind/wulin/images ok"

scp -r -P 22100 kind/wulin/wk bigred@dkreg.kube-system:/home/bigred/wulin &>/dev/null
[ "$?" == "0" ] && echo "wulin/wk ok"
