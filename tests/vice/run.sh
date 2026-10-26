#!/bin/bash
# Run the optional VICE tests in a Docker container, on a virtual display:
# no emulator window opens on the desktop.
#   tests/vice/run.sh [unittest options]     for example: -v -k frames_80
# The PET and drive ROMs come from a local VICE: the Flathub VICE
# (net.sf.VICE), or the directory in PDV_VICE_ROMS, with PET/ and DRIVES/.
set -e
IMAGE=pdv-vice-test
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"

roms="${PDV_VICE_ROMS:-}"
if [ -z "$roms" ]; then
    for d in /var/lib/flatpak/app/net.sf.VICE/current/active/files/share/vice \
             "$HOME/.local/share/flatpak/app/net.sf.VICE/current/active/files/share/vice"; do
        if [ -d "$d/PET" ]; then
            roms="$d"
            break
        fi
    done
fi
if [ -z "$roms" ] || [ ! -f "$roms/PET/kernal-4.901465-22.bin" ]; then
    echo "The PET ROMs were not found. Install the Flathub VICE (net.sf.VICE), or set" >&2
    echo "PDV_VICE_ROMS to a VICE data directory that has PET/ and DRIVES/." >&2
    exit 1
fi
roms="$(realpath "$roms")"          # Docker mounts the target of a symbolic link

# The first build takes some minutes; later ones only pick up changes.
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -q -t "$IMAGE" "$here" >/dev/null
else
    docker build -t "$IMAGE" "$here"
fi
exec docker run --rm --init -v "$root:/src:ro" -v "$roms:/roms:ro" "$IMAGE" "$@"
