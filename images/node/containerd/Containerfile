ARG VER=0.0
FROM docker.io/kindest/node:v${VER}
RUN apt-get update && apt-get upgrade -y 
RUN apt-get install -y nano net-tools curl wget iputils-ping ipvsadm jq 
RUN rm /kind/manifests/*.yaml
