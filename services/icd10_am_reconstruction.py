"""Pure, conservative reconstruction of a Tenth Edition Tabular TXT.

This module has no application, database or importer dependencies. Its output is
an ICD-10-AM candidate catalog, not an official electronic assignability list.
Titles retain heading-line text only; display uncertainty never changes code
membership. All collections in the domain objects are immutable tuples.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
import hashlib
import json
from pathlib import Path
import re

RECONSTRUCTION_LABEL = (
    "ICD-10-AM Tenth Edition reconstructed candidate catalog from supplied "
    "Tabular List text extraction"
)
PARSER_VERSION = "tenth-edition-conservative-1"
_CODE = r"[A-Z][0-9]{2}(?:\.[0-9]{1,2})?"
_TOKEN = re.compile(r"(?<![A-Za-z0-9.])" + _CODE + r"[†*]?(?![A-Za-z0-9.])")
_HEADING = re.compile(
    r"^(?P<prefix>[ \t\uf020\uf0b5]*)(?P<token>"
    + _CODE
    + r"[†*]?)(?P<gap>[ \t]+)(?P<title>\S.*)$"
)
_RANGE = re.compile(
    r"(?<![A-Za-z0-9.])(?P<a>"
    + _CODE
    + r")[†*]?\s*[–-]\s*(?P<b>"
    + _CODE
    + r")[†*]?(?![A-Za-z0-9.])"
)
_PLACEHOLDER = re.compile(
    r"(?<![A-Za-z0-9.])[A-Z][0-9]{2}\.[0-9]{0,2}-[†*]?(?![A-Za-z0-9.])"
)
_MORPHOLOGY = re.compile(r"(?<![A-Za-z0-9])M[0-9]{4}/[0-9](?![0-9])")
_PAREN_RANGE = re.compile(
    r"^\((?P<items>" + _CODE + r"(?:\s*[–-]\s*" + _CODE + r")?"
    r"(?:,\s*" + _CODE + r"(?:\s*[–-]\s*" + _CODE + r")?)*)\)"
    r"(?:\s+(?:CR|CHADx))*$"
)
_TITLE_METADATA = re.compile(r"(?:\s+(?:CR|CHADx))+$")
_REFERENCE_ATOM = r"[A-Z][0-9]{2}(?:\.[0-9]{0,2}-|\.[0-9]{1,2})?[†*]?"
_REFERENCE_ITEM = _REFERENCE_ATOM + r"(?:\s*[–-]\s*" + _REFERENCE_ATOM + r")?"
_REFERENCE_LIST = _REFERENCE_ITEM + r"(?:,\s*" + _REFERENCE_ITEM + r")*"
_REFERENCE_GROUP = re.compile(r"\s*\((?P<items>" + _REFERENCE_LIST + r")\)")
_OPEN_REFERENCE = re.compile(r"\s+\((?P<items>" + _REFERENCE_LIST + r")(?:,\s*)?$")
_INSTRUCTION = re.compile(
    r"^(Includes\b|Excludes\b|Note\b|Code also\b|Code first\b|"
    r"Use additional code\b|See\b|The following\b|[•\uf0d1\uf0b5\uf020]|\[)",
    re.I,
)
_CHAPTER = re.compile(r"^CHAPTER ([0-9]+)$")
_ACS = re.compile(r"^\s*\uf0d1\s+(?P<refs>[0-9]{4}(?:,\s*[0-9]{4})*)\s*$")
_EXTRACTION = "Extracted from EIS eBook, July 2017."


class CandidateClassification(str, Enum):
    LEAF_CANDIDATE = "leaf_candidate"
    NONASSIGNABLE_PARENT_CANDIDATE = "nonassignable_parent_candidate"


class DisplayConfidence(str, Enum):
    CLEAN_SINGLE_LINE = "clean_single_line"
    FLAGGED_AMBIGUOUS = "flagged_ambiguous"


class RejectionCategory(str, Enum):
    RANGE = "range"
    PLACEHOLDER = "placeholder"
    MORPHOLOGY = "morphology"
    NARRATIVE_REFERENCE = "narrative_reference"
    REPEATED_REFERENCE = "repeated_reference"
    EXCLUDED_REGION = "excluded_region"
    MALFORMED = "malformed"
    AMBIGUOUS = "ambiguous"


class SourceRegion(str, Enum):
    DISEASE_CLASSIFICATION = "disease_classification"
    APPENDIX_A = "appendix_A"
    EXCLUDED = "excluded_source_region"


@dataclass(frozen=True)
class SourceLine:
    number: int
    raw_text: str


@dataclass(frozen=True)
class SourceIdentity:
    filename: str
    sha256: str
    edition_label: str
    extraction_context: str
    evidence: tuple[SourceLine, ...]


@dataclass(frozen=True)
class CodeInterval:
    first: str
    last: str

    def contains(self, code: str) -> bool:
        return _rank(self.first) <= _rank(code) <= _rank(self.last)


@dataclass(frozen=True)
class BlockHeading:
    title: str
    source_line: int
    raw_range_line: str
    title_lines: tuple[int, ...]
    ranges: tuple[CodeInterval, ...]
    inline_title: bool


@dataclass(frozen=True)
class Chapter:
    number: int
    source_line_start: int
    source_line_end: int
    range_line: int
    ranges: tuple[CodeInterval, ...]
    blocks: tuple[BlockHeading, ...]


@dataclass(frozen=True)
class ClassificationBoundaries:
    source_line_start: int
    source_line_end: int
    appendix_a_start: int
    appendix_a_end: int
    chapters: tuple[Chapter, ...]


@dataclass(frozen=True)
class ACSReference:
    numbers: tuple[str, ...]
    provenance: SourceLine


@dataclass(frozen=True)
class CandidateHeading:
    canonical_code: str
    raw_code: str
    display: str
    raw_heading_title: str
    raw_source_line: str
    source_line_start: int
    source_line_end: int
    chapter: int
    block: BlockHeading
    dagger: bool
    asterisk: bool
    australian_code: bool
    acs_references: tuple[ACSReference, ...]
    code_references: tuple[str, ...]
    classification: CandidateClassification
    display_confidence: DisplayConfidence
    children: tuple[str, ...] = ()

    @property
    def code_length(self) -> int:
        return len(self.canonical_code.replace(".", ""))


@dataclass(frozen=True)
class Rejection:
    raw_token: str
    source_line: int
    raw_source_line: str
    column_start: int
    column_end: int
    source_region: SourceRegion
    category: RejectionCategory
    reason: str
    normalized_range: str | None = None


@dataclass(frozen=True)
class HeadingOccurrence:
    canonical_code: str
    source_line: int
    raw_source_line: str
    accepted_heading: bool
    reason: str


@dataclass(frozen=True)
class DuplicateGroup:
    canonical_code: str
    occurrences: tuple[HeadingOccurrence, ...]
    genuine_heading_count: int


@dataclass(frozen=True)
class DisplayDiagnostic:
    canonical_code: str
    source_line: int
    heading_fragment: str
    adjacent: SourceLine
    reason: str
    raw_reference_fragment: str | None = None


@dataclass(frozen=True)
class LengthCount:
    code_length: int
    count: int


@dataclass(frozen=True)
class RejectionCount:
    region: SourceRegion
    category: RejectionCategory
    count: int


@dataclass(frozen=True)
class ReconstructionCounts:
    chapters_detected: int
    explicit_block_range_headings_detected: int
    heading_shaped_occurrences: int
    accepted_structural_heading_occurrences: int
    unique_structural_heading_codes: int
    repeated_heading_shaped_occurrences: int
    heading_shaped_references_rejected: int
    duplicate_real_classification_headings: int
    parent_candidates: int
    parents_by_code_length: tuple[LengthCount, ...]
    leaf_candidates: int
    leaves_by_code_length: tuple[LengthCount, ...]
    unexpected_code_lengths: int
    range_occurrences_disease_region: int
    unique_normalized_ranges_disease_region: int
    placeholder_occurrences_disease_region: int
    morphology_unique_appendix_a: int
    morphology_occurrences_appendix_a: int
    narrative_reference_occurrences_disease_region: int
    malformed_code_column_tokens: int
    ambiguous_heading_identities: int
    display_flags: int
    leaf_display_flags: int
    dagger_heading_codes: int
    asterisk_heading_codes: int
    dagger_leaf_codes: int
    asterisk_leaf_codes: int
    australian_heading_codes: int
    australian_leaf_codes: int
    acs_marker_occurrences_disease_region: int
    headings_with_code_references: int
    wrapped_reference_groups: int
    rejections_by_region_and_category: tuple[RejectionCount, ...]


@dataclass(frozen=True)
class ConsistencyCheck:
    name: str
    passed: bool


@dataclass(frozen=True)
class ReconstructionReport:
    boundaries: ClassificationBoundaries
    counts: ReconstructionCounts
    duplicates: tuple[DuplicateGroup, ...]
    display_diagnostics: tuple[DisplayDiagnostic, ...]
    consistency_checks: tuple[ConsistencyCheck, ...]


@dataclass(frozen=True)
class ReconstructionResult:
    source: SourceIdentity
    headings: tuple[CandidateHeading, ...]
    rejections: tuple[Rejection, ...]
    report: ReconstructionReport
    reconstruction_label: str = RECONSTRUCTION_LABEL
    parser_version: str = PARSER_VERSION

    @property
    def leaves(self) -> tuple[CandidateHeading, ...]:
        return tuple(
            h
            for h in self.headings
            if h.classification == CandidateClassification.LEAF_CANDIDATE
        )

    @property
    def parents(self) -> tuple[CandidateHeading, ...]:
        return tuple(
            h
            for h in self.headings
            if h.classification
            == CandidateClassification.NONASSIGNABLE_PARENT_CANDIDATE
        )

    def to_dict(self) -> dict:
        """Return a detached JSON-compatible representation in stable order."""
        return _json_value(self)

    def to_json(self) -> str:
        return normalized_json(self.to_dict())

    def summary(self) -> dict:
        """Counts/provenance only; full diagnostic text remains in the result."""
        return {
            "reconstruction_label": self.reconstruction_label,
            "parser_version": self.parser_version,
            "source": _json_value(self.source),
            "counts": _json_value(self.report.counts),
            "consistency_checks": _json_value(self.report.consistency_checks),
            "classification_boundaries": {
                "start_line": self.report.boundaries.source_line_start,
                "end_line": self.report.boundaries.source_line_end,
                "appendix_a_start": self.report.boundaries.appendix_a_start,
            },
        }


class ReconstructionError(ValueError):
    """Unsafe structure; diagnostics are retained rather than silently repaired."""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: tuple[Rejection, ...] = (),
        duplicates: tuple[DuplicateGroup, ...] = (),
    ):
        super().__init__(message)
        self.diagnostics = diagnostics
        self.duplicates = duplicates


def _json_value(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def normalized_json(value: dict | list) -> str:
    """Stable UTF-8 JSON text without timestamps or unordered collections."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def parse_text(text: str, *, source_filename: str = "<text>") -> ReconstructionResult:
    """Reconstruct supplied text. SHA-256 identifies its UTF-8 representation."""
    return _parse(
        text, source_filename, hashlib.sha256(text.encode("utf-8")).hexdigest()
    )


def parse_file(path: str | Path) -> ReconstructionResult:
    """Read explicit UTF-8 source bytes once, with no database or source writes."""
    path = Path(path)
    data = path.read_bytes()
    return _parse(
        data.decode("utf-8", errors="strict"),
        path.name,
        hashlib.sha256(data).hexdigest(),
    )


def _rank(code: str) -> int:
    return ord(code[0]) * 100 + int(code[1:3])


def _intervals(value: str) -> tuple[CodeInterval, ...]:
    result = []
    for part in value.split(","):
        ends = re.split(r"\s*[–-]\s*", part.strip())
        if _rank(ends[0]) > _rank(ends[-1]):
            raise ReconstructionError("Reversed structural code range.")
        result.append(CodeInterval(ends[0], ends[-1]))
    return tuple(result)


def _inside(code: str, intervals: tuple[CodeInterval, ...]) -> bool:
    return any(interval.contains(code) for interval in intervals)


def _page_artifact(raw: str) -> bool:
    return raw.startswith("\f") or raw == _EXTRACTION


def _source_identity(
    lines: tuple[str, ...], filename: str, sha256: str, end: int
) -> SourceIdentity:
    evidence = []
    for marker in ("ICD-10-AM", "Tenth Edition", "Tabular List", _EXTRACTION):
        found = next(
            (
                SourceLine(i + 1, line)
                for i, line in enumerate(lines[:end])
                if marker in line
            ),
            None,
        )
        if found is None:
            raise ReconstructionError(
                "Required Tenth Edition source identity marker missing: " + marker
            )
        evidence.append(found)
    return SourceIdentity(
        filename,
        sha256,
        "ICD-10-AM Tenth Edition",
        "EIS eBook, July 2017",
        tuple(evidence),
    )


def _uppercase_title_before(
    lines: tuple[str, ...], at: int, floor: int
) -> tuple[int, ...]:
    found = []
    index = at - 1
    while index >= floor:
        raw = lines[index]
        text = _TITLE_METADATA.sub("", raw.strip())
        if not text or _page_artifact(raw):
            index -= 1
            continue
        if (
            text == text.upper()
            and re.search(r"[A-Z]{2}", text)
            and not text.startswith("CHAPTER ")
            and not _HEADING.match(raw)
        ):
            found.append(index)
            index -= 1
            continue
        break
    return tuple(reversed(found))


def _find_boundaries(lines: tuple[str, ...]) -> ClassificationBoundaries:
    chapter_markers = [
        (i, int(match[1]))
        for i, line in enumerate(lines)
        if (match := _CHAPTER.fullmatch(line))
    ]
    starts = []
    for position, (index, number) in enumerate(chapter_markers):
        if number == 1:
            following = (
                chapter_markers[position + 1][0]
                if position + 1 < len(chapter_markers)
                else len(lines)
            )
            if "This chapter contains the following blocks:" in lines[index:following]:
                starts.append(index)
    if len(starts) != 1:
        raise ReconstructionError("Disease classification start is not unique.")
    appendix_starts = []
    for letter in "ABCD":
        found = next(
            (
                i
                for i, line in enumerate(lines)
                if re.match(r"^\fAppendix " + letter + r":", line)
            ),
            None,
        )
        if found is None:
            raise ReconstructionError("Required appendix boundary missing: " + letter)
        appendix_starts.append(found)
    start = starts[0]
    end, morphology_end = appendix_starts[:2]
    if not start < end < morphology_end < appendix_starts[2] < appendix_starts[3]:
        raise ReconstructionError("Chapter/appendix boundaries are out of order.")
    positions = [(i, n) for i, n in chapter_markers if start <= i < end]
    if [number for _, number in positions] != list(range(1, 23)):
        raise ReconstructionError(
            "Disease classification requires chapters 1 through 22 in order."
        )
    chapters = []
    for position, (chapter_start, number) in enumerate(positions):
        chapter_end = (
            positions[position + 1][0] if position + 1 < len(positions) else end
        )
        ranges = []
        for i in range(chapter_start, chapter_end):
            raw = lines[i]
            if match := _PAREN_RANGE.fullmatch(raw):
                ranges.append((i, match["items"], None))
            elif raw == raw.lstrip() and " (" in raw:
                title, range_text = raw.rsplit(" (", 1)
                match = _PAREN_RANGE.fullmatch("(" + range_text)
                if (
                    match
                    and title == title.upper()
                    and re.search(r"[A-Z]{2}", title)
                    and not _HEADING.fullmatch(raw)
                ):
                    ranges.append((i, match["items"], title))
        if not ranges:
            raise ReconstructionError(f"Chapter {number} has no explicit range.")
        range_line, chapter_ranges, _ = ranges[0]
        blocks = []
        for i, values, inline_title in ranges[1:]:
            title_lines = (
                (i,)
                if inline_title
                else _uppercase_title_before(lines, i, range_line + 1)
            )
            if title_lines:
                title = inline_title or " ".join(
                    _TITLE_METADATA.sub("", lines[j].strip()) for j in title_lines
                )
                blocks.append(
                    BlockHeading(
                        title,
                        i + 1,
                        lines[i],
                        tuple(j + 1 for j in title_lines),
                        _intervals(values),
                        inline_title is not None,
                    )
                )
        if not blocks:
            raise ReconstructionError(
                f"Chapter {number} has no explicit block heading."
            )
        chapters.append(
            Chapter(
                number,
                chapter_start + 1,
                chapter_end,
                range_line + 1,
                _intervals(chapter_ranges),
                tuple(blocks),
            )
        )
    return ClassificationBoundaries(
        start + 1, end, end + 1, morphology_end, tuple(chapters)
    )


def _acs_after(lines: tuple[str, ...], at: int, end: int) -> tuple[ACSReference, ...]:
    """Associate only a contiguous run of blank lines and explicit ACS markers.

    A heading's explanatory text ends this scan, so references from later notes
    are never guessed to belong to the heading. Chapter/block ACS context stays
    separate from candidate-specific references.
    """
    references = []
    for index in range(at + 1, end):
        raw = lines[index]
        if not raw.strip():
            continue
        match = _ACS.fullmatch(raw)
        if not match:
            break
        references.append(
            ACSReference(
                tuple(re.split(r",\s*", match["refs"])), SourceLine(index + 1, raw)
            )
        )
    return tuple(references)


def _display(
    lines: tuple[str, ...], at: int, match: re.Match, chapter_end: int
) -> tuple[str, tuple[str, ...], DisplayDiagnostic | None]:
    display = _TITLE_METADATA.sub("", match["title"]).strip()
    references = tuple(
        item
        for group in _REFERENCE_GROUP.finditer(display)
        for item in re.split(r",\s*", group["items"])
    )
    display = _REFERENCE_GROUP.sub("", display).strip()
    open_reference = _OPEN_REFERENCE.search(display)
    if open_reference:
        references += (open_reference["items"],)
        display = display[: open_reference.start()].rstrip()
    if not display:
        raise ReconstructionError("Classification heading has an empty title.")
    diagnostic = None
    if at + 1 < chapter_end:
        adjacent = lines[at + 1]
        if open_reference:
            diagnostic = DisplayDiagnostic(
                match["token"].rstrip("†*"),
                at + 1,
                display,
                SourceLine(at + 2, adjacent),
                "wrapped_reference_group_unresolved",
                open_reference["items"],
            )
        elif (
            adjacent.strip()
            and not _page_artifact(adjacent)
            and not _HEADING.fullmatch(adjacent)
            and not _INSTRUCTION.match(adjacent.strip())
        ):
            indent = len(adjacent) - len(adjacent.lstrip(" \t"))
            if indent >= match.start("title"):
                diagnostic = DisplayDiagnostic(
                    match["token"].rstrip("†*"),
                    at + 1,
                    display,
                    SourceLine(at + 2, adjacent),
                    "continuation_vs_inclusion_unresolved",
                )
    elif open_reference:
        raise ReconstructionError("Unclosed heading reference at a chapter boundary.")
    return display, references, diagnostic


def _collect_headings(lines: tuple[str, ...], boundaries: ClassificationBoundaries):
    headings = []
    occurrences = []
    reasons = {}
    diagnostics = []
    accepted_spans = {}
    for chapter in boundaries.chapters:
        for i in range(chapter.source_line_start - 1, chapter.source_line_end):
            match = _HEADING.fullmatch(lines[i])
            if not match:
                continue
            token = match["token"]
            code = token.rstrip("†*")
            preceding = tuple(
                block for block in chapter.blocks if block.source_line - 1 < i
            )
            reason = None
            if not preceding:
                reason = "chapter_intro_summary_or_reference"
            elif _RANGE.match(match["title"]):
                reason = "range_narrative_collision"
            elif len(match["prefix"].replace("\uf0b5", "").replace("\uf020", " ")) > 1:
                reason = "indented_inclusion_instruction_or_reference"
            elif not _inside(code, chapter.ranges):
                reason = "outside_explicit_chapter_range"
            elif not any(_inside(code, block.ranges) for block in preceding):
                reason = "no_preceding_matching_block"
            occurrences.append(
                HeadingOccurrence(
                    code,
                    i + 1,
                    lines[i],
                    reason is None,
                    reason or "Real heading in explicit chapter/block at code column.",
                )
            )
            if reason:
                reasons[(i, match.start("token"))] = reason
                continue
            display, references, diagnostic = _display(
                lines, i, match, chapter.source_line_end
            )
            if diagnostic:
                diagnostics.append(diagnostic)
            block = next(
                block for block in reversed(preceding) if _inside(code, block.ranges)
            )
            headings.append(
                CandidateHeading(
                    code,
                    token,
                    display,
                    match["title"],
                    lines[i],
                    i + 1,
                    i + 1,
                    chapter.number,
                    block,
                    "†" in token,
                    "*" in token,
                    "\uf0b5" in match["prefix"],
                    _acs_after(lines, i, chapter.source_line_end),
                    references,
                    CandidateClassification.LEAF_CANDIDATE,
                    (
                        DisplayConfidence.FLAGGED_AMBIGUOUS
                        if diagnostic
                        else DisplayConfidence.CLEAN_SINGLE_LINE
                    ),
                )
            )
            accepted_spans[i] = match.span("token")
    return headings, occurrences, reasons, tuple(diagnostics), accepted_spans


def _classify_headings(
    headings: list[CandidateHeading], occurrences: list[HeadingOccurrence]
):
    groups = defaultdict(list)
    for occurrence in occurrences:
        groups[occurrence.canonical_code].append(occurrence)
    duplicates = tuple(
        DuplicateGroup(code, tuple(rows), sum(row.accepted_heading for row in rows))
        for code, rows in sorted(groups.items())
        if len(rows) > 1
    )
    conflicts = tuple(group for group in duplicates if group.genuine_heading_count > 1)
    if conflicts:
        diagnostics = tuple(
            Rejection(
                _HEADING.fullmatch(row.raw_source_line)["token"],
                row.source_line,
                row.raw_source_line,
                _HEADING.fullmatch(row.raw_source_line).start("token"),
                _HEADING.fullmatch(row.raw_source_line).end("token"),
                SourceRegion.DISEASE_CLASSIFICATION,
                RejectionCategory.AMBIGUOUS,
                "Multiple genuine classification headings; no silent collapse.",
            )
            for group in conflicts
            for row in group.occurrences
            if row.accepted_heading
        )
        raise ReconstructionError(
            "Duplicate real classification headings require review.",
            diagnostics=diagnostics,
            duplicates=conflicts,
        )
    codes = {heading.canonical_code for heading in headings}
    children = defaultdict(list)
    for child in sorted(codes):
        for parent in (child[:3], child[:5]):
            if parent != child and parent in codes:
                children[parent].append(child)
    classified = tuple(
        replace(
            heading,
            classification=(
                CandidateClassification.NONASSIGNABLE_PARENT_CANDIDATE
                if children.get(heading.canonical_code)
                else CandidateClassification.LEAF_CANDIDATE
            ),
            children=tuple(children.get(heading.canonical_code, ())),
        )
        for heading in sorted(headings, key=lambda h: h.canonical_code)
    )
    return classified, duplicates


def _region(index: int, boundaries: ClassificationBoundaries) -> SourceRegion:
    if boundaries.source_line_start - 1 <= index < boundaries.source_line_end:
        return SourceRegion.DISEASE_CLASSIFICATION
    if boundaries.appendix_a_start - 1 <= index < boundaries.appendix_a_end:
        return SourceRegion.APPENDIX_A
    return SourceRegion.EXCLUDED


def _collect_rejections(
    lines: tuple[str, ...],
    boundaries: ClassificationBoundaries,
    reasons: dict,
    accepted_spans: dict,
    codes: set[str],
) -> tuple[Rejection, ...]:
    result = []
    lexical_rules = (
        (
            RejectionCategory.RANGE,
            _RANGE,
            "Classification boundary or cross-reference range; not one disease-code heading.",
        ),
        (
            RejectionCategory.PLACEHOLDER,
            _PLACEHOLDER,
            "Unresolved dash placeholder; no disease-code row may be generated.",
        ),
        (
            RejectionCategory.MORPHOLOGY,
            _MORPHOLOGY,
            "Morphology is a separate domain excluded from the disease catalog.",
        ),
    )
    for i, line in enumerate(lines):
        region = _region(i, boundaries)
        covered = []
        for category, expression, reason in lexical_rules:
            for match in expression.finditer(line):
                if any(a <= match.start() < b for a, b in covered):
                    continue
                covered.append(match.span())
                normalized = (
                    match["a"] + "–" + match["b"]
                    if category == RejectionCategory.RANGE
                    else None
                )
                result.append(
                    Rejection(
                        match[0],
                        i + 1,
                        line,
                        match.start(),
                        match.end(),
                        region,
                        category,
                        reason,
                        normalized,
                    )
                )
        for match in _TOKEN.finditer(line):
            if (
                any(a <= match.start() < b for a, b in covered)
                or accepted_spans.get(i) == match.span()
            ):
                continue
            reason = reasons.get((i, match.start()))
            if reason and match[0].rstrip("†*") in codes:
                category = RejectionCategory.REPEATED_REFERENCE
            elif region == SourceRegion.DISEASE_CLASSIFICATION:
                category = RejectionCategory.NARRATIVE_REFERENCE
            else:
                category = RejectionCategory.EXCLUDED_REGION
            result.append(
                Rejection(
                    match[0],
                    i + 1,
                    line,
                    match.start(),
                    match.end(),
                    region,
                    category,
                    reason
                    or "No independent accepted classification-heading evidence at this occurrence.",
                )
            )
        if region == SourceRegion.DISEASE_CLASSIFICATION and not _HEADING.fullmatch(
            line
        ):
            near_heading = re.match(
                r"^(?P<prefix>[ \t\uf020\uf0b5]*)(?P<token>[A-Za-z][0-9]{2}\S*)", line
            )
            if (
                near_heading
                and len(
                    near_heading["prefix"].replace("\uf0b5", "").replace("\uf020", " ")
                )
                <= 1
                and not any(a <= near_heading.start("token") < b for a, b in covered)
            ):
                result.append(
                    Rejection(
                        near_heading["token"],
                        i + 1,
                        line,
                        near_heading.start("token"),
                        near_heading.end("token"),
                        region,
                        RejectionCategory.MALFORMED,
                        "Code-column token fails heading grammar; not repaired.",
                    )
                )
    return tuple(
        sorted(
            result,
            key=lambda row: (row.source_line, row.column_start, row.category.value),
        )
    )


def _length_counts(headings: tuple[CandidateHeading, ...]) -> tuple[LengthCount, ...]:
    return tuple(
        LengthCount(length, count)
        for length, count in sorted(Counter(h.code_length for h in headings).items())
    )


def _make_report(
    lines,
    boundaries,
    headings,
    occurrences,
    reasons,
    duplicates,
    diagnostics,
    rejections,
):
    parents = tuple(h for h in headings if h.children)
    leaves = tuple(h for h in headings if not h.children)
    codes = {h.canonical_code for h in headings}
    leaf_codes = {h.canonical_code for h in leaves}
    parent_codes = {h.canonical_code for h in parents}
    rejection_counts = Counter((r.source_region, r.category) for r in rejections)
    morphology = {
        r.raw_token
        for r in rejections
        if r.source_region == SourceRegion.APPENDIX_A
        and r.category == RejectionCategory.MORPHOLOGY
    }
    disease = SourceRegion.DISEASE_CLASSIFICATION
    flagged = {d.canonical_code for d in diagnostics}
    counts = ReconstructionCounts(
        chapters_detected=len(boundaries.chapters),
        explicit_block_range_headings_detected=sum(
            len(c.blocks) for c in boundaries.chapters
        ),
        heading_shaped_occurrences=len(occurrences),
        accepted_structural_heading_occurrences=len(headings),
        unique_structural_heading_codes=len(codes),
        repeated_heading_shaped_occurrences=sum(
            len(group.occurrences) - 1 for group in duplicates
        ),
        heading_shaped_references_rejected=len(reasons),
        duplicate_real_classification_headings=sum(
            group.genuine_heading_count > 1 for group in duplicates
        ),
        parent_candidates=len(parents),
        parents_by_code_length=_length_counts(parents),
        leaf_candidates=len(leaves),
        leaves_by_code_length=_length_counts(leaves),
        unexpected_code_lengths=sum(h.code_length not in (3, 4, 5) for h in headings),
        range_occurrences_disease_region=rejection_counts[
            (disease, RejectionCategory.RANGE)
        ],
        unique_normalized_ranges_disease_region=len(
            {
                r.normalized_range
                for r in rejections
                if r.source_region == disease and r.category == RejectionCategory.RANGE
            }
        ),
        placeholder_occurrences_disease_region=rejection_counts[
            (disease, RejectionCategory.PLACEHOLDER)
        ],
        morphology_unique_appendix_a=len(morphology),
        morphology_occurrences_appendix_a=rejection_counts[
            (SourceRegion.APPENDIX_A, RejectionCategory.MORPHOLOGY)
        ],
        narrative_reference_occurrences_disease_region=rejection_counts[
            (disease, RejectionCategory.NARRATIVE_REFERENCE)
        ],
        malformed_code_column_tokens=sum(
            r.category == RejectionCategory.MALFORMED for r in rejections
        ),
        ambiguous_heading_identities=sum(
            r.category == RejectionCategory.AMBIGUOUS for r in rejections
        ),
        display_flags=len(diagnostics),
        leaf_display_flags=len(flagged & leaf_codes),
        dagger_heading_codes=sum(h.dagger for h in headings),
        asterisk_heading_codes=sum(h.asterisk for h in headings),
        dagger_leaf_codes=sum(h.dagger for h in leaves),
        asterisk_leaf_codes=sum(h.asterisk for h in leaves),
        australian_heading_codes=sum(h.australian_code for h in headings),
        australian_leaf_codes=sum(h.australian_code for h in leaves),
        acs_marker_occurrences_disease_region="\n".join(
            lines[boundaries.source_line_start - 1 : boundaries.source_line_end]
        ).count("\uf0d1"),
        headings_with_code_references=sum(bool(h.code_references) for h in headings),
        wrapped_reference_groups=sum(
            d.reason == "wrapped_reference_group_unresolved" for d in diagnostics
        ),
        rejections_by_region_and_category=tuple(
            RejectionCount(region, category, count)
            for (region, category), count in sorted(
                rejection_counts.items(),
                key=lambda item: (item[0][0].value, item[0][1].value),
            )
        ),
    )
    checks = (
        ConsistencyCheck(
            "parent_and_leaf_sets_disjoint", not (parent_codes & leaf_codes)
        ),
        ConsistencyCheck(
            "sets_partition_classification_headings", parent_codes | leaf_codes == codes
        ),
        ConsistencyCheck(
            "no_leaf_has_deeper_child", all(not h.children for h in leaves)
        ),
        ConsistencyCheck("unique_canonical_leaf_codes", len(leaves) == len(leaf_codes)),
        ConsistencyCheck(
            "strict_canonical_code_shape",
            all(re.fullmatch(_CODE, c) is not None for c in codes),
        ),
        ConsistencyCheck(
            "all_headings_have_raw_source_provenance",
            all(h.raw_source_line == lines[h.source_line_start - 1] for h in headings),
        ),
        ConsistencyCheck(
            "every_parent_has_real_children",
            all(
                h.children
                and all(
                    c in codes
                    and c.startswith(h.canonical_code)
                    and c != h.canonical_code
                    for c in h.children
                )
                for h in parents
            ),
        ),
        ConsistencyCheck(
            "display_ambiguities_reported",
            flagged
            == {
                h.canonical_code
                for h in headings
                if h.display_confidence == DisplayConfidence.FLAGGED_AMBIGUOUS
            },
        ),
    )
    if not all(check.passed for check in checks):
        raise ReconstructionError("Reconstruction set consistency failed.")
    return ReconstructionReport(boundaries, counts, duplicates, diagnostics, checks)


def _parse(text: str, filename: str, sha256: str) -> ReconstructionResult:
    # Split on LF only: form-feed page headers must remain on their source line.
    lines = tuple(line.removesuffix("\r") for line in text.split("\n"))
    boundaries = _find_boundaries(lines)
    source = _source_identity(lines, filename, sha256, boundaries.source_line_start - 1)
    headings, occurrences, reasons, diagnostics, accepted_spans = _collect_headings(
        lines, boundaries
    )
    classified, duplicates = _classify_headings(headings, occurrences)
    rejections = _collect_rejections(
        lines,
        boundaries,
        reasons,
        accepted_spans,
        {h.canonical_code for h in classified},
    )
    report = _make_report(
        lines,
        boundaries,
        classified,
        occurrences,
        reasons,
        duplicates,
        diagnostics,
        rejections,
    )
    return ReconstructionResult(source, classified, rejections, report)
