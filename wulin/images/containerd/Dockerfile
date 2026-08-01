ARG VER=0.0
FROM quay.io/cloudwalker/alp.base
COPY crictl.yaml /etc/crictl.yaml 
RUN apk update && apk upgrade && apk add --no-cache containerd containerd-ctr cri-tools 

CMD ["/usr/bin/containerd"]
    
