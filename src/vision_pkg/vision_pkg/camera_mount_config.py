"""Load the adopted camera mounting transform shared by both vision launches."""

import math
from pathlib import Path

import yaml


def load_camera_mount(path):
    """Reject malformed mounting values rather than starting with a partial TF."""
    data = yaml.safe_load(Path(path).read_text())
    if data['parent_frame'] != 'end_effector_link':
        raise ValueError('Expected end_effector_link camera parent')
    if data['child_frame'] != 'd435i_link':
        raise ValueError('Expected d435i_link camera child')
    result = {}
    for group, names in [('xyz_m', ['x', 'y', 'z']),
                         ('rpy_rad', ['roll', 'pitch', 'yaw'])]:
        values = data[group]
        if len(values) != 3 or not all(
            isinstance(v, (float, int)) and not isinstance(v, bool)
            and math.isfinite(v) for v in values
        ):
            raise ValueError('Expected three finite mounting values')
        result.update(zip(names, (str(float(v)) for v in values)))
    return result
