#!/bin/bash
# Run the PDV server in Docker. Builds the image "pdv" when needed, and
# removes the container when the server stops (Ctrl+C). The image stays.
#   ./run-docker.sh [server options] [VIDEO_FILE_OR_URL]
#   ./run-docker.sh --clean          remove the image
# A local video file is mounted read-only as /videos/<name>. --port N
# publishes port N instead of 8000. PDV_NO_UPDATE=1 skips the yt-dlp update.
set -e
IMAGE=pdv
DIR="$(dirname "$0")"

if [ "$1" = --clean ]; then
    docker rmi "$IMAGE"
    exit
fi

# The first build takes minutes; later ones only pick up source changes.
# Docker shows the progress of each build.
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Checking the server image. After a change to the server, this takes a moment."
else
    echo "Building the server image \"$IMAGE\". The first build takes a few minutes."
fi
docker build -t "$IMAGE" "$DIR"
echo "Starting the container."

port=8000
mount=()
args=()
while [ $# -gt 0 ]; do
    a="$1"
    shift
    case "$a" in
        --port=*) port="${a#--port=}" ;;
        --port)   port="$1"; args+=("$a" "$1"); shift; continue ;;
    esac
    if [ -f "$a" ]; then
        mount=(-v "$(cd "$(dirname "$a")" && pwd):/videos:ro")
        a="/videos/$(basename "$a")"
    fi
    args+=("$a")
done

tty=-i
[ -t 0 ] && tty=-it
exec docker run --rm --name pdv-server $tty -p "$port:$port" "${mount[@]}" \
    ${PDV_NO_UPDATE:+-e PDV_NO_UPDATE=1} "$IMAGE" "${args[@]}"
