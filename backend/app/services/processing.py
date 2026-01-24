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
        audio_output_path: Path,
        xml_output_path: Path,
    ) -> bool:
        """
        Run auto-editor on TTS audio twice:
        1. Export as audio file (for whisper transcription)
        2. Export as XML (lossless reference for timing)

        Args:
            audio_path: Path to input audio file
            audio_output_path: Path for output audio file
            xml_output_path: Path for output Premiere XML file

        Returns:
            True if successful
        """
        backend_dir = settings.data_dir.parent

        # Common auto-editor settings
        base_args = [
            "--edit", "audio:threshold=0.05,stream=all",
            "--margin", "0.04sec,0.04sec",
            "--silent-speed", "99999",
            "--no-open",
        ]

        # Run 1: Export as audio file for whisper transcription
        audio_cmd = [
            "uv", "run", "--project", str(backend_dir),
            "auto-editor",
            str(audio_path),
            *base_args,
            "-o", str(audio_output_path),
        ]

        process = await asyncio.create_subprocess_exec(
            *audio_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"auto-editor (audio export) failed: {stderr.decode()}")

        # Run 2: Export as Premiere XML for lossless timing reference
        xml_cmd = [
            "uv", "run", "--project", str(backend_dir),
            "auto-editor",
            str(audio_path),
            *base_args,
            "--export", "premiere",
            "-o", str(xml_output_path),
        ]

        process = await asyncio.create_subprocess_exec(
            *xml_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"auto-editor (XML export) failed: {stderr.decode()}")

        return True

    @classmethod
    def generate_fcp_xml(
        cls,
        project: Project,
        transcription: Transcription,
        matches: list[SceneMatch],
        audio_filename: str,
        srt_filename: str,
        sources_dir: str = "sources",
    ) -> str:
        """
        Generate a Final Cut Pro 7 XML file for Premiere Pro import.

        Args:
            project: Project data
            transcription: Transcription with word timings from edited TTS audio
            matches: Scene matches with source timing
            audio_filename: Name of the edited TTS audio file
            srt_filename: Name of the SRT subtitle file
            sources_dir: Folder name for source video files

        Returns:
            The generated FCP XML content
        """
        import xml.etree.ElementTree as ET
        from xml.dom import minidom

        fps = project.video_fps or 30

        # Calculate total duration from transcription (in frames)
        total_duration_secs = 0.0
        if transcription.scenes:
            last_scene = transcription.scenes[-1]
            if last_scene.words:
                total_duration_secs = last_scene.words[-1].end
        total_duration_frames = int(total_duration_secs * fps)

        # Build clip data with timing from transcription
        clips = []
        for i, scene_trans in enumerate(transcription.scenes):
            if not scene_trans.words:
                continue

            # Find corresponding match
            match = next(
                (m for m in matches if m.scene_index == scene_trans.scene_index),
                None,
            )
            if not match or not match.episode:
                continue

            # Timeline position from transcription words
            timeline_start = scene_trans.words[0].start
            timeline_end = scene_trans.words[-1].end
            target_duration = timeline_end - timeline_start

            # Source timing from match
            source_in = match.start_time
            source_out = match.end_time
            source_duration = source_out - source_in

            # Calculate speed factor
            speed = source_duration / target_duration if target_duration > 0 else 1.0

            clips.append({
                "scene_index": scene_trans.scene_index,
                "source_path": match.episode,
                "source_filename": Path(match.episode).name,
                "source_in": source_in,
                "source_out": source_out,
                "source_duration": source_duration,
                "timeline_start": timeline_start,
                "timeline_end": timeline_end,
                "target_duration": target_duration,
                "speed": speed,
            })

        def seconds_to_frames(secs: float) -> int:
            return int(secs * fps)

        # Build XML structure
        root = ET.Element("xmeml", version="4")
        sequence = ET.SubElement(root, "sequence", id="sequence-1")

        ET.SubElement(sequence, "name").text = f"ATR_{project.id}"
        ET.SubElement(sequence, "duration").text = str(total_duration_frames)

        rate = ET.SubElement(sequence, "rate")
        ET.SubElement(rate, "timebase").text = str(fps)
        ET.SubElement(rate, "ntsc").text = "FALSE"

        # Timecode
        timecode = ET.SubElement(sequence, "timecode")
        tc_rate = ET.SubElement(timecode, "rate")
        ET.SubElement(tc_rate, "timebase").text = str(fps)
        ET.SubElement(tc_rate, "ntsc").text = "FALSE"
        ET.SubElement(timecode, "string").text = "00:00:00:00"
        ET.SubElement(timecode, "frame").text = "0"
        ET.SubElement(timecode, "displayformat").text = "NDF"

        media = ET.SubElement(sequence, "media")

        # Video tracks
        video = ET.SubElement(media, "video")

        video_format = ET.SubElement(video, "format")
        video_sample = ET.SubElement(video_format, "samplecharacteristics")
        ET.SubElement(video_sample, "width").text = "1080"
        ET.SubElement(video_sample, "height").text = "1920"
        ET.SubElement(video_sample, "pixelaspectratio").text = "square"
        sample_rate = ET.SubElement(video_sample, "rate")
        ET.SubElement(sample_rate, "timebase").text = str(fps)
        ET.SubElement(sample_rate, "ntsc").text = "FALSE"

        video_track = ET.SubElement(video, "track")

        # Track source files to avoid duplicates
        file_refs = {}

        for idx, clip in enumerate(clips):
            clipitem = ET.SubElement(video_track, "clipitem", id=f"clipitem-{idx + 1}")

            ET.SubElement(clipitem, "name").text = f"Scene {clip['scene_index'] + 1}"

            clip_rate = ET.SubElement(clipitem, "rate")
            ET.SubElement(clip_rate, "timebase").text = str(fps)
            ET.SubElement(clip_rate, "ntsc").text = "FALSE"

            # Timeline position (in frames)
            start_frame = seconds_to_frames(clip["timeline_start"])
            end_frame = seconds_to_frames(clip["timeline_end"])

            ET.SubElement(clipitem, "start").text = str(start_frame)
            ET.SubElement(clipitem, "end").text = str(end_frame)

            # Source in/out points (in frames)
            in_frame = seconds_to_frames(clip["source_in"])
            out_frame = seconds_to_frames(clip["source_out"])

            ET.SubElement(clipitem, "in").text = str(in_frame)
            ET.SubElement(clipitem, "out").text = str(out_frame)

            # File reference
            filename = clip["source_filename"]
            file_id = f"file-{filename.replace('.', '-')}"

            if file_id not in file_refs:
                file_elem = ET.SubElement(clipitem, "file", id=file_id)
                ET.SubElement(file_elem, "name").text = filename
                ET.SubElement(file_elem, "pathurl").text = f"{sources_dir}/{filename}"

                file_rate = ET.SubElement(file_elem, "rate")
                ET.SubElement(file_rate, "timebase").text = str(fps)
                ET.SubElement(file_rate, "ntsc").text = "FALSE"

                # Assume source is 1 hour long (will be read from actual file)
                ET.SubElement(file_elem, "duration").text = str(fps * 3600)

                file_media = ET.SubElement(file_elem, "media")
                file_video = ET.SubElement(file_media, "video")
                file_sample = ET.SubElement(file_video, "samplecharacteristics")
                ET.SubElement(file_sample, "width").text = "1920"
                ET.SubElement(file_sample, "height").text = "1080"

                file_refs[file_id] = True
            else:
                # Reference existing file
                ET.SubElement(clipitem, "file", id=file_id)

            # Add speed filter if not 1.0
            if abs(clip["speed"] - 1.0) > 0.01:
                speed_pct = clip["speed"] * 100
                filter_elem = ET.SubElement(clipitem, "filter")
                effect = ET.SubElement(filter_elem, "effect")
                ET.SubElement(effect, "name").text = "Time Remap"
                ET.SubElement(effect, "effectid").text = "timeremap"
                ET.SubElement(effect, "effectcategory").text = "motion"
                ET.SubElement(effect, "effecttype").text = "motion"

                # Speed parameter
                param = ET.SubElement(effect, "parameter")
                ET.SubElement(param, "parameterid").text = "speed"
                ET.SubElement(param, "name").text = "Speed"
                ET.SubElement(param, "value").text = str(speed_pct)

            # Add marker/comment with speed info for manual adjustment
            marker = ET.SubElement(clipitem, "marker")
            ET.SubElement(marker, "name").text = f"Speed: {clip['speed']*100:.0f}%"
            ET.SubElement(marker, "in").text = str(in_frame)
            ET.SubElement(marker, "out").text = str(in_frame)

        # Audio tracks
        audio = ET.SubElement(media, "audio")

        audio_format = ET.SubElement(audio, "format")
        audio_sample = ET.SubElement(audio_format, "samplecharacteristics")
        ET.SubElement(audio_sample, "samplerate").text = "48000"
        ET.SubElement(audio_sample, "depth").text = "16"

        audio_track = ET.SubElement(audio, "track")

        # TTS Audio clip
        audio_clipitem = ET.SubElement(audio_track, "clipitem", id="audio-tts")
        ET.SubElement(audio_clipitem, "name").text = "TTS Audio"

        audio_clip_rate = ET.SubElement(audio_clipitem, "rate")
        ET.SubElement(audio_clip_rate, "timebase").text = str(fps)
        ET.SubElement(audio_clip_rate, "ntsc").text = "FALSE"

        ET.SubElement(audio_clipitem, "start").text = "0"
        ET.SubElement(audio_clipitem, "end").text = str(total_duration_frames)
        ET.SubElement(audio_clipitem, "in").text = "0"
        ET.SubElement(audio_clipitem, "out").text = str(total_duration_frames)

        audio_file = ET.SubElement(audio_clipitem, "file", id="file-tts-audio")
        ET.SubElement(audio_file, "name").text = audio_filename
        ET.SubElement(audio_file, "pathurl").text = audio_filename
        ET.SubElement(audio_file, "duration").text = str(total_duration_frames)

        audio_file_media = ET.SubElement(audio_file, "media")
        audio_file_audio = ET.SubElement(audio_file_media, "audio")
        audio_file_sample = ET.SubElement(audio_file_audio, "samplecharacteristics")
        ET.SubElement(audio_file_sample, "samplerate").text = "48000"
        ET.SubElement(audio_file_sample, "depth").text = "16"

        # Add sequence markers for each scene
        markers_elem = ET.SubElement(sequence, "marker")
        for scene_trans in transcription.scenes:
            if scene_trans.words:
                marker = ET.SubElement(markers_elem, "marker")
                frame = seconds_to_frames(scene_trans.words[0].start)
                ET.SubElement(marker, "name").text = f"Scene {scene_trans.scene_index + 1}"
                ET.SubElement(marker, "in").text = str(frame)
                ET.SubElement(marker, "out").text = str(frame)

        # Convert to pretty XML string
        rough_string = ET.tostring(root, encoding="unicode")
        reparsed = minidom.parseString(rough_string)
        xml_content = reparsed.toprettyxml(indent="  ")

        # Add DOCTYPE
        lines = xml_content.split("\n")
        lines.insert(1, "<!DOCTYPE xmeml>")

        return "\n".join(lines)

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
    def generate_subtitle_style_preset(cls) -> str:
        """
        Generate a Premiere Pro caption style preset (.prfpset).

        This creates a centered subtitle style optimized for TikTok/short-form video.
        The preset can be imported into Premiere Pro's Essential Graphics panel.

        Returns:
            JSON content for the .prfpset file
        """
        # Premiere Pro caption style preset format (JSON-based)
        preset = {
            "name": "ATR TikTok Subtitles",
            "fontFamily": "Arial",
            "fontStyle": "Bold",
            "fontSize": 72,
            "fontColor": "#FFFFFF",
            "backgroundColor": "#000000",
            "backgroundOpacity": 0.75,
            "textAlignment": "center",
            "verticalPosition": 0.85,  # 85% from top (near bottom)
            "horizontalPosition": 0.5,  # Centered
            "strokeColor": "#000000",
            "strokeWidth": 3,
            "shadowEnabled": True,
            "shadowColor": "#000000",
            "shadowOpacity": 0.5,
            "shadowDistance": 2,
            "shadowAngle": 135,
            "lineSpacing": 1.2,
            "maxWidth": 0.9,  # 90% of frame width
        }
        return json.dumps(preset, indent=2)

    @classmethod
    def generate_subtitle_style_guide(cls) -> str:
        """
        Generate a text guide for applying subtitle styles in Premiere Pro.

        Returns:
            Markdown-formatted style guide
        """
        return """# Subtitle Style Guide for Premiere Pro

## Recommended Settings for TikTok-Style Subtitles

### Font Settings
- **Font Family**: Arial or Montserrat (Bold)
- **Font Size**: 72-90px (depending on text length)
- **Font Color**: White (#FFFFFF)

### Background/Box
- **Background Color**: Black (#000000)
- **Background Opacity**: 75%
- **Padding**: 10px horizontal, 5px vertical

### Text Style
- **Stroke**: Black, 3px width
- **Shadow**: Black, 50% opacity, 2px distance, 135° angle
- **Text Alignment**: Center

### Position
- **Vertical Position**: 85% from top (near bottom of frame)
- **Horizontal Position**: Center
- **Max Width**: 90% of frame width

## How to Apply in Premiere Pro

### Method 1: Essential Graphics Panel
1. Select your captions on the timeline
2. Open Window > Essential Graphics
3. Use the text formatting options to match the settings above
4. Save as a preset: Click ≡ menu > Save Style Preset

### Method 2: Import Captions + Style
1. Import the SRT file: File > Import
2. Drag the SRT to the timeline
3. Select all caption clips
4. Open Essential Graphics and apply formatting

### Method 3: Auto-Style with MOGRT (Advanced)
1. Create a custom MOGRT template with these settings
2. Apply to caption track via Essential Graphics

## Quick Keyboard Shortcuts
- Ctrl+Shift+K: Add marker
- Ctrl+D: Default transition
- Ctrl+M: Export Media
"""

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
            # Step 1: Auto-editor (generates both audio and XML)
            edited_audio_path = output_dir / "tts_edited.wav"
            auto_editor_xml_path = output_dir / "auto_editor_cuts.xml"
            await cls.run_auto_editor(audio_path, edited_audio_path, auto_editor_xml_path)

            yield ProcessingProgress(
                "processing",
                "transcription",
                0.3,
                "Extracting word timings from audio...",
            )

            # Step 2: Transcribe edited audio for timings (uses medium model for TTS)
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
                "xml_generation",
                0.7,
                "Generating Premiere Pro XML project...",
            )

            # Step 4: Generate FCP XML for Premiere Pro
            fcp_xml_content = cls.generate_fcp_xml(
                project,
                new_transcription,
                matches,
                "tts_edited.wav",
                "subtitles.srt",
            )
            fcp_xml_path = output_dir / "premiere_project.xml"
            fcp_xml_path.write_text(fcp_xml_content, encoding="utf-8")

            # Step 4b: Generate subtitle style preset
            style_preset = cls.generate_subtitle_style_preset()
            style_preset_path = output_dir / "subtitle_style.prfpset"
            style_preset_path.write_text(style_preset, encoding="utf-8")

            # Step 4c: Generate subtitle style guide
            style_guide = cls.generate_subtitle_style_guide()
            style_guide_path = output_dir / "SUBTITLE_STYLE_GUIDE.md"
            style_guide_path.write_text(style_guide, encoding="utf-8")

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
                # Add FCP XML project
                zf.write(fcp_xml_path, "premiere_project.xml")

                # Add auto-editor XML (lossless timing reference)
                zf.write(auto_editor_xml_path, "auto_editor_cuts.xml")

                # Add edited audio
                zf.write(edited_audio_path, "tts_edited.wav")

                # Add subtitles
                zf.write(srt_path, "subtitles.srt")

                # Add subtitle style preset
                zf.write(style_preset_path, "subtitle_style.prfpset")

                # Add subtitle style guide
                zf.write(style_guide_path, "SUBTITLE_STYLE_GUIDE.md")

                # Add source episode files to sources/ folder
                episode_paths_in_bundle = {}
                for episode_path_str in source_episodes:
                    episode_path = Path(episode_path_str)
                    if episode_path.exists():
                        # Use just the filename in sources/ folder
                        dest_name = f"sources/{episode_path.name}"
                        zf.write(episode_path, dest_name)
                        episode_paths_in_bundle[episode_path_str] = dest_name

                # Add episode mapping file
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
- premiere_project.xml: FCP 7 XML project file (import into Premiere Pro)
- auto_editor_cuts.xml: Auto-editor XML with lossless cut timing reference
- tts_edited.wav: Processed TTS audio with silences removed
- subtitles.srt: Subtitles with word-level timing
- subtitle_style.prfpset: Subtitle style preset (centered TikTok style)
- SUBTITLE_STYLE_GUIDE.md: Manual subtitle styling instructions
- sources/: Source anime episode files
{episode_list}

Quick Start:
1. Extract this entire ZIP to a folder
2. Open Adobe Premiere Pro 2025
3. Import the XML: File > Import > premiere_project.xml
4. The sequence will be created with:
   - TTS audio on audio track
   - Video clips placed on video track with timing
   - Markers at each scene start

After Import:
1. Review clip placements and adjust speed if needed
   (Each clip has a marker showing required speed percentage)
2. Import subtitles.srt and apply style from SUBTITLE_STYLE_GUIDE.md
3. Create video clip preset manually (crop to 9:16, center) if needed

Auto-Editor XML:
The auto_editor_cuts.xml contains the exact cuts made by auto-editor.
You can import this separately if you prefer the lossless XML format.
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
