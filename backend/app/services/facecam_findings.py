"""Keep review annotations attached to unchanged text, without checking facts again."""
from difflib import SequenceMatcher


def remap_findings(old_blocks, new_blocks, findings):
    """Fallback for callers without editor transaction ranges; never cross block IDs."""
    old_offsets, new_offsets = {}, {}
    for blocks, offsets in [(old_blocks, old_offsets), (new_blocks, new_offsets)]:
        offset = 0
        for block in blocks:
            if block.kind != "raw":
                offsets[block.id] = (offset, block.text)
                offset += len(block.text) + 2
    result = []
    for block_id, (old_offset, old_text) in old_offsets.items():
        if block_id not in new_offsets:
            continue
        new_offset, new_text = new_offsets[block_id]
        matches = SequenceMatcher(None, old_text, new_text, autojunk=False).get_matching_blocks()
        for finding in findings:
            start, end = finding.start - old_offset, finding.end - old_offset
            if not (0 <= start < end <= len(old_text)) or old_text[start:end] != finding.quote:
                continue
            for a, b, size in matches:
                if a <= start < end <= a + size:
                    result.append(finding.model_copy(update={
                        "start": new_offset + b + start - a, "end": new_offset + b + end - a,
                    }))
                    break
    return result


def retain_editor_ranges(findings, ranges, text):
    """Clients may move/remove existing annotations, never supply new review claims."""
    by_id = {finding.id: finding for finding in findings}
    retained, seen = [], set()
    for anchor in ranges:
        finding = by_id.get(anchor.id)
        if (finding is None or anchor.id in seen or not 0 <= anchor.start < anchor.end <= len(text)
                or text[anchor.start:anchor.end] != finding.quote):
            raise ValueError("Un repère de vérification ne correspond plus au texte. Rechargez le script.")
        seen.add(anchor.id)
        retained.append(finding.model_copy(update={"start": anchor.start, "end": anchor.end}))
    return retained
