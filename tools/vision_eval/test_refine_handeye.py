"""Check joint reprojection refinement against known synthetic geometry."""

import cv2
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from handeye import transform
from refine_handeye import refine


def test_recovers_known_mount_and_board_without_height_constraint():
    rng = np.random.default_rng(817)
    k = np.array([[900., 0, 640], [0, 900, 360], [0, 0, 1]])
    obj = np.array([[x * .025, y * .025, 0] for y in range(1, 5) for x in range(1, 7)])
    mount = transform(Rotation.from_euler('xyz', [.1, -.05, .08]).as_matrix(), [.02, -.03, .05])
    board = transform(Rotation.from_euler('xyz', [.05, .03, -.02]).as_matrix(), [0, 0, .6])
    ee, images = [], []
    for _ in range(12):
        e = transform(Rotation.from_rotvec(rng.uniform(-.18, .18, 3)).as_matrix(),
                      rng.uniform(-.035, .035, 3))
        ob = np.linalg.inv(e @ mount) @ board
        images.append(cv2.projectPoints(obj, cv2.Rodrigues(ob[:3, :3])[0], ob[:3, 3],
                                        k, np.zeros(5))[0].reshape(-1, 2))
        ee.append(e)
    initial_mount, initial_board = mount.copy(), board.copy()
    initial_mount[:3, 3] += [.007, -.004, .005]
    initial_board[:3, 3] += [-.004, .003, .006]
    estimated_mount, estimated_board, metrics = refine(
        ee, [obj] * 12, images, k, np.zeros(5), initial_mount, initial_board)
    assert np.allclose(estimated_mount, mount, atol=1e-7)
    assert np.allclose(estimated_board, board, atol=1e-7)
    assert metrics['refined_corner_rms_px'] < 1e-7


def test_insufficient_refinement_data_is_rejected():
    with pytest.raises(ValueError, match='eight'):
        refine([], [], [], np.eye(3), np.zeros(5), np.eye(4), np.eye(4))
