"""Prepare or verify the matching +25 mm model before hardware or MoveIt launch."""

import argparse
import hashlib
import json
from pathlib import Path

import xacro

from prepare_measured_model import prepare


def ensure(directory):
    """Verify audited sources, expanded model and vendor-derived launch files."""
    directory = directory.resolve()
    if not directory.exists():
        prepare(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    model_name = 'turtlebot3_manipulation.urdf.xacro'
    for name, expected in manifest['sources_sha256'].items():
        source = Path(name)
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError('Vendor source changed; model needs review: ' + name)
        if source.suffix == '.py':
            body = source.read_text()
            if source.name != 'hardware.launch.py':
                body = body.replace(repr(model_name), repr(str(directory / model_name)))
            if body != (directory / source.name).read_text():
                raise ValueError('Generated launch differs from reviewed source: ' + source.name)
    expanded = xacro.process_file(str(directory / model_name)).toxml()
    expected = manifest['urdf_sha256']
    if (hashlib.sha256(expanded.encode()).hexdigest() != expected
            or hashlib.sha256((directory / 'measured.urdf').read_bytes()).hexdigest() != expected):
        raise ValueError('Generated model hash mismatch')
    config = directory.parents[1] / 'src/vision_pkg/config/camera_mount.yaml'
    if config.exists():
        import yaml
        selected = yaml.safe_load(config.read_text())
        if selected['required_model_sha256'] != expected:
            raise ValueError('Adopted camera calibration expects a different robot model')
    print('Verified calibrated arm model:', expected)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    ensure(parser.parse_args().directory)
