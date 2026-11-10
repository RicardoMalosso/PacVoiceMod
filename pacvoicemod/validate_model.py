"""Validate the staged checkpoint with a restricted CPU-only PyTorch load."""

import sys

try:
    import torch
except ImportError:
    raise SystemExit("Install the CPU PyTorch validation dependency listed in the README.")


def validate(path):
    checkpoint = torch.load(path, map_location="cpu", weights_only=True)
    if not isinstance(checkpoint, dict):
        raise ValueError()
    weights = checkpoint.get("weight")
    config = checkpoint.get("config")
    if not (isinstance(weights, dict) and weights and
            all(isinstance(tensor, torch.Tensor) for tensor in weights.values()) and
            isinstance(config, (list, tuple)) and len(config) == 18 and
            config[-1] in (32000, 40000, 48000) and
            checkpoint.get("version", "v1") in ("v1", "v2") and
            checkpoint.get("f0") in (0, 1)):
        raise ValueError()


if __name__ == "__main__":
    try:
        validate(sys.argv[1])
    except Exception:
        raise SystemExit("Expected a standard RVC inference .pth (v1/v2). Unsafe or invalid checkpoint rejected.")
