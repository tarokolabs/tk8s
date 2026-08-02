FROM quay.io/cloudwalker/alp.base

# containers.conf  - The container engine (podman, buildah) configuration file specifies
# default configuration options and command-line flags for container engines.
COPY "containers.conf" "/etc/containers/"

RUN apk update && apk upgrade && \
    apk add --no-cache fuse-overlayfs podman && \
    curl -L https://github.com/regclient/regclient/releases/latest/download/regctl-linux-amd64 >regctl && \
    chmod 755 regctl && mv regctl /usr/local/bin/

CMD ["/bin/sleep","infinity"]
