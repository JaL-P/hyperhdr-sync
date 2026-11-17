FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.0

# The bridge and its rendering engine come from this repository's release tag of the same
# version as the add-on (config.yaml), so a new add-on version is a new release.
ARG BRIDGE_REF=v0.2.0

WORKDIR /app
RUN curl -fsSL "https://github.com/JaL-P/hyperhdr-sync/archive/refs/tags/${BRIDGE_REF}.tar.gz" \
        | tar -xz --strip-components=1 \
    && rm -rf tests scripts .github .devcontainer .vscode \
    && pip3 install --no-cache-dir --only-binary=:all: -r standalone/requirements.txt

COPY run.py /app/run.py

# S6 overlay (the base image's entrypoint) runs this as the main process and stops the
# container when it exits.
CMD ["python3", "/app/run.py"]
