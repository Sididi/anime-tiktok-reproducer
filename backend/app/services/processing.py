"""Processing pipeline service for final video generation."""

import asyncio
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

from ..config import settings
from ..models import Project, Transcription, SceneMatch
from .transcriber import TranscriberService


@dataclass
class ProcessingProgress:
    """Progress information for processing."""

    status: str  # starting, processing, complete, error
    step: str = ""  # Current step ID
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    download_url: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "step": self.step,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
            "download_url": self.download_url,
        }


class ProcessingService:
    """Service for processing the final video generation pipeline."""

    @staticmethod
    def get_output_dir(project_id: str) -> Path:
        """Get the output directory for processed files."""
        return settings.projects_dir / project_id / "output"

    @classmethod
    async def run_auto_editor(
        cls,
        audio_path: Path,
        output_path: Path,
    ) -> bool:
        """
        Run auto-editor on TTS audio to remove silences.

        Args:
            audio_path: Path to input audio file
            output_path: Path for output audio file

        Returns:
            True if successful
        """
        # Use uv run to execute auto-editor from the backend's virtual environment
        cmd = [
            "uv", "run", "--project", str(settings.data_dir.parent),
            "auto-editor",
            str(audio_path),
            "--edit", "audio:threshold=0.05,stream=all",
            "--margin", "0.04sec,0.04sec",
            "--silent-speed", "99999",
            "-o", str(output_path),
            "--no-open",
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"auto-editor failed: {stderr.decode()}")

        return True

    @classmethod
    def generate_jsx_script(
        cls,
        project: Project,
        transcription: Transcription,
        matches: list[SceneMatch],
        output_path: Path,
        audio_filename: str,
        srt_filename: str,
    ) -> str:
        """
        Generate a Premiere Pro ExtendScript (.jsx) file.

        Args:
            project: Project data
            transcription: Transcription with word timings
            matches: Scene matches with source timing
            output_path: Directory for output files
            audio_filename: Name of the edited TTS audio file
            srt_filename: Name of the SRT subtitle file

        Returns:
            The generated JSX script content
        """
        # Calculate markers from transcription - one per scene start
        markers = []
        for scene_trans in transcription.scenes:
            if scene_trans.words:
                # Use first word timing as marker position
                markers.append({
                    "name": f"Scene {scene_trans.scene_index + 1}",
                    "time": scene_trans.words[0].start,
                    "scene_index": scene_trans.scene_index,
                })

        # Build clip data with source paths and speed adjustments
        clips = []
        for i, match in enumerate(matches):
            if not match.episode or match.start_time == 0:
                continue

            scene_trans = next(
                (s for s in transcription.scenes if s.scene_index == match.scene_index),
                None,
            )
            if not scene_trans:
                continue

            # Calculate target duration (until next marker or end)
            start_time = markers[i]["time"] if i < len(markers) else 0
            if i + 1 < len(markers):
                end_time = markers[i + 1]["time"]
            else:
                # Last scene - use last word end time
                end_time = scene_trans.words[-1].end if scene_trans.words else start_time + 3

            target_duration = end_time - start_time
            source_duration = match.end_time - match.start_time

            # Calculate speed multiplier
            speed = source_duration / target_duration if target_duration > 0 else 1.0

            # Enforce minimum speed (max slow down to 75%)
            if speed < 0.75:
                speed = 0.75  # Let gap exist

            clips.append({
                "scene_index": match.scene_index,
                "source_path": match.episode,
                "in_point": match.start_time,
                "out_point": match.end_time,
                "timeline_start": start_time,
                "speed": speed,
                "target_duration": target_duration,
            })

        jsx_content = f'''// Premiere Pro ExtendScript - Generated by Anime TikTok Reproducer
// Project: {project.id}
// Generated: {datetime.now().isoformat()}

// Configuration
var COMPOSITION_WIDTH = 1080;
var COMPOSITION_HEIGHT = 1920;
var FRAME_RATE = {project.video_fps or 30};
var AUDIO_FILE = "tts_edited.wav";
var SRT_FILE = "subtitles.srt";

// Source clips data - paths are relative to script location (sources/ folder)
var CLIPS = {json.dumps(clips, indent=2)};

// Markers for scene starts
var MARKERS = {json.dumps(markers, indent=2)};

// Get script folder path for relative imports
function getScriptFolder() {{
    return File($.fileName).parent.fsName;
}}

function secondsToTicks(seconds) {{
    // Premiere Pro uses ticks: 254016000000 ticks per second
    return Math.round(seconds * 254016000000);
}}

function main() {{
    // Check if a project is open
    if (app.project === null) {{
        alert("Please open or create a Premiere Pro project first.");
        return;
    }}

    var project = app.project;
    var rootItem = project.rootItem;
    var scriptFolder = getScriptFolder();

    // Create a bin for our assets
    var binName = "ATR_Import_" + new Date().toISOString().slice(0, 10);
    var bin = rootItem.createBin(binName);

    // Import audio file
    var audioPath = scriptFolder + "/" + AUDIO_FILE;
    project.importFiles([audioPath], true, bin, false);

    // Import SRT file
    var srtPath = scriptFolder + "/" + SRT_FILE;
    project.importFiles([srtPath], true, bin, false);

    // Create sequence
    var sequenceName = "ATR_Sequence";
    var sequence = project.createNewSequence(sequenceName, "ATR_Sequence_Preset");

    // Set sequence settings
    var seqSettings = sequence.getSettings();
    seqSettings.videoFrameWidth = COMPOSITION_WIDTH;
    seqSettings.videoFrameHeight = COMPOSITION_HEIGHT;
    seqSettings.videoFrameRate = FRAME_RATE;
    sequence.setSettings(seqSettings);

    // Find imported audio in bin and add to timeline
    for (var i = 0; i < bin.children.numItems; i++) {{
        var item = bin.children[i];
        if (item.name.indexOf(AUDIO_FILE) !== -1) {{
            sequence.audioTracks[0].insertClip(item, 0);
            break;
        }}
    }}

    // Add markers for each scene
    for (var m = 0; m < MARKERS.length; m++) {{
        var marker = MARKERS[m];
        var markerTime = marker.time;
        sequence.markers.createMarker(markerTime);
        var addedMarker = sequence.markers.getLastMarker();
        if (addedMarker) {{
            addedMarker.name = marker.name;
            addedMarker.comments = "Scene " + (marker.scene_index + 1);
        }}
    }}

    // Import and place source clips
    var importedSources = {{}};
    var sourcesBin = bin.createBin("Sources");

    for (var c = 0; c < CLIPS.length; c++) {{
        var clipData = CLIPS[c];
        var sourcePath = clipData.source_path;

        // Build relative path - sources are in sources/ folder
        var sourceFileName = sourcePath.split(/[/\\\\]/).pop();
        var relativeSourcePath = scriptFolder + "/sources/" + sourceFileName;

        // Import source if not already imported
        if (!importedSources[sourceFileName]) {{
            project.importFiles([relativeSourcePath], true, sourcesBin, false);

            // Find the imported item
            for (var j = 0; j < sourcesBin.children.numItems; j++) {{
                var binItem = sourcesBin.children[j];
                if (binItem.name === sourceFileName || binItem.name.indexOf(sourceFileName.split(".")[0]) !== -1) {{
                    importedSources[sourceFileName] = binItem;
                    break;
                }}
            }}
        }}

        var sourceClip = importedSources[sourceFileName];
        if (!sourceClip) {{
            $.writeln("Warning: Could not find source clip: " + relativeSourcePath);
            continue;
        }}

        // Calculate placement
        var inPoint = clipData.in_point;
        var outPoint = clipData.out_point;
        var timelineStart = clipData.timeline_start;
        var speed = clipData.speed;
        var targetDuration = clipData.target_duration;

        // Insert clip at timeline position
        var trackIndex = 0;
        var insertTime = timelineStart;

        // Insert clip
        sequence.videoTracks[trackIndex].insertClip(sourceClip, insertTime);

        // Get the inserted clip to set in/out points
        var insertedClip = null;
        var clipCount = sequence.videoTracks[trackIndex].clips.numItems;
        for (var k = clipCount - 1; k >= 0; k--) {{
            var testClip = sequence.videoTracks[trackIndex].clips[k];
            if (Math.abs(testClip.start.seconds - timelineStart) < 0.1) {{
                insertedClip = testClip;
                break;
            }}
        }}

        if (insertedClip) {{
            // Set source in/out points
            insertedClip.inPoint = new Time();
            insertedClip.inPoint.seconds = inPoint;
            insertedClip.outPoint = new Time();
            insertedClip.outPoint.seconds = outPoint;

            // Log speed info - user will need to manually adjust
            var speedPercent = Math.round(speed * 100);
            $.writeln("Scene " + clipData.scene_index + ": Placed at " + timelineStart.toFixed(2) + "s");
            $.writeln("  Source: " + inPoint.toFixed(2) + "s - " + outPoint.toFixed(2) + "s");
            $.writeln("  Target duration: " + targetDuration.toFixed(2) + "s");
            $.writeln("  Required speed: " + speedPercent + "% (Right-click > Speed/Duration)");
        }}
    }}
    }}

    // Import subtitles
    for (var s = 0; s < bin.children.numItems; s++) {{
        var srtItem = bin.children[s];
        if (srtItem.name.indexOf(".srt") !== -1) {{
            // Add to caption track
            // Note: Caption import in ExtendScript is limited
            $.writeln("SRT file imported: " + srtItem.name);
            break;
        }}
    }}

    // Write speed adjustment summary to console
    $.writeln("\\n=== SPEED ADJUSTMENTS NEEDED ===");
    for (var sc = 0; sc < CLIPS.length; sc++) {{
        var speedClip = CLIPS[sc];
        var pct = Math.round(speedClip.speed * 100);
        if (pct !== 100) {{
            $.writeln("Scene " + speedClip.scene_index + ": Set to " + pct + "%");
        }}
    }}
    $.writeln("=================================\\n");

    alert("Import complete!\\n\\n" +
          "Markers placed: " + MARKERS.length + "\\n" +
          "Clips placed: " + CLIPS.length + "\\n\\n" +
          "IMPORTANT: Check ExtendScript Toolkit console (or Info panel)\\n" +
          "for required speed adjustments per clip.\\n\\n" +
          "To adjust speed:\\n" +
          "1. Select clip on timeline\\n" +
          "2. Right-click > Speed/Duration\\n" +
          "3. Enter the percentage shown in console");
}}

main();
'''
        return jsx_content

    @classmethod
    def generate_srt(
        cls,
        transcription: Transcription,
        max_chars_per_line: int = 42,
        max_lines: int = 2,
    ) -> str:
        """
        Generate SRT subtitles optimized for short-form video.

        Args:
            transcription: Transcription with word timings
            max_chars_per_line: Maximum characters per line
            max_lines: Maximum lines per subtitle block

        Returns:
            SRT file content
        """
        srt_blocks = []
        block_index = 1

        for scene_trans in transcription.scenes:
            words = scene_trans.words
            if not words:
                continue

            current_block_words = []
            current_block_chars = 0

            for word in words:
                word_len = len(word.text) + 1  # +1 for space

                # Check if we need to start a new block
                if current_block_chars + word_len > max_chars_per_line * max_lines:
                    # Flush current block
                    if current_block_words:
                        block = cls._create_srt_block(
                            block_index,
                            current_block_words,
                            max_chars_per_line,
                        )
                        srt_blocks.append(block)
                        block_index += 1
                        current_block_words = []
                        current_block_chars = 0

                current_block_words.append(word)
                current_block_chars += word_len

            # Flush remaining words
            if current_block_words:
                block = cls._create_srt_block(
                    block_index,
                    current_block_words,
                    max_chars_per_line,
                )
                srt_blocks.append(block)
                block_index += 1

        return "\n".join(srt_blocks)

    @staticmethod
    def _create_srt_block(
        index: int,
        words: list,
        max_chars_per_line: int,
    ) -> str:
        """Create a single SRT block from words."""
        if not words:
            return ""

        start_time = words[0].start
        end_time = words[-1].end

        # Format timestamps
        def format_srt_time(seconds: float) -> str:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            secs = int(seconds % 60)
            millis = int((seconds % 1) * 1000)
            return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

        # Build text with line breaks
        text = " ".join(w.text for w in words)

        # Split into lines if needed
        lines = []
        current_line = []
        current_len = 0

        for word in text.split():
            word_len = len(word) + 1
            if current_len + word_len > max_chars_per_line and current_line:
                lines.append(" ".join(current_line))
                current_line = [word]
                current_len = word_len
            else:
                current_line.append(word)
                current_len += word_len

        if current_line:
            lines.append(" ".join(current_line))

        formatted_text = "\n".join(lines[:2])  # Max 2 lines

        return f"{index}\n{format_srt_time(start_time)} --> {format_srt_time(end_time)}\n{formatted_text}\n"

    @classmethod
    async def process(
        cls,
        project: Project,
        new_script: dict,
        audio_path: Path,
        matches: list[SceneMatch],
    ) -> AsyncIterator[ProcessingProgress]:
        """
        Run the full processing pipeline.

        Args:
            project: Project data
            new_script: New restructured script JSON
            audio_path: Path to uploaded TTS audio
            matches: Scene matches with source timings

        Yields:
            ProcessingProgress updates
        """
        output_dir = cls.get_output_dir(project.id)
        output_dir.mkdir(parents=True, exist_ok=True)

        yield ProcessingProgress(
            "processing",
            "auto_editor",
            0.1,
            "Running auto-editor on TTS audio...",
        )

        try:
            # Step 1: Auto-editor
            edited_audio_path = output_dir / "tts_edited.wav"
            await cls.run_auto_editor(audio_path, edited_audio_path)

            yield ProcessingProgress(
                "processing",
                "transcription",
                0.3,
                "Extracting word timings...",
            )

            # Step 2: Transcribe edited audio for timings
            # We have the script, just need timing alignment
            transcriber = TranscriberService()
            
            # Run transcription in thread pool
            loop = asyncio.get_event_loop()
            new_transcription = await loop.run_in_executor(
                None,
                transcriber.transcribe_with_alignment,
                edited_audio_path,
                new_script,
            )

            yield ProcessingProgress(
                "processing",
                "srt_generation",
                0.5,
                "Creating subtitles...",
            )

            # Step 3: Generate SRT
            srt_content = cls.generate_srt(new_transcription)
            srt_path = output_dir / "subtitles.srt"
            srt_path.write_text(srt_content, encoding="utf-8")

            yield ProcessingProgress(
                "processing",
                "jsx_generation",
                0.7,
                "Generating Premiere Pro script...",
            )

            # Step 4: Generate JSX
            jsx_content = cls.generate_jsx_script(
                project,
                new_transcription,
                matches,
                output_dir,
                "tts_edited.wav",
                "subtitles.srt",
            )
            jsx_path = output_dir / "import_project.jsx"
            jsx_path.write_text(jsx_content, encoding="utf-8")

            yield ProcessingProgress(
                "processing",
                "bundling",
                0.9,
                "Creating project bundle...",
            )

            # Step 5: Bundle everything
            bundle_path = cls.get_output_dir(project.id).parent / "project_bundle.zip"

            # Collect unique source episodes
            source_episodes: set[str] = set()
            for match in matches:
                if match.episode:
                    source_episodes.add(match.episode)

            with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # Add JSX script
                zf.write(jsx_path, "import_project.jsx")

                # Add edited audio
                zf.write(edited_audio_path, "tts_edited.wav")

                # Add subtitles
                zf.write(srt_path, "subtitles.srt")

                # Add source episode files to sources/ folder
                episode_paths_in_bundle = {}
                for episode_path_str in source_episodes:
                    episode_path = Path(episode_path_str)
                    if episode_path.exists():
                        # Use just the filename in sources/ folder
                        dest_name = f"sources/{episode_path.name}"
                        zf.write(episode_path, dest_name)
                        episode_paths_in_bundle[episode_path_str] = dest_name

                # Add episode mapping file for JSX reference
                zf.writestr(
                    "source_mapping.json",
                    json.dumps(episode_paths_in_bundle, indent=2),
                )

                # Add README
                episode_list = "\n".join(f"  - {Path(ep).name}" for ep in source_episodes)
                readme = f"""Anime TikTok Reproducer - Project Bundle
=========================================

Project ID: {project.id}
Generated: {datetime.now().isoformat()}

Contents:
- import_project.jsx: ExtendScript to import assets into Premiere Pro
- tts_edited.wav: Processed TTS audio with silences removed
- subtitles.srt: Subtitles optimized for short-form video
- sources/: Source anime episode files
{episode_list}

Instructions:
1. Extract this entire ZIP to a folder
2. Open Adobe Premiere Pro 2025
3. Create or open a project
4. Run the script: File > Scripts > Run Script File...
5. Select import_project.jsx from the extracted folder
6. The script will import sources from the 'sources/' folder automatically

Speed Adjustments:
The script will place clips on the timeline. To adjust speed:
1. Right-click the clip > Speed/Duration
2. Enter the target speed percentage (shown in JSX comments)
Or use Time Remapping:
1. Select clip > Effect Controls > Time Remapping
2. Adjust keyframes for variable speed
"""
                zf.writestr("README.txt", readme)

            download_url = f"/api/projects/{project.id}/download/bundle"

            yield ProcessingProgress(
                "complete",
                "bundling",
                1.0,
                "Processing complete!",
                download_url=download_url,
            )

        except Exception as e:
            yield ProcessingProgress(
                "error",
                "",
                0,
                "",
                error=str(e),
            )
