#!/usr/bin/env python3
"""Create a deterministic, slightly perturbed NVFlare global model copy."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import torch


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()  # nosec B324 - checksum reporting, not security.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_key(state_dict: dict[str, object]) -> str:
    head_terms = ("head", "classifier", "classif", "fc", "linear")
    candidates: list[str] = []
    fallback: list[str] = []
    for key, value in state_dict.items():
        if not torch.is_tensor(value) or not value.is_floating_point():
            continue
        if value.ndim < 2 or value.numel() == 0:
            continue
        fallback.append(key)
        lowered = key.lower()
        if any(term in lowered for term in head_terms):
            candidates.append(key)

    if candidates:
        return sorted(candidates)[-1]
    if fallback:
        return sorted(fallback)[-1]
    raise ValueError("No floating 2D+ tensor found to perturb.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--seed", type=int, default=20260502)
    parser.add_argument("--scale", type=float, default=3.0)
    parser.add_argument("--max-elements", type=int, default=5000)
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    manifest = Path(args.manifest).expanduser().resolve()
    payload = torch.load(source, map_location="cpu", weights_only=False)
    if not isinstance(payload, dict) or "model" not in payload:
        raise ValueError(f"{source} is not an NVFlare FL_global_model.pt payload")
    state_dict = payload["model"]
    if not isinstance(state_dict, dict):
        raise ValueError(f"{source} has a non-dict model payload")

    key = _candidate_key(state_dict)
    tensor = state_dict[key].detach().clone()
    flat = tensor.reshape(-1)
    changed_count = min(args.max_elements, flat.numel())
    if changed_count <= 0:
        raise ValueError(f"Cannot perturb empty tensor {key}")

    generator = torch.Generator(device="cpu")
    generator.manual_seed(args.seed)
    indices = torch.randperm(flat.numel(), generator=generator)[:changed_count]
    reference = float(flat.float().std().item())
    if reference == 0.0:
        reference = max(float(flat.float().abs().mean().item()), 1.0)
    noise = torch.randn(changed_count, generator=generator) * reference * args.scale
    flat[indices] = flat[indices] + noise.to(dtype=flat.dtype)
    state_dict[key] = tensor.reshape_as(state_dict[key])

    output.parent.mkdir(parents=True, exist_ok=True)
    torch.save(payload, output)
    source_md5 = _file_md5(source)
    output_md5 = _file_md5(output)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "source": str(source),
                "output": str(output),
                "source_md5": source_md5,
                "output_md5": output_md5,
                "seed": args.seed,
                "scale": args.scale,
                "tensor_names": [key],
                "changed_element_count": int(changed_count),
                "selection": "random_subset_without_replacement",
                "note": (
                    "Generated exploratory checkpoint. Deterministic "
                    "perturbation applied to selected elements from one "
                    "classifier or head-like floating tensor."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"source_md5={source_md5}")
    print(f"output_md5={output_md5}")
    print(f"tensor={key} changed_elements={changed_count} scale={args.scale}")


if __name__ == "__main__":
    main()
