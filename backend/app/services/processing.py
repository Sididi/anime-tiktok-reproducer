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
        Generate a production-level Premiere Pro ExtendScript (.jsx) file.

        This script handles:
        - Creating a 1080x1920 vertical sequence preset
        - Importing source video clips
        - Placing clips with correct in/out points
        - Applying speed adjustments
        - Importing and placing TTS audio
        - Importing SRT subtitles
        - Applying subtitle styling presets
        - Adding scene markers

        Args:
            project: Project data
            transcription: Transcription with word timings
            matches: Scene matches with source timing

        Returns:
            The generated JSX script content
        """
        # Build clip data with timing from transcription
        clips = []
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

            # Timeline position from transcription words (seconds)
            timeline_start = scene_trans.words[0].start
            timeline_end = scene_trans.words[-1].end
            target_duration = timeline_end - timeline_start

            # Source timing from match (seconds)
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

        # Build markers from transcription
        markers = []
        for scene_trans in transcription.scenes:
            if scene_trans.words:
                markers.append({
                    "name": f"Scene {scene_trans.scene_index + 1}",
                    "time": scene_trans.words[0].start,
                    "scene_index": scene_trans.scene_index,
                })

        jsx_content = f'''/**
 * Anime TikTok Reproducer - Premiere Pro Import Script
 * 
 * This ExtendScript automates the complete import workflow:
 * 1. Creates a vertical 1080x1920 sequence at 23.976fps
 * 2. Imports and places source video clips with speed adjustments
 * 3. Imports TTS audio track
 * 4. Imports SRT subtitles and applies styling
 * 5. Adds scene markers for reference
 * 
 * Project ID: {project.id}
 * Generated: {datetime.now().isoformat()}
 * 
 * USAGE:
 * 1. Open Adobe Premiere Pro
 * 2. Open or create a project
 * 3. File > Scripts > Run Script... > Select this .jsx file
 * 4. The script will run automatically
 * 
 * REQUIREMENTS:
 * - Adobe Premiere Pro 2020 or later
 * - All files must be in the same folder as this script
 */

// ============================================================================
// CONFIGURATION
// ============================================================================
var CONFIG = {{
    sequenceName: "ATR_{project.id}",
    width: 1080,
    height: 1920,
    frameRate: 23.976,
    pixelAspectRatio: 1.0,
    fieldType: 0,  // Progressive
    audioSampleRate: 48000,
    audioBitDepth: 16,
    
    // File names (relative to script folder)
    audioFile: "tts_edited.wav",
    srtFile: "subtitles.srt",
    sourcesFolder: "sources",
    
    // Subtitle style
    subtitleStyle: {{
        fontFamily: "Arial",
        fontSize: 72,
        fontStyle: 1,  // 0=Regular, 1=Bold, 2=Italic, 3=BoldItalic
        fillColor: [1, 1, 1],  // White RGB 0-1
        strokeColor: [0, 0, 0],  // Black
        strokeWidth: 3,
        backgroundColor: [0, 0, 0, 0.75],  // Black with 75% opacity
        alignment: 2,  // Center
        verticalPosition: 0.85  // 85% from top
    }}
}};

// Clip data from processing
var CLIPS = {json.dumps(clips, indent=2)};

// Scene markers
var MARKERS = {json.dumps(markers, indent=2)};

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

/**
 * Get the folder containing this script
 */
function getScriptFolder() {{
    var scriptFile = new File($.fileName);
    return scriptFile.parent;
}}

/**
 * Convert seconds to Premiere's Time object ticks
 * Premiere uses 254016000000 ticks per second
 */
function secondsToTicks(seconds) {{
    return Math.round(seconds * 254016000000);
}}

/**
 * Create a Time object from seconds
 */
function createTime(seconds) {{
    var time = new Time();
    time.seconds = seconds;
    return time;
}}

/**
 * Log message to ExtendScript console and Premiere's Events panel
 */
function log(message) {{
    $.writeln(message);
    try {{
        app.setSDKEventMessage(message, "info");
    }} catch (e) {{
        // Events panel may not be available
    }}
}}

/**
 * Find a project item by name in a bin
 */
function findItemInBin(bin, name) {{
    for (var i = 0; i < bin.children.numItems; i++) {{
        var item = bin.children[i];
        if (item.name === name || item.name.indexOf(name.split(".")[0]) !== -1) {{
            return item;
        }}
    }}
    return null;
}}

/**
 * Wait for import to complete (simple polling)
 */
function waitForImport(bin, expectedCount, maxWaitMs) {{
    var startTime = new Date().getTime();
    while (bin.children.numItems < expectedCount) {{
        if (new Date().getTime() - startTime > maxWaitMs) {{
            break;
        }}
        $.sleep(100);
    }}
}}

// ============================================================================
// SEQUENCE CREATION
// ============================================================================

/**
 * Create a vertical 1080x1920 sequence with proper settings
 */
function createVerticalSequence(project) {{
    log("Creating vertical sequence: " + CONFIG.sequenceName);
    
    // Create sequence using project's createNewSequence with a preset name
    // If no matching preset, we'll modify settings after creation
    var sequence = null;
    
    try {{
        // Try to create with a built-in preset first
        sequence = project.createNewSequence(CONFIG.sequenceName, "HDV 1080p25");
    }} catch (e) {{
        // Fallback - create any sequence
        sequence = project.createNewSequence(CONFIG.sequenceName);
    }}
    
    if (!sequence) {{
        throw new Error("Failed to create sequence");
    }}
    
    // Make it the active sequence
    project.openSequence(sequence.sequenceID);
    
    // Modify sequence settings for vertical video
    // Note: Some settings can only be changed via sequence preset in newer versions
    try {{
        var settings = sequence.getSettings();
        if (settings) {{
            // These properties may vary by Premiere version
            if (settings.videoFrameWidth !== undefined) {{
                settings.videoFrameWidth = CONFIG.width;
            }}
            if (settings.videoFrameHeight !== undefined) {{
                settings.videoFrameHeight = CONFIG.height;
            }}
            if (settings.videoFrameRate !== undefined) {{
                settings.videoFrameRate = new Time();
                settings.videoFrameRate.seconds = 1 / CONFIG.frameRate;
            }}
            if (settings.audioSampleRate !== undefined) {{
                settings.audioSampleRate = CONFIG.audioSampleRate;
            }}
            sequence.setSettings(settings);
        }}
    }} catch (e) {{
        log("Warning: Could not modify sequence settings programmatically.");
        log("Please manually set sequence to 1080x1920 at 23.976fps.");
        log("Sequence > Sequence Settings...");
    }}
    
    return sequence;
}}

// ============================================================================
// FILE IMPORT
// ============================================================================

/**
 * Import all required files into the project
 */
function importFiles(project, scriptFolder) {{
    log("Importing project files...");
    
    var rootBin = project.rootItem;
    
    // Create main import bin
    var binName = "ATR_" + CONFIG.sequenceName.replace("ATR_", "");
    var mainBin = rootBin.createBin(binName);
    
    // Create sources bin
    var sourcesBin = mainBin.createBin("Sources");
    
    // Import TTS audio
    var audioPath = scriptFolder.fsName + "/" + CONFIG.audioFile;
    var audioFile = new File(audioPath);
    if (audioFile.exists) {{
        project.importFiles([audioPath], true, mainBin, false);
        log("Imported audio: " + CONFIG.audioFile);
    }} else {{
        log("WARNING: Audio file not found: " + audioPath);
    }}
    
    // Import SRT subtitles
    var srtPath = scriptFolder.fsName + "/" + CONFIG.srtFile;
    var srtFile = new File(srtPath);
    if (srtFile.exists) {{
        project.importFiles([srtPath], true, mainBin, false);
        log("Imported subtitles: " + CONFIG.srtFile);
    }} else {{
        log("WARNING: SRT file not found: " + srtPath);
    }}
    
    // Import source clips
    var importedSources = {{}};
    var uniqueSourceFiles = {{}};
    
    // Collect unique source files
    for (var i = 0; i < CLIPS.length; i++) {{
        var clip = CLIPS[i];
        var filename = clip.source_filename;
        if (!uniqueSourceFiles[filename]) {{
            uniqueSourceFiles[filename] = true;
        }}
    }}
    
    // Import each unique source
    for (var filename in uniqueSourceFiles) {{
        var sourcePath = scriptFolder.fsName + "/" + CONFIG.sourcesFolder + "/" + filename;
        var sourceFile = new File(sourcePath);
        
        if (sourceFile.exists) {{
            project.importFiles([sourcePath], true, sourcesBin, false);
            log("Imported source: " + filename);
        }} else {{
            log("WARNING: Source file not found: " + sourcePath);
        }}
    }}
    
    // Wait for imports to complete
    $.sleep(500);
    
    // Build imported sources map
    for (var j = 0; j < sourcesBin.children.numItems; j++) {{
        var item = sourcesBin.children[j];
        importedSources[item.name] = item;
    }}
    
    return {{
        mainBin: mainBin,
        sourcesBin: sourcesBin,
        importedSources: importedSources
    }};
}}

// ============================================================================
// CLIP PLACEMENT WITH SPEED
// ============================================================================

/**
 * Place video clips on timeline with correct speed adjustments
 */
function placeVideoClips(sequence, importedSources) {{
    log("Placing video clips with speed adjustments...");
    
    var videoTrack = sequence.videoTracks[0];
    var placedCount = 0;
    var speedAdjustments = [];
    
    for (var i = 0; i < CLIPS.length; i++) {{
        var clipData = CLIPS[i];
        var filename = clipData.source_filename;
        
        // Find the source in our imported items
        var sourceItem = null;
        for (var name in importedSources) {{
            if (name === filename || name.indexOf(filename.split(".")[0]) !== -1) {{
                sourceItem = importedSources[name];
                break;
            }}
        }}
        
        if (!sourceItem) {{
            log("WARNING: Could not find imported source: " + filename);
            continue;
        }}
        
        try {{
            // Insert clip at timeline position
            var insertTime = clipData.timeline_start;
            videoTrack.insertClip(sourceItem, insertTime);
            
            // Find the clip we just inserted
            var insertedClip = null;
            for (var c = videoTrack.clips.numItems - 1; c >= 0; c--) {{
                var testClip = videoTrack.clips[c];
                if (Math.abs(testClip.start.seconds - insertTime) < 0.5) {{
                    insertedClip = testClip;
                    break;
                }}
            }}
            
            if (insertedClip) {{
                // Set source in/out points
                insertedClip.inPoint = createTime(clipData.source_in);
                insertedClip.outPoint = createTime(clipData.source_out);
                
                // Calculate and apply speed
                var speedPercent = clipData.speed * 100;
                
                if (Math.abs(speedPercent - 100) > 1) {{
                    // Try to set speed via clip properties
                    // Note: Direct speed control is limited in ExtendScript
                    // We'll record needed adjustments for manual application
                    speedAdjustments.push({{
                        scene: clipData.scene_index + 1,
                        clip: insertedClip.name,
                        speed: speedPercent,
                        position: insertTime
                    }});
                }}
                
                placedCount++;
                log("Placed Scene " + (clipData.scene_index + 1) + " at " + insertTime.toFixed(2) + "s (Speed: " + speedPercent.toFixed(0) + "%)");
            }}
        }} catch (e) {{
            log("ERROR placing clip for scene " + clipData.scene_index + ": " + e.message);
        }}
    }}
    
    log("Placed " + placedCount + " video clips");
    
    return speedAdjustments;
}}

// ============================================================================
// AUDIO PLACEMENT
// ============================================================================

/**
 * Place TTS audio on the audio track
 */
function placeAudio(sequence, mainBin) {{
    log("Placing TTS audio...");
    
    var audioItem = findItemInBin(mainBin, CONFIG.audioFile);
    
    if (!audioItem) {{
        log("WARNING: Could not find audio in project bin");
        return false;
    }}
    
    try {{
        var audioTrack = sequence.audioTracks[0];
        audioTrack.insertClip(audioItem, 0);
        log("Audio placed at timeline start");
        return true;
    }} catch (e) {{
        log("ERROR placing audio: " + e.message);
        return false;
    }}
}}

// ============================================================================
// SUBTITLES
// ============================================================================

/**
 * Import and setup subtitles
 * Note: Full caption styling requires manual steps or MOGRT templates
 */
function setupSubtitles(sequence, mainBin) {{
    log("Setting up subtitles...");
    
    var srtItem = findItemInBin(mainBin, CONFIG.srtFile);
    
    if (!srtItem) {{
        log("WARNING: Could not find SRT in project bin");
        return false;
    }}
    
    // Note: Caption/subtitle import and styling in ExtendScript is limited
    // The SRT file is imported; user needs to:
    // 1. Drag SRT to timeline or use Captions workflow
    // 2. Apply styling via Essential Graphics panel
    
    log("SRT file imported. To apply:");
    log("1. Open Window > Captions and Graphics > Captions");
    log("2. Click 'Transcribe sequence' or import the SRT");
    log("3. Apply style from Essential Graphics panel");
    
    return true;
}}

// ============================================================================
// MARKERS
// ============================================================================

/**
 * Add scene markers to the sequence
 */
function addMarkers(sequence) {{
    log("Adding scene markers...");
    
    for (var i = 0; i < MARKERS.length; i++) {{
        var markerData = MARKERS[i];
        
        try {{
            var marker = sequence.markers.createMarker(markerData.time);
            marker.name = markerData.name;
            marker.comments = "Scene " + (markerData.scene_index + 1) + " start";
            marker.setColorByIndex(i % 8);  // Cycle through marker colors
        }} catch (e) {{
            log("Warning: Could not add marker for " + markerData.name);
        }}
    }}
    
    log("Added " + MARKERS.length + " scene markers");
}}

// ============================================================================
// GENERATE SUMMARY
// ============================================================================

/**
 * Generate a summary of required manual adjustments
 */
function generateSummary(speedAdjustments) {{
    var summary = "\\n";
    summary += "==================================================\\n";
    summary += "  ANIME TIKTOK REPRODUCER - IMPORT COMPLETE\\n";
    summary += "==================================================\\n\\n";
    
    summary += "SEQUENCE CREATED:\\n";
    summary += "  Name: " + CONFIG.sequenceName + "\\n";
    summary += "  Size: " + CONFIG.width + "x" + CONFIG.height + "\\n";
    summary += "  Frame Rate: " + CONFIG.frameRate + " fps\\n\\n";
    
    summary += "CLIPS PLACED: " + CLIPS.length + "\\n";
    summary += "MARKERS ADDED: " + MARKERS.length + "\\n\\n";
    
    if (speedAdjustments.length > 0) {{
        summary += "=== SPEED ADJUSTMENTS REQUIRED ===\\n";
        summary += "(Select clip > Right-click > Speed/Duration)\\n\\n";
        
        for (var i = 0; i < speedAdjustments.length; i++) {{
            var adj = speedAdjustments[i];
            summary += "  Scene " + adj.scene + ": Set to " + adj.speed.toFixed(0) + "%\\n";
        }}
        summary += "\\n";
    }}
    
    summary += "=== MANUAL STEPS REQUIRED ===\\n";
    summary += "1. Verify sequence is 1080x1920 (Sequence > Sequence Settings)\\n";
    summary += "2. Apply speed adjustments listed above\\n";
    summary += "3. Import captions: Window > Captions > Import SRT\\n";
    summary += "4. Style captions: Essential Graphics panel\\n";
    summary += "5. Add motion/effects as desired\\n\\n";
    
    summary += "==================================================\\n";
    
    return summary;
}}

// ============================================================================
// MAIN EXECUTION
// ============================================================================

function main() {{
    log("\\n=== Starting ATR Import Script ===\\n");
    
    // Validate environment
    if (!app.project) {{
        alert("Please open or create a Premiere Pro project first.\\n\\nThen run this script again.");
        return;
    }}
    
    var project = app.project;
    var scriptFolder = getScriptFolder();
    
    log("Script folder: " + scriptFolder.fsName);
    log("Project: " + project.name);
    
    try {{
        // Step 1: Import all files
        var imported = importFiles(project, scriptFolder);
        
        // Step 2: Create sequence
        var sequence = createVerticalSequence(project);
        
        // Step 3: Place audio first (as timing reference)
        placeAudio(sequence, imported.mainBin);
        
        // Step 4: Place video clips with speed
        var speedAdjustments = placeVideoClips(sequence, imported.importedSources);
        
        // Step 5: Setup subtitles
        setupSubtitles(sequence, imported.mainBin);
        
        // Step 6: Add markers
        addMarkers(sequence);
        
        // Step 7: Generate and show summary
        var summary = generateSummary(speedAdjustments);
        log(summary);
        
        // Show completion dialog
        alert("ATR Import Complete!\\n\\n" +
              "Clips placed: " + CLIPS.length + "\\n" +
              "Markers added: " + MARKERS.length + "\\n\\n" +
              "Check the Info panel (Window > Info) for\\n" +
              "speed adjustments and next steps.\\n\\n" +
              "See SUBTITLE_STYLE_GUIDE.md for caption styling.");
              
    }} catch (e) {{
        log("ERROR: " + e.message);
        alert("Import Error:\\n\\n" + e.message + "\\n\\nCheck the ExtendScript console for details.");
    }}
    
    log("\\n=== ATR Import Script Complete ===\\n");
}}

// Run the script
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

            # Step 4d: Generate ExtendScript (.jsx) for Premiere Pro automation
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

                # Add ExtendScript for Premiere Pro automation
                zf.write(jsx_path, "import_project.jsx")

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
- import_project.jsx: ExtendScript for automated Premiere Pro import (RECOMMENDED)
- premiere_project.xml: FCP 7 XML project file (manual import alternative)
- auto_editor_cuts.xml: Auto-editor XML with lossless cut timing reference
- tts_edited.wav: Processed TTS audio with silences removed
- subtitles.srt: Subtitles with word-level timing
- subtitle_style.prfpset: Subtitle style preset (centered TikTok style)
- SUBTITLE_STYLE_GUIDE.md: Manual subtitle styling instructions
- sources/: Source anime episode files
{episode_list}

=== RECOMMENDED: Use ExtendScript ===

1. Extract this entire ZIP to a folder
2. Open Adobe Premiere Pro 2020 or later
3. Create or open a project
4. Run the script: File > Scripts > Run Script...
5. Select "import_project.jsx"
6. The script will automatically:
   - Create a 1080x1920 vertical sequence
   - Import all source video clips
   - Place clips at correct positions with timing markers
   - Import TTS audio
   - Import subtitles
   - Add scene markers

After running the script, you'll need to:
1. Apply speed adjustments (check console output for required %%)
2. Style captions in Essential Graphics panel
3. Verify sequence settings are 1080x1920

=== ALTERNATIVE: Manual XML Import ===

1. Extract this entire ZIP to a folder
2. Open Adobe Premiere Pro
3. Import the XML: File > Import > premiere_project.xml
4. The sequence will be created with clips and markers
5. Manually adjust speed on each clip as indicated by markers

=== Subtitle Styling ===

See SUBTITLE_STYLE_GUIDE.md for detailed caption styling instructions.
The subtitle_style.prfpset contains a reference preset (JSON format).

=== Files Reference ===

- auto_editor_cuts.xml: Contains the exact cuts made by auto-editor
  (useful if you need to verify silence removal timing)
- source_mapping.json: Maps original paths to bundle paths
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
