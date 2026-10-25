# PETdisk Video (PDV)

![A Commodore 8296 shows a Star Fox title screen in PETSCII characters](docs/example1.jpg)
![A Commodore 8296 shows the title text of the film 2001 in PETSCII: "Beyond love, beyond death"](docs/example2.jpg)

PETdisk Video lets you search YouTube on a Commodore PET and watch the videos on its screen in PETSCII. A PC does the work: it gets the video, converts it to PETSCII while it plays, and streams it to the PET through a PETdisk MAX network drive. The PC can also play a video file. You control everything from the PET keyboard: search, pause, seek, loop a part of the video, and adjust the brightness, contrast, gamma and dithering.

PDV is a proof of concept, made just for fun. It is not a finished product, and it has no warranty. It runs on the stock, unmodified PETdisk MAX firmware. The PET client and the PC server do all the work. The PETdisk only does what it always does: it reads files from a web server for the PET.

Watching YouTube in black-and-white PETSCII at about seven frames per second on a business computer from 1980 is a niche interest. You were into it before it was cool. It will not become cool.

## PDV and the PETdisk MAX

The [PETdisk MAX](https://github.com/bitfixer/petdisk-max) by bitfixer is a storage device for the PET. It plugs into the IEEE-488 port and reads files from a microSD card, or over WiFi from "network drives" on web servers. A network drive is a directory on a web server with the PHP script `petdisk.php`.

PDV uses the network drive feature. `server.py` answers the same requests as `petdisk.php`, so the PETdisk MAX sees it as an ordinary network drive. One of its files, the video stream, never ends and is made while the PET reads it.

PDV is an independent project. It is not associated with the PETdisk MAX, its author or bitfixer.

## Requirements

Hardware:

- A Commodore PET or CBM with BASIC 2 or BASIC 4, and at least 16 KB of RAM. The PETdisk MAX does not support BASIC 1 (an original, not upgraded PET 2001).
  - `PDV80` is for PETs with an 80-column screen, for example the 8032, 8096 and 8296.
  - `PDV40` is for PETs with a 40-column screen, for example the 2001N, 3032 and 4032.
- A PETdisk MAX on the IEEE-488 port of the PET.
- A 2.4 GHz WiFi network. The PETdisk MAX does not support 5 GHz.
- A PC on the same network, which runs the server.

Software on the PC:

- PETdisk Video: the files of this project.
- Python 3 with the numpy and Pillow packages.
- ffmpeg, which decodes the video.
- yt-dlp, for YouTube. Keep it up to date: YouTube changes often.
- A JavaScript runtime for yt-dlp: deno, or Node.js. yt-dlp needs it for YouTube.
- Optional, only to change and build the PET client again: ACME (the 6502 assembler `acme`), and `petcat` from VICE. The built programs are already in `serve/`.

On Debian or Ubuntu, for example:

```
sudo apt install python3-numpy python3-pil ffmpeg nodejs
pip install --user yt-dlp
sudo apt install acme vice        # optional: to build the PET client
```

Tested with:

| Item | Version |
|---|---|
| PET | a real 8296 (BASIC 4), and an 8032 in VICE |
| PETdisk MAX | ESP32 version (v2), firmware build 78a8022 from 2022 |
| PC | Linux, Python 3.13, numpy 2.4, Pillow 12.1, ffmpeg 7.1, yt-dlp 2026.08.19, Node.js 22, ACME 0.97, VICE 3.9 |

Other PET models, BASIC 2 and the ATmega version (v1) of the PETdisk MAX are not tested.

## Limitations

- The frame rate is about 7 frames per second on 80 columns, and depends on how much of the picture changes. The PETdisk transfers about 3 to 4 KB per second, because the stock firmware makes a new HTTP request for every 512 bytes.
- Each hotkey causes a short pause. The stock firmware loses the open video file when the PET opens a second file, so the PET must open the video file again after each key.
- WiFi problems on the PETdisk side show as pauses of up to two seconds.
- There is no sound.
- The server has no password. Anyone on the same network can use it, so run it only on a network that you trust.

## Files

- `server.py` imitates `petdisk.php`. It serves the files in `serve/`, the endless virtual files `VIDEO80.SEQ` (80 columns) and `VIDEO40.SEQ` (40 columns), and the search screen files `RESULTS80.SEQ` and `RESULTS40.SEQ`.
- `convert.py` holds the PETSCII conversion, the ffmpeg decoding and the yt-dlp calls that the server uses.
- `petscii_unicode.py` maps PET screen codes to Unicode characters for the server's terminal view.
- `client.a` is the machine code part of the PET client, in ACME assembly.
- `stub.bas.in` is the BASIC part of the PET client: the main loop and the search screen.
- `serve/PDV80.PRG` and `serve/PDV40.PRG` are the built PET programs for 80 and 40 columns.
- `build.sh` builds them again from `client.a` and `stub.bas.in`, and also `build/PDV8.PRG`, a test version that reads from device 8.
- `build/timing.log` gets one line per request from the server. It grows without limit. Delete it when you do not need it.

## One-time PETdisk MAX setup

Edit `PETDISK.CFG` on the SD card. Replace the IP address with the LAN address of this PC:

```
8,SD0
10,192.168.X.Y:8000/petdisk.php
ssid,<2.4 GHz network>
password,<password>
```

Power-cycle the PET. While the server runs, `LOAD"$",10` must list the programs `PDV80` and `PDV40`. The video and search files are not listed, because the player opens them by name.

## Start the server

The built PET programs are in `serve/`. Only if you change `client.a` or `stub.bas.in`, build them again:

```
./build.sh
```

Start the server on this PC in one of these ways:

```
python3 server.py
python3 server.py video.mp4
python3 server.py 'https://www.youtube.com/watch?v=...'
```

Without a video, the PET goes directly to the YouTube search. A video loops, also from a URL.

Start values are options, for example:

```
python3 server.py video.mp4 --brightness 0.1 --contrast 1.5 --gamma 1.2 --dither 2
```

PDV selects the graphics character set on every PET. On BASIC 4 machines it also switches the screen to graphics mode with `PRINT CHR$(142)`. On 80-column machines this gives 8-line character rows without gaps between them, where text mode uses 10-line rows. BASIC 1 and 2 do not have this mode switch.

`--zoom fill` or `--zoom stretch` sets the start zoom mode. The default is `fit`.

`--aspect` sets the shape of the PET picture on the monitor, as width divided by height. The default, 1.8, is an estimate for an 80-column PET. If circles look too tall, raise it. If they look too wide, lower it. On a 40-column PET, start with `--aspect 1.33`.

`python3 server.py -h` lists all options. The default port is 8000. To use another port, start the server with `--port`, for example `--port 8080`, and use the same port in `PETDISK.CFG`.

## Start PDV on the PET

The server must run before you start PDV. Use `PDV80` on a PET with an 80-column screen and `PDV40` on a PET with a 40-column screen.

1. Optional: type `LOAD"$",10` and `LIST` to make sure that the PET can see the server. The listing shows the programs `PDV80` and `PDV40`.
2. Type `LOAD"PDV80",10` and press RETURN. On a 40-column PET, type `LOAD"PDV40",10`.
3. Type `RUN` and press RETURN. The video or the search screen shows after a few seconds.

## Hotkeys

These keys work on the PET and in the server terminal. In the terminal, lowercase is the key without the shift key, and uppercase is the key with the shift key.

| Key | Without shift key | With shift key |
|---|---|---|
| B | brightness up | brightness down |
| C | contrast up | contrast down |
| G | gamma up | gamma down |
| D | next dither mode | next dither mode |
| R | reset brightness, contrast, gamma and dithering to the start values | same |
| Z | next zoom mode: fit, fill, stretch | same |
| SPACE or P | pause or continue | same |
| 1 | back 1 minute | rewind to the start (`!`) |
| 2 | back 10 seconds | |
| 3 | forward 10 seconds | |
| 4 | forward 1 minute | |
| 5 | one frame back, and pause | |
| 6 | one frame forward, and pause | |
| 8 | mark the loop start | |
| 9 | mark the loop end | |
| 0 | go to the loop start | |
| 7 | clear the loop marks | |
| S | YouTube search screen | same |
| H | settings, hotkeys and timeline on or off | same |

In the server terminal, the cursor keys also seek: up is forward 1 minute, down is back 1 minute, right is forward 10 seconds and left is back 10 seconds.

H switches the settings, the hotkeys and the timeline on the top lines of the PET screen on or off. While they are on, they show each change at once. The timeline shows the current time at the current position, and the loop marks as `[` and `]`.

Z switches between three zoom modes. Fit shows the whole video, with black bars where its shape differs from the PET screen. Fill uses the whole screen and cuts off the edges of the video. Stretch shows the whole video on the whole screen, distorted. A zoom change takes a moment, because the server opens the video again at the same position.

To adjust the picture on a still frame, pause first. The server converts the paused frame again after each change. Seeking also works while paused. 5 and 6 step one frame (1/10 second at the default frame rate) and pause the video if it plays.

Seeking wraps around at the start and the end of the video. When playback reaches the loop end, it jumps to the loop start, or to the start of the video if no loop start is set. With only a loop start, the video loops back to the loop start at its end. If the loop end is earlier than the loop start, the server swaps them.

The settings, the loop marks and the playback position stay the same when the server restarts itself.

## YouTube search

![A Commodore 8296 shows the YouTube search screen: 20 results for Buck Rogers, with an input field below them](docs/youtube.jpg)

Press S on the PET. The PET shows the results of the last search, 20 to a page, and an input field below them. The video stops while the search screen shows. Type one of these entries and press RETURN:

- A result number plays that video. The numbers continue across pages, so 23 is the third result on page 2.
- N shows the next page. P shows the previous page. When you go past the results that the server has, it gets more from YouTube.
- Other text is a new search.
- An empty entry goes back to the video, which continues where it stopped.

## The server terminal

In a terminal, the server shows a full-screen view:

1. The frame that it sent last, or the search screen, with the PET characters approximated by Unicode characters.
2. The same settings, hotkeys and timeline that the PET shows.
3. Keys that work only in the terminal.
4. The frame rate, the transfer rate, and the min, 90th percentile and max of frame sizes and request gaps over the last 5 seconds.
5. The newest event, for example a key press or a yt-dlp error.

## How the PET talks to the server

The PET can only reach the server by opening files through the PETdisk. The PETdisk turns each OPEN into a web request that contains the filename. A filename can be at most 16 characters.

- A hotkey makes the PET close the video file, open a file named `K<key code>`, and open the video file again. The playback clock continues, so the video does not start again from the beginning.
- The search screen reads the file `RESULTS80` (or `RESULTS40`), which the server makes from the current results.
- A result number, N or P goes to the server as a file named `C<entry>`, for example `C23` or `CN`.
- Search text goes in pieces: files named `Q` plus up to 15 characters, then a file named only `Q`, which starts the search.
- When there is nothing to play, the video stream starts with the byte $FC. The player then goes to the search screen.

## Do not stop the server while the PET plays

If the PETdisk MAX cannot connect to the server, the stock firmware reads a null pointer and the PETdisk crashes. The PET then waits forever. Power-cycle the PET to recover.

- When `server.py` or `convert.py` changes, the server restarts itself. It passes its listening socket and the stream state on to the new process, so the PET sees no interruption.
- Stop the server with Ctrl-C only when the PET does not play video.

![HAL 9000's red eye from the film 2001: A Space Odyssey, shown in green PETSCII on a Commodore 8296](docs/hal.jpg)

*I'm sorry Dave, I can not render colors.*
