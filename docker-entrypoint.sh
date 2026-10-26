#!/bin/sh
# Update yt-dlp, then run the server with the given arguments.
# The update needs the network; when it fails, the installed version is used.
if [ -z "$PDV_NO_UPDATE" ]; then
    echo "Updating yt-dlp. This can take up to a minute. PDV_NO_UPDATE=1 skips it."
    timeout 60 yt-dlp -U 2>&1 | grep -v "^$" | sed 's/^/  yt-dlp: /' || true
fi
echo "Starting the server."
exec python3 server.py "$@"
