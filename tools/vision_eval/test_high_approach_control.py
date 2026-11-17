"""Reject unsafe/stale trajectory artifacts without sending actuator goals."""

from copy import deepcopy
import math

import pytest

from high_approach_control import validate_trajectory


def example():
    points = [
        {'positions': [0.0, -0.5, -0.7, 2.0], 'velocities': [0.0] * 4,
         'accelerations': [0.0] * 4,
         'time_from_start': {'sec': 0, 'nanosec': 0}},
        {'positions': [0.05, 0.7, -0.8, 0.1], 'velocities': [0.0] * 4,
         'accelerations': [0.0] * 4,
         'time_from_start': {'sec': 5, 'nanosec': 0}},
    ]
    return {
        'approved': True, 'executed': False, 'model_sha256': 'example-model',
        'velocity_scale': 0.1, 'acceleration_scale': 0.1,
        'target_m': [0.27, 0.02, 0.29], 'response': {'trajectory': {
            'joint_trajectory': {
                'joint_names': ['joint1', 'joint2', 'joint3', 'joint4'],
                'points': points},
            'multi_dof_joint_trajectory': {'points': []},
        }},
    }


def test_accepts_only_reviewed_high_arm_trajectory():
    plan = example()
    trajectory, duration = validate_trajectory(
        plan, [0, -0.5, -0.7, 2], 'example-model')
    assert duration == 5
    assert trajectory == plan['response']['trajectory']


@pytest.mark.parametrize('case', [
    'unapproved', 'replay', 'model', 'speed_scale', 'low_target', 'gripper',
    'wheel', 'nan', 'time', 'velocity', 'acceleration', 'elbow', 'drift',
])
def test_rejects_invalid_or_stale_plan(case):
    plan = deepcopy(example())
    current = [0, -0.5, -0.7, 2]
    trajectory = plan['response']['trajectory']
    point = trajectory['joint_trajectory']['points'][-1]
    if case == 'unapproved':
        plan['approved'] = False
    elif case == 'replay':
        plan['executed'] = True
    elif case == 'model':
        plan['model_sha256'] = 'another-model'
    elif case == 'speed_scale':
        plan['velocity_scale'] = 0.2
    elif case == 'low_target':
        plan['target_m'][2] = 0.20
    elif case == 'gripper':
        names = trajectory['joint_trajectory']['joint_names']
        names[-1] = 'gripper_left_joint'
    elif case == 'wheel':
        trajectory['multi_dof_joint_trajectory']['points'] = [{}]
    elif case == 'nan':
        point['positions'][0] = float('nan')
    elif case == 'time':
        point['time_from_start']['sec'] = 0
    elif case == 'velocity':
        point['velocities'][0] = 0.5
    elif case == 'acceleration':
        point['accelerations'][0] = 1.1
    elif case == 'elbow':
        point['positions'][2] = -0.94
    elif case == 'drift':
        current[0] = 0.1
    with pytest.raises(ValueError):
        validate_trajectory(plan, current, 'example-model')


@pytest.mark.parametrize('tamper', [None, 'elbow', 'lower', 'endpoint'])
def test_diagnostic_only_allows_reviewed_shoulder_lift(tamper):
    plan = example()
    plan.update(
        kind='diagnostic_lift', target_m=[0.26, 0.02, 0.328],
        previous_reference_rad=[0.05, 0.7 + math.radians(6), -0.8, 0.1],
        diagnostic_target_rad=[0.05, 0.7, -0.8, 0.1])
    if tamper == 'elbow':
        plan['diagnostic_target_rad'][2] += 0.02
    elif tamper == 'lower':
        plan['target_m'][2] = 0.25
    elif tamper == 'endpoint':
        trajectory = plan['response']['trajectory']['joint_trajectory']
        trajectory['points'][-1]['positions'][1] += 0.02
    if tamper is None:
        validate_trajectory(plan, [0, -0.5, -0.7, 2], 'example-model')
    else:
        with pytest.raises(ValueError):
            validate_trajectory(plan, [0, -0.5, -0.7, 2], 'example-model')


@pytest.mark.parametrize('tamper', [None, 'trajectory', 'velocity', 'margin', 'overlap', 'bound', 'nan', 'linear'])
def test_screened_approach_binds_full_trajectory_and_clearance(tamper):
    import hashlib
    import json
    plan = example()
    trajectory = plan['response']['trajectory']
    positions = [p['positions'] for p in trajectory['joint_trajectory']['points']]
    plan.update(kind='screened_grasp_approach', target_m=[.27,.02,.27], path_screen={
        'trajectory_positions_sha256': hashlib.sha256(json.dumps(positions,separators=(',',':')).encode()).hexdigest(),
        'trajectory_sha256': hashlib.sha256(json.dumps(trajectory,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'sampled_envelope_clear': True, 'possible_overlap_count': 0,
        'min_separating_axis_gap_m': .014, 'max_between_sample_motion_bound_m': .004,
        'interpolation': 'quintic_with_derivative_bound'})
    if tamper == 'trajectory': trajectory['joint_trajectory']['points'][1]['positions'][0] += .001
    elif tamper == 'velocity': trajectory['joint_trajectory']['points'][1]['velocities'][0] += .001
    elif tamper == 'margin': plan['path_screen']['min_separating_axis_gap_m'] = .009
    elif tamper == 'overlap': plan['path_screen']['possible_overlap_count'] = 1
    elif tamper == 'bound': plan['path_screen']['max_between_sample_motion_bound_m'] = .015
    elif tamper == 'nan': plan['path_screen']['min_separating_axis_gap_m'] = float('nan')
    elif tamper == 'linear': plan['path_screen']['interpolation'] = 'linear'
    if tamper is None:
        validate_trajectory(plan,[0,-.5,-.7,2],'example-model')
    else:
        with pytest.raises(ValueError): validate_trajectory(plan,[0,-.5,-.7,2],'example-model')


def test_active_model_ignores_formatting_but_not_geometry_changes():
    import threading
    import xml.etree.ElementTree as ET
    from types import SimpleNamespace
    from high_approach_control import HighApproachControl
    xml='<robot name="test"><link name="one" /></robot>'
    node=SimpleNamespace(_lock=threading.RLock(),model_canonical=ET.canonicalize(xml,strip_text=True),_model_matches=False)
    HighApproachControl.on_active_model(node,SimpleNamespace(data='<robot name="test">\n <link name="one"></link>\n</robot>'))
    assert node._model_matches
    HighApproachControl.on_active_model(node,SimpleNamespace(data=xml.replace('one','two')))
    assert not node._model_matches
    HighApproachControl.on_active_model(node,SimpleNamespace(data=xml))
    assert not node._model_matches
    HighApproachControl.on_active_model(node,SimpleNamespace(data='<bad'))
    assert not node._model_matches


@pytest.mark.parametrize('tamper', [None, 'other_joint', 'shoulder_direction', 'upward', 'lift'])
def test_contact_clearance_is_only_small_upward_shoulder_motion(tamper):
    plan=example()
    points=plan['response']['trajectory']['joint_trajectory']['points']
    start=[.1,.8,-.85,.2]
    points[0]['positions']=start.copy()
    points[1]['positions']=[.1,.8-math.radians(6),-.85,.2]
    plan.update(kind='contact_clearance_lift',target_m=[.26,.06,.295],measured_start_rad=start,
                relative_finger_motion_upward=True,predicted_finger_lift_m=.038)
    if tamper=='other_joint': points[1]['positions'][2]+=.01
    elif tamper=='shoulder_direction': points[1]['positions'][1]=.9
    elif tamper=='upward': plan['relative_finger_motion_upward']=False
    elif tamper=='lift': plan['predicted_finger_lift_m']=.10
    if tamper is None: validate_trajectory(plan,start,'example-model')
    else:
        with pytest.raises(ValueError): validate_trajectory(plan,start,'example-model')
