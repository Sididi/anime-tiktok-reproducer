"""The title follows the editable reveal on the final, frame-quantized voice clock."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.models import SceneTranscription, Transcription, Word
from app.models.facecam import ScriptBlock
from app.services.facecam_media_service import reveal_timing


def test_reveal_uses_final_occurrence_with_normalized_apostrophes_and_punctuation():
    # The phrase can occur in an opinion too; it must not trigger the card early.
    text = "Le nom de l'anime c'est : Naruto."
    scenes = [SceneTranscription(scene_index=i, text=text, start_time=start, end_time=start+5,
        words=[Word(text=w, start=start+j*.4, end=start+j*.4+.3) for j,w in enumerate(text.split())])
        for i,start in enumerate([1, 14.123])]
    blocks = [ScriptBlock(id='ending', kind='ending', text='Le nom de l’anime c’est :\nNaruto.')]
    assert reveal_timing(blocks, Transcription(language='fr', scenes=scenes), 19.007) == (847, 1141)


def test_omitted_reveal_retains_the_silent_card_and_missing_alignment_is_rejected():
    transcription = Transcription(language='fr', scenes=[])
    blocks = [ScriptBlock(id='ending', kind='ending', text='')]
    assert reveal_timing(blocks, transcription, 60.5) == (3630, 3690)
    blocks[0].text = 'Le nom de l’anime c’est :'
    with pytest.raises(ValueError, match='synchroniser la révélation'):
        reveal_timing(blocks, transcription, 60.5)
