"""Check the adopted transform and reject invalid camera mount files."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial.transform import Rotation
import yaml

from vision_pkg.camera_mount_config import load_camera_mount


def test_adopted_rpy_xyz_reconstruct_the_selected_camera_transform():
    """Keep optical-camera inversion and Euler order correct at the launch boundary."""
    root = Path(__file__).resolve().parents[3]
    path = root / 'src/vision_pkg/config/camera_mount.yaml'
    config = yaml.safe_load(path.read_text())
    source = root / config['source_profile']
    assert hashlib.sha256(source.read_bytes()).hexdigest() == config['source_profile_sha256']
    candidate = json.loads(source.read_text())
    values = load_camera_mount(path)
    matrix = np.eye(4)
    matrix[:3, 3] = [float(values[k]) for k in ['x', 'y', 'z']]
    matrix[:3, :3] = Rotation.from_euler(
        'xyz', [float(values[k]) for k in ['roll', 'pitch', 'yaw']]
    ).as_matrix()
    assert np.allclose(matrix, candidate['T_ee_camera'], atol=1e-12)
    assert config['required_model_sha256'] == candidate['model_sha256']


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), True, '0.1'])
def test_nonfinite_or_ambiguous_coordinates_are_rejected(tmp_path, bad):
    """Fail before publishing a malformed static transform."""
    path = tmp_path / 'mount.yaml'
    config = {'parent_frame': 'end_effector_link', 'child_frame': 'd435i_link',
              'xyz_m': [bad, 0., 0.], 'rpy_rad': [0., 0., 0.]}
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match='finite'):
        load_camera_mount(path)


def test_frame_swap_cannot_silently_invert_the_transform(tmp_path):
    """Reject a transform labeled with the opposite parent and child."""
    path = tmp_path / 'mount.yaml'
    path.write_text(yaml.safe_dump({
        'parent_frame': 'd435i_link', 'child_frame': 'end_effector_link',
        'xyz_m': [0., 0., 0.], 'rpy_rad': [0., 0., 0.]}))
    with pytest.raises(ValueError, match='parent'):
        load_camera_mount(path)
