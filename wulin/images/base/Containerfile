ARG VER=0.0
FROM alpine:${VER}
COPY mc ash.rc /usr/bin/

RUN apk update && apk upgrade && apk add --no-cache nano sudo wget curl python3 py3-pip mysql-client git \ 
    openssh-client-default nfs-utils go sshpass tree bash shadow procps bind-tools util-linux coreutils \
    binutils findutils grep tzdata libcap jq yq zip unzip fuse shellcheck iptables iperf3 stress-ng \
    postgresql-client && \

    touch /usr/bin/ash.init /usr/bin/ash.service /usr/bin/ash.start && \
    passwd -dl root && \

    cp /usr/share/zoneinfo/Asia/Taipei /etc/localtime && \

    # install s3fs-fuse
    apk add s3fs-fuse --no-cache --repository http://dl-3.alpinelinux.org/alpine/edge/community/ --allow-untrusted && \
    apk add mailcap --update-cache --repository http://dl-3.alpinelinux.org/alpine/edge/main/ --allow-untrusted && \
    echo 'user_allow_other' >> /etc/fuse.conf 
    
    # install JuiceFS Client
    # curl -sSL https://d.juicefs.com/install | sh -

ENV ENV=/usr/bin/ash.rc

#CMD ["/bin/ash"]
CMD ["/bin/sleep","infinity"]
