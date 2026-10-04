"""Synthetic fragments only: no proprietary source/catalog or database fixture."""

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from reconstruct_icd10_am import main
from services.icd10_am_reconstruction import (
    CandidateClassification,
    DisplayConfidence,
    ReconstructionError,
    RejectionCategory,
    SourceRegion,
    parse_file,
    parse_text,
)


def synthetic_source(
    body="I10       SYNTHETIC TEST ONLY",
    *,
    overview="",
    intro="",
    inline=False,
    appendix="M8140/3 SYNTHETIC MORPHOLOGY",
):
    """Tiny complete structural scaffold, deliberately not a clinical catalog."""
    lines = [
        "ICD-10-AM Tenth Edition Tabular List",
        "Extracted from EIS eBook, July 2017.",
        intro,
        "CHAPTER 1",  # The overview must not be selected as classification input.
        "SYNTHETIC CATEGORY OVERVIEW",
        overview,
        "CHAPTER 2",
        "SYNTHETIC OVERVIEW CONTINUED",
    ]
    for chapter in range(1, 23):
        lines.extend(
            [
                f"CHAPTER {chapter}",
                "SYNTHETIC CHAPTER",
                "(A00–Z99)",
                "This chapter contains the following blocks:",
            ]
        )
        if chapter == 1:
            lines.extend([overview, ""])
        lines.extend(
            ["SYNTHETIC BLOCK (A00–Z99)"]
            if inline
            else ["SYNTHETIC BLOCK", "(A00–Z99)"]
        )
        if chapter == 1:
            lines.extend(body.split("\n"))
    lines.extend(
        [
            "\fAppendix A: SYNTHETIC MORPHOLOGY",
            appendix,
            "\fAppendix B: SYNTHETIC TABULATION",
            "K38.8 APPENDIX TEST ONLY",
            "\fAppendix C: SYNTHETIC PRINCIPAL DIAGNOSIS",
            "\fAppendix D: SYNTHETIC CHADx",
        ]
    )
    return "\n".join(lines) + "\n"


def heading(result, code):
    return next(h for h in result.headings if h.canonical_code == code)


def test_source_identity_and_provenance():
    text = synthetic_source()
    result = parse_text(text, source_filename="synthetic.txt")
    assert result.source.filename == "synthetic.txt"
    assert result.source.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert result.source.edition_label == "ICD-10-AM Tenth Edition"
    assert result.source.extraction_context == "EIS eBook, July 2017"
    assert all(
        row.raw_text == text.split("\n")[row.number - 1]
        for row in result.source.evidence
    )


@pytest.mark.parametrize(
    "marker",
    [
        "ICD-10-AM",
        "Tenth Edition",
        "Tabular List",
        "Extracted from EIS eBook, July 2017.",
    ],
)
def test_missing_source_identity_is_rejected(marker):
    with pytest.raises(ReconstructionError, match="identity marker missing"):
        parse_text(synthetic_source().replace(marker, "UNIDENTIFIED"))


def test_structural_boundaries_ignore_overview_and_appendices():
    text = synthetic_source(
        intro="K38.8 INTRODUCTORY TEST ONLY",
        overview="K38.8 SUMMARY TEST ONLY",
        body="K38.8     HEADING TEST ONLY",
    )
    result = parse_text(text)
    boundaries = result.report.boundaries
    lines = text.split("\n")
    assert lines[boundaries.source_line_start - 1] == "CHAPTER 1"
    assert lines[boundaries.appendix_a_start - 1].startswith("\fAppendix A:")
    assert [c.number for c in boundaries.chapters] == list(range(1, 23))
    assert boundaries.source_line_end + 1 == boundaries.appendix_a_start
    assert len(result.headings) == 1
    assert heading(result, "K38.8").display == "HEADING TEST ONLY"
    assert len([r for r in result.rejections if r.raw_token == "K38.8"]) == 4
    assert all(
        boundaries.source_line_start
        <= h.source_line_start
        <= boundaries.source_line_end
        for h in result.headings
    )


@pytest.mark.parametrize("replacement", ["CHAPTER 21", "CHAPTER 23", "MISSING CHAPTER"])
def test_chapter_sequence_is_required(replacement):
    with pytest.raises(ReconstructionError, match="chapters 1 through 22"):
        parse_text(synthetic_source().replace("CHAPTER 22", replacement))


@pytest.mark.parametrize("letter", ["A", "B", "C", "D"])
def test_appendix_boundaries_are_required(letter):
    with pytest.raises(ReconstructionError, match="appendix boundary missing"):
        parse_text(
            synthetic_source().replace(
                "\fAppendix " + letter + ":", "\fRemoved " + letter + ":"
            )
        )


def test_inline_block_title_is_generic_and_equivalent():
    first = parse_text(
        synthetic_source(body="A01       GENERIC TEST ONLY", inline=False)
    )
    second = parse_text(
        synthetic_source(body="A01       GENERIC TEST ONLY", inline=True)
    )
    assert (
        [h.canonical_code for h in first.leaves]
        == [h.canonical_code for h in second.leaves]
        == ["A01"]
    )
    block = second.headings[0].block
    assert block.inline_title
    assert block.title == "SYNTHETIC BLOCK"
    assert block.raw_range_line == "SYNTHETIC BLOCK (A00–Z99)"


def test_three_character_leaf_and_parent_with_real_children():
    result = parse_text(
        synthetic_source(
            body="I10       LEAF TEST ONLY\nI12       PARENT TEST ONLY\nI12.0     CHILD ZERO TEST ONLY\nI12.9     CHILD NINE TEST ONLY"
        )
    )
    assert (
        heading(result, "I10").classification == CandidateClassification.LEAF_CANDIDATE
    )
    parent = heading(result, "I12")
    assert (
        parent.classification == CandidateClassification.NONASSIGNABLE_PARENT_CANDIDATE
    )
    assert parent.children == ("I12.0", "I12.9")


def test_four_character_parent_and_five_character_leaf():
    result = parse_text(
        synthetic_source(body="C95.0     PARENT TEST ONLY\nC95.00    CHILD TEST ONLY")
    )
    assert heading(result, "C95.0").children == ("C95.00",)
    assert (
        heading(result, "C95.00").classification
        == CandidateClassification.LEAF_CANDIDATE
    )
    assert heading(result, "C95.00").code_length == 5


def test_missing_intermediate_prefix_is_not_fabricated():
    result = parse_text(
        synthetic_source(body="X85       PARENT TEST ONLY\nX85.00    CHILD TEST ONLY")
    )
    assert heading(result, "X85").children == ("X85.00",)
    assert {h.canonical_code for h in result.headings} == {"X85", "X85.00"}
    assert "X85.0" not in {
        row["canonical_code"] for row in json.loads(result.to_json())["headings"]
    }


def test_ranges_are_rejected_without_accepting_endpoints():
    result = parse_text(
        synthetic_source(body="A00–A09   RANGE TEST ONLY\nI10       HEADING TEST ONLY")
    )
    assert [h.canonical_code for h in result.leaves] == ["I10"]
    rejection = next(r for r in result.rejections if r.raw_token == "A00–A09")
    assert rejection.category == RejectionCategory.RANGE
    assert rejection.normalized_range == "A00–A09"


@pytest.mark.parametrize("token", ["M90.7-*", "M90.-†", "C95.0-", "M90.7-"])
def test_placeholders_remain_diagnostic_tokens(token):
    result = parse_text(
        synthetic_source(
            body=token + " PLACEHOLDER TEST ONLY\nI10       HEADING TEST ONLY"
        )
    )
    assert [h.canonical_code for h in result.leaves] == ["I10"]
    rejection = next(r for r in result.rejections if r.raw_token == token)
    assert rejection.category == RejectionCategory.PLACEHOLDER


def test_morphology_is_excluded_and_counted_uniquely():
    result = parse_text(
        synthetic_source(
            appendix="M8140/3 MORPHOLOGY TEST ONLY\nM8140/3 REPEATED TEST ONLY\nM8000/0 SECOND TEST ONLY"
        )
    )
    assert result.report.counts.morphology_unique_appendix_a == 2
    assert result.report.counts.morphology_occurrences_appendix_a == 3
    assert all(not h.canonical_code.startswith("M8140") for h in result.headings)
    assert all(
        r.source_region == SourceRegion.APPENDIX_A
        for r in result.rejections
        if r.category == RejectionCategory.MORPHOLOGY
    )


@pytest.mark.parametrize(
    "raw,code,dagger,asterisk",
    [("B37.3†", "B37.3", True, False), ("N77.1*", "N77.1", False, True)],
)
def test_coding_symbols_are_metadata(raw, code, dagger, asterisk):
    item = parse_text(synthetic_source(body=raw + "    SYMBOL TEST ONLY")).headings[0]
    assert item.raw_code == raw
    assert item.canonical_code == code
    assert (item.dagger, item.asterisk) == (dagger, asterisk)


def test_australian_marker_is_separate_from_code():
    item = parse_text(
        synthetic_source(body="\uf0b5C95.00   MARKER TEST ONLY")
    ).headings[0]
    assert item.australian_code
    assert item.raw_code == item.canonical_code == "C95.00"
    assert item.raw_source_line.startswith("\uf0b5")


def test_acs_reference_association_is_conservative():
    text = synthetic_source(
        body="I10       ACS TEST ONLY\n\n\uf0d1 0049, 0050\n          SYNTHETIC INCLUSION\n\uf0d1 0999\nI12       OTHER TEST ONLY"
    )
    item = heading(parse_text(text), "I10")
    assert len(item.acs_references) == 1
    assert item.acs_references[0].numbers == ("0049", "0050")
    provenance = item.acs_references[0].provenance
    assert text.split("\n")[provenance.number - 1] == provenance.raw_text
    assert "0999" not in str(item.acs_references)
    assert "0049" not in item.display


def test_narrative_and_introductory_collisions_cannot_create_candidates():
    text = synthetic_source(
        intro="Example K38.8 INTRODUCTORY TEST ONLY",
        body="K38.8     REAL HEADING TEST ONLY\n          K38.8 INDENTED REFERENCE TEST ONLY\n          Excludes A00.9\n          Narrative I12.0",
    )
    result = parse_text(text)
    assert [h.canonical_code for h in result.headings] == ["K38.8"]
    assert any(
        r.raw_token == "K38.8" and r.source_region == SourceRegion.EXCLUDED
        for r in result.rejections
    )
    assert any(
        r.raw_token == "I12.0" and r.category == RejectionCategory.NARRATIVE_REFERENCE
        for r in result.rejections
    )


def test_repeated_heading_shaped_references_are_analyzed_before_collapse():
    result = parse_text(
        synthetic_source(
            overview="I10 SUMMARY TEST ONLY", body="I10       REAL TEST ONLY"
        )
    )
    assert len(result.headings) == 1
    group = result.report.duplicates[0]
    assert group.canonical_code == "I10"
    assert group.genuine_heading_count == 1
    assert len(group.occurrences) == 2
    assert {r.accepted_heading for r in group.occurrences} == {False, True}
    assert result.report.counts.duplicate_real_classification_headings == 0
    assert any(
        r.category == RejectionCategory.REPEATED_REFERENCE for r in result.rejections
    )


def test_duplicate_real_headings_fail_with_structured_evidence():
    with pytest.raises(ReconstructionError, match="Duplicate real") as error:
        parse_text(
            synthetic_source(
                body="I10       FIRST TEST ONLY\nI10       SECOND TEST ONLY"
            )
        )
    assert error.value.duplicates[0].genuine_heading_count == 2
    assert len(error.value.diagnostics) == 2
    assert all(
        r.category == RejectionCategory.AMBIGUOUS for r in error.value.diagnostics
    )


def test_duplicate_heading_diagnostics_preserve_original_symbol_tokens():
    with pytest.raises(ReconstructionError) as error:
        parse_text(
            synthetic_source(
                body="B37.3†    FIRST TEST ONLY\nB37.3†    SECOND TEST ONLY"
            )
        )
    assert all(row.raw_token == "B37.3†" for row in error.value.diagnostics)
    assert all(
        row.raw_source_line[row.column_start : row.column_end] == row.raw_token
        for row in error.value.diagnostics
    )


@pytest.mark.parametrize(
    "title,display,references",
    [
        ("TITLE TEST ONLY CHADx CR", "TITLE TEST ONLY", ()),
        ("TITLE TEST ONLY (N77.1*) CHADx", "TITLE TEST ONLY", ("N77.1*",)),
        ("TITLE TEST ONLY (K50.-†)", "TITLE TEST ONLY", ("K50.-†",)),
        ("TITLE TEST ONLY (C00–D48†)", "TITLE TEST ONLY", ("C00–D48†",)),
        ("TITLE TEST ONLY (K50.-†), hand", "TITLE TEST ONLY, hand", ("K50.-†",)),
        (
            "TITLE TEST ONLY (ordinary title text)",
            "TITLE TEST ONLY (ordinary title text)",
            (),
        ),
    ],
)
def test_only_proven_display_metadata_is_separated(title, display, references):
    item = parse_text(synthetic_source(body="I10       " + title)).headings[0]
    assert item.display == display
    assert item.code_references == references
    assert item.raw_heading_title == title
    assert item.source_line_start == item.source_line_end


def test_ambiguous_display_never_appends_text_or_changes_membership():
    clean = parse_text(synthetic_source(body="I10       FRAGMENT TEST ONLY"))
    uncertain = parse_text(
        synthetic_source(
            body="I10       FRAGMENT TEST ONLY\n          possible continuation or inclusion"
        )
    )
    item = uncertain.headings[0]
    assert item.display == clean.headings[0].display
    assert item.display_confidence == DisplayConfidence.FLAGGED_AMBIGUOUS
    assert clean.headings[0].display_confidence == DisplayConfidence.CLEAN_SINGLE_LINE
    assert item.canonical_code == clean.headings[0].canonical_code
    assert (
        uncertain.report.display_diagnostics[0].adjacent.raw_text.strip()
        == "possible continuation or inclusion"
    )
    assert item.source_line_end == item.source_line_start


@pytest.mark.parametrize(
    "instruction",
    [
        "Includes: TEST ONLY",
        "Excludes: TEST ONLY",
        "Note: TEST ONLY",
        "See site code",
        "[0-9]",
        "Extracted from EIS eBook, July 2017.",
        "\fPage header 1",
    ],
)
def test_instructions_and_page_artifacts_are_not_appended(instruction):
    result = parse_text(
        synthetic_source(
            body=(
                "I10       TITLE TEST ONLY\n          " + instruction
                if not instruction.startswith("\f")
                and not instruction.startswith("Extracted")
                else "I10       TITLE TEST ONLY\n" + instruction
            )
        )
    )
    assert result.headings[0].display == "TITLE TEST ONLY"


def test_wrapped_reference_is_flagged_without_guessing_completion():
    result = parse_text(
        synthetic_source(body="I10       TITLE TEST ONLY (A00–B94.9,\n          B99†)")
    )
    item = result.headings[0]
    assert item.display == "TITLE TEST ONLY"
    assert item.code_references == ("A00–B94.9",)
    assert item.display_confidence == DisplayConfidence.FLAGGED_AMBIGUOUS
    diagnostic = result.report.display_diagnostics[0]
    assert diagnostic.reason == "wrapped_reference_group_unresolved"
    assert diagnostic.adjacent.raw_text == "          B99†)"
    assert diagnostic.raw_reference_fragment == "A00–B94.9"


@pytest.mark.parametrize("token", ["A00.999", "A00..9", "a00.9", "A00.9x", "A00.9**"])
def test_malformed_code_column_tokens_are_flagged_not_repaired(token):
    result = parse_text(
        synthetic_source(body=token + " MALFORMED TEST ONLY\nI10       REAL TEST ONLY")
    )
    assert [h.canonical_code for h in result.headings] == ["I10"]
    assert result.report.counts.malformed_code_column_tokens == 1
    rejection = next(
        r for r in result.rejections if r.category == RejectionCategory.MALFORMED
    )
    assert rejection.raw_token == token


def test_deterministic_ordering_immutable_results_and_set_consistency():
    text = synthetic_source(
        body="I12.9     CHILD TEST ONLY\nC95.00    CHILD TEST ONLY\nI10       LEAF TEST ONLY\nI12       PARENT TEST ONLY\nC95.0     PARENT TEST ONLY\nI12.0     CHILD TEST ONLY"
    )
    first, second = parse_text(text), parse_text(text)
    assert first == second
    assert first.to_json() == second.to_json()
    assert [h.canonical_code for h in first.headings] == sorted(
        h.canonical_code for h in first.headings
    )
    leaves = {h.canonical_code for h in first.leaves}
    parents = {h.canonical_code for h in first.parents}
    assert not leaves & parents
    assert len(leaves) == len(first.leaves)
    assert all(not h.children for h in first.leaves)
    assert all(check.passed for check in first.report.consistency_checks)
    with pytest.raises(FrozenInstanceError):
        first.headings[0].display = "MODIFIED"
    with pytest.raises(FrozenInstanceError):
        first.report.counts.leaf_candidates = 999
    detached = first.to_dict()
    detached["headings"][0]["display"] = "MODIFIED"
    assert first.headings[0].display != "MODIFIED"


def test_parse_file_hashes_actual_bytes_and_preserves_form_feed(tmp_path):
    data = synthetic_source().replace("\n", "\r\n").encode("utf-8")
    path = tmp_path / "synthetic.txt"
    path.write_bytes(data)
    result = parse_file(path)
    assert result.source.sha256 == hashlib.sha256(data).hexdigest()
    assert result.report.counts.chapters_detected == 22
    assert path.read_bytes() == data


def test_cli_defaults_to_read_only_structured_report(tmp_path, capsys):
    source = tmp_path / "synthetic.txt"
    text = synthetic_source()
    source.write_text(text, encoding="utf-8")
    assert main(["--source", str(source)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["counts"]["leaf_candidates"] == 1
    assert "reconstructed candidate catalog" in summary["reconstruction_label"]
    assert list(tmp_path.iterdir()) == [source]
    assert source.read_text(encoding="utf-8") == text


def test_cli_optional_artifacts_include_confidence_and_candidate_label(
    tmp_path, capsys
):
    source = tmp_path / "synthetic.txt"
    source.write_text(
        synthetic_source(
            body="I10       FRAGMENT TEST ONLY\n          ambiguous continuation"
        ),
        encoding="utf-8",
    )
    output = tmp_path / "output"
    assert main(["--source", str(source), "--output-dir", str(output)]) == 0
    capsys.readouterr()
    catalog = json.loads(
        (output / "candidate_catalog.json").read_text(encoding="utf-8")
    )
    assert catalog["candidates"][0]["classification"] == "leaf_candidate"
    assert catalog["candidates"][0]["display_confidence"] == "flagged_ambiguous"
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    assert main(["--source", str(source), "--output-dir", str(output)]) == 2
    assert "overwrite" in capsys.readouterr().out
    assert before == {path.name: path.read_bytes() for path in output.iterdir()}


def test_cli_protects_source_when_its_name_collides_with_output(tmp_path, capsys):
    source = tmp_path / "candidate_catalog.json"
    original = synthetic_source().encode()
    source.write_bytes(original)
    assert main(["--source", str(source), "--output-dir", str(tmp_path)]) == 2
    assert source.read_bytes() == original
    assert "overwrite" in capsys.readouterr().out


def test_cli_reports_malformed_source_without_silent_success(tmp_path, capsys):
    source = tmp_path / "synthetic.txt"
    source.write_text(
        synthetic_source(body="A00.999 MALFORMED TEST ONLY"), encoding="utf-8"
    )
    assert main(["--source", str(source), "--report-only"]) == 2
    assert (
        json.loads(capsys.readouterr().out)["counts"]["malformed_code_column_tokens"]
        == 1
    )


def test_parser_and_cli_import_and_run_without_database_or_application(tmp_path):
    source = tmp_path / "synthetic.txt"
    source.write_text(synthetic_source(), encoding="utf-8")
    root = Path(__file__).resolve().parent
    script = """
import builtins, json, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
blocked = {"sqlalchemy", "psycopg", "sqlite3", "fastapi", "database", "async_database", "models", "auth", "dotenv", "import_icd10_am"}
def guarded(name, *args, **kwargs):
    if name.split(".")[0] in blocked:
        raise AssertionError("Forbidden parser dependency: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from reconstruct_icd10_am import main
raise SystemExit(main(["--source", sys.argv[2], "--report-only"]))
"""
    process = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(root), str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["counts"]["leaf_candidates"] == 1
