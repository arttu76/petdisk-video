# Headless VICE for the optional emulator tests. tests/vice/run.sh builds
# and runs it. VICE draws on a virtual X display (Xvfb) in the container,
# so no window opens on the desktop. The PET and drive ROMs are not in the
# image: run.sh mounts them read-only from a local VICE as /roms.
FROM debian:trixie-slim
RUN sed -i 's/^Components: main$/Components: main contrib/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends vice xvfb libgl1-mesa-dri \
       python3 python3-numpy python3-pil \
    && rm -rf /var/lib/apt/lists/*
# test_vice.py runs only where this is set
ENV PDV_VICE_DOCKER=1 PYTHONDONTWRITEBYTECODE=1
COPY entrypoint.sh /entrypoint.sh
WORKDIR /src
ENTRYPOINT ["/entrypoint.sh"]
