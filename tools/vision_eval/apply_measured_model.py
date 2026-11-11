"""Apply only the audited fixed mount height without restarting motor hardware."""

import argparse
import hashlib
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import rclpy
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.time import Time
from rcl_interfaces.srv import GetParameters, SetParametersAtomically
from std_msgs.msg import Bool
from tf2_ros import Buffer, TransformListener

from prepare_measured_model import patch_mount


def canonical(xml):
    """Ignore XML formatting while retaining every element and attribute."""
    def visit(element):
        return (element.tag, sorted(element.attrib.items()), (element.text or '').strip(),
                [visit(child) for child in element])
    return visit(ET.fromstring(xml))


def validate_update(original, candidate):
    """Reject any change except the audited fixed arm mounting translation."""
    if canonical(patch_mount(original)) != canonical(candidate):
        raise ValueError('Candidate contains changes beyond the fixed mount height')


def main():
    """Update parameter copies and RSP TF; retain the active hardware instance."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    candidate = args.model.read_text()
    rclpy.init(args=[])
    node = Node('apply_fixed_mount_geometry')
    buffer = Buffer()
    listener = TransformListener(buffer, node)
    readiness = [False, 0.0]

    def on_ready(msg):
        readiness[:] = [msg.data, time.monotonic()]

    node.create_subscription(Bool, '/measurement/ready', on_ready, 10)

    def call(kind, path, request):
        client = node.create_client(kind, path)
        if not client.wait_for_service(timeout_sec=3):
            raise RuntimeError(f'Missing service {path}')
        future = client.call_async(request)
        rclpy.spin_until_future_complete(node, future, timeout_sec=5)
        if not future.done() or future.exception():
            raise RuntimeError(f'Service outcome unknown: {path}')
        return future.result()

    def get(name):
        return call(GetParameters, f'/{name}/get_parameters',
                    GetParameters.Request(names=['robot_description'])).values[0].string_value

    def set_model(name, xml):
        result = call(
            SetParametersAtomically, f'/{name}/set_parameters_atomically',
            SetParametersAtomically.Request(parameters=[
                Parameter('robot_description', value=xml).to_parameter_msg()])).result
        if not result.successful:
            raise RuntimeError(f'{name} rejected model: {result.reason}')

    originals = {}
    attempted = []
    result = {
        'status': 'not_applied', 'hardware_reinitialized': False,
        'note': 'Hardware keeps its loaded control block, verified identical to candidate.',
    }
    try:
        if any(name == 'move_group' for name, _ in node.get_node_names_and_namespaces()):
            raise RuntimeError('MoveIt is running; cannot leave a cached old kinematic model')
        for name in ['controller_manager', 'robot_state_publisher']:
            originals[name] = get(name)
            validate_update(originals[name], candidate)
            (args.output / f'{name}_before.urdf').write_text(originals[name])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not readiness[0]:
            rclpy.spin_once(node, timeout_sec=0.1)
        if not readiness[0] or time.monotonic() - readiness[1] > 0.5:
            raise RuntimeError('Measurement controller is not freshly ready')
        for name in ['controller_manager', 'robot_state_publisher']:
            attempted.append(name)
            set_model(name, candidate)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
            if buffer.can_transform('base_link', 'link1', Time()):
                z = buffer.lookup_transform('base_link', 'link1', Time()).transform.translation.z
                if abs(z - 0.116) < 1e-9:
                    break
        else:
            raise RuntimeError('Updated fixed TF not observed')
        for name in originals:
            if get(name) != candidate:
                raise RuntimeError(f'{name} model readback mismatch')
        result.update(status='applied_and_tf_verified', base_to_link1_z_m=z,
                      model_sha256=hashlib.sha256(candidate.encode()).hexdigest())
    except Exception as exc:
        result['error'] = str(exc)
        result['rollback'] = {}
        for name in reversed(attempted):
            try:
                set_model(name, originals[name])
                result['rollback'][name] = 'parameter_restored'
            except Exception as rollback_exc:
                result['rollback'][name] = str(rollback_exc)
        raise
    finally:
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
        del listener
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
