# PETdisk Video server image. Build: docker build -t pdv .
# Run:   docker run --rm -it -p 8000:8000 pdv [options] [VIDEO_OR_URL]
#
# The image uses the PET programs in serve/ as they are in the repository.
# After a change to client.a or stub.bas.in, run ./build.sh first.
# python:3.13-slim is Debian trixie with Python 3.13.
FROM python:3.13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir numpy pillow
# yt-dlp as the standalone program, which updates itself at every start
# (docker-entrypoint.sh): YouTube changes often, the image does not.
RUN curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
        -o /usr/local/bin/yt-dlp \
    && chmod +x /usr/local/bin/yt-dlp
WORKDIR /app
COPY server.py convert.py petscii_unicode.py docker-entrypoint.sh ./
COPY serve ./serve
RUN mkdir build                     # timing.log and the state for restarts
# Video files: mount a directory and name the file, for example
#   docker run --rm -it -p 8000:8000 -v "$PWD:/videos" pdv /videos/clip.mp4
VOLUME /videos
EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
