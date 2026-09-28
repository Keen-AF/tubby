# Tubby

A tiny self-hosted web page for [yt-dlp](https://github.com/yt-dlp/yt-dlp). Paste a link, pick a quality, hit **Grab**, then **Save** the file to whatever device you're on.

Files are **amnesiac**. Each browser window gets its own folder on the server. That folder is wiped 5 minutes after the window closes, or 5 minutes after it stops checking in (crash, sleep). Reloading the page, or reopening a tab you closed by mistake, keeps your files. Restarting the server wipes everything.

## Run it

The only thing the host needs is Docker. yt-dlp, ffmpeg and deno (the JS runtime yt-dlp needs for YouTube) all come bundled in the image, which builds on both amd64 and arm64.

```sh
docker compose up -d --build
```

Then open `http://<server-ip>:8080`.

## Configure

Set these under `environment:` in `docker-compose.yml`:

| Variable | Default | What it does |
|---|---|---|
| `TUBBY_PASSWORD` | *(unset)* | Turns on a login page. When unset, anyone on your network can use it. |
| `TUBBY_SECRET` | random per start | Signs login cookies. Set it to stay logged in across restarts. |
| `TUBBY_GRACE` | `300` | Seconds files are kept after the window closes. |
| `TUBBY_TIMEOUT` | `300` | Seconds files are kept after the window goes quiet with no close signal. |
| `TUBBY_CONCURRENCY` | `2` | Downloads that run at the same time. The rest wait in a queue. |
| `TUBBY_COOKIES` | *(unset)* | Path to a Netscape `cookies.txt` file. Use it if YouTube says "confirm you're not a bot". |
| `TUBBY_PORT` / `TUBBY_HOST` | `8080` / `0.0.0.0` | Where the server listens. |

## Updating yt-dlp

YouTube changes things often and old yt-dlp versions stop working. To update, bump `YTDLP_VERSION` in `docker-compose.yml` to the [latest release](https://github.com/yt-dlp/yt-dlp/releases), then:

```sh
docker compose up -d --build
```

The build runs `server.py --check`, so an image with missing tools fails to build rather than failing on your first download.

## Quality presets

| Preset | yt-dlp format |
|---|---|
| Best | `bv*+ba/b` merged to mp4 |
| 1080p / 720p | best video at or below that height, plus best audio, merged to mp4 |
| Audio | best audio converted to mp3 |

Playlists are ignored; only the video in the link is downloaded.

## Handy extras

- `http://<server>:8080/?url=<video-url>` opens with the box already filled in. You can use it from a bookmarklet:
  `javascript:location='http://<server>:8080/?url='+encodeURIComponent(location.href)`
- `/healthz` returns `ok`, for monitoring.

## Run without Docker

This needs `yt-dlp`, `ffmpeg` and `deno` on your PATH, and Python 3.11 or newer. Nothing needs to be installed with pip.

```sh
python3 server.py --check   # confirms the tools are found
python3 server.py
```

## Security notes

- This is meant for a LAN. If you expose it to the internet, set `TUBBY_PASSWORD` and put it behind HTTPS with a reverse proxy such as Caddy.
- A file can only be downloaded by the browser window that created it.
- URLs are passed to yt-dlp as arguments, never through a shell.
