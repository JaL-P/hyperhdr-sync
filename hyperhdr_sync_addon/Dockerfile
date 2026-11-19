FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.0

# The bridge and its rendering engine come from the latest code on this repository's main
# branch, so a merged change reaches the add-on without a release tag. Supervisor passes
# config.yaml's version as BUILD_VERSION; it is part of the download step, so every new
# add-on version downloads main afresh instead of reusing a cached copy.
ARG BUILD_VERSION=0.4.1
ARG BUILD_REF=main

WORKDIR /app
RUN echo "Building add-on ${BUILD_VERSION} from ${BUILD_REF}" \
    && curl -fsSL "https://github.com/JaL-P/hyperhdr-sync/archive/refs/heads/${BUILD_REF}.tar.gz" \
        | tar -xz --strip-components=1 \
    && rm -rf tests scripts .github .devcontainer .vscode \
    && pip3 install --no-cache-dir --only-binary=:all: -r standalone/requirements.txt

COPY run.py /app/run.py

# S6 overlay (the base image's entrypoint) runs this as the main process and stops the
# container when it exits. with-contenv hands it the container's variables, among them
# SUPERVISOR_TOKEN, which S6 otherwise withholds (run.py also reads them itself).
CMD ["/usr/bin/with-contenv", "python3", "/app/run.py"]
