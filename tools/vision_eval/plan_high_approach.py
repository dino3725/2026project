"""Plan a high coke approach with a forward keepout slab; never execute."""

import argparse
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import rclpy
from geometry_msgs.msg import Pose
from moveit_msgs.msg import AttachedCollisionObject, CollisionObject
from moveit_msgs.msg import Constraints, JointConstraint, PositionConstraint
from moveit_msgs.srv import ApplyPlanningScene, GetMotionPlan, GetStateValidity
from rclpy.qos import qos_profile_sensor_data
from rosidl_runtime_py.convert import message_to_ordereddict
from sensor_msgs.msg import JointState
from shape_msgs.msg import SolidPrimitive
from std_msgs.msg import Bool
import yaml
from scipy.spatial.transform import Rotation

from calibration_plan import JOINTS, fk


def main():
    """Save the planned trajectory and sampled collision-check evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--x', type=float)
    parser.add_argument('--y', type=float)
    parser.add_argument(
        '--diagnostic-reference', type=Path,
        help='Prior plan: lift shoulder 6deg, retain other goals')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if (args.diagnostic_reference is None
            and (args.x is None or args.y is None
                 or not 0.20 <= args.x <= 0.31 or abs(args.y) > 0.08)):
        parser.error('Target outside this centered coke test envelope')
    args.output.mkdir(exist_ok=False)
    root = Path(__file__).resolve().parents[2]
    model = root / 'log/measured_model_20261111/measured.urdf'
    xml = model.read_text()
    mount = yaml.safe_load(
        (root / 'src/vision_pkg/config/camera_mount.yaml').read_text())
    target = [args.x, args.y, 0.290]
    artifact = {'target_m': target, 'executed': False, 'approved': False,
                'model_sha256': hashlib.sha256(model.read_bytes()).hexdigest(),
                'velocity_scale': 0.10, 'acceleration_scale': 0.10,
                'keepout': {'x_min': 0.18, 'z_top': 0.24},
                'note': ('Keepout top is 36mm over nominal coke top; '
                         'conservative scene, not measured ground truth')}
    diagnostic_joints = None
    if args.diagnostic_reference is not None:
        raw = args.diagnostic_reference.read_bytes()
        prior = json.loads(raw)
        if prior['model_sha256'] != artifact['model_sha256']:
            raise ValueError('Diagnostic reference uses another model')
        prior_joints = prior['response']['trajectory']['joint_trajectory']
        if prior_joints['joint_names'] != JOINTS:
            raise ValueError('Diagnostic reference has unexpected joints')
        reference = np.array(prior_joints['points'][-1]['positions'])
        diagnostic_joints = reference.copy()
        diagnostic_joints[1] -= np.deg2rad(6)
        target = fk(xml, dict(zip(JOINTS, diagnostic_joints)))[:3, 3].tolist()
        if not (0.20 <= target[0] <= 0.31 and abs(target[1]) <= 0.08
                and 0.30 <= target[2] <= 0.36):
            raise ValueError('Diagnostic lift outside reviewed upper envelope')
        artifact.update(kind='diagnostic_lift', target_m=target,
                        previous_reference_rad=reference.tolist(),
                        diagnostic_target_rad=diagnostic_joints.tolist(),
                        reference_sha256=hashlib.sha256(raw).hexdigest())
    rclpy.init(args=[])
    node = rclpy.create_node('high_approach_planner')
    received = {}
    node.create_subscription(
        JointState, '/joint_states', lambda m: received.update(joints=m),
        qos_profile_sensor_data)
    node.create_subscription(
        Bool, '/measurement/ready',
        lambda m: received.update(ready=m.data, at=time.monotonic()), 10)

    def call(kind, name, request, timeout=12):
        client = node.create_client(kind, name)
        try:
            if not client.wait_for_service(timeout_sec=3):
                raise RuntimeError(f'No service: {name}')
            future = client.call_async(request)
            rclpy.spin_until_future_complete(node, future, timeout_sec=timeout)
            if not future.done():
                raise RuntimeError(f'Timed out: {name}')
            return future.result()
        finally:
            node.destroy_client(client)

    try:
        end = time.monotonic() + 6
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.05)
            if ('joints' in received and received.get('ready')
                    and time.monotonic() - received['at'] < 0.5):
                break
        else:
            raise RuntimeError('No live measurement readiness or joints')
        scene = ApplyPlanningScene.Request()
        scene.scene.is_diff = True
        scene.scene.robot_state.is_diff = True
        slab = CollisionObject()
        slab.header.frame_id = 'base_link'
        slab.id = 'approach_forward_keepout'
        slab.operation = CollisionObject.ADD
        slab.primitives = [SolidPrimitive(
            type=SolidPrimitive.BOX, dimensions=[0.70, 1.20, 0.74])]
        pose = Pose()
        pose.orientation.w = 1.0
        pose.position.x, pose.position.z = 0.53, -0.13
        slab.primitive_poses = [pose]
        scene.scene.world.collision_objects = [slab]
        camera = AttachedCollisionObject()
        camera.link_name = 'end_effector_link'
        camera.touch_links = ['end_effector_link', 'link5',
                              'gripper_left_link', 'gripper_right_link']
        camera.object.header.frame_id = 'end_effector_link'
        camera.object.id = 'approach_camera_envelope'
        camera.object.operation = CollisionObject.ADD
        camera.object.primitives = [SolidPrimitive(
            type=SolidPrimitive.BOX, dimensions=[0.14, 0.10, 0.10])]
        pose = Pose()
        pose.position.x, pose.position.y, pose.position.z = mount['xyz_m']
        q = Rotation.from_euler('xyz', mount['rpy_rad']).as_quat()
        (pose.orientation.x, pose.orientation.y,
         pose.orientation.z, pose.orientation.w) = q.tolist()
        camera.object.primitive_poses = [pose]
        scene.scene.robot_state.attached_collision_objects = [camera]
        artifact['scene'] = message_to_ordereddict(scene.scene)
        applied = call(ApplyPlanningScene, '/apply_planning_scene', scene)
        if not applied.success:
            raise RuntimeError('Planning scene rejected')
        validity = GetStateValidity.Request()
        validity.group_name = 'arm'
        validity.robot_state.joint_state = received['joints']
        validity.robot_state.is_diff = True
        initial = call(GetStateValidity, '/check_state_validity', validity)
        artifact['initial_validity'] = message_to_ordereddict(initial)
        if not initial.valid:
            raise RuntimeError(
                'Initial pose collides with conservative scene; no plan')
        request = GetMotionPlan.Request()
        req = request.motion_plan_request
        req.group_name = 'arm'
        req.start_state = validity.robot_state
        req.num_planning_attempts = 3
        req.allowed_planning_time = 3.0
        req.max_velocity_scaling_factor = 0.10
        req.max_acceleration_scaling_factor = 0.10
        # Keep the elbow off its mechanical/model boundary during this test.
        req.path_constraints.joint_constraints = [JointConstraint(
            joint_name='joint3', position=float(np.deg2rad(12.5)),
            tolerance_above=float(np.deg2rad(65.5)),
            tolerance_below=float(np.deg2rad(65.5)), weight=1.0)]
        artifact['elbow_allowed_deg'] = [-53.0, 78.0]
        constraint = PositionConstraint()
        constraint.header.frame_id = 'base_link'
        constraint.link_name = 'end_effector_link'
        constraint.weight = 1.0
        constraint.constraint_region.primitives = [SolidPrimitive(
            type=SolidPrimitive.BOX, dimensions=[0.01] * 3)]
        goal_pose = Pose()
        goal_pose.orientation.w = 1.0
        (goal_pose.position.x, goal_pose.position.y,
         goal_pose.position.z) = target
        constraint.constraint_region.primitive_poses = [goal_pose]
        req.goal_constraints = [Constraints(position_constraints=[constraint])]
        if diagnostic_joints is not None:
            req.goal_constraints[0].joint_constraints = [
                JointConstraint(joint_name=name, position=float(value),
                                tolerance_above=0.0001,
                                tolerance_below=0.0001, weight=1.0)
                for name, value in zip(JOINTS, diagnostic_joints)]
        planned = call(GetMotionPlan, '/plan_kinematic_path', request)
        response = planned.motion_plan_response
        artifact['response'] = message_to_ordereddict(response)
        if response.error_code.val != 1:
            raise RuntimeError(f'Planning failed: {response.error_code.val}')
        trajectory = response.trajectory.joint_trajectory
        if list(trajectory.joint_names) != JOINTS or not trajectory.points:
            raise RuntimeError('Unexpected trajectory')
        if diagnostic_joints is not None:
            # Match the existing four-second joint diagnostic duration.
            # Never increase MoveIt's requested speed or acceleration scales.
            final = trajectory.points[-1].time_from_start
            duration = final.sec + final.nanosec * 1e-9
            if duration <= 0:
                raise RuntimeError('Invalid diagnostic timing')
            factor = max(1.0, 4.0 / duration)
            for p in trajectory.points:
                ns = round((p.time_from_start.sec * 1_000_000_000
                            + p.time_from_start.nanosec) * factor)
                p.time_from_start.sec = ns // 1_000_000_000
                p.time_from_start.nanosec = ns % 1_000_000_000
                p.velocities = [v / factor for v in p.velocities]
                p.accelerations = [a / factor**2 for a in p.accelerations]
            artifact['original_duration_sec'] = duration
            artifact['duration_factor'] = factor
            artifact['response'] = message_to_ordereddict(response)
        samples = []
        for first, second in zip(
                trajectory.points[:-1], trajectory.points[1:]):
            for fraction in np.linspace(0, 1, 5):
                samples.append((1 - fraction) * np.array(first.positions)
                               + fraction * np.array(second.positions))
        artifact['sample_count'] = len(samples)
        positions = []
        for q in samples:
            ee = fk(xml, dict(zip(JOINTS, q)))
            positions.append(ee[:3, 3])
            current = dict(zip(validity.robot_state.joint_state.name,
                               validity.robot_state.joint_state.position))
            current.update(zip(JOINTS, q.tolist()))
            validity.robot_state.joint_state.name = list(current)
            validity.robot_state.joint_state.position = list(current.values())
            checked = call(
                GetStateValidity, '/check_state_validity', validity, 3)
            if not checked.valid:
                raise RuntimeError('Interpolated trajectory sample invalid')
        artifact['sample_ee_min_m'] = np.min(positions, axis=0).tolist()
        artifact['sample_ee_max_m'] = np.max(positions, axis=0).tolist()
        artifact['endpoint_m'] = positions[-1].tolist()
        artifact['approved'] = True
    except Exception as exc:
        artifact['error'] = str(exc)
        raise
    finally:
        (args.output / 'plan.json').write_text(
            json.dumps(artifact, indent=2) + '\n')
        brief = {k: v for k, v in artifact.items()
                 if k not in ('scene', 'response', 'initial_validity')}
        print(json.dumps(brief, indent=2))
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
