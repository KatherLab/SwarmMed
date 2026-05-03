#!/usr/bin/env python3
"""Build DUKE MST swarm/local prediction tables and paired DeLong report."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import math
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import numpy as np


DATASETS = ["DUKE_test", "CAM", "MHA", "RUMC", "UKA", "UMCU", "RSH"]
PREDICTION_FIELDS = [
    "model_id",
    "model_family",
    "generated_model",
    "dataset",
    "uid",
    "y_true",
    "y_score",
    "y_pred",
    "checkpoint_md5",
]
SWARM_MODELS = [
    ("swarm_repetition_1", False),
    ("swarm_repetition_2", False),
    ("swarm_repetition_3", True),
]
LOCAL_RUNS = [
    ("node_A", 1, "MST_binary_unilateral_2026_04_14_215628_fold0"),
    ("node_A", 2, "MST_binary_unilateral_2026_04_16_161821_fold0"),
    ("node_A", 3, "MST_binary_unilateral_2026_04_17_161237_fold0"),
    ("node_B", 1, "MST_binary_unilateral_2026_04_14_221009_fold0"),
    ("node_B", 2, "MST_binary_unilateral_2026_04_17_151546_fold0"),
    ("node_B", 3, "MST_binary_unilateral_2026_04_17_163350_fold0"),
    ("node_C", 1, "MST_binary_unilateral_2026_04_14_223444_fold0"),
    ("node_C", 2, "MST_binary_unilateral_2026_04_17_154154_fold0"),
    ("node_C", 3, "MST_binary_unilateral_2026_04_17_165451_fold0"),
    ("node_all", 1, "MST_binary_unilateral_2026_04_15_094319_fold0"),
    ("node_all", 2, "MST_binary_unilateral_2026_04_17_155107_fold0"),
    ("node_all", 3, "MST_binary_unilateral_2026_04_17_170100_fold0"),
]


def file_md5(path: Path) -> str:
    digest = hashlib.md5()  # nosec B324 - checksum reporting, not security.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_scalar(value: str) -> Any:
    parsed = ast.literal_eval(value)
    while isinstance(parsed, (list, tuple)):
        if len(parsed) != 1:
            raise ValueError(f"Expected scalar-like value, got {value!r}")
        parsed = parsed[0]
    return parsed


def read_csv_dicts(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv_dicts(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_odelia_uid_map(meta_dir: Path) -> dict[str, str]:
    uid_to_dataset: dict[str, str] = {}
    for dataset in ["CAM", "MHA", "RUMC", "UKA", "UMCU", "RSH"]:
        path = meta_dir / dataset / "annotation.csv"
        for row in read_csv_dicts(path):
            uid = row["UID"]
            previous = uid_to_dataset.get(uid)
            if previous and previous != dataset:
                raise ValueError(f"UID {uid} appears in both {previous} and {dataset}")
            uid_to_dataset[uid] = dataset
    return uid_to_dataset


def normalize_local_predictions(stat_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_dir = stat_dir / "raw" / "local_iid"
    output_dir = stat_dir / "predictions" / "local"
    uid_to_dataset = load_odelia_uid_map(stat_dir / "metadata" / "odelia")
    all_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []

    for node, run_idx, run_dir in LOCAL_RUNS:
        model_id = f"local_{node}_run{run_idx}"
        model_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        sources = {
            "DUKE_test": raw_dir / node / run_dir / "DUKE_test" / "results.csv",
            "ODELIA_standard": raw_dir / node / run_dir / "ODELIA_standard" / "results.csv",
            "RSH": raw_dir / node / run_dir / "RSH" / "results.csv",
        }

        for row in read_csv_dicts(sources["DUKE_test"]):
            normalized = {
                "model_id": model_id,
                "model_family": "local_iid",
                "generated_model": "false",
                "dataset": "DUKE_test",
                "uid": row["UID"],
                "y_true": int(parse_scalar(row["GT"])),
                "y_score": float(parse_scalar(row["NN_prob"])),
                "y_pred": int(parse_scalar(row["NN"])),
                "checkpoint_md5": "",
            }
            model_rows["DUKE_test"].append(normalized)

        for row in read_csv_dicts(sources["ODELIA_standard"]):
            uid = row["UID"]
            dataset = uid_to_dataset.get(uid)
            if dataset not in {"CAM", "MHA", "RUMC", "UKA", "UMCU"}:
                raise ValueError(f"Could not map standard ODELIA UID {uid}")
            normalized = {
                "model_id": model_id,
                "model_family": "local_iid",
                "generated_model": "false",
                "dataset": dataset,
                "uid": uid,
                "y_true": int(parse_scalar(row["GT"])),
                "y_score": float(parse_scalar(row["NN_prob"])),
                "y_pred": int(parse_scalar(row["NN"])),
                "checkpoint_md5": "",
            }
            model_rows[dataset].append(normalized)

        for row in read_csv_dicts(sources["RSH"]):
            normalized = {
                "model_id": model_id,
                "model_family": "local_iid",
                "generated_model": "false",
                "dataset": "RSH",
                "uid": row["UID"],
                "y_true": int(parse_scalar(row["GT"])),
                "y_score": float(parse_scalar(row["NN_prob"])),
                "y_pred": int(parse_scalar(row["NN"])),
                "checkpoint_md5": "",
            }
            model_rows["RSH"].append(normalized)

        for dataset in DATASETS:
            rows = sorted(model_rows[dataset], key=lambda item: item["uid"])
            if not rows:
                raise ValueError(f"No rows for {model_id} {dataset}")
            write_csv_dicts(output_dir / model_id / f"{dataset}.csv", rows, PREDICTION_FIELDS)
            all_rows.extend(rows)
            source_rows.append(
                {
                    "model_id": model_id,
                    "model_family": "local_iid",
                    "generated_model": False,
                    "dataset": dataset,
                    "run_dir": run_dir,
                    "source": str(
                        sources["DUKE_test"]
                        if dataset == "DUKE_test"
                        else sources["RSH"]
                        if dataset == "RSH"
                        else sources["ODELIA_standard"]
                    ),
                    "source_md5": file_md5(
                        sources["DUKE_test"]
                        if dataset == "DUKE_test"
                        else sources["RSH"]
                        if dataset == "RSH"
                        else sources["ODELIA_standard"]
                    ),
                }
            )

    return all_rows, source_rows


def load_swarm_predictions(stat_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    all_rows: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    for model_id, generated_model in SWARM_MODELS:
        for dataset in DATASETS:
            csv_path = stat_dir / "predictions" / "swarm" / model_id / f"{dataset}.csv"
            json_path = stat_dir / "predictions" / "swarm" / model_id / f"{dataset}.json"
            rows = read_csv_dicts(csv_path)
            normalized = [
                {
                    "model_id": model_id,
                    "model_family": row["model_family"],
                    "generated_model": "true" if generated_model else "false",
                    "dataset": row["dataset"],
                    "uid": row["uid"],
                    "y_true": int(row["y_true"]),
                    "y_score": float(row["y_score"]),
                    "y_pred": int(row["y_pred"]),
                    "checkpoint_md5": row["checkpoint_md5"],
                }
                for row in rows
            ]
            normalized.sort(key=lambda item: item["uid"])
            all_rows.extend(normalized)

            metrics = json.loads(json_path.read_text(encoding="utf-8"))["metrics"]
            recomputed_auc = auc_from_rows(normalized)
            checks.append(
                {
                    "check": "swarm_auc_recompute",
                    "model_id": model_id,
                    "dataset": dataset,
                    "passed": abs(recomputed_auc - float(metrics["auroc"])) < 1e-10,
                    "metric_json_auc": float(metrics["auroc"]),
                    "recomputed_auc": recomputed_auc,
                }
            )
            source_rows.append(
                {
                    "model_id": model_id,
                    "model_family": "swarm",
                    "generated_model": generated_model,
                    "dataset": dataset,
                    "run_dir": "",
                    "source": str(csv_path),
                    "source_md5": file_md5(csv_path),
                }
            )
    return all_rows, source_rows, checks


def compute_midrank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values)
    sorted_values = values[order]
    ranks = np.zeros(len(values), dtype=float)
    i = 0
    while i < len(values):
        j = i
        while j < len(values) and sorted_values[j] == sorted_values[i]:
            j += 1
        ranks[i:j] = 0.5 * (i + j - 1) + 1
        i = j
    out = np.empty(len(values), dtype=float)
    out[order] = ranks
    return out


def auc_from_arrays(y_true: np.ndarray, y_score: np.ndarray) -> float:
    positives = y_true == 1
    m = int(positives.sum())
    n = int((~positives).sum())
    if m == 0 or n == 0:
        return float("nan")
    ranks = compute_midrank(y_score)
    return float((ranks[positives].sum() - m * (m + 1) / 2.0) / (m * n))


def auc_from_rows(rows: list[dict[str, Any]]) -> float:
    y_true = np.asarray([int(row["y_true"]) for row in rows], dtype=int)
    y_score = np.asarray([float(row["y_score"]) for row in rows], dtype=float)
    return auc_from_arrays(y_true, y_score)


def fast_delong(predictions_sorted: np.ndarray, positive_count: int) -> tuple[np.ndarray, np.ndarray]:
    m = positive_count
    n = predictions_sorted.shape[1] - m
    positive = predictions_sorted[:, :m]
    negative = predictions_sorted[:, m:]
    k = predictions_sorted.shape[0]
    tx = np.empty((k, m), dtype=float)
    ty = np.empty((k, n), dtype=float)
    tz = np.empty((k, m + n), dtype=float)
    for row in range(k):
        tx[row, :] = compute_midrank(positive[row, :])
        ty[row, :] = compute_midrank(negative[row, :])
        tz[row, :] = compute_midrank(predictions_sorted[row, :])
    aucs = tz[:, :m].sum(axis=1) / (m * n) - float(m + 1.0) / (2.0 * n)
    v01 = (tz[:, :m] - tx) / n
    v10 = 1.0 - (tz[:, m:] - ty) / m
    sx = np.atleast_2d(np.cov(v01))
    sy = np.atleast_2d(np.cov(v10))
    covariance = sx / m + sy / n
    return aucs, covariance


def paired_delong(y_true: np.ndarray, score_a: np.ndarray, score_b: np.ndarray) -> dict[str, float]:
    order = np.argsort(-y_true)
    sorted_predictions = np.vstack((score_a, score_b))[:, order]
    aucs, covariance = fast_delong(sorted_predictions, int(y_true.sum()))
    diff = float(aucs[0] - aucs[1])
    variance = float(covariance[0, 0] + covariance[1, 1] - 2 * covariance[0, 1])
    if variance <= 0:
        z = 0.0 if abs(diff) < 1e-15 else float("nan")
        p_value = 1.0 if abs(diff) < 1e-15 else float("nan")
    else:
        z = diff / math.sqrt(variance)
        p_value = math.erfc(abs(z) / math.sqrt(2.0))
    return {
        "auc_a": float(aucs[0]),
        "auc_b": float(aucs[1]),
        "auc_delta": diff,
        "z": z,
        "p_value": p_value,
    }


def model_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    y_true = np.asarray([int(row["y_true"]) for row in rows], dtype=int)
    y_pred = np.asarray([int(row["y_pred"]) for row in rows], dtype=int)
    y_score = np.asarray([float(row["y_score"]) for row in rows], dtype=float)
    tn = int(((y_true == 0) & (y_pred == 0)).sum())
    fp = int(((y_true == 0) & (y_pred == 1)).sum())
    fn = int(((y_true == 1) & (y_pred == 0)).sum())
    tp = int(((y_true == 1) & (y_pred == 1)).sum())
    return {
        "n": int(len(y_true)),
        "positives": int(y_true.sum()),
        "negatives": int((y_true == 0).sum()),
        "auroc": auc_from_arrays(y_true, y_score),
        "accuracy": float((y_true == y_pred).mean()),
        "f1": float((2 * tp) / (2 * tp + fp + fn)) if (2 * tp + fp + fn) else 0.0,
        "sensitivity": float(tp / (tp + fn)) if (tp + fn) else 0.0,
        "specificity": float(tn / (tn + fp)) if (tn + fp) else 0.0,
    }


def build_comparisons(all_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by_model_dataset: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    model_meta: dict[str, dict[str, Any]] = {}
    for row in all_rows:
        model_id = row["model_id"]
        dataset = row["dataset"]
        by_model_dataset[(model_id, dataset)][row["uid"]] = row
        model_meta[model_id] = {
            "model_family": row["model_family"],
            "generated_model": str(row["generated_model"]).lower() == "true",
        }

    metric_rows: list[dict[str, Any]] = []
    for (model_id, dataset), uid_rows in sorted(by_model_dataset.items()):
        rows = [uid_rows[uid] for uid in sorted(uid_rows)]
        meta = model_meta[model_id]
        metric_rows.append(
            {
                "dataset": dataset,
                "model_id": model_id,
                "model_family": meta["model_family"],
                "generated_model": meta["generated_model"],
                **model_metrics(rows),
            }
        )

    swarm_real = ["swarm_repetition_1", "swarm_repetition_2"]
    generated_swarm = ["swarm_repetition_3"]
    local_models = [f"local_{node}_run{run_idx}" for node, run_idx, _ in LOCAL_RUNS]
    comparison_specs: list[tuple[str, str]] = []
    comparison_specs.extend((swarm, local) for swarm in swarm_real for local in local_models)
    comparison_specs.append(("swarm_repetition_1", "swarm_repetition_2"))
    comparison_specs.extend((generated_swarm[0], local) for local in local_models)
    comparison_specs.extend((generated_swarm[0], swarm) for swarm in swarm_real)

    pairwise_rows: list[dict[str, Any]] = []
    match_checks: list[dict[str, Any]] = []
    for dataset in DATASETS:
        for model_a, model_b in comparison_specs:
            rows_a = by_model_dataset[(model_a, dataset)]
            rows_b = by_model_dataset[(model_b, dataset)]
            common_uids = sorted(set(rows_a).intersection(rows_b))
            if not common_uids:
                raise ValueError(f"No common UIDs for {dataset}: {model_a} vs {model_b}")
            labels_a = np.asarray([int(rows_a[uid]["y_true"]) for uid in common_uids], dtype=int)
            labels_b = np.asarray([int(rows_b[uid]["y_true"]) for uid in common_uids], dtype=int)
            labels_match = bool(np.array_equal(labels_a, labels_b))
            match_checks.append(
                {
                    "check": "paired_uid_label_match",
                    "dataset": dataset,
                    "model_a": model_a,
                    "model_b": model_b,
                    "passed": labels_match,
                    "n": len(common_uids),
                }
            )
            if not labels_match:
                mismatch_uid = next(uid for uid in common_uids if int(rows_a[uid]["y_true"]) != int(rows_b[uid]["y_true"]))
                raise ValueError(
                    f"Label mismatch for {dataset}: {model_a} vs {model_b} at UID {mismatch_uid}"
                )
            scores_a = np.asarray([float(rows_a[uid]["y_score"]) for uid in common_uids], dtype=float)
            scores_b = np.asarray([float(rows_b[uid]["y_score"]) for uid in common_uids], dtype=float)
            delong = paired_delong(labels_a, scores_a, scores_b)
            generated_model_involved = bool(
                model_meta[model_a]["generated_model"]
                or model_meta[model_b]["generated_model"]
            )
            pairwise_rows.append(
                {
                    "dataset": dataset,
                    "model_a": model_a,
                    "model_b": model_b,
                    "generated_model_involved": generated_model_involved,
                    "exploratory": generated_model_involved,
                    "n": len(common_uids),
                    **delong,
                    "method": "paired_delong_auc",
                }
            )
    return metric_rows, pairwise_rows, match_checks


def sheet_xml(rows: list[list[Any]]) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        "<sheetData>",
    ]
    for r_idx, row in enumerate(rows, start=1):
        out.append(f'<row r="{r_idx}">')
        for c_idx, value in enumerate(row, start=1):
            ref = f"{column_name(c_idx)}{r_idx}"
            if value is None or (isinstance(value, float) and math.isnan(value)):
                out.append(f'<c r="{ref}"/>')
            elif isinstance(value, bool):
                out.append(f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>')
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                out.append(
                    f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
                )
        out.append("</row>")
    out.extend(["</sheetData>", "</worksheet>"])
    return "".join(out)


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def rows_from_dicts(rows: list[dict[str, Any]], fields: list[str]) -> list[list[Any]]:
    return [fields] + [[row.get(field, "") for field in fields] for row in rows]


def write_xlsx(path: Path, sheets: list[tuple[str, list[list[Any]]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_sheets = [(sanitize_sheet_name(name), rows) for name, rows in sheets]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(safe_sheets) + 1)
            )
            + "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(safe_sheets) + 1)
            )
            + "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(
                f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>'
                for i, (name, _) in enumerate(safe_sheets, start=1)
            )
            + "</sheets></workbook>",
        )
        for i, (_, rows) in enumerate(safe_sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", sheet_xml(rows))


def sanitize_sheet_name(name: str) -> str:
    return re.sub(r"[][\\:*?/]", "_", name)[:31]


def main() -> None:
    repo_root = Path.cwd()
    stat_dir = repo_root / "results" / "duke_mst_cli" / "stat_tests"

    swarm_rows, swarm_sources, swarm_checks = load_swarm_predictions(stat_dir)
    local_rows, local_sources = normalize_local_predictions(stat_dir)
    all_rows = sorted(
        swarm_rows + local_rows,
        key=lambda row: (row["dataset"], row["model_family"], row["model_id"], row["uid"]),
    )
    write_csv_dicts(stat_dir / "all_predictions.csv", all_rows, PREDICTION_FIELDS)

    metric_rows, pairwise_rows, match_checks = build_comparisons(all_rows)
    metric_fields = [
        "dataset",
        "model_id",
        "model_family",
        "generated_model",
        "n",
        "positives",
        "negatives",
        "auroc",
        "accuracy",
        "f1",
        "sensitivity",
        "specificity",
    ]
    pairwise_fields = [
        "dataset",
        "model_a",
        "model_b",
        "generated_model_involved",
        "exploratory",
        "n",
        "auc_a",
        "auc_b",
        "auc_delta",
        "z",
        "p_value",
        "method",
    ]
    write_csv_dicts(stat_dir / "pairwise_delong_results.csv", pairwise_rows, pairwise_fields)

    manifest_path = repo_root / "results" / "duke_mst_cli" / "repetition_3" / "perturbation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_md5 = manifest["source_md5"]
    output_md5 = manifest["output_md5"]
    generated_duke = next(
        row
        for row in metric_rows
        if row["model_id"] == "swarm_repetition_3" and row["dataset"] == "DUKE_test"
    )
    validation_checks = [
        {
            "check": "generated_model_md5_differs_from_source",
            "passed": source_md5 != output_md5,
            "source_md5": source_md5,
            "output_md5": output_md5,
        },
        {
            "check": "generated_model_duke_auroc_in_requested_range",
            "passed": 0.87 <= generated_duke["auroc"] <= 0.92,
            "auroc": generated_duke["auroc"],
            "lower": 0.87,
            "upper": 0.92,
        },
        *swarm_checks,
        *match_checks,
    ]
    (stat_dir / "validation_checks.json").write_text(
        json.dumps(validation_checks, indent=2), encoding="utf-8"
    )

    swarm_model_rows = [
        {
            "model_id": model_id,
            "flare_job_id": "1a581be8-069f-45d5-9e87-80b78c7e285a"
            if model_id == "swarm_repetition_1"
            else "37bc58d0-9425-4180-9846-7273cd55d2bf"
            if model_id == "swarm_repetition_2"
            else "deterministic perturbed copy of 37bc58d0-9425-4180-9846-7273cd55d2bf",
            "generated_model": generated_model,
            "checkpoint_md5": next(
                row["checkpoint_md5"]
                for row in all_rows
                if row["model_id"] == model_id
            ),
        }
        for model_id, generated_model in SWARM_MODELS
    ]
    local_model_rows = [
        {
            "model_id": f"local_{node}_run{run_idx}",
            "node": node,
            "run": run_idx,
            "run_dir": run_dir,
            "generated_model": False,
        }
        for node, run_idx, run_dir in LOCAL_RUNS
    ]
    summary = [
        ["item", "value"],
        ["all_prediction_rows", len(all_rows)],
        ["pairwise_delong_rows", len(pairwise_rows)],
        ["exploratory_pairwise_rows", sum(bool(row["exploratory"]) for row in pairwise_rows)],
        ["validation_checks", len(validation_checks)],
        ["validation_checks_failed", sum(not bool(row["passed"]) for row in validation_checks)],
    ]
    for model_id, _ in SWARM_MODELS:
        duke_metric = next(
            row
            for row in metric_rows
            if row["model_id"] == model_id and row["dataset"] == "DUKE_test"
        )
        summary.append([f"{model_id}_DUKE_test_auroc", duke_metric["auroc"]])
        summary.append([f"{model_id}_DUKE_test_n", duke_metric["n"]])
    notes = [
        ["item", "value"],
        ["generated_model", "swarm_repetition_3 is a deterministic perturbed copy of swarm_repetition_2"],
        ["generation_seed", manifest["seed"]],
        ["perturbed_tensor", ", ".join(manifest["tensor_names"])],
        ["perturbation_scale", manifest["scale"]],
        ["changed_element_count", manifest["changed_element_count"]],
        ["exploratory_rows", "Any DeLong row with generated_model_involved=true is exploratory"],
        ["p_test", "p_value is from paired DeLong AUROC test using matched UIDs"],
    ]
    source_fields = [
        "model_id",
        "model_family",
        "generated_model",
        "dataset",
        "run_dir",
        "source",
        "source_md5",
    ]
    check_fields = sorted({key for row in validation_checks for key in row})
    write_xlsx(
        stat_dir / "duke_mst_delong_comparison.xlsx",
        [
            ("Summary", summary),
            ("Per Model Metrics", rows_from_dicts(metric_rows, metric_fields)),
            ("Pairwise DeLong", rows_from_dicts(pairwise_rows, pairwise_fields)),
            ("Swarm Models", rows_from_dicts(swarm_model_rows, ["model_id", "flare_job_id", "generated_model", "checkpoint_md5"])),
            ("Local Models", rows_from_dicts(local_model_rows, ["model_id", "node", "run", "run_dir", "generated_model"])),
            ("Prediction Sources", rows_from_dicts(swarm_sources + local_sources, source_fields)),
            ("Validation Checks", rows_from_dicts(validation_checks, check_fields)),
            ("Model Notes", notes),
        ],
    )
    print(f"wrote {len(all_rows)} prediction rows")
    print(f"wrote {len(pairwise_rows)} pairwise DeLong rows")
    print(stat_dir / "duke_mst_delong_comparison.xlsx")


if __name__ == "__main__":
    main()
