"""Query IK and paths for explicit contact candidates; never execute goals."""

import argparse
import hashlib
import itertools
import json
from pathlib import Path
import sys
import time

import numpy as np
import rclpy
from geometry_msgs.msg import Pose, PoseStamped
from moveit_msgs.msg import Constraints, JointConstraint, RobotState
from moveit_msgs.srv import GetCartesianPath, GetMotionPlan, GetPositionIK
from rclpy.qos import qos_profile_sensor_data
from rosidl_runtime_py.convert import message_to_ordereddict
from sensor_msgs.msg import JointState
from scipy.spatial.transform import Rotation
import yaml

from calibration_plan import fk, JOINTS
from screen_grasp_path import trajectory_samples

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/control_pkg'))
from control_pkg.grasp_reference import contact_to_tool  # noqa: E402
from control_pkg.grasp_reference import upright_contact  # noqa: E402
from control_pkg.grasp_reference import upright_candidate, review_contact_path  # noqa: E402


def main():
    """Check endpoint IK and at most one complete approach chain per candidate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--geometry', type=Path, required=True)
    parser.add_argument('--tcp', type=Path, required=True)
    parser.add_argument('--config', type=Path,
                        default=ROOT / 'src/control_pkg/config/grasp_candidate_review.yaml')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--object-shift-x-m', type=float, default=0.,
                        help='Hypothetical object shift for planning only; no base motion')
    parser.add_argument('--screen-finger-clearance', action='store_true',
                        help='Raise approach using nominal open finger bounds, then verify FK')
    parser.add_argument('--cartesian-grasp', action='store_true',
                        help='Require full, FK-checked straight descent and lift; no execution')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output already exists')
    if args.cartesian_grasp and not args.screen_finger_clearance:
        parser.error('--cartesian-grasp requires --screen-finger-clearance')
    profile = yaml.safe_load(args.config.read_text())
    geometry = json.loads(args.geometry.read_text())
    tcp_info = json.loads(args.tcp.read_text())
    model = ROOT / 'log/measured_model_20261111/measured.urdf'
    xml = model.read_text()
    axes = [row['axis']['axis_xy_m'] for row in geometry['frames']
            if row['axis'] and row['axis']['accepted']
            and row['axis']['residual_p90_m'] <= profile['axis_residual_p90_limit_m']]
    if len(axes) < 10:
        raise ValueError('Not enough accepted axis observations for planning study')
    center = np.median(axes, axis=0)
    observed_center = center.copy()
    if not np.isfinite(args.object_shift_x_m):
        raise ValueError('Hypothetical shift must be finite')
    center[0] += args.object_shift_x_m
    tcp = tcp_info['tcp_in_tool_m']
    finger_corners = None
    if args.screen_finger_clearance:
        finger_corners = np.concatenate([
            np.array(list(itertools.product(*zip(*pad['open_mesh_bounds_in_tool_m']))))
            for pad in tcp_info['pads']])
    base_yaw_origin = fk(xml, dict.fromkeys(JOINTS, 0.), 'link2')[:3, 3]
    yaw = float(np.arctan2(center[1]-base_yaw_origin[1], center[0]-base_yaw_origin[0]))
    report = {
        'executed': False, 'hardware_tcp_validated': False,
        'axis_xy_m': center.tolist(), 'profile': profile, 'tcp_in_tool_m': tcp,
        'observed_axis_xy_m': observed_center.tolist(),
        'hypothetical_object_shift_x_m': args.object_shift_x_m,
        'finger_clearance_screen': args.screen_finger_clearance,
        'cartesian_grasp_review': args.cartesian_grasp,
        'limitations': [
            'Read-only IK/path services; no action goals or motion commands.',
            'Installed IK is position-only; solved orientation is checked explicitly.',
            'Current planning scene may omit the box, can and camera geometry.',
            'Joint-space path feasibility does not certify a straight approach.',
            'Saved object position and nominal TCP are hypotheses, not fresh execution goals.',
        ],
        'input_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (args.geometry, args.tcp, args.config, model, Path(__file__),
                                   ROOT / 'src/control_pkg/control_pkg/grasp_reference.py')},
        'candidates': [],
    }
    rclpy.init(args=[])
    node = rclpy.create_node('grasp_reachability_review')
    received = {}
    node.create_subscription(JointState, '/joint_states',
                             lambda msg: received.update(msg=msg, at=time.monotonic()),
                             qos_profile_sensor_data)
    ik_client = node.create_client(GetPositionIK, '/compute_ik')
    plan_client = node.create_client(GetMotionPlan, '/plan_kinematic_path')
    cartesian_client = node.create_client(GetCartesianPath, '/compute_cartesian_path')

    def call(client, request, timeout):
        future = client.call_async(request)
        rclpy.spin_until_future_complete(node, future, timeout_sec=timeout)
        if not future.done() or future.result() is None:
            raise RuntimeError('Read-only planning service timed out')
        return future.result()

    def state(joints):
        merged = dict(zip(received['msg'].name, received['msg'].position))
        merged.update(joints)
        # Planning state only: do not open physical fingers.
        merged.update(gripper_left_joint=.019, gripper_right_joint=.019)
        result = RobotState()
        result.is_diff = True
        result.joint_state.name = list(merged)
        result.joint_state.position = [float(v) for v in merged.values()]
        return result

    def solve(contact, rotation, seeds):
        tool = contact_to_tool(contact, tcp, rotation.as_matrix())
        attempts = []
        for seed in seeds:
            request = GetPositionIK.Request()
            req = request.ik_request
            req.group_name = 'arm'
            req.ik_link_name = 'end_effector_link'
            req.robot_state = state(dict(zip(JOINTS, seed)))
            req.avoid_collisions = True
            req.timeout.sec = 0
            req.timeout.nanosec = 200_000_000
            req.pose_stamped = PoseStamped()
            req.pose_stamped.header.frame_id = 'base_link'
            p = req.pose_stamped.pose
            p.position.x, p.position.y, p.position.z = tool
            p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w = rotation.as_quat().tolist()
            response = call(ik_client, request, 3.)
            attempt = {'error_code': response.error_code.val}
            attempts.append(attempt)
            if response.error_code.val != 1:
                continue
            joints = dict(zip(response.solution.joint_state.name,
                              response.solution.joint_state.position))
            actual = fk(xml, joints)
            orientation_error = np.rad2deg((rotation.inv()*Rotation.from_matrix(actual[:3, :3])).magnitude())
            contact_error = np.linalg.norm(actual[:3, 3] + actual[:3, :3] @ tcp - contact)
            attempt.update(orientation_error_deg=float(orientation_error),
                           contact_error_m=float(contact_error),
                           joints_rad=[joints[x] for x in JOINTS])
            if (orientation_error <= profile['orientation_error_limit_deg']
                    and contact_error <= profile['position_error_limit_m']):
                return dict(success=True, tool_target_m=tool, attempts=attempts,
                            joints_rad=attempt['joints_rad'])
        return dict(success=False, tool_target_m=tool, attempts=attempts)

    def path(start, finish):
        request = GetMotionPlan.Request()
        req = request.motion_plan_request
        req.group_name = 'arm'
        req.start_state = state(dict(zip(JOINTS, start)))
        req.allowed_planning_time = 2.
        req.num_planning_attempts = 1
        req.max_velocity_scaling_factor = .10
        req.max_acceleration_scaling_factor = .10
        req.goal_constraints = [Constraints(joint_constraints=[
            JointConstraint(joint_name=name, position=float(q), tolerance_above=.001,
                            tolerance_below=.001, weight=1.) for name, q in zip(JOINTS, finish)])]
        response = call(plan_client, request, 6.).motion_plan_response
        trajectory = response.trajectory.joint_trajectory
        return {'error_code': response.error_code.val, 'points': len(trajectory.points),
                'joint_names': list(trajectory.joint_names),
                'trajectory': message_to_ordereddict(response.trajectory),
                'positions_rad': [list(p.positions) for p in trajectory.points]}

    def cartesian(start, start_contact, end_contact, rotation):
        request = GetCartesianPath.Request()
        request.header.frame_id = 'base_link'
        request.start_state = state(dict(zip(JOINTS, start)))
        request.group_name = 'arm'
        request.link_name = 'end_effector_link'
        request.max_step = .005
        request.revolute_jump_threshold = .35
        request.avoid_collisions = True
        request.max_velocity_scaling_factor = .10
        request.max_acceleration_scaling_factor = .10
        pose = Pose()
        pose.position.x, pose.position.y, pose.position.z = contact_to_tool(
            end_contact, tcp, rotation.as_matrix())
        pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w = rotation.as_quat().tolist()
        request.waypoints = [pose]
        response = call(cartesian_client, request, 10.)
        trajectory = message_to_ordereddict(response.solution)
        joint = trajectory['joint_trajectory']
        row = {'error_code': response.error_code.val, 'fraction': response.fraction,
               'points': len(joint['points']), 'joint_names': joint['joint_names'],
               'trajectory': trajectory,
               'positions_rad': [p['positions'] for p in joint['points']],
               'accepted': False}
        if (response.error_code.val != 1 or not np.isfinite(response.fraction)
                or response.fraction < 1. - 1e-9 or len(joint['points']) < 2
                or joint['joint_names'] != JOINTS):
            row['rejection'] = 'Incomplete Cartesian path or unexpected joints'
            return row
        try:
            samples, bound = trajectory_samples(joint['points'])
            transforms = [fk(xml, dict(zip(JOINTS, q))) for q in samples]
            contacts = [t[:3, 3] + t[:3, :3] @ tcp for t in transforms]
            check = review_contact_path(
                contacts, [t[:3, :3] for t in transforms], start_contact, end_contact,
                rotation.as_matrix(), profile['position_error_limit_m'],
                np.deg2rad(profile['orientation_error_limit_deg']),
                np.deg2rad(profile['cartesian_pitch_range_deg']).tolist()
                if 'cartesian_pitch_range_deg' in profile else None)
            row['geometry_review'] = check
            row['interpolation'] = 'quintic_with_derivative_bound'
            row['between_sample_motion_bound_m'] = bound
            row['accepted'] = check['accepted']
            if not row['accepted']:
                row['rejection'] = 'TCP line or orientation not satisfied by planned path'
        except ValueError as exc:
            row['rejection'] = str(exc)
        return row

    try:
        if not ik_client.wait_for_service(timeout_sec=10.) or not plan_client.wait_for_service(timeout_sec=10.):
            raise RuntimeError('MoveIt planning services unavailable')
        if args.cartesian_grasp and not cartesian_client.wait_for_service(timeout_sec=10.):
            raise RuntimeError('Cartesian planning service unavailable')
        deadline = time.monotonic()+8
        while 'msg' not in received and time.monotonic()<deadline:
            rclpy.spin_once(node, timeout_sec=.1)
        if 'msg' not in received or time.monotonic()-received['at']>.5:
            raise RuntimeError('No fresh joint feedback')
        start = dict(zip(received['msg'].name, received['msg'].position))
        report['initial_joints_rad'] = [start[x] for x in JOINTS]
        for inset in profile['top_insets_m']:
            for pitch_deg in profile['pitch_candidates_deg']:
                pitch = float(np.deg2rad(pitch_deg))
                rotation = Rotation.from_euler('z', yaw)*Rotation.from_euler('y', pitch)
                contact = upright_contact(center, profile['support_z_base_m'],
                                          profile['object_height_m'], inset)
                row = {'top_inset_m': inset, 'pitch_deg': pitch_deg,
                       'contact_base_m': contact}
                seeds = [[yaw, np.deg2rad(a), np.deg2rad(b), pitch-np.deg2rad(a+b)]
                         for a,b in [(45,-15),(65,-30),(85,-45),
                                     (45,-35),(35,-50),(55,-50),(65,-50),(75,-50)]]
                pick = solve(contact, rotation, seeds)
                row['pick'] = pick
                if pick['success']:
                    approach_height = profile['approach_height_m']
                    if finger_corners is not None:
                        targets = upright_candidate(
                            center, profile['support_z_base_m'], profile['object_height_m'],
                            inset, tcp, rotation.as_matrix(), finger_corners,
                            profile['approach_height_m'], profile['minimum_finger_clearance_m'],
                            profile['minimum_study_lift_m'], profile['position_error_limit_m'],
                            np.deg2rad(profile['orientation_error_limit_deg']),
                            profile.get('approach_backoff_m', 0.), base_yaw_origin[:2])
                        row['model_targets'] = targets
                        approach_height = targets['approach_height_m']
                        row['ik_clearance_buffer_m'] = targets['ik_clearance_buffer_m']
                    row['approach_height_m'] = float(approach_height)
                    approach = np.array(contact)+[0,0,approach_height]
                    if finger_corners is not None:
                        approach = np.array(targets['approach_contact_base_m'])
                    lift = np.array(contact)+[0,0,profile['minimum_study_lift_m']]
                    row['approach'] = solve(approach, rotation, [pick['joints_rad']]+seeds)
                    row['lift'] = solve(lift, rotation, [pick['joints_rad']]+seeds)
                    if finger_corners is not None and row['approach']['success']:
                        transform = fk(xml, dict(zip(JOINTS, row['approach']['joints_rad'])))
                        lowest = (finger_corners @ transform[:3, :3].T + transform[:3, 3])[:, 2].min()
                        clearance = lowest - profile['support_z_base_m'] - profile['object_height_m']
                        row['approach']['nominal_finger_clearance_above_top_m'] = float(clearance)
                        if clearance < profile['minimum_finger_clearance_m'] - 1e-6:
                            row['approach']['success'] = False
                            row['approach']['rejection'] = 'Nominal finger clearance below configured minimum'
                    if row['approach']['success'] and row['lift']['success']:
                        entry = path(report['initial_joints_rad'], row['approach']['joints_rad'])
                        row['paths'] = [entry]
                        if args.cartesian_grasp:
                            if (entry['error_code'] == 1 and entry['points'] >= 2
                                    and entry['joint_names'] == JOINTS):
                                down = cartesian(entry['positions_rad'][-1], approach, contact, rotation)
                                row['paths'].append(down)
                                if down['accepted']:
                                    up = cartesian(down['positions_rad'][-1], contact, lift, rotation)
                                    row['paths'].append(up)
                                    row['planning_chain_accepted'] = up['accepted']
                        else:
                            row['paths'].extend([
                                path(row['approach']['joints_rad'], pick['joints_rad']),
                                path(pick['joints_rad'], row['lift']['joints_rad'])])
                report['candidates'].append(row)
                print(f'inset={inset*1000:.0f}mm pitch={pitch_deg:.0f}deg: '
                      f'pick={pick["success"]} approach={row.get("approach",{}).get("success")} '
                      f'lift={row.get("lift",{}).get("success")}', flush=True)
        eligible = [(index, row) for index, row in enumerate(report['candidates'])
                    if row.get('planning_chain_accepted')]
        report['planning_chain_accepted_count'] = len(eligible)
        report['selected_candidate_index'] = None
        if eligible:
            for _, row in eligible:
                row['total_joint_travel_rad'] = float(sum(
                    np.abs(np.diff(np.asarray(p['positions_rad']), axis=0)).sum()
                    for p in row['paths']))
            report['selected_candidate_index'] = min(
                eligible, key=lambda item: (item[1]['total_joint_travel_rad'], item[0]))[0]
        report['execution_authorized'] = False
        report['selection_scope'] = 'Complete model path only; scene/finger clearance review still required'
    finally:
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
