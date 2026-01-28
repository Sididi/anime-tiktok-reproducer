"""FCP XML Version 5 generator for Premiere Pro import."""

import asyncio
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from xml.dom import minidom

from PIL import Image


@dataclass
class TimebaseInfo:
    """Frame rate information for FCP XML."""

    timebase: int  # e.g., 24, 30, 60
    ntsc: bool  # TRUE for 23.976, 29.97, etc.

    @property
    def actual_fps(self) -> float:
        if self.ntsc:
            return self.timebase * 1000 / 1001
        return float(self.timebase)

    def seconds_to_frames(self, seconds: float) -> int:
        return int(round(seconds * self.actual_fps))

    def frames_to_seconds(self, frames: int) -> float:
        return frames / self.actual_fps


@dataclass
class AudioCut:
    """Represents a single audio cut from auto-editor XML."""

    start_frame: int  # Timeline start (output)
    end_frame: int  # Timeline end (output)
    in_frame: int  # Source in point
    out_frame: int  # Source out point


@dataclass
class VideoClipInfo:
    """Information for a video clip placement."""

    scene_index: int
    source_path: Path
    source_in_seconds: float
    source_out_seconds: float
    timeline_start_seconds: float
    timeline_end_seconds: float
    speed_ratio: float  # source_duration / target_duration
    effective_speed: float  # capped at 0.75 minimum
    leaves_gap: bool  # True if clip ends before next marker


class AutoEditorXMLParser:
    """Parse auto-editor generated FCP XML to extract audio cuts."""

    @staticmethod
    def parse(xml_path: Path) -> tuple[list[AudioCut], TimebaseInfo]:
        """
        Parse auto-editor XML and extract audio clip cuts.

        Returns:
            Tuple of (list of AudioCuts, TimebaseInfo from the XML)
        """
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # Extract timebase from sequence/rate
        rate_elem = root.find(".//sequence/rate")
        if rate_elem is None:
            raise ValueError("No rate element found in auto-editor XML")

        timebase_elem = rate_elem.find("timebase")
        ntsc_elem = rate_elem.find("ntsc")

        timebase = int(timebase_elem.text) if timebase_elem is not None else 30
        ntsc = ntsc_elem is not None and ntsc_elem.text.upper() == "TRUE"

        timebase_info = TimebaseInfo(timebase=timebase, ntsc=ntsc)

        # Extract audio clips from first track only (avoid duplicates from stereo)
        audio_cuts = []
        first_track = root.find(".//audio/track")

        if first_track is not None:
            for clipitem in first_track.findall("clipitem"):
                start_elem = clipitem.find("start")
                end_elem = clipitem.find("end")
                in_elem = clipitem.find("in")
                out_elem = clipitem.find("out")

                if all(e is not None for e in [start_elem, end_elem, in_elem, out_elem]):
                    cut = AudioCut(
                        start_frame=int(start_elem.text),
                        end_frame=int(end_elem.text),
                        in_frame=int(in_elem.text),
                        out_frame=int(out_elem.text),
                    )
                    audio_cuts.append(cut)

        return audio_cuts, timebase_info


async def detect_video_fps(video_path: Path) -> TimebaseInfo:
    """Detect FPS of a video file and return appropriate TimebaseInfo."""
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate",
        "-of",
        "json",
        str(video_path),
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()

    data = json.loads(stdout)
    if not data.get("streams"):
        return TimebaseInfo(timebase=24, ntsc=True)  # Default 23.976

    r_frame_rate = data["streams"][0]["r_frame_rate"]  # e.g., "24000/1001"

    num, den = map(int, r_frame_rate.split("/"))
    fps = num / den

    # Map common FPS values to timebase/ntsc pairs
    fps_mapping = {
        23.976: (24, True),
        24.0: (24, False),
        25.0: (25, False),
        29.97: (30, True),
        30.0: (30, False),
        50.0: (50, False),
        59.94: (60, True),
        60.0: (60, False),
    }

    # Find closest match
    for target_fps, (timebase, ntsc) in fps_mapping.items():
        if abs(fps - target_fps) < 0.05:
            return TimebaseInfo(timebase=timebase, ntsc=ntsc)

    # Fallback: use rounded fps, assume not NTSC
    return TimebaseInfo(timebase=round(fps), ntsc=False)


def generate_white_rectangle(
    output_path: Path, width: int = 926, height: int = 746
) -> Path:
    """
    Generate a white rectangle image for the V2 border effect.

    The rectangle creates a white border effect when placed on V2,
    with the main video on V3 scaled to 68%.

    Default dimensions: 926x746 (tested in Premiere Pro)
    """
    img = Image.new("RGB", (width, height), color="white")
    img.save(output_path, "PNG")
    return output_path


class FCPXMLGenerator:
    """Generate FCP XML Version 5 for Premiere Pro import."""

    # 60fps sequence for TikTok-style smooth playback
    SEQUENCE_FPS = TimebaseInfo(timebase=60, ntsc=False)

    # White rectangle position (centered in 1080x1920)
    WHITE_RECT_CENTER_X = 540
    WHITE_RECT_CENTER_Y = 960

    def __init__(
        self,
        project_id: str,
        clips: list[VideoClipInfo],
        audio_cuts: list[AudioCut],
        auto_editor_timebase: TimebaseInfo,
        source_timebase: TimebaseInfo,
        tts_audio_filename: str,
        white_rect_filename: str,
        total_duration_seconds: float,
    ):
        self.project_id = project_id
        self.clips = clips
        self.audio_cuts = audio_cuts
        self.auto_editor_timebase = auto_editor_timebase
        self.source_timebase = source_timebase
        self.tts_audio_filename = tts_audio_filename
        self.white_rect_filename = white_rect_filename
        self.total_duration_seconds = total_duration_seconds
        self.total_duration_frames = self.SEQUENCE_FPS.seconds_to_frames(
            total_duration_seconds
        )

        # Track file references to avoid duplicates
        self._file_refs: dict[str, bool] = {}
        self._clipitem_counter = 0

    def generate(self) -> str:
        """Generate complete FCP XML document."""
        root = ET.Element("xmeml", version="5")
        self._build_sequence(root)

        # Pretty print
        rough_string = ET.tostring(root, encoding="unicode")
        reparsed = minidom.parseString(rough_string)
        xml_content = reparsed.toprettyxml(indent="  ")

        # Add DOCTYPE after XML declaration
        lines = xml_content.split("\n")
        lines.insert(1, "<!DOCTYPE xmeml>")

        return "\n".join(lines)

    def _build_sequence(self, root: ET.Element) -> ET.Element:
        """Build the main sequence element with all tracks."""
        sequence = ET.SubElement(root, "sequence", id="sequence-1")
        sequence.set("explodedTracks", "true")

        ET.SubElement(sequence, "name").text = f"ATR_{self.project_id}"
        ET.SubElement(sequence, "duration").text = str(self.total_duration_frames)

        # Sequence rate (60fps)
        self._add_rate_element(sequence, self.SEQUENCE_FPS)

        # Timecode
        self._add_timecode_element(sequence)

        # Media container
        media = ET.SubElement(sequence, "media")

        # Video section with 3 tracks
        video = ET.SubElement(media, "video")
        self._add_video_format(video)

        # V1: Blurred background (Scale 183 + Gaussian Blur 50)
        v1_track = ET.SubElement(video, "track")
        self._populate_video_track(v1_track, track_type="blurred_bg")

        # V2: White rectangle
        v2_track = ET.SubElement(video, "track")
        self._populate_white_rectangle_track(v2_track)

        # V3: Main video (Scale 68)
        v3_track = ET.SubElement(video, "track")
        self._populate_video_track(v3_track, track_type="main")

        # Audio section with 2 tracks
        audio = ET.SubElement(media, "audio")
        self._add_audio_format(audio)

        # A1: Original anime audio (DISABLED)
        a1_track = ET.SubElement(audio, "track")
        a1_track.set("premiereTrackType", "Stereo")
        self._populate_disabled_audio_track(a1_track)

        # A2: TTS audio with auto-editor cuts
        a2_track = ET.SubElement(audio, "track")
        a2_track.set("premiereTrackType", "Stereo")
        self._populate_tts_audio_track(a2_track)

        return sequence

    def _add_rate_element(self, parent: ET.Element, timebase: TimebaseInfo) -> None:
        """Add rate element with timebase and ntsc."""
        rate = ET.SubElement(parent, "rate")
        ET.SubElement(rate, "timebase").text = str(timebase.timebase)
        ET.SubElement(rate, "ntsc").text = "TRUE" if timebase.ntsc else "FALSE"

    def _add_timecode_element(self, sequence: ET.Element) -> None:
        """Add timecode element to sequence."""
        timecode = ET.SubElement(sequence, "timecode")
        tc_rate = ET.SubElement(timecode, "rate")
        ET.SubElement(tc_rate, "timebase").text = str(self.SEQUENCE_FPS.timebase)
        ET.SubElement(tc_rate, "ntsc").text = (
            "TRUE" if self.SEQUENCE_FPS.ntsc else "FALSE"
        )
        ET.SubElement(timecode, "string").text = "00:00:00:00"
        ET.SubElement(timecode, "frame").text = "0"
        ET.SubElement(timecode, "displayformat").text = "NDF"

    def _add_video_format(self, video: ET.Element) -> None:
        """Add video format element (1080x1920 vertical)."""
        video_format = ET.SubElement(video, "format")
        sample = ET.SubElement(video_format, "samplecharacteristics")
        ET.SubElement(sample, "width").text = "1080"
        ET.SubElement(sample, "height").text = "1920"
        ET.SubElement(sample, "pixelaspectratio").text = "square"
        self._add_rate_element(sample, self.SEQUENCE_FPS)

    def _add_audio_format(self, audio: ET.Element) -> None:
        """Add audio format element."""
        ET.SubElement(audio, "numOutputChannels").text = "2"
        audio_format = ET.SubElement(audio, "format")
        sample = ET.SubElement(audio_format, "samplecharacteristics")
        ET.SubElement(sample, "depth").text = "16"
        ET.SubElement(sample, "samplerate").text = "48000"

    def _populate_video_track(self, track: ET.Element, track_type: str) -> None:
        """
        Populate a video track with clips.

        Args:
            track: The track element to populate
            track_type: Either "blurred_bg" (V1) or "main" (V3)
        """
        track_prefix = "v1" if track_type == "blurred_bg" else "v3"

        for clip in self.clips:
            clipitem = self._create_video_clipitem(clip, track_type, track_prefix)
            track.append(clipitem)

    def _create_video_clipitem(
        self, clip: VideoClipInfo, track_type: str, track_prefix: str
    ) -> ET.Element:
        """Create a video clipitem with appropriate effects and speed."""
        self._clipitem_counter += 1
        clip_id = f"clipitem-{track_prefix}-{self._clipitem_counter}"

        clipitem = ET.Element("clipitem", id=clip_id)
        ET.SubElement(clipitem, "name").text = f"Scene {clip.scene_index + 1}"

        # Rate
        self._add_rate_element(clipitem, self.SEQUENCE_FPS)

        # Calculate actual clip end based on speed
        source_duration = clip.source_out_seconds - clip.source_in_seconds
        actual_duration = source_duration / clip.effective_speed

        # Timeline position (in sequence frames)
        start_frame = self.SEQUENCE_FPS.seconds_to_frames(clip.timeline_start_seconds)
        actual_end_seconds = clip.timeline_start_seconds + actual_duration
        end_frame = self.SEQUENCE_FPS.seconds_to_frames(actual_end_seconds)

        ET.SubElement(clipitem, "start").text = str(start_frame)
        ET.SubElement(clipitem, "end").text = str(end_frame)

        # Source in/out (in source frames)
        in_frame = self.source_timebase.seconds_to_frames(clip.source_in_seconds)
        out_frame = self.source_timebase.seconds_to_frames(clip.source_out_seconds)

        ET.SubElement(clipitem, "in").text = str(in_frame)
        ET.SubElement(clipitem, "out").text = str(out_frame)

        # File reference
        self._add_file_reference(clipitem, clip.source_path)

        # Add linked audio reference (links video to its audio on A1)
        link = ET.SubElement(clipitem, "link")
        ET.SubElement(link, "linkclipref").text = f"clipitem-a1-{clip.scene_index + 1}"
        ET.SubElement(link, "mediatype").text = "audio"
        ET.SubElement(link, "trackindex").text = "1"
        ET.SubElement(link, "clipindex").text = str(clip.scene_index + 1)

        # Speed filter (if not 100%)
        if abs(clip.effective_speed - 1.0) > 0.001:
            self._add_speed_filter(clipitem, clip.effective_speed)

        # Video effects based on track type
        if track_type == "blurred_bg":
            self._add_blurred_bg_effects(clipitem)  # Scale 183 + Blur 50
        else:
            self._add_main_video_effects(clipitem)  # Scale 68

        return clipitem

    def _add_file_reference(self, clipitem: ET.Element, source_path: Path) -> None:
        """Add file reference to clipitem."""
        filename = source_path.name
        file_id = f"file-{filename.replace('.', '-').replace(' ', '_')}"

        if file_id not in self._file_refs:
            file_elem = ET.SubElement(clipitem, "file", id=file_id)
            ET.SubElement(file_elem, "name").text = filename
            ET.SubElement(file_elem, "pathurl").text = f"sources/{filename}"

            self._add_rate_element(file_elem, self.source_timebase)

            # Assume source is 1 hour long (Premiere will read actual duration)
            ET.SubElement(file_elem, "duration").text = str(
                self.source_timebase.seconds_to_frames(3600)
            )

            file_media = ET.SubElement(file_elem, "media")

            # Video characteristics
            file_video = ET.SubElement(file_media, "video")
            video_sample = ET.SubElement(file_video, "samplecharacteristics")
            ET.SubElement(video_sample, "width").text = "1920"
            ET.SubElement(video_sample, "height").text = "1080"

            # Audio characteristics
            file_audio = ET.SubElement(file_media, "audio")
            audio_sample = ET.SubElement(file_audio, "samplecharacteristics")
            ET.SubElement(audio_sample, "depth").text = "16"
            ET.SubElement(audio_sample, "samplerate").text = "48000"
            ET.SubElement(file_audio, "channelcount").text = "2"

            self._file_refs[file_id] = True
        else:
            # Reference existing file
            ET.SubElement(clipitem, "file", id=file_id)

    def _add_speed_filter(self, clipitem: ET.Element, speed: float) -> None:
        """
        Add speed/duration filter to clipitem.

        In FCP XML, speed is expressed as a percentage in a filter.
        """
        filter_elem = ET.SubElement(clipitem, "filter")
        effect = ET.SubElement(filter_elem, "effect")

        ET.SubElement(effect, "name").text = "Time Remap"
        ET.SubElement(effect, "effectid").text = "timeremap"
        ET.SubElement(effect, "effectcategory").text = "motion"
        ET.SubElement(effect, "effecttype").text = "motion"
        ET.SubElement(effect, "mediatype").text = "video"

        # Speed parameter (as percentage)
        param = ET.SubElement(effect, "parameter")
        ET.SubElement(param, "parameterid").text = "speed"
        ET.SubElement(param, "name").text = "Speed"
        ET.SubElement(param, "value").text = str(speed * 100)

        # Reverse parameter (always false)
        reverse_param = ET.SubElement(effect, "parameter")
        ET.SubElement(reverse_param, "parameterid").text = "reverse"
        ET.SubElement(reverse_param, "name").text = "Reverse"
        ET.SubElement(reverse_param, "value").text = "FALSE"

        # Frame blending (none for anime)
        blend_param = ET.SubElement(effect, "parameter")
        ET.SubElement(blend_param, "parameterid").text = "frameblending"
        ET.SubElement(blend_param, "name").text = "Frame Blending"
        ET.SubElement(blend_param, "value").text = "FALSE"

    def _add_blurred_bg_effects(self, clipitem: ET.Element) -> None:
        """Add Scale 183% and Gaussian Blur 50 effects for V1 (background)."""
        # Motion/Scale effect (183%)
        motion_filter = ET.SubElement(clipitem, "filter")
        motion_effect = ET.SubElement(motion_filter, "effect")
        ET.SubElement(motion_effect, "name").text = "Basic Motion"
        ET.SubElement(motion_effect, "effectid").text = "basic"
        ET.SubElement(motion_effect, "effectcategory").text = "motion"
        ET.SubElement(motion_effect, "effecttype").text = "motion"
        ET.SubElement(motion_effect, "mediatype").text = "video"

        # Scale parameter
        scale_param = ET.SubElement(motion_effect, "parameter")
        ET.SubElement(scale_param, "parameterid").text = "scale"
        ET.SubElement(scale_param, "name").text = "Scale"
        ET.SubElement(scale_param, "valuemin").text = "0"
        ET.SubElement(scale_param, "valuemax").text = "1000"
        ET.SubElement(scale_param, "value").text = "183"

        # Gaussian Blur effect (50)
        blur_filter = ET.SubElement(clipitem, "filter")
        blur_effect = ET.SubElement(blur_filter, "effect")
        ET.SubElement(blur_effect, "name").text = "Gaussian Blur"
        ET.SubElement(blur_effect, "effectid").text = "AE.ADBE Gaussian Blur 2"
        ET.SubElement(blur_effect, "effectcategory").text = "blur"
        ET.SubElement(blur_effect, "effecttype").text = "filter"
        ET.SubElement(blur_effect, "mediatype").text = "video"

        blur_param = ET.SubElement(blur_effect, "parameter")
        ET.SubElement(blur_param, "parameterid").text = "blurriness"
        ET.SubElement(blur_param, "name").text = "Blurriness"
        ET.SubElement(blur_param, "valuemin").text = "0"
        ET.SubElement(blur_param, "valuemax").text = "100"
        ET.SubElement(blur_param, "value").text = "50"

    def _add_main_video_effects(self, clipitem: ET.Element) -> None:
        """Add Scale 68% effect for V3 (main video)."""
        motion_filter = ET.SubElement(clipitem, "filter")
        motion_effect = ET.SubElement(motion_filter, "effect")
        ET.SubElement(motion_effect, "name").text = "Basic Motion"
        ET.SubElement(motion_effect, "effectid").text = "basic"
        ET.SubElement(motion_effect, "effectcategory").text = "motion"
        ET.SubElement(motion_effect, "effecttype").text = "motion"
        ET.SubElement(motion_effect, "mediatype").text = "video"

        # Scale parameter (68%)
        scale_param = ET.SubElement(motion_effect, "parameter")
        ET.SubElement(scale_param, "parameterid").text = "scale"
        ET.SubElement(scale_param, "name").text = "Scale"
        ET.SubElement(scale_param, "valuemin").text = "0"
        ET.SubElement(scale_param, "valuemax").text = "1000"
        ET.SubElement(scale_param, "value").text = "68"

    def _populate_white_rectangle_track(self, track: ET.Element) -> None:
        """Add single white rectangle image spanning entire duration on V2."""
        clipitem = ET.Element("clipitem", id="clipitem-white-rect")
        ET.SubElement(clipitem, "name").text = "White Border"

        self._add_rate_element(clipitem, self.SEQUENCE_FPS)

        ET.SubElement(clipitem, "start").text = "0"
        ET.SubElement(clipitem, "end").text = str(self.total_duration_frames)
        ET.SubElement(clipitem, "in").text = "0"
        ET.SubElement(clipitem, "out").text = str(self.total_duration_frames)

        # Still frame (image)
        ET.SubElement(clipitem, "stillframe").text = "TRUE"

        # File reference for white rectangle
        file_elem = ET.SubElement(clipitem, "file", id="file-white-rect")
        ET.SubElement(file_elem, "name").text = self.white_rect_filename
        ET.SubElement(file_elem, "pathurl").text = f"assets/{self.white_rect_filename}"

        self._add_rate_element(file_elem, self.SEQUENCE_FPS)
        ET.SubElement(file_elem, "duration").text = str(self.total_duration_frames)

        file_media = ET.SubElement(file_elem, "media")
        file_video = ET.SubElement(file_media, "video")
        file_sample = ET.SubElement(file_video, "samplecharacteristics")
        ET.SubElement(file_sample, "width").text = "926"
        ET.SubElement(file_sample, "height").text = "746"

        track.append(clipitem)

    def _populate_disabled_audio_track(self, track: ET.Element) -> None:
        """
        Populate A1 with anime audio from video clips (DISABLED).
        Links to same source files as video but extracts audio.
        """
        for i, clip in enumerate(self.clips):
            clipitem = ET.Element(
                "clipitem", id=f"clipitem-a1-{clip.scene_index + 1}"
            )
            clipitem.set("premiereChannelType", "stereo")

            ET.SubElement(
                clipitem, "name"
            ).text = f"Scene {clip.scene_index + 1} Audio"
            ET.SubElement(clipitem, "enabled").text = "FALSE"  # DISABLED

            self._add_rate_element(clipitem, self.SEQUENCE_FPS)

            # Calculate actual clip timing (same as video)
            source_duration = clip.source_out_seconds - clip.source_in_seconds
            actual_duration = source_duration / clip.effective_speed

            start_frame = self.SEQUENCE_FPS.seconds_to_frames(
                clip.timeline_start_seconds
            )
            actual_end_seconds = clip.timeline_start_seconds + actual_duration
            end_frame = self.SEQUENCE_FPS.seconds_to_frames(actual_end_seconds)

            in_frame = self.source_timebase.seconds_to_frames(clip.source_in_seconds)
            out_frame = self.source_timebase.seconds_to_frames(clip.source_out_seconds)

            ET.SubElement(clipitem, "start").text = str(start_frame)
            ET.SubElement(clipitem, "end").text = str(end_frame)
            ET.SubElement(clipitem, "in").text = str(in_frame)
            ET.SubElement(clipitem, "out").text = str(out_frame)

            # Reference same file as video clip
            filename = clip.source_path.name
            file_id = f"file-{filename.replace('.', '-').replace(' ', '_')}"
            ET.SubElement(clipitem, "file", id=file_id)

            # Source track info
            sourcetrack = ET.SubElement(clipitem, "sourcetrack")
            ET.SubElement(sourcetrack, "mediatype").text = "audio"
            ET.SubElement(sourcetrack, "trackindex").text = "1"

            # Link to corresponding video clip
            link = ET.SubElement(clipitem, "link")
            ET.SubElement(link, "linkclipref").text = f"clipitem-v3-{i + 1}"
            ET.SubElement(link, "mediatype").text = "video"
            ET.SubElement(link, "trackindex").text = "3"
            ET.SubElement(link, "clipindex").text = str(i + 1)

            track.append(clipitem)

    def _populate_tts_audio_track(self, track: ET.Element) -> None:
        """
        Populate A2 with TTS audio using cuts from auto-editor XML.
        This preserves the original cuts for manual edge case fixing.
        """
        # Convert auto-editor frames to sequence frames
        # Auto-editor uses its own timebase, we need to convert
        ae_fps = self.auto_editor_timebase.actual_fps
        seq_fps = self.SEQUENCE_FPS.actual_fps

        # File reference for TTS audio
        file_id = "file-tts-audio"
        first_clip = True

        for i, cut in enumerate(self.audio_cuts):
            clipitem = ET.Element("clipitem", id=f"clipitem-a2-{i + 1}")
            clipitem.set("premiereChannelType", "stereo")

            ET.SubElement(clipitem, "name").text = "TTS Audio"
            ET.SubElement(clipitem, "enabled").text = "TRUE"

            self._add_rate_element(clipitem, self.SEQUENCE_FPS)

            # Convert from auto-editor timebase to sequence timebase
            start_seconds = cut.start_frame / ae_fps
            end_seconds = cut.end_frame / ae_fps
            in_seconds = cut.in_frame / ae_fps
            out_seconds = cut.out_frame / ae_fps

            start_frame = self.SEQUENCE_FPS.seconds_to_frames(start_seconds)
            end_frame = self.SEQUENCE_FPS.seconds_to_frames(end_seconds)
            in_frame = self.SEQUENCE_FPS.seconds_to_frames(in_seconds)
            out_frame = self.SEQUENCE_FPS.seconds_to_frames(out_seconds)

            ET.SubElement(clipitem, "start").text = str(start_frame)
            ET.SubElement(clipitem, "end").text = str(end_frame)
            ET.SubElement(clipitem, "in").text = str(in_frame)
            ET.SubElement(clipitem, "out").text = str(out_frame)

            # File reference (full definition on first clip, reference thereafter)
            if first_clip:
                file_elem = ET.SubElement(clipitem, "file", id=file_id)
                ET.SubElement(file_elem, "name").text = self.tts_audio_filename
                ET.SubElement(file_elem, "pathurl").text = self.tts_audio_filename

                self._add_rate_element(file_elem, self.SEQUENCE_FPS)

                file_media = ET.SubElement(file_elem, "media")
                file_audio = ET.SubElement(file_media, "audio")
                audio_sample = ET.SubElement(file_audio, "samplecharacteristics")
                ET.SubElement(audio_sample, "depth").text = "16"
                ET.SubElement(audio_sample, "samplerate").text = "48000"
                ET.SubElement(file_audio, "channelcount").text = "2"

                first_clip = False
            else:
                ET.SubElement(clipitem, "file", id=file_id)

            # Source track info
            sourcetrack = ET.SubElement(clipitem, "sourcetrack")
            ET.SubElement(sourcetrack, "mediatype").text = "audio"
            ET.SubElement(sourcetrack, "trackindex").text = "1"

            track.append(clipitem)
