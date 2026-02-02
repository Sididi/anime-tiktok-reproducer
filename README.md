# Anime TikTok Reproducer

A web application to remaster TikTok videos featuring anime content for other short-form platforms. The app automates the process of detecting scenes, matching them to original anime episodes, handling transcription, and generating Adobe Premiere Pro projects.

## Features

### Core Workflow

1. **Project Setup** - Enter TikTok URL and select source anime from indexed library
2. **Video Download** - Automatic download via yt-dlp with progress tracking
3. **Scene Detection** - Automatic scene detection using PySceneDetect
4. **Scene Validation** - Interactive timeline editor to refine scene boundaries
5. **Anime Matching** - SSCD+FAISS-powered matching to find original anime clips
6. **Match Validation** - Side-by-side comparison of TikTok and source clips
7. **Transcription** - Word-level transcription using WhisperX
8. **Script Restructure** - AI-assisted script rewriting with duration constraints
9. **Processing** - Auto-editor silence removal and Premiere Pro project generation

### Key Components

- **Real-time Video Timeline Editor** - Synchronized video player with interactive scene timeline
- **Anime Library Management** - Index and search anime episodes with SSCD embeddings
- **SSE Progress Streaming** - Real-time progress updates for long-running operations
- **Premiere Pro Integration** - Automated JSX script generation for project setup

## Architecture

```
├── backend/                 # FastAPI Python backend
│   ├── app/
│   │   ├── api/routes/      # API endpoints
│   │   ├── models/          # Pydantic data models
│   │   ├── services/        # Business logic
│   │   └── config.py        # Configuration
│   └── data/
│       ├── cache/           # Temporary files
│       └── projects/        # Project data storage
├── frontend/                # React + TypeScript frontend
│   └── src/
│       ├── api/             # API client
│       ├── components/      # UI components
│       ├── pages/           # Page components
│       ├── stores/          # Zustand state management
│       └── types/           # TypeScript definitions
└── modules/
    └── anime_searcher/      # SSCD+FAISS anime search submodule
```

## Prerequisites

- **Python 3.11+**
- **Node.js 18+** (managed via fnm recommended)
- **pixi** - Package manager for GPU-accelerated Python environments
- **NVIDIA GPU** with CUDA 12.4+ for GPU acceleration
- **Adobe Premiere Pro 2025** - For final project generation

### Python Dependencies (Managed by pixi)

- FastAPI + Uvicorn
- PySceneDetect
- WhisperX (GPU-accelerated)
- yt-dlp
- auto-editor
- OpenCV
- Pillow
- PyTorch + CUDA
- FAISS-GPU (for anime_searcher)

### Frontend Dependencies

- React 18
- TypeScript
- Vite
- Zustand (state management)
- Tailwind CSS
- Lucide React (icons)

## Installation

### 1. Install pixi (Arch Linux)

```bash
# Install pixi using the official installer
curl -fsSL https://pixi.sh/install.sh | bash

# Or using yay/paru from AUR
paru -S pixi
```

### 2. Clone the repository

```bash
git clone --recursive https://github.com/your-repo/anime-tiktok-reproducer.git
cd anime-tiktok-reproducer
```

### 3. Install all dependencies with pixi

```bash
# This installs all Python dependencies including PyTorch with CUDA, WhisperX, FAISS-GPU, etc.
pixi install

# The SSCD model should be in modules/anime_searcher/
# Download if not present: sscd_disc_mixup.torchscript.pt
```

### 4. Frontend Setup

```bash
cd frontend

# Install dependencies
npm install
```

## Configuration

### Backend Configuration

Environment variables or defaults in `backend/app/config.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `DATA_DIR` | `backend/data` | Data storage directory |
| `ANIME_LIBRARY_PATH` | `modules/anime_searcher/library` | Indexed anime library path |
| `ANIME_SEARCHER_PATH` | `modules/anime_searcher` | Path to anime_searcher module |
| `SSCD_MODEL_PATH` | Auto-detected | Path to SSCD model file |

## Usage

### Starting the Application

**Option 1: Using the dev script**

```bash
./scripts/dev.sh
```

**Option 2: Using pixi tasks**

```bash
# Start backend only
pixi run backend

# Or start in a subshell
pixi shell
uvicorn app.main:app --reload --port 8000

# Terminal 2: Frontend
cd frontend
npm run dev
```

Access the app at `http://localhost:5173`

### Workflow Guide

#### 1. Project Setup

1. Enter the TikTok URL you want to remaster
2. Select an anime from the indexed library dropdown
   - Use the search to filter anime
   - Or click "Index New Anime" to add a new series

#### 2. Indexing New Anime

If your anime isn't indexed yet:

1. Click "Index New Anime" in the dropdown
2. Enter the path to the folder containing episode video files
3. Optionally provide a custom name (defaults to folder name)
4. Click "Index & Start" - indexing runs at 2 FPS for efficiency
5. Progress is displayed in real-time

#### 3. Scene Validation

After download completes, you'll enter scene validation:

- **Timeline Navigation**: Click to seek, right-click to jump to scene start
- **Play Controls**: Play/Pause, frame-by-frame navigation
- **Scene Editing**:
  - Split: Divide scene at current cursor position
  - Merge: Combine with previous/next scene
  - Set Start/End: Adjust boundaries to cursor position
  - Manual timing input via text fields

#### 4. Match Validation

Review matched anime clips:

- Side-by-side video comparison
- Duration and speed ratio display
- Confirm or manually adjust matches
- Select alternative candidates if auto-match failed

#### 5. Transcription

1. Select language (auto, English, Spanish, French)
2. Start transcription
3. Review and edit any misheard words
4. Confirm transcription

#### 6. Script Restructure

1. Copy the generated prompt for your LLM
2. Paste the restructured script JSON
3. Upload the new TTS audio file
4. Process for final output

### CLI Commands (anime_searcher)

The anime_searcher submodule provides CLI tools:

```bash
# List indexed anime
pixi run anime-search list /path/to/library

# Index new anime
pixi run anime-search index /path/to/library --fps 2

# Search for a frame
pixi run anime-search search /path/to/library image.png --flip --series "Anime Name"
```

## API Reference

### Projects

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/projects` | Create new project |
| GET | `/api/projects` | List all projects |
| GET | `/api/projects/{id}` | Get project details |
| PATCH | `/api/projects/{id}` | Update project settings |
| DELETE | `/api/projects/{id}` | Delete project |

### Video & Scenes

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/projects/{id}/download` | Download TikTok (SSE) |
| GET | `/api/projects/{id}/video` | Stream video file |
| GET | `/api/projects/{id}/video/info` | Get video metadata |
| POST | `/api/projects/{id}/scenes/detect` | Detect scenes (SSE) |
| GET | `/api/projects/{id}/scenes` | Get scenes |
| PUT | `/api/projects/{id}/scenes` | Update scenes |
| POST | `/api/projects/{id}/scenes/{idx}/split` | Split scene |
| POST | `/api/projects/{id}/scenes/merge` | Merge scenes |

### Matching

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/projects/{id}/matches/find` | Find matches (SSE) |
| GET | `/api/projects/{id}/matches` | Get matches |
| PUT | `/api/projects/{id}/matches/{idx}` | Update match |

### Transcription

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/projects/{id}/transcription/start` | Start transcription (SSE) |
| GET | `/api/projects/{id}/transcription` | Get transcription |
| PUT | `/api/projects/{id}/transcription` | Update transcription |
| POST | `/api/projects/{id}/transcription/confirm` | Confirm transcription |

### Anime Library

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/anime/list` | List indexed anime |
| POST | `/api/anime/index` | Index new anime (SSE) |
| POST | `/api/anime/check-folders` | Check available folders |

### Processing

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/projects/{id}/processing/start` | Start final processing (SSE) |
| GET | `/api/projects/{id}/processing/download` | Download output archive |

## Project Data Structure

Each project is stored in `backend/data/projects/{project_id}/`:

```
{project_id}/
├── project.json          # Project metadata
├── scenes.json           # Scene definitions
├── matches.json          # Anime match results
├── transcription.json    # Transcription data
├── video.mp4             # Downloaded TikTok
├── tts_processed.wav     # Processed TTS audio
└── output/               # Generated output files
    ├── project.jsx       # Premiere Pro script
    ├── subtitles.srt     # Generated subtitles
    └── assets/           # Required media files
```

## Technical Details

### Scene Matching Algorithm

1. Extract frames at start, middle, and end of each scene
2. Generate SSCD embeddings for each frame
3. Search FAISS index for top-5 candidates per frame
4. Find temporally consistent matches across all three positions
5. Validate speed ratio is within 70%-160% of original

### Anime Indexing

- Frames extracted at configurable FPS (default: 2 FPS for indexing)
- SSCD (Self-Supervised Contrastive Distillation) for embeddings
- FAISS for efficient similarity search
- Metadata stored: series name, episode, timestamp

### Auto-Editor Parameters

Optimized for ElevenLabs TTS audio:
```
--edit audio:threshold=0.05,stream=all
--margin 0.04sec,0.04sec
--silent-speed 99999
```

## Troubleshooting

### Common Issues

**"Import could not be resolved" errors in IDE**
- Ensure you've activated the virtual environment
- Run `uv sync` in both backend and modules/anime_searcher

**anime_searcher not found**
- Initialize submodules: `git submodule update --init --recursive`
- Install submodule deps: `cd modules/anime_searcher && uv sync`

**SSCD model not found**
- Download `sscd_disc_mixup.torchscript.pt` to `modules/anime_searcher/`

**Video download fails**
- Update yt-dlp: `uv pip install -U yt-dlp`
- Check TikTok URL format

**Matching returns no results**
- Verify anime is indexed: `uv run anime-search list /path/to/library`
- Check that the correct anime is selected in project

## License

[Your License Here]

## Contributing

[Contributing guidelines]
