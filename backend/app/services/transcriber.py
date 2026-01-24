import asyncio
from pathlib import Path
from typing import AsyncIterator

from ..models import Word, SceneTranscription, Transcription, SceneList
from ..services import ProjectService


class TranscriptionProgress:
    """Progress information for transcription."""

    def __init__(
        self,
        status: str,
        progress: float = 0,
        message: str = "",
        transcription: Transcription | None = None,
        error: str | None = None,
    ):
        self.status = status
        self.progress = progress
        self.message = message
        self.transcription = transcription
        self.error = error

    def to_dict(self) -> dict:
        result = {
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "error": self.error,
        }
        if self.transcription is not None:
            result["transcription"] = self.transcription.model_dump()
        return result


# Language code mapping
LANGUAGE_MAP = {
    "auto": None,
    "en": "en",
    "anglais": "en",
    "es": "es",
    "espagnol": "es",
    "fr": "fr",
    "français": "fr",
}


class TranscriberService:
    """Service for transcribing audio using faster-whisper."""

    _models: dict = {}  # Cache models by size

    @classmethod
    def _init_model(cls, model_size: str = "large-v3"):
        """Initialize the whisper model for a given size."""
        if model_size in cls._models:
            return cls._models[model_size]

        from faster_whisper import WhisperModel

        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Initializing faster-whisper model: {model_size}")

        model = WhisperModel(model_size, device="auto", compute_type="auto")
        cls._models[model_size] = model
        return model

    @classmethod
    def _transcribe_sync(
        cls,
        audio_path: Path,
        language: str | None = None,
        model_size: str = "large-v3",
    ) -> tuple[list[dict], str]:
        """Synchronous transcription with word-level timestamps."""
        model = cls._init_model(model_size)

        segments, info = model.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=True,
            vad_filter=True,
        )

        words = []
        for segment in segments:
            if segment.words:
                for word in segment.words:
                    words.append({
                        "text": word.word.strip(),
                        "start": word.start,
                        "end": word.end,
                        "confidence": word.probability,
                    })

        detected_language = info.language if info else (language or "en")
        return words, detected_language

    @classmethod
    def _assign_words_to_scenes(
        cls,
        words: list[dict],
        scenes: SceneList,
    ) -> list[SceneTranscription]:
        """Assign transcribed words to scenes based on timing."""
        scene_transcriptions = []

        for scene in scenes.scenes:
            scene_words = []
            scene_text_parts = []

            for word in words:
                # Word belongs to scene if its midpoint is within scene bounds
                word_mid = (word["start"] + word["end"]) / 2
                if scene.start_time <= word_mid < scene.end_time:
                    scene_words.append(Word(
                        text=word["text"],
                        start=word["start"],
                        end=word["end"],
                        confidence=word["confidence"],
                    ))
                    scene_text_parts.append(word["text"])

            scene_transcriptions.append(SceneTranscription(
                scene_index=scene.index,
                text=" ".join(scene_text_parts),
                words=scene_words,
                start_time=scene.start_time,
                end_time=scene.end_time,
            ))

        return scene_transcriptions

    @classmethod
    async def transcribe(
        cls,
        project_id: str,
        language: str = "auto",
    ) -> AsyncIterator[TranscriptionProgress]:
        """Transcribe a project's video and yield progress updates."""
        yield TranscriptionProgress("starting", 0, "Loading project...")

        try:
            # Load project and scenes
            project = ProjectService.load(project_id)
            if not project or not project.video_path:
                yield TranscriptionProgress("error", 0, "", error="Project or video not found")
                return

            scenes = ProjectService.load_scenes(project_id)
            if not scenes or not scenes.scenes:
                yield TranscriptionProgress("error", 0, "", error="No scenes found")
                return

            video_path = Path(project.video_path)

            yield TranscriptionProgress("starting", 0.1, "Loading transcription model...")

            # Map language
            lang_code = LANGUAGE_MAP.get(language.lower(), language if language != "auto" else None)

            yield TranscriptionProgress("transcribing", 0.2, "Transcribing audio (this may take a while)...")

            # Run transcription in thread pool (use large-v3 for original TikTok video)
            loop = asyncio.get_event_loop()
            words, detected_lang = await loop.run_in_executor(
                None,
                lambda: cls._transcribe_sync(video_path, lang_code, "large-v3"),
            )

            yield TranscriptionProgress("processing", 0.8, "Assigning words to scenes...")

            # Assign words to scenes
            scene_transcriptions = cls._assign_words_to_scenes(words, scenes)

            transcription = Transcription(
                language=detected_lang,
                scenes=scene_transcriptions,
            )

            # Save transcription
            ProjectService.save_transcription(project_id, transcription)

            yield TranscriptionProgress(
                "complete",
                1.0,
                f"Transcribed {len(words)} words in {detected_lang}",
                transcription=transcription,
            )

        except Exception as e:
            yield TranscriptionProgress("error", 0, "", error=str(e))

    @classmethod
    def transcribe_with_alignment(
        cls,
        audio_path: Path,
        script: dict,
        model_size: str = "medium",
    ) -> Transcription:
        """
        Transcribe audio and align with a known script.

        This is used when we have the text but need accurate word timings.
        The transcription will be done normally, then aligned to the provided script.

        Args:
            audio_path: Path to the audio file
            script: Script JSON with scenes and text
            model_size: Whisper model size (default: medium for TTS audio)

        Returns:
            Transcription with word-level timings
        """
        # Transcribe to get timings (use medium model for TTS audio)
        words, detected_lang = cls._transcribe_sync(audio_path, None, model_size)

        # Build scene transcriptions from script + timing alignment
        scene_transcriptions = []
        word_index = 0

        for scene_data in script.get("scenes", []):
            scene_index = scene_data.get("scene_index", 0)
            expected_text = scene_data.get("text", "")
            expected_words = expected_text.split()

            scene_words = []
            matched_text_parts = []

            # Simple greedy alignment - match words by position
            for expected_word in expected_words:
                if word_index < len(words):
                    w = words[word_index]
                    scene_words.append(Word(
                        text=expected_word,  # Use expected text, timing from transcription
                        start=w["start"],
                        end=w["end"],
                        confidence=w["confidence"],
                    ))
                    matched_text_parts.append(expected_word)
                    word_index += 1

            # Determine scene timing from words
            if scene_words:
                start_time = scene_words[0].start
                end_time = scene_words[-1].end
            else:
                start_time = 0.0
                end_time = 0.0

            scene_transcriptions.append(SceneTranscription(
                scene_index=scene_index,
                text=" ".join(matched_text_parts) or expected_text,
                words=scene_words,
                start_time=start_time,
                end_time=end_time,
            ))

        return Transcription(
            language=script.get("language", detected_lang),
            scenes=scene_transcriptions,
        )
