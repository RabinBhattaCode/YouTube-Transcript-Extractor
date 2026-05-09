<p align="center">
  <img src="static/favicon.svg" alt="YouTube Transcript Extractor reverse play button logo" width="96">
</p>

# YouTube Transcript Extractor

A local Flask tool for extracting YouTube transcripts and exporting them into useful formats. The goal is to support research, content review, media planning, and faster creative workflow.

This project is intended for personal or authorised use. Users should follow YouTube's terms and only download or process content they have the right to use.

## Features

- Extract transcripts from YouTube videos.
- Process single videos, playlists, and supported YouTube URLs.
- Export transcript results as Markdown, text, CSV, or ZIP files.
- Download MP3 or MP4 outputs when the user has permission to do so.
- Convert supported local audio and document files.
- Run locally in the browser with a simple Flask interface.

## Tech Stack

![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-000000?style=for-the-badge&logo=flask&logoColor=white)
![yt-dlp](https://img.shields.io/badge/yt--dlp-111827?style=for-the-badge)
![HTML](https://img.shields.io/badge/HTML-E34F26?style=for-the-badge&logo=html5&logoColor=white)
![CSS](https://img.shields.io/badge/CSS-1572B6?style=for-the-badge&logo=css3&logoColor=white)

## Project Files

```text
app.py                    Main Flask application
proxy_gateway.py          Optional local proxy helper
requirements.txt          Python dependencies
package.json              Convenience npm scripts
templates/index.html      Browser UI
static/favicon.svg        App icon
scripts/                  Optional macOS launcher scripts
```

## Requirements

- Python 3.11 or newer
- `pip`
- `ffmpeg` for audio/video conversion and MP3/MP4 export

On macOS, `ffmpeg` can be installed with Homebrew:

```bash
brew install ffmpeg
```

## Install

Clone the repository:

```bash
git clone https://github.com/RabinBhattaCode/YouTube-Transcript-Extractor.git
cd YouTube-Transcript-Extractor
```

Create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the Python packages:

```bash
pip install -r requirements.txt
```

## Run

Start the local Flask app:

```bash
python3 app.py
```

Open this address in a browser:

```text
http://127.0.0.1:5050
```

You can also change the port:

```bash
PORT=5051 python3 app.py
```

## Optional macOS Launcher

The `scripts/` folder includes simple macOS helper scripts.

Start the app and open the browser:

```bash
zsh scripts/open-extractor.command
```

Check server status:

```bash
zsh scripts/launcher-control.command status
```

Stop the server:

```bash
zsh scripts/launcher-control.command stop
```

## Notes

The repository does not include local cache files, Python bytecode, built app bundles, or laptop setup archives. These files are generated locally and are not needed for users to run the source code.

If transcript extraction fails for a video, the likely reason is that the video has no available transcript, the transcript language is unsupported, or YouTube has limited access for that request.

