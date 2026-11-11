"""Offline arm/hand-eye residual and identifiability calculations; no ROS."""

import xml.etree.ElementTree as ET

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from handeye import transform
from refine_handeye import pack, unpack


JOINT_NAMES = ['joint1', 'joint2', 'joint3', 'joint4']


class ArmChain:
    """Cache the measured URDF arm chain for vectorized offline hypotheses."""

    def __init__(self, xml):
        """Read fixed origins and revolute axes without changing the source XML."""
        root = ET.fromstring(xml)
        by_child = {j.find('child').get('link'): j for j in root.findall('joint')}
        chain, child = [], 'end_effector_link'
        while child != 'base_link':
            joint = by_child[child]
            chain.append(joint)
            child = joint.find('parent').get('link')
        self.steps = []
        for joint in reversed(chain):
            origin = joint.find('origin')
            xyz = np.fromstring(origin.get('xyz', '0 0 0'), sep=' ')
            rpy = np.fromstring(origin.get('rpy', '0 0 0'), sep=' ')
            matrix = transform(Rotation.from_euler('xyz', rpy).as_matrix(), xyz)
            index, axis = None, None
            if joint.get('type') == 'revolute':
                index = JOINT_NAMES.index(joint.get('name'))
                axis = np.fromstring(joint.find('axis').get('xyz'), sep=' ')
                if not np.isclose(np.linalg.norm(axis), 1):
                    raise ValueError('Expected unit joint axis')
            elif joint.get('type') != 'fixed':
                raise ValueError('Unsupported arm joint')
            self.steps.append((matrix, index, axis))

    def forward(self, q):
        """Calculate model transforms for N joint vectors; not motion targets."""
        q = np.asarray(q, dtype=float)
        if q.ndim != 2 or q.shape[1] != 4 or not np.isfinite(q).all():
            raise ValueError('Expected finite Nx4 joint positions')
        result = np.tile(np.eye(4), (len(q), 1, 1))
        for matrix, index, axis in self.steps:
            result = result @ matrix
            if index is not None:
                rotation = np.tile(np.eye(4), (len(q), 1, 1))
                rotation[:, :3, :3] = Rotation.from_rotvec(q[:, index, None] * axis).as_matrix()
                result = result @ rotation
        return result


def residual(parameters, data, chain, k, d, selected_joints):
    """Predict image corners using a shared mount and one board pose per session."""
    count = max(row['session_index'] for row in data) + 1
    offsets = np.zeros(4)
    offsets[list(selected_joints)] = parameters[6 + 6 * count:]
    poses = chain.forward(np.array([row['q'] for row in data]) + offsets)
    inverse_mount = np.linalg.inv(unpack(parameters[:6]))
    boards = [unpack(parameters[6 + 6 * i:12 + 6 * i]) for i in range(count)]
    errors = []
    for row, pose in zip(data, poses):
        ob = inverse_mount @ np.linalg.inv(pose) @ boards[row['session_index']]
        projected = cv2.projectPoints(row['objects'], cv2.Rodrigues(ob[:3, :3])[0],
                                      ob[:3, 3], k, d)[0].reshape(-1, 2)
        errors.append((projected - row['pixels']).ravel())
    return np.concatenate(errors)


def initial_vector(mount, boards, selected_joints):
    """Pack geometry and zero bias hypotheses in a deterministic order."""
    return np.concatenate([pack(mount)] + [pack(b) for b in boards]
                          + [np.zeros(len(selected_joints))])


def sensitivity(parameters, data, chain, k, d, selected_joints):
    """Inspect the pixel Jacobian with explicit 1-degree/1-mm column scales."""
    count = max(row['session_index'] for row in data) + 1
    scales = np.r_[np.tile([np.pi / 180] * 3 + [.001] * 3, count + 1),
                   np.full(len(selected_joints), np.pi / 180)]
    columns = []
    for i, scale in enumerate(scales):
        delta = np.zeros_like(parameters)
        delta[i] = scale * 1e-3
        columns.append((residual(parameters + delta, data, chain, k, d, selected_joints)
                        - residual(parameters - delta, data, chain, k, d, selected_joints))
                       / 2e-3)
    jacobian = np.array(columns).T
    _, values, directions = np.linalg.svd(jacobian, full_matrices=False)
    return {'scaled_singular_values': values.tolist(),
            'relative_singular_values': (values / values[0]).tolist(),
            'rank_at_relative_1e_7': int(np.sum(values > values[0] * 1e-7)),
            'parameter_count': len(parameters),
            'weakest_scaled_parameter_directions': directions[-3:].tolist(),
            'scales': '1 degree per rotation/bias and 1 mm per translation; diagnostic only'}


def evaluate_geometry(parameters, data, chain, k, d, selected_joints):
    """Report signed metric and pixel residuals without fitting evaluation views."""
    count = max(row['session_index'] for row in data) + 1
    offsets = np.zeros(4)
    offsets[list(selected_joints)] = parameters[6 + 6 * count:]
    poses = chain.forward(np.array([row['q'] for row in data]) + offsets)
    mount = unpack(parameters[:6])
    rows = []
    for row, pose in zip(data, poses):
        fixed = unpack(parameters[6 + 6 * row['session_index']:12 + 6 * row['session_index']])
        ok, rv, tv = cv2.solvePnP(row['objects'], row['pixels'], k, d)
        if not ok:
            raise ValueError('Evaluation PnP failed')
        observed = transform(cv2.Rodrigues(rv)[0], tv)
        predicted = np.linalg.inv(pose @ mount) @ fixed
        if min(np.min((predicted[:3, :3] @ row['objects'].T
                       + predicted[:3, 3, None])[2]),
               np.min((observed[:3, :3] @ row['objects'].T + observed[:3, 3, None])[2])) <= 0:
            raise ValueError('Board behind camera')
        projected = cv2.projectPoints(row['objects'], cv2.Rodrigues(predicted[:3, :3])[0],
                                      predicted[:3, 3], k, d)[0].reshape(-1, 2)
        world = pose @ mount @ observed
        error = (world[:3, 3] - fixed[:3, 3]) * 1000
        angle = Rotation.from_matrix(fixed[:3, :3].T @ world[:3, :3]).as_rotvec() * 180 / np.pi
        rows.append({'name': row['name'], 'session_index': row['session_index'],
                     'role': row['role'], 'q_deg': np.rad2deg(row['q']).tolist(),
                     'position_residual_mm': error.tolist(),
                     'position_error_mm': float(np.linalg.norm(error)),
                     'rotation_residual_deg': angle.tolist(),
                     'pixel_rms_px': float(np.sqrt(np.mean(np.sum(
                         (projected - row['pixels']) ** 2, axis=1))))})
    return rows


def fit_geometry(data, chain, k, d, mount, boards, selected_joints=(), start_bias_deg=0):
    """Fit only training observations; optional bounded biases are diagnostic."""
    if len(data) < 8 or any(row['role'] != 'train' for row in data):
        raise ValueError('Training-only views required')
    p = initial_vector(mount, boards, selected_joints)
    lower, upper = np.full(len(p), -np.inf), np.full(len(p), np.inf)
    if selected_joints:
        p[-len(selected_joints):] = np.deg2rad(start_bias_deg)
        lower[-len(selected_joints):] = np.deg2rad(-5)
        upper[-len(selected_joints):] = np.deg2rad(5)
    fit = least_squares(residual, p, args=(data, chain, k, d, selected_joints),
                        bounds=(lower, upper), x_scale='jac', max_nfev=250,
                        ftol=1e-9, xtol=1e-9, gtol=1e-9)
    if not fit.success or not np.isfinite(fit.x).all():
        raise ValueError('Joint geometry fit did not converge')
    biases = np.rad2deg(fit.x[-len(selected_joints):]) if selected_joints else np.array([])
    return {'parameters': fit.x.tolist(), 'selected_joint_indices': list(selected_joints),
            'bias_hypothesis_deg': biases.tolist(),
            'bias_hit_5deg_bound': bool(np.any(np.abs(biases) > 4.99)),
            'training_corner_rms_px': float(np.sqrt(np.mean(fit.fun ** 2) * 2)),
            'nfev': int(fit.nfev), 'applied': False,
            'identifiability': sensitivity(fit.x, data, chain, k, d, selected_joints)}


def diagnostic_decision(report):
    """Describe unreliable hypotheses without exporting operational calibration."""
    findings = []
    for model, fits in report['models'].items():
        full = fits['all_joint_bias_observability']
        deficit = full['parameter_count'] - full['rank_at_relative_1e_7']
        if deficit:
            findings.append({'model': model, 'issue': 'all_joint_bias_rank_deficit',
                             'unobservable_directions': deficit})
        for name, fit in fits.items():
            if name == 'all_joint_bias_observability':
                continue
            if fit['bias_hit_5deg_bound']:
                findings.append({'model': model, 'fit': name, 'issue': 'bias_bound_hit'})
            if min(fit['identifiability']['relative_singular_values']) < 1e-4:
                findings.append({'model': model, 'fit': name,
                                 'issue': 'weak_scaled_parameter_combination'})
    return {'status': 'diagnostic_only_do_not_apply', 'findings': findings,
            'runtime_calibration_exported': False, 'user_setup_required': False,
            'additional_capture_requested': False,
            'limits': ['Reused validation views are not a new independent holdout.',
                       'Pixel agreement does not prove absolute metric accuracy.',
                       'Rank and weakness thresholds are diagnostic heuristics.']}
