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
    async def convert_audio_for_auto_editor(
        cls,
        input_path: Path,
        output_path: Path,
    ) -> Path:
        """
        Convert audio to a format compatible with auto-editor (48kHz stereo).

        Auto-editor has issues with some audio formats (e.g., 24kHz mono TTS audio).
        This converts to a standard 48kHz stereo WAV format.

        Args:
            input_path: Path to input audio file
            output_path: Path for converted audio file

        Returns:
            Path to converted audio file
        """
        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-ar", "48000",  # 48kHz sample rate
            "-ac", "2",       # Stereo
            str(output_path),
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"Audio conversion failed: {stderr.decode()}")

        return output_path

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

        # Convert audio to compatible format first (auto-editor has issues with some formats)
        converted_audio_path = audio_path.parent / "tts_converted.wav"
        await cls.convert_audio_for_auto_editor(audio_path, converted_audio_path)

        # Common auto-editor settings
        base_args = [
            "--edit", "audio",
            "--no-open",
        ]

        # Run 1: Export as audio file for whisper transcription
        audio_cmd = [
            "uv", "run", "--project", str(backend_dir),
            "auto-editor",
            str(converted_audio_path),
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
            str(converted_audio_path),
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

        # Clean up converted audio
        if converted_audio_path.exists():
            converted_audio_path.unlink()

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

        NOTE: This generates a reference XML that places clips at approximate positions.
        The ExtendScript (.jsx) file should be used for proper import with speed adjustments,
        as FCP XML speed filters don't import correctly into Premiere Pro.

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

        # Use 23.976 fps for compatibility (most anime source fps)
        # This is also a common timeline fps for video editing
        sequence_fps = 23.976
        source_fps = 23.976  # Most anime is 23.976fps

        # Calculate total duration from transcription (in frames)
        total_duration_secs = 0.0
        if transcription.scenes:
            last_scene = transcription.scenes[-1]
            if last_scene.words:
                total_duration_secs = last_scene.words[-1].end
        total_duration_frames = int(total_duration_secs * sequence_fps)

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

            # Timeline position from transcription words (seconds)
            timeline_start = scene_trans.words[0].start
            timeline_end = scene_trans.words[-1].end
            target_duration = timeline_end - timeline_start

            # Source timing from match (seconds)
            source_in = match.start_time
            source_out = match.end_time
            source_duration = source_out - source_in

            # Calculate speed factor (source_duration / target_duration)
            # If source is 2s and target is 3s, speed = 0.667 (slow down to 66.7%)
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

        def seconds_to_frames(secs: float, fps: float = sequence_fps) -> int:
            return int(secs * fps)

        # Build XML structure
        root = ET.Element("xmeml", version="4")
        sequence = ET.SubElement(root, "sequence", id="sequence-1")

        ET.SubElement(sequence, "name").text = f"ATR_{project.id}"
        ET.SubElement(sequence, "duration").text = str(total_duration_frames)

        rate = ET.SubElement(sequence, "rate")
        ET.SubElement(rate, "timebase").text = str(int(sequence_fps))
        ET.SubElement(rate, "ntsc").text = "TRUE" if abs(sequence_fps - 23.976) < 0.01 else "FALSE"

        # Timecode
        timecode = ET.SubElement(sequence, "timecode")
        tc_rate = ET.SubElement(timecode, "rate")
        ET.SubElement(tc_rate, "timebase").text = str(int(sequence_fps))
        ET.SubElement(tc_rate, "ntsc").text = "TRUE" if abs(sequence_fps - 23.976) < 0.01 else "FALSE"
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
        ET.SubElement(sample_rate, "timebase").text = str(int(sequence_fps))
        ET.SubElement(sample_rate, "ntsc").text = "TRUE" if abs(sequence_fps - 23.976) < 0.01 else "FALSE"

        video_track = ET.SubElement(video, "track")

        # Track source files to avoid duplicates
        file_refs = {}

        for idx, clip in enumerate(clips):
            clipitem = ET.SubElement(video_track, "clipitem", id=f"clipitem-{idx + 1}")

            ET.SubElement(clipitem, "name").text = f"Scene {clip['scene_index'] + 1}"

            clip_rate = ET.SubElement(clipitem, "rate")
            ET.SubElement(clip_rate, "timebase").text = str(int(sequence_fps))
            ET.SubElement(clip_rate, "ntsc").text = "TRUE" if abs(sequence_fps - 23.976) < 0.01 else "FALSE"

            # Timeline position in SEQUENCE frames
            start_frame = seconds_to_frames(clip["timeline_start"], sequence_fps)
            end_frame = seconds_to_frames(clip["timeline_end"], sequence_fps)

            ET.SubElement(clipitem, "start").text = str(start_frame)
            ET.SubElement(clipitem, "end").text = str(end_frame)

            # Source in/out points in SOURCE frames (using source fps)
            in_frame = seconds_to_frames(clip["source_in"], source_fps)
            out_frame = seconds_to_frames(clip["source_out"], source_fps)

            ET.SubElement(clipitem, "in").text = str(in_frame)
            ET.SubElement(clipitem, "out").text = str(out_frame)

            # File reference
            filename = clip["source_filename"]
            file_id = f"file-{filename.replace('.', '-').replace(' ', '_')}"

            if file_id not in file_refs:
                file_elem = ET.SubElement(clipitem, "file", id=file_id)
                ET.SubElement(file_elem, "name").text = filename
                ET.SubElement(file_elem, "pathurl").text = f"{sources_dir}/{filename}"

                file_rate = ET.SubElement(file_elem, "rate")
                ET.SubElement(file_rate, "timebase").text = str(int(source_fps))
                ET.SubElement(file_rate, "ntsc").text = "TRUE" if abs(source_fps - 23.976) < 0.01 else "FALSE"

                # Assume source is 1 hour long (will be read from actual file)
                ET.SubElement(file_elem, "duration").text = str(int(source_fps * 3600))

                file_media = ET.SubElement(file_elem, "media")
                file_video = ET.SubElement(file_media, "video")
                file_sample = ET.SubElement(file_video, "samplecharacteristics")
                ET.SubElement(file_sample, "width").text = "1920"
                ET.SubElement(file_sample, "height").text = "1080"

                file_refs[file_id] = True
            else:
                # Reference existing file
                ET.SubElement(clipitem, "file", id=file_id)

            # NOTE: We don't add speed filter here because FCP XML speed filters
            # don't import correctly into Premiere Pro. Use the ExtendScript instead.
            # Add a comment marker with speed info for reference
            speed_pct = clip["speed"] * 100
            marker = ET.SubElement(clipitem, "marker")
            ET.SubElement(marker, "name").text = f"Speed: {speed_pct:.0f}% (use JSX script)"
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
        ET.SubElement(audio_clip_rate, "timebase").text = str(int(sequence_fps))
        ET.SubElement(audio_clip_rate, "ntsc").text = "TRUE" if abs(sequence_fps - 23.976) < 0.01 else "FALSE"

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
                frame = seconds_to_frames(scene_trans.words[0].start, sequence_fps)
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
    ) -> str:
        """
        Generate a production-ready Premiere Pro 2025 ExtendScript (.jsx) file.

        Uses the QE (Quality Engineering) DOM for reliable:
        - 60fps vertical sequence creation via .sqpreset
        - Speed adjustments via qeItem.setSpeed()
        - Video effect application via QE DOM
        - MOGRT subtitle placement per scene

        Args:
            project: Project data
            transcription: Transcription with word timings
            matches: Scene matches with source timing

        Returns:
            The generated JSX script content (ES3 compatible)
        """
        # Build scenes data with timing from transcription and elastic time logic
        scenes = []
        for scene_trans in transcription.scenes:
            if not scene_trans.words:
                continue

            # Find corresponding match
            match = next(
                (m for m in matches if m.scene_index == scene_trans.scene_index),
                None,
            )
            if not match or not match.episode:
                continue

            # Timeline position from TTS transcription words (seconds)
            timeline_start = scene_trans.words[0].start
            timeline_end = scene_trans.words[-1].end
            target_duration = timeline_end - timeline_start

            # Source timing from match (seconds) - the original anime clip
            source_in = match.start_time
            source_out = match.end_time
            clip_original_duration = source_out - source_in

            # Elastic Time: SpeedRatio = ClipOriginalDuration / TargetDuration
            # If ratio > 1: clip is too long, speed UP
            # If ratio < 1: clip is too short, slow DOWN (with 75% floor)
            speed_ratio = clip_original_duration / target_duration if target_duration > 0 else 1.0

            # Apply 75% floor constraint for slowdowns
            effective_speed = speed_ratio
            leaves_gap = False
            if speed_ratio < 1.0:
                # Need to slow down
                if speed_ratio < 0.75:
                    # Cap at 75% - clip will finish before next marker
                    effective_speed = 0.75
                    leaves_gap = True

            # Subtitle text for this scene
            subtitle_text = scene_trans.text if scene_trans.text else ""

            scenes.append({
                "scene_index": scene_trans.scene_index,
                "start": timeline_start,
                "end": timeline_end,
                "text": subtitle_text,
                "clipName": Path(match.episode).name,
                "source_in": source_in,
                "source_out": source_out,
                "clip_duration": clip_original_duration,
                "target_duration": target_duration,
                "speed_ratio": speed_ratio,
                "effective_speed": effective_speed,
                "leaves_gap": leaves_gap,
            })

        jsx_content = f'''/**
 * Anime TikTok Reproducer - Premiere Pro 2025 Automation Script
 *
 * PRODUCTION-READY ExtendScript (ES3) for Adobe Premiere Pro 2025
 *
 * Project ID: {project.id}
 * Generated: {datetime.now().isoformat()}
 *
 * USAGE:
 * 1. Extract the entire ZIP to a folder
 * 2. Open Adobe Premiere Pro 2025
 * 3. Open or create a project
 * 4. File > Scripts > Run Script... > Select this .jsx file
 */

(function() {{
    // ========================================================================
    // 1. DYNAMIC ROOT DETECTION
    // ========================================================================
    var scriptFile = new File($.fileName);
    var rootDir = scriptFile.parent.fsName;
    var assetsDir = rootDir + "/assets";
    var sourcesDir = rootDir + "/sources";

    // 2. ASSET MAPPING
    var SEQUENCE_PRESET_PATH = assetsDir + "/TikTok60fps.sqpreset";
    var MOGRT_PATH = assetsDir + "/SPM_Anime_Subtitle.mogrt";
    var VIDEO_EFFECT_NAME = "SPM_Anime_Tiktok";
    var MAIN_AUDIO_NAME = "tts_edited.wav";
    var TICKS_PER_SECOND = 254016000000;

    // 3. INPUT DATA (Generated by Python)
    var scenes = {json.dumps(scenes, indent=4)};

    // ========================================================================
    // UTILITY FUNCTIONS
    // ========================================================================

    function timeToTicks(seconds) {{
        return Math.round(seconds * TICKS_PER_SECOND);
    }}

    function ticksToSeconds(ticks) {{
        return ticks / TICKS_PER_SECOND;
    }}

    function log(message) {{
        $.writeln("[ATR] " + message);
    }}

    function criticalError(message) {{
        alert("ATR Error:\\n\\n" + message);
        throw new Error(message);
    }}

    function fileExists(path) {{
        var f = new File(path);
        return f.exists;
    }}

    function findProjectItem(name, searchBin) {{
        var bin = searchBin || app.project.rootItem;
        for (var i = 0; i < bin.children.numItems; i++) {{
            var item = bin.children[i];
            if (item.name === name) {{
                return item;
            }}
            if (item.type === ProjectItemType.BIN) {{
                var found = findProjectItem(name, item);
                if (found) return found;
            }}
        }}
        return null;
    }}

    function findProjectItemByPartialName(partialName, searchBin) {{
        var bin = searchBin || app.project.rootItem;
        for (var i = 0; i < bin.children.numItems; i++) {{
            var item = bin.children[i];
            if (item.name.indexOf(partialName) !== -1) {{
                return item;
            }}
            if (item.type === ProjectItemType.BIN) {{
                var found = findProjectItemByPartialName(partialName, item);
                if (found) return found;
            }}
        }}
        return null;
    }}

    // ========================================================================
    // VALIDATION
    // ========================================================================

    function validateEnvironment() {{
        log("Validating environment...");

        if (!app.project) {{
            criticalError("Please open or create a Premiere Pro project first.");
        }}

        var assetsFolder = new Folder(assetsDir);
        if (!assetsFolder.exists) {{
            criticalError("Assets folder missing at:\\n" + assetsDir);
        }}

        if (!fileExists(SEQUENCE_PRESET_PATH)) {{
            criticalError("Sequence preset missing at:\\n" + SEQUENCE_PRESET_PATH);
        }}

        if (!fileExists(MOGRT_PATH)) {{
            criticalError("MOGRT template missing at:\\n" + MOGRT_PATH);
        }}

        var audioPath = rootDir + "/" + MAIN_AUDIO_NAME;
        if (!fileExists(audioPath)) {{
            criticalError("TTS audio missing at:\\n" + audioPath);
        }}

        var sourcesFolder = new Folder(sourcesDir);
        if (!sourcesFolder.exists) {{
            criticalError("Sources folder missing at:\\n" + sourcesDir);
        }}

        log("Environment validated successfully");
        return true;
    }}

    // ========================================================================
    // STEP A: SEQUENCE CREATION
    // ========================================================================

    function getOrCreateSequence() {{
        log("Getting or creating sequence...");

        var sequenceName = "ATR_{project.id}";
        var sequence = null;

        // First check if sequence already exists
        for (var i = 0; i < app.project.sequences.numSequences; i++) {{
            var seq = app.project.sequences[i];
            if (seq.name === sequenceName) {{
                sequence = seq;
                log("Found existing sequence: " + sequenceName);
                break;
            }}
        }}

        if (!sequence) {{
            // Try to create with QE DOM using preset file
            try {{
                app.enableQE();

                if (typeof qe !== "undefined" && qe.project) {{
                    // QE expects file:// URL format on some systems
                    var presetFile = new File(SEQUENCE_PRESET_PATH);
                    var presetPath = presetFile.fsName;

                    log("Attempting QE sequence creation with preset: " + presetPath);
                    qe.project.newSequence(sequenceName, presetPath);

                    $.sleep(500);

                    // Find the newly created sequence
                    for (var j = 0; j < app.project.sequences.numSequences; j++) {{
                        var newSeq = app.project.sequences[j];
                        if (newSeq.name === sequenceName) {{
                            sequence = newSeq;
                            log("Sequence created via QE DOM");
                            break;
                        }}
                    }}
                }}
            }} catch (e) {{
                log("QE sequence creation failed: " + e.message);
            }}
        }}

        if (!sequence) {{
            // Fallback: use active sequence or ask user to create one
            sequence = app.project.activeSequence;
            if (sequence) {{
                log("Using active sequence: " + sequence.name);
                alert("Using existing active sequence: " + sequence.name + "\\n\\n" +
                      "Please ensure it is set to:\\n" +
                      "- 1080 x 1920\\n" +
                      "- 60 fps\\n\\n" +
                      "You can change this in Sequence > Sequence Settings...");
            }} else {{
                criticalError("No sequence available.\\n\\n" +
                    "Please manually create a sequence first:\\n" +
                    "1. File > New > Sequence\\n" +
                    "2. Choose a preset or set: 1080x1920, 60fps\\n" +
                    "3. Run this script again.");
            }}
        }}

        // Open/activate the sequence
        app.project.openSequence(sequence.sequenceID);

        return sequence;
    }}

    // ========================================================================
    // STEP B: IMPORT & PLACE MAIN AUDIO
    // ========================================================================

    function importAndPlaceAudio(sequence) {{
        log("Importing and placing TTS audio...");

        var audioPath = rootDir + "/" + MAIN_AUDIO_NAME;

        // Import audio file
        app.project.importFiles([audioPath], true, app.project.rootItem, false);
        $.sleep(500);

        // Find the imported audio item
        var audioItem = findProjectItem(MAIN_AUDIO_NAME);
        if (!audioItem) {{
            audioItem = findProjectItemByPartialName("tts_edited");
        }}

        if (!audioItem) {{
            criticalError("Could not find imported audio in project");
        }}

        log("Found audio item: " + audioItem.name);

        // Ensure we have audio tracks
        if (sequence.audioTracks.numTracks < 1) {{
            criticalError("Sequence has no audio tracks");
        }}

        // Place on Audio Track 1 at Time 0.0
        var audioTrack = sequence.audioTracks[0];
        log("Inserting audio on track: " + audioTrack.name + " (Audio Track 1)");

        try {{
            // Insert at time 0
            audioTrack.insertClip(audioItem, 0);
            log("Audio placed on Audio Track 1 at 0.0s");
        }} catch (e) {{
            log("Error inserting audio: " + e.message);
            // Try overwrite insert
            try {{
                audioTrack.overwriteClip(audioItem, 0);
                log("Audio placed via overwrite");
            }} catch (e2) {{
                criticalError("Failed to place audio: " + e2.message);
            }}
        }}

        return audioItem;
    }}

    // ========================================================================
    // STEP C: IMPORT AND PLACE VIDEO CLIPS
    // ========================================================================

    function importSourceClips() {{
        log("Importing source video clips...");

        var uniqueClips = {{}};
        for (var i = 0; i < scenes.length; i++) {{
            var clipName = scenes[i].clipName;
            if (!uniqueClips[clipName]) {{
                uniqueClips[clipName] = true;
            }}
        }}

        var importPaths = [];
        for (var clipName in uniqueClips) {{
            var clipPath = sourcesDir + "/" + clipName;
            if (fileExists(clipPath)) {{
                importPaths.push(clipPath);
                log("Will import: " + clipName);
            }} else {{
                log("WARNING: Source clip not found: " + clipPath);
            }}
        }}

        if (importPaths.length > 0) {{
            app.project.importFiles(importPaths, true, app.project.rootItem, false);
            $.sleep(1000);
        }}

        log("Imported " + importPaths.length + " source clips");
    }}

    function placeClipsWithElasticTime(sequence) {{
        log("Placing clips with Elastic Time logic...");

        var videoTrack = sequence.videoTracks[0];
        var speedInfo = [];

        // Enable QE for potential speed control
        var qeSeq = null;
        try {{
            app.enableQE();
            qeSeq = qe.project.getActiveSequence();
        }} catch (e) {{
            log("QE not available for speed control");
        }}

        for (var i = 0; i < scenes.length; i++) {{
            var scene = scenes[i];
            log("\\nProcessing Scene " + (scene.scene_index + 1) + "...");
            log("  Timeline: " + scene.start.toFixed(3) + "s - " + scene.end.toFixed(3) + "s");
            log("  Source: " + scene.source_in.toFixed(3) + "s - " + scene.source_out.toFixed(3) + "s");
            log("  Speed: " + (scene.effective_speed * 100).toFixed(1) + "%");

            // 1. Create marker at scene start
            try {{
                var marker = sequence.markers.createMarker(scene.start);
                marker.name = "Scene " + (scene.scene_index + 1);
                if (scene.text) {{
                    marker.comments = scene.text.substring(0, 100);
                }}
                marker.setColorByIndex(i % 8);
                log("  Marker created at " + scene.start.toFixed(3) + "s");
            }} catch (e) {{
                log("  Warning: Could not create marker: " + e.message);
            }}

            // 2. Find the source clip in project
            var sourceItem = findProjectItem(scene.clipName);
            if (!sourceItem) {{
                var baseName = scene.clipName.replace(/\\.[^.]+$/, "");
                sourceItem = findProjectItemByPartialName(baseName);
            }}

            if (!sourceItem) {{
                log("  ERROR: Could not find source: " + scene.clipName);
                continue;
            }}

            log("  Found source: " + sourceItem.name);

            // 3. Create a subclip with the correct in/out points
            // This ensures the clip starts from the correct source position
            var inPointTicks = timeToTicks(scene.source_in);
            var outPointTicks = timeToTicks(scene.source_out);

            // 4. Insert clip at the timeline position
            var insertTimeTicks = timeToTicks(scene.start);

            try {{
                // Use overwriteClip for precise placement
                videoTrack.overwriteClip(sourceItem, scene.start);
                log("  Clip inserted at " + scene.start.toFixed(3) + "s");
            }} catch (e) {{
                log("  Insert failed, trying insertClip: " + e.message);
                try {{
                    videoTrack.insertClip(sourceItem, scene.start);
                }} catch (e2) {{
                    log("  ERROR: Could not insert clip: " + e2.message);
                    continue;
                }}
            }}

            // 5. Find and adjust the clip we just inserted
            $.sleep(100);  // Brief wait for clip to be available

            var insertedClip = null;
            var numClips = videoTrack.clips.numItems;

            // Find clip closest to our insert position
            for (var c = 0; c < numClips; c++) {{
                var testClip = videoTrack.clips[c];
                var clipStartSec = ticksToSeconds(testClip.start.ticks);
                if (Math.abs(clipStartSec - scene.start) < 0.5) {{
                    insertedClip = testClip;
                    break;
                }}
            }}

            if (!insertedClip) {{
                log("  WARNING: Could not locate inserted clip");
                continue;
            }}

            // 6. Set source in/out points on the clip
            try {{
                // Create Time objects for in/out points
                var inTime = new Time();
                inTime.ticks = String(inPointTicks);

                var outTime = new Time();
                outTime.ticks = String(outPointTicks);

                insertedClip.inPoint = inTime;
                insertedClip.outPoint = outTime;

                log("  In/Out set: " + scene.source_in.toFixed(3) + "s - " + scene.source_out.toFixed(3) + "s");
            }} catch (e) {{
                log("  Warning: Could not set in/out points: " + e.message);
            }}

            // 7. Set clip start position precisely
            try {{
                var startTime = new Time();
                startTime.ticks = String(insertTimeTicks);
                insertedClip.start = startTime;
                log("  Start position set to " + scene.start.toFixed(3) + "s");
            }} catch (e) {{
                log("  Warning: Could not set start position: " + e.message);
            }}

            // 8. Apply speed if needed
            var speedPercent = scene.effective_speed * 100;
            var speedApplied = false;

            if (Math.abs(speedPercent - 100) > 1) {{
                // Try QE speed control
                if (qeSeq) {{
                    try {{
                        var qeTrack = qeSeq.getVideoTrackAt(0);
                        var qeNumItems = qeTrack.numItems;

                        for (var q = 0; q < qeNumItems; q++) {{
                            var qeItem = qeTrack.getItemAt(q);
                            if (qeItem && qeItem.start) {{
                                var qeStartSec = qeItem.start.secs;
                                if (Math.abs(qeStartSec - scene.start) < 0.5) {{
                                    // setSpeed(speed, ripple, maintainPitch)
                                    qeItem.setSpeed(speedPercent, false, true);
                                    speedApplied = true;
                                    log("  Speed set to " + speedPercent.toFixed(0) + "% via QE");
                                    break;
                                }}
                            }}
                        }}
                    }} catch (e) {{
                        log("  QE speed failed: " + e.message);
                    }}
                }}
            }} else {{
                speedApplied = true;  // No speed change needed
            }}

            speedInfo.push({{
                scene: scene.scene_index + 1,
                speed: speedPercent,
                applied: speedApplied,
                gap: scene.leaves_gap
            }});
        }}

        return speedInfo;
    }}

    // ========================================================================
    // STEP D: APPLY VIDEO PRESET
    // ========================================================================

    function applyVideoPreset(sequence) {{
        log("\\nApplying video effect preset: " + VIDEO_EFFECT_NAME);

        try {{
            app.enableQE();
            var qeSeq = qe.project.getActiveSequence();
            var qeTrack = qeSeq.getVideoTrackAt(0);

            var effect = qe.project.getVideoEffectByName(VIDEO_EFFECT_NAME);

            if (!effect) {{
                log("WARNING: Video effect '" + VIDEO_EFFECT_NAME + "' not found.");
                log("Please manually apply your desired effect to all clips.");
                return false;
            }}

            var applied = 0;
            var numItems = qeTrack.numItems;
            for (var i = 0; i < numItems; i++) {{
                var qeItem = qeTrack.getItemAt(i);
                if (qeItem) {{
                    try {{
                        qeItem.addVideoEffect(effect);
                        applied++;
                    }} catch (e) {{
                        // Ignore individual failures
                    }}
                }}
            }}

            log("Applied video effect to " + applied + " clips");
            return true;

        }} catch (e) {{
            log("ERROR applying video preset: " + e.message);
            return false;
        }}
    }}

    // ========================================================================
    // STEP E: SUBTITLES (MOGRT WORKFLOW)
    // ========================================================================

    function placeSubtitles(sequence) {{
        log("\\nPlacing MOGRT subtitles...");

        var placedCount = 0;
        var textSetCount = 0;

        // Ensure we have at least 2 video tracks
        while (sequence.videoTracks.numTracks < 2) {{
            try {{
                sequence.videoTracks.addTrack();
            }} catch (e) {{
                break;
            }}
        }}

        for (var i = 0; i < scenes.length; i++) {{
            var scene = scenes[i];

            if (!scene.text || scene.text.length === 0) {{
                log("  Scene " + (scene.scene_index + 1) + ": No text, skipping");
                continue;
            }}

            try {{
                // importMGT(mogrtPath, time, videoTrackIndex, audioTrackIndex)
                // videoTrackIndex 1 = Video Track 2
                var mgtResult = sequence.importMGT(
                    MOGRT_PATH,
                    scene.start,
                    1,
                    0
                );

                if (mgtResult) {{
                    placedCount++;

                    // Try to find the clip and set text
                    var subtitleTrack = sequence.videoTracks[1];
                    var numClips = subtitleTrack.clips.numItems;

                    // Get the last added clip (should be our MOGRT)
                    if (numClips > 0) {{
                        var mgtClip = subtitleTrack.clips[numClips - 1];

                        // Try to set the text
                        try {{
                            var mgtComp = mgtClip.getMGTComponent();
                            if (mgtComp) {{
                                var numProps = mgtComp.properties.numProperties;
                                for (var p = 0; p < numProps; p++) {{
                                    var prop = mgtComp.properties[p];
                                    var propName = prop.displayName;

                                    // Look for text-related properties
                                    if (propName === "TextLayer" ||
                                        propName === "Source Text" ||
                                        propName === "Text" ||
                                        propName.toLowerCase().indexOf("text") !== -1) {{

                                        log("  Found property: " + propName);

                                        // Try to set value
                                        prop.setValue(scene.text);
                                        textSetCount++;
                                        log("  Text set for Scene " + (scene.scene_index + 1));
                                        break;
                                    }}
                                }}
                            }}
                        }} catch (textErr) {{
                            log("  Could not set text: " + textErr.message);
                        }}

                        // Set duration
                        try {{
                            var endTime = new Time();
                            endTime.ticks = String(timeToTicks(scene.end));
                            mgtClip.end = endTime;
                        }} catch (durErr) {{
                            // Ignore duration errors
                        }}
                    }}

                    log("  Scene " + (scene.scene_index + 1) + ": MOGRT placed at " + scene.start.toFixed(3) + "s");
                }}

            }} catch (e) {{
                log("  ERROR placing MOGRT for scene " + (scene.scene_index + 1) + ": " + e.message);
            }}
        }}

        log("Placed " + placedCount + " MOGRTs, text set on " + textSetCount);

        if (textSetCount < placedCount) {{
            log("\\nNOTE: Some MOGRT text may need manual editing.");
            log("Select MOGRT clip > Essential Graphics panel > Edit text");
        }}

        return placedCount;
    }}

    // ========================================================================
    // SUMMARY
    // ========================================================================

    function generateSummary(speedInfo, subtitleCount) {{
        var summary = "\\n";
        summary += "==========================================================\\n";
        summary += "  ATR IMPORT COMPLETE\\n";
        summary += "==========================================================\\n\\n";

        summary += "SEQUENCE: " + app.project.activeSequence.name + "\\n";
        summary += "CLIPS: " + scenes.length + "\\n";
        summary += "SUBTITLES: " + subtitleCount + " MOGRTs\\n\\n";

        var manualSpeeds = [];
        for (var i = 0; i < speedInfo.length; i++) {{
            if (!speedInfo[i].applied && Math.abs(speedInfo[i].speed - 100) > 1) {{
                manualSpeeds.push(speedInfo[i]);
            }}
        }}

        if (manualSpeeds.length > 0) {{
            summary += "=== MANUAL SPEED ADJUSTMENTS NEEDED ===\\n";
            summary += "(Right-click clip > Speed/Duration)\\n\\n";
            for (var j = 0; j < manualSpeeds.length; j++) {{
                summary += "  Scene " + manualSpeeds[j].scene + ": " + manualSpeeds[j].speed.toFixed(0) + "%\\n";
            }}
            summary += "\\n";
        }}

        summary += "=== NEXT STEPS ===\\n";
        summary += "1. Check clip positions match markers\\n";
        summary += "2. Verify/apply speed adjustments\\n";
        summary += "3. Edit MOGRT text in Essential Graphics if needed\\n";
        summary += "4. Add transitions and polish\\n";
        summary += "==========================================================\\n";

        return summary;
    }}

    // ========================================================================
    // MAIN
    // ========================================================================

    function main() {{
        log("\\n========================================");
        log("ATR Import Script Starting");
        log("Root: " + rootDir);
        log("========================================\\n");

        validateEnvironment();

        var sequence = getOrCreateSequence();
        log("Using sequence: " + sequence.name);

        importAndPlaceAudio(sequence);

        importSourceClips();
        var speedInfo = placeClipsWithElasticTime(sequence);

        applyVideoPreset(sequence);

        var subtitleCount = placeSubtitles(sequence);

        var summary = generateSummary(speedInfo, subtitleCount);
        log(summary);

        alert("ATR Import Complete!\\n\\n" +
              "Sequence: " + sequence.name + "\\n" +
              "Clips: " + scenes.length + "\\n" +
              "Subtitles: " + subtitleCount + "\\n\\n" +
              "Check ExtendScript console (Window > Console)\\nfor detailed log.");

        log("\\n=== DONE ===\\n");
    }}

    main();

}})();
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
    def get_assets_dir(cls) -> Path:
        """Get the static assets directory (contains .sqpreset, .mogrt, etc.)."""
        # Assets are in the repository root /assets folder
        return Path(__file__).parent.parent.parent.parent / "assets"

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

        Generates a Premiere Pro 2025 automation bundle with:
        - JSX script using QE DOM for 60fps sequence, speed control, MOGRT subtitles
        - Static assets (TikTok60fps.sqpreset, SPM_Anime_Subtitle.mogrt)
        - Processed TTS audio
        - Source video clips

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
                "jsx_generation",
                0.5,
                "Generating Premiere Pro automation script...",
            )

            # Step 3: Generate ExtendScript (.jsx) for Premiere Pro 2025 automation
            # Uses QE DOM for 60fps sequence creation, speed control, and MOGRT subtitles
            jsx_content = cls.generate_jsx_script(
                project,
                new_transcription,
                matches,
            )
            jsx_path = output_dir / "import_project.jsx"
            jsx_path.write_text(jsx_content, encoding="utf-8")

            yield ProcessingProgress(
                "processing",
                "bundling",
                0.7,
                "Creating project bundle...",
            )

            # Step 4: Bundle everything
            bundle_path = cls.get_output_dir(project.id).parent / "project_bundle.zip"
            assets_dir = cls.get_assets_dir()

            # Collect unique source episodes
            source_episodes: set[str] = set()
            for match in matches:
                if match.episode:
                    source_episodes.add(match.episode)

            with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
                # Add ExtendScript for Premiere Pro automation (main entry point)
                zf.write(jsx_path, "import_project.jsx")

                # Add edited TTS audio
                zf.write(edited_audio_path, "tts_edited.wav")

                # Add auto-editor XML (lossless timing reference)
                zf.write(auto_editor_xml_path, "auto_editor_cuts.xml")

                # Add static assets to assets/ folder
                sqpreset_path = assets_dir / "TikTok60fps.sqpreset"
                mogrt_path = assets_dir / "SPM_Anime_Subtitle.mogrt"

                if sqpreset_path.exists():
                    zf.write(sqpreset_path, "assets/TikTok60fps.sqpreset")
                else:
                    raise FileNotFoundError(f"Sequence preset not found: {sqpreset_path}")

                if mogrt_path.exists():
                    zf.write(mogrt_path, "assets/SPM_Anime_Subtitle.mogrt")
                else:
                    raise FileNotFoundError(f"MOGRT template not found: {mogrt_path}")

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

                # Count scenes for README
                scene_count = len([m for m in matches if m.episode])
                episode_list = "\n".join(f"  - {Path(ep).name}" for ep in source_episodes)

                readme = f"""Anime TikTok Reproducer - Project Bundle
=========================================

Project ID: {project.id}
Generated: {datetime.now().isoformat()}
Scenes: {scene_count}

=== CONTENTS ===

import_project.jsx     - Premiere Pro 2025 automation script (MAIN)
tts_edited.wav         - Processed TTS audio (silences removed)
auto_editor_cuts.xml   - Auto-editor timing reference
source_mapping.json    - Original path to bundle path mapping

assets/
  TikTok60fps.sqpreset       - 60fps 1080x1920 sequence preset
  SPM_Anime_Subtitle.mogrt   - Styled subtitle MOGRT template

sources/
{episode_list}

=== USAGE (Premiere Pro 2025) ===

1. Extract this entire ZIP to a folder
2. Open Adobe Premiere Pro 2025
3. Create or open a project
4. File > Scripts > Run Script...
5. Select "import_project.jsx"

The script will automatically:
  - Create 60fps 1080x1920 vertical sequence (via QE DOM)
  - Import and place TTS audio on Audio Track 1
  - Import source video clips
  - Place clips with Elastic Time logic:
    * Speed UP clips that are too long
    * Slow DOWN clips that are too short (75% floor)
    * Creates gaps when slowdown would exceed 75%
  - Apply SPM_Anime_Tiktok video effect preset
  - Place MOGRT subtitles on Video Track 2 for each scene
  - Add scene markers at each clip start

=== MANUAL STEPS AFTER SCRIPT ===

1. Check console output for any clips needing manual speed adjustment
2. Edit MOGRT subtitle text if auto-population didn't work:
   - Select MOGRT clip on Track 2
   - Essential Graphics panel > Edit "TextLayer" property
3. Fill any gaps if clips were capped at 75% slowdown
4. Add transitions and final polish
5. Export!

=== TROUBLESHOOTING ===

"QE DOM not available":
  - Ensure you're using Premiere Pro 2025
  - Try creating a blank sequence first, then re-run

"SPM_Anime_Tiktok effect not found":
  - The video effect preset must be installed in Premiere Pro
  - Manually apply your desired effect to all clips

Speed not applied:
  - Right-click clip > Speed/Duration
  - Enter the percentage shown in console output

=== TECHNICAL NOTES ===

- Sequence preset: TikTok60fps.sqpreset (60fps, 1080x1920, progressive)
- Elastic Time: SpeedRatio = SourceDuration / TargetDuration
- 75% Floor: Clips won't slow below 75% (leaves gap to next scene)
- MOGRT: SPM_Anime_Subtitle with "TextLayer" property (id: 3)
- Ticks per second: 254016000000
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
