FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.0

# The bridge and its rendering engine come from this repository's release tag of the same
# version as the add-on: Supervisor passes config.yaml's version as BUILD_VERSION, so every
# add-on version needs a tag v<version>. The default is for building by hand.
ARG BUILD_VERSION=0.3.4

WORKDIR /app
RUN curl -fsSL "https://github.com/JaL-P/hyperhdr-sync/archive/refs/tags/v${BUILD_VERSION}.tar.gz" \
        | tar -xz --strip-components=1 \
    && rm -rf tests scripts .github .devcontainer .vscode \
    && pip3 install --no-cache-dir --only-binary=:all: -r standalone/requirements.txt

COPY run.py /app/run.py

# S6 overlay (the base image's entrypoint) runs this as the main process and stops the
# container when it exits. with-contenv hands it the container's variables, among them
# SUPERVISOR_TOKEN, which S6 otherwise withholds (run.py also reads them itself).
CMD ["/usr/bin/with-contenv", "python3", "/app/run.py"]
