#!/usr/bin/env python3
"""Latent-embedding case retrieval for a swarm-trained model (Stage 6).

After aggregation, a clinician looking at a prediction often wants the same thing
they want from a colleague: "show me similar cases you have seen, and what they
turned out to be". This module provides that, using the aggregated model itself
as the similarity measure.

How it works
------------
1. The aggregated checkpoint is loaded and the classification head is bypassed,
   so the model emits the latent embedding it computes just before the head.
2. Every exam in the site's *own* reference set (typically its training split,
   where outcomes are known) is embedded once and cached.
3. For a query exam, its embedding is compared against the cache by cosine
   similarity, and the closest cases are returned with their known labels and the
   model's prediction.

Privacy
-------
Everything here runs at one site, on that site's data. Embeddings are never
exchanged between sites, and the cache never leaves the host. Cross-site
retrieval would require sharing embeddings, which are derived from patient
images and must be treated as patient data — see docs/threat-model.md. That is
deliberately not implemented.

Usage
-----
    python case_retrieval.py build \
        --checkpoint <FL_global_model.pt> --data-root /mnt/data/DUKE_iid \
        --institution node_A --split train --out reference_embeddings.npz

    python case_retrieval.py query \
        --checkpoint <FL_global_model.pt> --data-root /mnt/data/DUKE_iid \
        --institution test --split test --reference reference_embeddings.npz \
        --uid Breast_MRI_002_left --top-k 5
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from predictor import build_dataloader, load_model  # provided by this package


def embed_dataset(model: torch.nn.Module, loader, device: torch.device) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Return (embeddings, uids, labels) for every exam the loader yields."""
    features: list[np.ndarray] = []
    uids: list[str] = []
    labels: list[int] = []

    captured: dict[str, torch.Tensor] = {}

    def hook(_module, _inputs, output):
        # The input to the classification head is the latent representation.
        captured["z"] = _inputs[0].detach()

    head = _resolve_head(model)
    handle = head.register_forward_hook(hook)
    model.eval()
    try:
        with torch.no_grad():
            for batch in loader:
                images = batch["source"].to(device)
                model(images)
                z = captured["z"]
                z = z.reshape(z.shape[0], -1).float().cpu().numpy()
                features.append(z)
                uids.extend(_batch_uids(batch, len(z)))
                labels.extend(int(v) for v in batch["target"].reshape(-1).tolist())
    finally:
        handle.remove()

    return np.concatenate(features, axis=0), uids, np.asarray(labels, dtype=int)


def _resolve_head(model: torch.nn.Module) -> torch.nn.Module:
    """The final linear layer, whose input is the embedding we want."""
    candidates = [
        module
        for name, module in model.named_modules()
        if isinstance(module, torch.nn.Linear)
    ]
    if not candidates:
        raise RuntimeError("No linear layer found; adapt _resolve_head for this architecture.")
    return candidates[-1]


def _batch_uids(batch: dict, count: int) -> list[str]:
    value = batch.get("uid")
    if value is None:
        return [f"case_{i}" for i in range(count)]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(v) for v in np.atleast_1d(value).tolist()]


def normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def cmd_build(args: argparse.Namespace) -> None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = load_model(args.checkpoint).to(device)
    _dataset, loader = build_dataloader(
        args.data_root,
        args.institution,
        split=args.split,
        config=args.config,
        num_workers=args.num_workers,
    )
    embeddings, uids, labels = embed_dataset(model, loader, device)
    np.savez_compressed(
        args.out,
        embeddings=embeddings.astype(np.float32),
        uids=np.array(uids),
        labels=labels,
        meta=json.dumps(
            {
                "checkpoint": str(args.checkpoint),
                "data_root": str(args.data_root),
                "institution": args.institution,
                "split": args.split,
                "dimension": int(embeddings.shape[1]),
                "count": int(embeddings.shape[0]),
            }
        ),
    )
    print(f"cached {embeddings.shape[0]} embeddings of dimension {embeddings.shape[1]} -> {args.out}")


def cmd_query(args: argparse.Namespace) -> None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = load_model(args.checkpoint).to(device)
    reference = np.load(args.reference, allow_pickle=False)
    ref_emb = normalise(reference["embeddings"].astype(np.float32))
    ref_uids = [str(u) for u in reference["uids"]]
    ref_labels = reference["labels"]

    _dataset, loader = build_dataloader(
        args.data_root,
        args.institution,
        split=args.split,
        config=args.config,
        num_workers=args.num_workers,
    )
    query_emb, query_uids, query_labels = embed_dataset(model, loader, device)
    query_emb = normalise(query_emb)

    wanted = args.uid or query_uids[: args.num_queries]
    wanted = [wanted] if isinstance(wanted, str) else list(wanted)

    results = []
    for uid in wanted:
        if uid not in query_uids:
            print(f"  {uid}: not in the selected split, skipped")
            continue
        index = query_uids.index(uid)
        similarity = ref_emb @ query_emb[index]
        # Exclude the query itself if the reference set contains it.
        order = [i for i in np.argsort(-similarity) if ref_uids[i] != uid][: args.top_k]
        neighbours = [
            {
                "uid": ref_uids[i],
                "similarity": round(float(similarity[i]), 4),
                "known_label": int(ref_labels[i]),
            }
            for i in order
        ]
        results.append(
            {
                "query_uid": uid,
                "query_label": int(query_labels[index]),
                "neighbours": neighbours,
                "neighbour_positive_fraction": round(
                    sum(n["known_label"] == args.positive_class for n in neighbours) / max(1, len(neighbours)), 3
                ),
            }
        )
        print(f"\n{uid} (label {query_labels[index]}):")
        for n in neighbours:
            print(f"    {n['uid']:32s} similarity {n['similarity']:.3f}  label {n['known_label']}")

    if args.output_json:
        Path(args.output_json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {args.output_json}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--checkpoint", required=True, type=Path)
    common.add_argument("--data-root", required=True, type=Path)
    common.add_argument("--institution", required=True)
    common.add_argument("--split", default="train", choices=["train", "val", "test"])
    common.add_argument("--config", default="unilateral")
    common.add_argument("--num-workers", type=int, default=4)

    build = sub.add_parser("build", parents=[common], help="Embed a reference set and cache it")
    build.add_argument("--out", required=True, type=Path)
    build.set_defaults(func=cmd_build)

    query = sub.add_parser("query", parents=[common], help="Retrieve similar cases for one or more exams")
    query.add_argument("--reference", required=True, type=Path)
    query.add_argument("--uid", help="Exam to look up; defaults to the first --num-queries of the split")
    query.add_argument("--num-queries", type=int, default=3)
    query.add_argument("--top-k", type=int, default=5)
    query.add_argument("--positive-class", type=int, default=2)
    query.add_argument("--output-json", type=Path)
    query.set_defaults(func=cmd_query)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
