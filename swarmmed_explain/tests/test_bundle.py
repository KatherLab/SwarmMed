import io
import json
import os
import struct

import numpy as np
import pytest
from PIL import Image, PngImagePlugin

from swarmmed_explain import bundle
from swarmmed_explain.bundle import (
    MAX_BUNDLE_BYTES,
    MAX_CASES,
    MAX_CASES_JSON_BYTES,
    MAX_PANELS_PER_CASE,
    MAX_PNG_BYTES,
    MAX_SUMMARIES,
    SCHEMA,
    BundleError,
    BundleWriter,
    check_case_id,
    check_details,
    dumps_cases,
    loads_cases,
    read_bundle,
    safe_copy_bundle,
    sanitize_png,
    validate_bundle,
    validate_cases,
    write_bundle,
)


def make_png(width=6, height=4, color=(200, 30, 30), *, mode="RGB", text=None):
    image = Image.new(mode, (width, height), color)
    info = None
    if text:
        info = PngImagePlugin.PngInfo()
        for key, value in text.items():
            info.add_text(key, value)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def make_description(n_cases=2, n_panels=1, n_summaries=1, *, complete=True):
    cases = [
        {
            "case_id": f"case-{position:03d}",
            "label": "1",
            "prediction": "1",
            "score": 0.9,
            "target": "abnormal",
            "outcome": "TP",
            "details": {"age_group": "60-69", "n_leads": 12},
            "panels": [
                {
                    "method": "occlusion",
                    "title": f"panel {k}",
                    "file": f"case_{position:02d}_panel_{k}.png",
                }
                for k in range(1, n_panels + 1)
            ],
        }
        for position in range(1, n_cases + 1)
    ]
    summaries = [
        {"title": f"summary {k}", "file": f"summary_{k}.png"}
        for k in range(1, n_summaries + 1)
    ]
    return {
        "schema": SCHEMA,
        "complete": complete,
        "info": {
            "methods": ["occlusion"],
            "target": "abnormal",
            "notes": "baseline: zeros",
            "metrics": {"auroc_test": 0.8},
        },
        "cases": cases,
        "summaries": summaries,
    }


def images_for(data, **kwargs):
    return {name: make_png(**kwargs) for name in bundle.bundle_file_names(data)}


@pytest.fixture
def good_bundle(tmp_path):
    data = make_description()
    path = tmp_path / "bundle"
    write_bundle(path, data, images_for(data))
    return path


def write_cases(path, data):
    (path / "cases.json").write_text(json.dumps(data), encoding="utf-8")


# --------------------------------------------------------------------- round trip


def test_write_validate_and_read_round_trip(good_bundle):
    data = validate_bundle(good_bundle)
    assert data == validate_cases(make_description())
    assert sorted(os.listdir(good_bundle)) == [
        "case_01_panel_1.png",
        "case_02_panel_1.png",
        "cases.json",
        "summary_1.png",
    ]
    description, images = read_bundle(good_bundle)
    assert description == data
    assert sorted(images) == [
        "case_01_panel_1.png",
        "case_02_panel_1.png",
        "summary_1.png",
    ]
    assert all(raw.startswith(bundle.PNG_SIGNATURE) for raw in images.values())
    assert bundle.describe(data) == "2 cases, 2 panels, 1 summary"


def test_validate_cases_normalises_missing_optional_values():
    data = validate_cases(
        {"schema": SCHEMA, "complete": False, "cases": [{"case_id": "a1"}]}
    )
    assert data["info"] == {"methods": [], "target": None, "notes": None, "metrics": {}}
    assert data["cases"] == [
        {
            "case_id": "a1",
            "label": None,
            "prediction": None,
            "score": None,
            "target": None,
            "outcome": None,
            "details": {},
            "panels": [],
        }
    ]
    assert data["summaries"] == []


def test_write_bundle_checks_the_images(tmp_path):
    data = make_description(n_cases=1, n_summaries=0)
    with pytest.raises(BundleError, match="no image given"):
        write_bundle(tmp_path / "a", data, {})
    extra = {**images_for(data), "summary_1.png": make_png()}
    with pytest.raises(BundleError, match="not referenced"):
        write_bundle(tmp_path / "b", data, extra)
    with pytest.raises(BundleError, match="not a PNG"):
        write_bundle(
            tmp_path / "c", data, {"case_01_panel_1.png": b"GIF89a" + bytes(40)}
        )
    target = tmp_path / "d"
    target.mkdir()
    (target / "other.txt").write_text("x")
    with pytest.raises(BundleError, match="not empty"):
        write_bundle(target, data, images_for(data))


# --------------------------------------------------------------------- symbolic links


def test_validator_rejects_a_symlinked_bundle_directory(good_bundle, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(good_bundle, target_is_directory=True)
    with pytest.raises(BundleError, match="symbolic link"):
        validate_bundle(link)
    with pytest.raises(BundleError, match="symbolic link"):
        safe_copy_bundle(link, tmp_path / "copy")
    assert os.listdir(tmp_path / "copy") == []


@pytest.mark.parametrize("name", ["case_01_panel_1.png", "cases.json"])
def test_validator_rejects_symlinked_files(good_bundle, tmp_path, name):
    outside = tmp_path / "outside"
    outside.write_bytes((good_bundle / name).read_bytes())
    (good_bundle / name).unlink()
    (good_bundle / name).symlink_to(outside)
    with pytest.raises(BundleError, match="symbolic link"):
        validate_bundle(good_bundle)
    with pytest.raises(BundleError, match="symbolic link"):
        safe_copy_bundle(good_bundle, tmp_path / "copy")
    assert os.listdir(tmp_path / "copy") == []


def test_validator_rejects_dangling_symlinks(good_bundle, tmp_path):
    (good_bundle / "summary_1.png").unlink()
    (good_bundle / "summary_1.png").symlink_to(tmp_path / "missing.png")
    with pytest.raises(BundleError, match="symbolic link"):
        validate_bundle(good_bundle)


# --------------------------------------------------------------------- names and types


@pytest.mark.parametrize(
    "name",
    [
        "notes.txt",
        ".hidden",
        ".cases.json.tmp",
        "CASES.JSON",
        "case_1_panel_1.png",
        "case_00_panel_1.png",
        "case_25_panel_1.png",
        "case_01_panel_0.png",
        "case_01_panel_5.png",
        "case_01_panel_1.PNG",
        "summary_0.png",
        "summary_5.png",
        "explanation.png",
    ],
)
def test_validator_rejects_unknown_names(good_bundle, tmp_path, name):
    (good_bundle / name).write_bytes(make_png())
    with pytest.raises(BundleError, match="unexpected file"):
        validate_bundle(good_bundle)
    with pytest.raises(BundleError, match="unexpected file"):
        safe_copy_bundle(good_bundle, tmp_path / "copy")


def test_validator_rejects_unreferenced_missing_and_irregular_files(good_bundle):
    (good_bundle / "case_02_panel_2.png").write_bytes(make_png())
    with pytest.raises(BundleError, match="case_02_panel_2.png is not referenced"):
        validate_bundle(good_bundle)
    (good_bundle / "case_02_panel_2.png").unlink()

    (good_bundle / "summary_1.png").unlink()
    with pytest.raises(BundleError, match="summary_1.png, which is missing"):
        validate_bundle(good_bundle)

    (good_bundle / "summary_1.png").mkdir()
    with pytest.raises(BundleError, match="not a regular file"):
        validate_bundle(good_bundle)
    (good_bundle / "summary_1.png").rmdir()

    os.mkfifo(good_bundle / "summary_1.png")
    with pytest.raises(BundleError, match="not a regular file"):
        validate_bundle(good_bundle)
    (good_bundle / "summary_1.png").unlink()

    (good_bundle / "cases.json").unlink()
    with pytest.raises(BundleError, match="no cases.json"):
        validate_bundle(good_bundle)


def test_validator_rejects_hard_links(good_bundle, tmp_path):
    os.link(good_bundle / "summary_1.png", tmp_path / "second_name.png")
    with pytest.raises(BundleError, match="hard links"):
        validate_bundle(good_bundle)
    with pytest.raises(BundleError, match="hard links"):
        safe_copy_bundle(good_bundle, tmp_path / "copy")


def test_panel_files_are_fixed_by_position(good_bundle):
    data = make_description()
    data["cases"][0]["panels"][0]["file"] = "case_02_panel_1.png"
    write_cases(good_bundle, data)
    with pytest.raises(BundleError, match="file must be 'case_01_panel_1.png'"):
        validate_bundle(good_bundle)
    data["cases"][0]["panels"][0]["file"] = "../../etc/passwd"
    with pytest.raises(BundleError, match="file must be"):
        validate_cases(data)
    data = make_description()
    data["summaries"][0]["file"] = "summary_2.png"
    with pytest.raises(BundleError, match="file must be 'summary_1.png'"):
        validate_cases(data)


@pytest.mark.parametrize(
    "case_id", ["", " a", "../x", "a/b", "a b", "-a", "x" * 129, "ü1", 7, None]
)
def test_case_ids_are_checked(case_id):
    with pytest.raises(BundleError, match="case_id"):
        check_case_id(case_id)


def test_valid_case_ids_and_duplicates():
    assert check_case_id("fl-client-1:00042") == "fl-client-1:00042"
    assert check_case_id("A.b_c-1") == "A.b_c-1"
    assert check_case_id("x" * 128) == "x" * 128
    data = make_description(n_cases=2, n_panels=0, n_summaries=0)
    data["cases"][1]["case_id"] = data["cases"][0]["case_id"]
    with pytest.raises(BundleError, match="used twice"):
        validate_cases(data)


# --------------------------------------------------------------------- sizes


def test_validator_rejects_an_oversize_cases_json(good_bundle):
    raw = (good_bundle / "cases.json").read_bytes()
    (good_bundle / "cases.json").write_bytes(raw + b" " * MAX_CASES_JSON_BYTES)
    with pytest.raises(BundleError, match="cases.json is .* bytes; the limit is"):
        validate_bundle(good_bundle)
    with pytest.raises(BundleError, match="the limit is"):
        loads_cases(raw + b" " * MAX_CASES_JSON_BYTES)


def test_validator_rejects_an_oversize_png(good_bundle, tmp_path):
    # A sparse file: the size check runs before anything is read.
    os.truncate(good_bundle / "case_01_panel_1.png", MAX_PNG_BYTES + 1)
    with pytest.raises(BundleError, match="case_01_panel_1.png is .* bytes; the limit"):
        validate_bundle(good_bundle)
    with pytest.raises(BundleError, match="the limit is"):
        safe_copy_bundle(good_bundle, tmp_path / "copy")


def test_validator_rejects_an_oversize_bundle(tmp_path):
    path = tmp_path / "big"
    path.mkdir()
    write_cases(path, make_description(n_cases=0, n_summaries=0))
    names = [f"case_{c:02d}_panel_{p}.png" for c in range(1, 10) for p in range(1, 5)]
    for name in names[: MAX_BUNDLE_BYTES // MAX_PNG_BYTES + 1]:
        with open(path / name, "wb") as handle:
            handle.truncate(MAX_PNG_BYTES)
    with pytest.raises(BundleError, match="the bundle holds .* bytes; the limit"):
        validate_bundle(path)


def test_validator_rejects_too_many_pixels(good_bundle, tmp_path):
    raw = bytearray(make_png())
    # Claim 8000 x 8000 pixels in the IHDR header; the size check needs no decoding.
    raw[16:24] = struct.pack(">II", 8000, 8000)
    (good_bundle / "case_01_panel_1.png").write_bytes(bytes(raw))
    with pytest.raises(BundleError, match="8000 x 8000 pixels"):
        validate_bundle(good_bundle)
    with pytest.raises(BundleError, match="pixels"):
        safe_copy_bundle(good_bundle, tmp_path / "copy")
    raw[16:24] = struct.pack(">II", 0, 5)
    with pytest.raises(BundleError, match="no pixels"):
        bundle.check_png(bytes(raw))


# --------------------------------------------------------------------- cases.json


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"{not json", "not valid JSON"),
        (b"", "not valid JSON"),
        (b'{"schema": NaN}', "NaN"),
        (b'{"score": Infinity}', "Infinity"),
        (b'{"schema": "a", "schema": "b"}', "repeats the key"),
        (b"\xff\xfe{}", "UTF-8"),
        (b"[]", "JSON object"),
        (
            b'{"schema": "swarmmed.explanations/2", "complete": true, "cases": []}',
            "schema",
        ),
        (
            b'{"schema": "swarmmed.explanations/1", "complete": "yes", "cases": []}',
            "complete",
        ),
        (
            b'{"schema": "swarmmed.explanations/1", "complete": true, "cases": [],'
            b' "script": "x"}',
            "unknown keys",
        ),
        (b'{"schema": "swarmmed.explanations/1", "complete": true}', "'cases' must"),
        (b"[" * 100000 + b"]" * 100000, "not valid JSON|JSON object"),
    ],
)
def test_validator_rejects_bad_json(good_bundle, raw, message):
    (good_bundle / "cases.json").write_bytes(raw)
    with pytest.raises(BundleError, match=message):
        validate_bundle(good_bundle)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["cases"][0].update(extra=1), "unknown keys"),
        (lambda d: d["cases"][0].update(outcome="positive"), "outcome must be"),
        (lambda d: d["cases"][0].update(score="0.9"), "score must be a number"),
        (lambda d: d["cases"][0].update(score=True), "score must be a number"),
        (lambda d: d["cases"][0].update(label=1), "label must be a string"),
        (lambda d: d["cases"][0].update(label="x" * 65), "65 characters"),
        (lambda d: d["cases"][0].update(target="a\x00b"), "control character"),
        (lambda d: d["cases"][0]["panels"][0].update(method=""), "must not be empty"),
        (lambda d: d["cases"][0]["panels"][0].pop("method"), "method is required"),
        (lambda d: d["info"].update(notes="n" * 4001), "4001 characters"),
        (lambda d: d["info"].update(methods="occlusion"), "list of strings"),
        (lambda d: d["info"].update(metrics={"auc": "high"}), "must be a number"),
        (lambda d: d["info"].update(metrics={"auc": [0.8]}), "must be a number"),
        (lambda d: d["summaries"][0].update(title=None), "title is required"),
    ],
)
def test_validate_cases_rejects_bad_values(change, message):
    data = make_description()
    change(data)
    with pytest.raises(BundleError, match=message):
        validate_cases(data)


def test_notes_may_hold_line_breaks():
    data = make_description()
    data["info"]["notes"] = "line one\nline two\ttabbed"
    assert validate_cases(data)["info"]["notes"] == "line one\nline two\ttabbed"
    data["info"]["target"] = "line one\nline two"
    with pytest.raises(BundleError, match="control character"):
        validate_cases(data)


def test_dumps_and_loads_cases_are_inverse():
    data = make_description()
    raw = dumps_cases(data)
    assert loads_cases(raw) == validate_cases(data)
    assert loads_cases(raw.decode("utf-8")) == validate_cases(data)
    assert raw.endswith(b"\n")


def test_incomplete_bundles_are_rejected_unless_allowed(tmp_path):
    data = make_description(complete=False)
    write_bundle(tmp_path / "b", data, images_for(data))
    with pytest.raises(BundleError, match="incomplete"):
        validate_bundle(tmp_path / "b")
    with pytest.raises(BundleError, match="incomplete"):
        safe_copy_bundle(tmp_path / "b", tmp_path / "copy")
    assert validate_bundle(tmp_path / "b", require_complete=False)["complete"] is False
    copied = safe_copy_bundle(
        tmp_path / "b", tmp_path / "copy2", require_complete=False
    )
    assert copied["data"]["complete"] is False


# --------------------------------------------------------------------- PNG content


@pytest.mark.parametrize(
    "content",
    [
        b"just some text, long enough to have a header-sized prefix",
        b"\xff\xd8\xff\xe0" + bytes(60),
        b"GIF89a" + bytes(60),
        bundle.PNG_SIGNATURE + bytes(4) + b"IDAT" + bytes(40),
        bundle.PNG_SIGNATURE,
    ],
)
def test_validator_rejects_non_png_data(good_bundle, tmp_path, content):
    (good_bundle / "case_02_panel_1.png").write_bytes(content)
    with pytest.raises(BundleError, match="case_02_panel_1.png: (not a PNG|PNG file)"):
        validate_bundle(good_bundle)
    with pytest.raises(BundleError, match="case_02_panel_1.png"):
        safe_copy_bundle(good_bundle, tmp_path / "copy")
    assert os.listdir(tmp_path / "copy") == []


def test_jpeg_named_png_is_rejected(good_bundle):
    buffer = io.BytesIO()
    Image.new("RGB", (5, 5), (1, 2, 3)).save(buffer, format="JPEG")
    (good_bundle / "summary_1.png").write_bytes(buffer.getvalue())
    with pytest.raises(BundleError, match="summary_1.png: not a PNG"):
        validate_bundle(good_bundle)


def test_a_corrupt_png_passes_the_header_check_but_not_the_safe_copy(
    good_bundle, tmp_path
):
    noise = np.random.default_rng(0).integers(0, 256, (20, 20, 3), dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(noise).save(buffer, format="PNG")
    assert len(buffer.getvalue()) > 600
    (good_bundle / "case_02_panel_1.png").write_bytes(buffer.getvalue()[:300])
    # validate_bundle reads headers only, as documented ...
    validate_bundle(good_bundle)
    # ... and the safe copy decodes every image, writes nothing on failure.
    with pytest.raises(BundleError, match="case_02_panel_1.png is not a valid PNG"):
        safe_copy_bundle(good_bundle, tmp_path / "copy")
    assert os.listdir(tmp_path / "copy") == []


# --------------------------------------------------------------------- safe copy


def test_safe_copy_bundle_reencodes_pngs_and_drops_metadata(tmp_path):
    data = make_description(n_cases=2, n_panels=2, n_summaries=1)
    images = images_for(data, text={"Comment": "Jane Doe, born 1950-01-01"})
    images["case_02_panel_1.png"] = make_png(
        7, 5, (10, 20, 30, 128), mode="RGBA", text={"Author": "Jane Doe"}
    )
    images["summary_1.png"] = make_png(9, 3, 128, mode="L")
    source = tmp_path / "src"
    write_bundle(source, data, images)

    result = safe_copy_bundle(source, tmp_path / "dst")

    assert result["data"] == validate_cases(data)
    names = sorted(images)
    assert sorted(result["files"]) == names
    assert sorted(os.listdir(tmp_path / "dst")) == sorted([*names, "cases.json"])
    for name in names:
        original = images[name]
        copied = (tmp_path / "dst" / name).read_bytes()
        assert copied != original
        assert b"Jane" not in copied
        with (
            Image.open(io.BytesIO(copied)) as clean,
            Image.open(io.BytesIO(original)) as source_image,
        ):
            assert clean.size == source_image.size
            assert "Comment" not in clean.info and "Author" not in clean.info
            assert clean.mode in ("RGB", "RGBA")
            expected_mode = "RGBA" if source_image.mode == "RGBA" else "RGB"
            assert clean.mode == expected_mode
            np.testing.assert_array_equal(
                np.asarray(clean), np.asarray(source_image.convert(expected_mode))
            )
        info = result["files"][name]
        assert info["bytes"] == len(copied)
        assert (info["width"], info["height"]) == Image.open(io.BytesIO(copied)).size
    assert (tmp_path / "dst" / "cases.json").read_bytes() == dumps_cases(data)
    assert validate_bundle(tmp_path / "dst") == result["data"]


def test_safe_copy_bundle_downscales_very_wide_images(tmp_path):
    data = make_description(n_cases=1, n_panels=1, n_summaries=0)
    source = tmp_path / "src"
    write_bundle(source, data, {"case_01_panel_1.png": make_png(5000, 10)})
    result = safe_copy_bundle(source, tmp_path / "dst")
    assert result["files"]["case_01_panel_1.png"]["width"] == 4096


def test_safe_copy_bundle_needs_an_empty_destination(good_bundle, tmp_path):
    destination = tmp_path / "dst"
    destination.mkdir()
    (destination / "left_over").write_text("x")
    with pytest.raises(BundleError, match="not empty"):
        safe_copy_bundle(good_bundle, destination)
    link = tmp_path / "dst_link"
    empty = tmp_path / "empty"
    empty.mkdir()
    link.symlink_to(empty, target_is_directory=True)
    with pytest.raises(BundleError, match="not a plain directory"):
        safe_copy_bundle(good_bundle, link)
    assert os.listdir(empty) == []


def test_sanitize_png_rejects_undecodable_data():
    with pytest.raises(BundleError, match="not a PNG"):
        sanitize_png(b"plain text that is not an image at all, but long")
    png, width, height = sanitize_png(make_png(3, 2))
    assert (width, height) == (3, 2)
    assert png.startswith(bundle.PNG_SIGNATURE)


# --------------------------------------------------------------------- limits


def test_too_many_cases_panels_or_summaries(tmp_path):
    with pytest.raises(BundleError, match=f"{MAX_CASES + 1} cases; the limit is 24"):
        validate_cases(make_description(n_cases=MAX_CASES + 1, n_panels=0))
    with pytest.raises(BundleError, match="5 panels; the limit is 4"):
        validate_cases(make_description(n_cases=1, n_panels=MAX_PANELS_PER_CASE + 1))
    with pytest.raises(BundleError, match="5 summaries; the limit is 4"):
        validate_cases(make_description(n_summaries=MAX_SUMMARIES + 1))
    assert (
        len(validate_cases(make_description(n_cases=MAX_CASES, n_panels=4))["cases"])
        == 24
    )

    on_disk = tmp_path / "many"
    on_disk.mkdir()
    write_cases(on_disk, make_description(n_cases=MAX_CASES + 1, n_panels=0))
    with pytest.raises(BundleError, match="25 cases"):
        validate_bundle(on_disk)


def test_bundle_writer_enforces_the_limits(tmp_path):
    writer = BundleWriter(tmp_path / "w")
    for index in range(MAX_CASES):
        assert writer.add_case(f"c{index}") == index + 1
    with pytest.raises(BundleError, match="at most 24 cases"):
        writer.add_case("one-more")
    png = make_png()
    for panel in range(1, MAX_PANELS_PER_CASE + 1):
        assert (
            writer.add_panel_png("c0", "occlusion", png) == f"case_01_panel_{panel}.png"
        )
    with pytest.raises(BundleError, match="4 panels already"):
        writer.add_panel_png("c0", "occlusion", png)
    assert writer.add_panel_png("c23", "ig", png) == "case_24_panel_1.png"
    for position in range(1, MAX_SUMMARIES + 1):
        assert (
            writer.add_summary_png(f"figure {position}", png)
            == f"summary_{position}.png"
        )
    with pytest.raises(BundleError, match="at most 4 summary figures"):
        writer.add_summary_png("figure 5", png)
    data = writer.finish()
    assert len(data["cases"]) == 24
    assert len(data["cases"][0]["panels"]) == 4
    assert len(data["summaries"]) == 4


# --------------------------------------------------------------------- details


@pytest.mark.parametrize(
    "key",
    [
        "name",
        "patient_name",
        "PatientID",
        "first_name",
        "DOB",
        "date_of_birth",
        "birthdate",
        "mrn",
        "MRN_hash",
        "home_address",
        "phone",
        "Email",
        "ssn",
        "insurance_no",
        "accession_number",
    ],
)
def test_identifier_like_detail_keys_are_rejected(key, tmp_path):
    with pytest.raises(BundleError, match="looks like a direct identifier"):
        check_details({key: "x"})
    data = make_description()
    data["cases"][1]["details"] = {key: "x"}
    with pytest.raises(BundleError, match="looks like a direct identifier"):
        validate_cases(data)
    writer = BundleWriter(tmp_path / "w")
    with pytest.raises(BundleError, match="looks like a direct identifier"):
        writer.add_case("c1", details={"age": 61, key: "x"})
    assert writer.case_ids == []


def test_details_rules():
    allowed = {
        "age_group": "60-69",
        "sex": "F",
        "site": "fl-client-1",
        "n_leads": 12,
        "snr_db": 18.5,
        "paced": False,
        "missing": None,
    }
    assert check_details(allowed) == allowed
    converted = check_details(
        {"n": np.int64(3), "x": np.float32(0.5), "b": np.bool_(1)}
    )
    assert converted == {"n": 3, "x": 0.5, "b": True}
    assert type(converted["n"]) is int and type(converted["b"]) is bool
    assert check_details(None) == {}
    with pytest.raises(BundleError, match="flat"):
        check_details({"nested": {"a": 1}})
    with pytest.raises(BundleError, match="flat"):
        check_details({"values": [1, 2]})
    with pytest.raises(BundleError, match="flat dict"):
        check_details([("a", 1)])
    with pytest.raises(BundleError, match="17 keys"):
        check_details({f"k{index}": index for index in range(17)})
    with pytest.raises(BundleError, match="must match"):
        check_details({"a b": 1})
    with pytest.raises(BundleError, match="must match"):
        check_details({1: 1})
    with pytest.raises(BundleError, match="257 characters"):
        check_details({"comment": "x" * 257})
    with pytest.raises(BundleError, match="finite"):
        check_details({"value": float("nan")})
    with pytest.raises(BundleError, match="64-bit"):
        check_details({"value": 2**70})
    with pytest.raises(BundleError, match="bytes as JSON"):
        check_details({f"text_{index}": "y" * 256 for index in range(16)})


# --------------------------------------------------------------------- writer


def test_bundle_writer_is_incomplete_until_finished(tmp_path):
    out = tmp_path / "w"
    writer = BundleWriter(out)
    assert json.loads((out / "cases.json").read_text())["complete"] is False
    writer.add_case(
        "site1-0007",
        label=np.int64(1),
        prediction=True,
        score=np.float32(0.25),
        target=1,
        outcome="FN",
        details={"age_group": "70-79"},
    )
    writer.add_panel_png("site1-0007", "integrated_gradients", make_png(), title="IG")
    writer.set_info(methods=["integrated_gradients"], target="abnormal")
    writer.set_info(notes="baseline: zeros", metrics={"auroc_test": np.float64(0.75)})
    with pytest.raises(BundleError, match="incomplete"):
        validate_bundle(out)
    on_disk = validate_bundle(out, require_complete=False)
    case = on_disk["cases"][0]
    assert (case["label"], case["prediction"], case["target"]) == ("1", "True", "1")
    assert case["score"] == 0.25 and isinstance(case["score"], float)
    assert on_disk["info"] == {
        "methods": ["integrated_gradients"],
        "target": "abnormal",
        "notes": "baseline: zeros",
        "metrics": {"auroc_test": 0.75},
    }

    data = writer.finish()
    assert data["complete"] is True
    assert validate_bundle(out) == data
    assert writer.finish() == data
    with pytest.raises(RuntimeError, match="finished"):
        writer.add_case("late")
    assert not os.path.exists(out / ".cases.json.tmp")


def test_bundle_writer_rejects_bad_calls(tmp_path):
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    with pytest.raises(BundleError, match="not empty"):
        BundleWriter(busy)
    writer = BundleWriter(tmp_path / "w")
    writer.add_case("c1")
    with pytest.raises(BundleError, match="added already"):
        writer.add_case("c1")
    with pytest.raises(BundleError, match="unknown case_id"):
        writer.add_panel_png("c2", "occlusion", make_png())
    with pytest.raises(BundleError, match="not a PNG"):
        writer.add_panel_png("c1", "occlusion", b"not a png at all, really not, no")
    with pytest.raises(BundleError, match="method is required"):
        writer.check_panel("c1", None)
    with pytest.raises(BundleError, match="outcome must be"):
        writer.add_case("c3", outcome="tp")
    with pytest.raises(BundleError, match="finite"):
        writer.add_case("c3", score=float("inf"))
    with pytest.raises(BundleError, match="one string"):
        writer.set_info(methods="occlusion")
    with pytest.raises(BundleError, match="title is required"):
        writer.add_summary_png(None, make_png())
    # Nothing that failed was recorded.
    assert writer.case_ids == ["c1"]
    assert writer.data["cases"][0]["panels"] == []
    assert sorted(os.listdir(tmp_path / "w")) == ["cases.json"]


# ------------------------------------------------- safe copy: memory and output caps


def make_palette_noise(side, seed=0):
    """A palette PNG of random colours: re-encoded as RGB it grows about 3x."""
    rng = np.random.default_rng(seed)
    image = Image.fromarray(rng.integers(0, 256, (side, side), dtype=np.uint8))
    image = image.convert("P")
    image.putpalette(rng.integers(0, 256, 768, dtype=np.uint8).tolist())
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def noise_bundle(path, n_panels=3):
    data = make_description(n_cases=n_panels, n_panels=1, n_summaries=0)
    images = {
        name: make_palette_noise(64, seed=k)
        for k, name in enumerate(bundle.bundle_file_names(data))
    }
    write_bundle(path, data, images)
    return images


def test_safe_copy_bundle_writes_each_image_before_decoding_the_next(
    good_bundle, tmp_path, monkeypatch
):
    events = []
    real_sanitize, real_write = bundle.sanitize_png, bundle._write_new

    def sanitize(data, name="image", **kwargs):
        events.append(("decode", name))
        return real_sanitize(data, name, **kwargs)

    def write(dir_fd, name, data):
        events.append(("write", name))
        real_write(dir_fd, name, data)

    monkeypatch.setattr(bundle, "sanitize_png", sanitize)
    monkeypatch.setattr(bundle, "_write_new", write)
    safe_copy_bundle(good_bundle, tmp_path / "dst")

    names = bundle.bundle_file_names(validate_bundle(good_bundle))
    expected = [(kind, name) for name in names for kind in ("decode", "write")]
    assert events == [*expected, ("write", "cases.json")]


def test_safe_copy_bundle_caps_the_reencoded_bytes(tmp_path, monkeypatch):
    images = noise_bundle(tmp_path / "src")
    outputs = [len(sanitize_png(raw)[0]) for raw in images.values()]
    stored = sum(len(raw) for raw in images.values())
    stored += (tmp_path / "src" / "cases.json").stat().st_size
    # Every input fits the bundle limit; the re-encoded images together do not.
    limit = sum(outputs) - 1
    assert stored < limit
    monkeypatch.setattr(bundle, "MAX_BUNDLE_BYTES", limit)
    with pytest.raises(BundleError, match="re-encoded images hold more than"):
        safe_copy_bundle(tmp_path / "src", tmp_path / "dst")
    assert os.listdir(tmp_path / "dst") == []


def test_safe_copy_bundle_caps_each_reencoded_png(tmp_path, monkeypatch):
    images = noise_bundle(tmp_path / "src")
    raw = images["case_02_panel_1.png"]
    output = len(sanitize_png(raw)[0])
    assert max(len(image) for image in images.values()) < output
    monkeypatch.setattr(bundle, "MAX_PNG_BYTES", output - 1)
    with pytest.raises(BundleError, match="bytes after re-encoding; the limit"):
        safe_copy_bundle(tmp_path / "src", tmp_path / "dst")
    assert os.listdir(tmp_path / "dst") == []


def test_safe_copy_bundle_caps_the_decoded_pixels(good_bundle, tmp_path, monkeypatch):
    # good_bundle holds three 6 x 4 images; allow two of them.
    decoded = []
    real_sanitize = bundle.sanitize_png

    def sanitize(data, name="image", **kwargs):
        decoded.append(name)
        return real_sanitize(data, name, **kwargs)

    monkeypatch.setattr(bundle, "sanitize_png", sanitize)
    monkeypatch.setattr(bundle, "MAX_BUNDLE_PIXELS", 2 * 6 * 4)
    with pytest.raises(BundleError, match="images hold more than 48 pixels"):
        safe_copy_bundle(good_bundle, tmp_path / "dst")
    # The third image is refused from its header, before it is decoded.
    assert len(decoded) == 2
    assert os.listdir(tmp_path / "dst") == []


def test_failed_writes_leave_no_files(good_bundle, tmp_path, monkeypatch):
    real_write = bundle._write_new
    calls = []

    def write(dir_fd, name, data):
        calls.append(name)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        real_write(dir_fd, name, data)

    monkeypatch.setattr(bundle, "_write_new", write)
    with pytest.raises(OSError, match="No space left"):
        safe_copy_bundle(good_bundle, tmp_path / "copy")
    assert os.listdir(tmp_path / "copy") == []
    calls.clear()
    data = make_description()
    with pytest.raises(OSError, match="No space left"):
        write_bundle(tmp_path / "written", data, images_for(data))
    assert os.listdir(tmp_path / "written") == []


def test_a_file_that_cannot_be_written_is_removed(tmp_path):
    released = memoryview(b"png")
    released.release()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError):
            bundle._write_new(fd, "case_01_panel_1.png", released)
    finally:
        os.close(fd)
    assert os.listdir(tmp_path) == []


# --------------------------------------------------------------- destinations


def test_destination_errors_are_bundle_errors(good_bundle, tmp_path):
    missing = tmp_path / "no" / "such" / "out"
    data = make_description()
    with pytest.raises(BundleError, match="parent folder .* does not exist"):
        write_bundle(missing, data, images_for(data))
    with pytest.raises(BundleError, match="parent folder .* does not exist"):
        safe_copy_bundle(good_bundle, missing)
    assert not (tmp_path / "no").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_parent_is_a_bundle_error(good_bundle, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(BundleError, match="cannot create destination"):
            safe_copy_bundle(good_bundle, locked / "out")
    finally:
        locked.chmod(0o700)


def test_check_destination_creates_nothing(tmp_path):
    bundle.check_destination(tmp_path / "new")
    assert not (tmp_path / "new").exists()
    (tmp_path / "empty").mkdir()
    bundle.check_destination(tmp_path / "empty")
    with pytest.raises(BundleError, match="parent folder .* does not exist"):
        bundle.check_destination(tmp_path / "no" / "out")
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "keep.txt").write_text("x")
    with pytest.raises(BundleError, match="not empty"):
        bundle.check_destination(tmp_path / "busy")
    (tmp_path / "file").write_text("x")
    with pytest.raises(BundleError, match="not a plain directory"):
        bundle.check_destination(tmp_path / "file")
    (tmp_path / "link").symlink_to(tmp_path / "empty", target_is_directory=True)
    with pytest.raises(BundleError, match="not a plain directory"):
        bundle.check_destination(tmp_path / "link")
    assert sorted(os.listdir(tmp_path)) == ["busy", "empty", "file", "link"]


# ------------------------------------------------------------ format characters


@pytest.mark.parametrize("text", ["TN‮NT", "T​P", "⁦abnormal⁩", "1﻿", "a­b"])
def test_single_line_text_rejects_format_characters(text, tmp_path):
    data = make_description(n_cases=1, n_panels=1, n_summaries=0)
    for change in (
        lambda d: d["cases"][0].update(label=text),
        lambda d: d["cases"][0].update(prediction=text),
        lambda d: d["cases"][0]["panels"][0].update(title=text),
        lambda d: d["cases"][0]["details"].update(age_group=text),
        lambda d: d["info"].update(target=text),
    ):
        changed = json.loads(json.dumps(data))
        change(changed)
        with pytest.raises(BundleError, match="invisible formatting character"):
            validate_cases(changed)
    writer = BundleWriter(tmp_path / "w")
    writer.add_case("c1")
    with pytest.raises(BundleError, match="invisible formatting character"):
        writer.add_case("c2", label=text)
    with pytest.raises(BundleError, match="invisible formatting character"):
        writer.check_panel("c1", "occlusion", text)


@pytest.mark.parametrize("text", ["line\u2028two", "para\u2029two"])
def test_single_line_text_rejects_line_separators(text):
    data = make_description(n_cases=1, n_panels=1, n_summaries=0)
    data["cases"][0]["label"] = text
    with pytest.raises(BundleError, match="line or paragraph separator"):
        validate_cases(data)


def test_notes_keep_their_format_characters():
    data = make_description()
    data["info"]["notes"] = "Arabic text: ‏مثال\nline two"
    assert validate_cases(data)["info"]["notes"] == data["info"]["notes"]
