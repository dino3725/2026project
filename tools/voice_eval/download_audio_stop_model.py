#!/usr/bin/env python3
"""Download only the pinned, hash-checked model assets to the local cache."""

import argparse
import hashlib
from pathlib import Path
import tempfile
import urllib.request

from voice_command_pkg.audio_stop_model import (
    AudioStopConfig, DEFAULT_MODEL_DIR, model_hashes, MODEL_REPO, MODEL_REVISION, verify_model,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', default=DEFAULT_MODEL_DIR)
    parser.add_argument('--precision', choices=['int8', 'fp32'], default=AudioStopConfig().precision)
    args = parser.parse_args()
    directory = Path(args.model_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in model_hashes(args.precision).items():
        destination = directory / name
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == expected:
            print(f'verified {name}')
            continue
        url = f'https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}'
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f'download SHA256 mismatch: {name}')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(f'downloaded and verified {name}')
    verify_model(directory, args.precision)
    print(f'model ready: {directory} precision={args.precision}')


if __name__ == '__main__':
    main()
