#!/bin/sh
# In the container: put the mounted ROMs where VICE looks for them, start
# a virtual X display, then run the VICE tests. Arguments go to unittest.
# (xvfb-run is not used: it hangs when it is process 1 of a container.)
set -e
cp /roms/PET/*.bin /usr/share/vice/PET/
cp /roms/DRIVES/*.bin /usr/share/vice/DRIVES/
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp >/dev/null 2>&1 &
export DISPLAY=:99
for i in $(seq 50); do
    [ -e /tmp/.X11-unix/X99 ] && break
    sleep 0.1
done
exec python3 -m unittest discover -s /src/tests -p test_vice.py "$@"
