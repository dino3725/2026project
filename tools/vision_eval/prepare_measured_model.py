"""Prepare an opt-in model and matching vendor launches, without starting ROS."""

import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from ament_index_python.packages import get_package_share_directory
import xacro


def patch_mount(xml):
    """Change only the verified arm mount height; reject unexpected models."""
    root = ET.fromstring(xml)
    joint = root.find("joint[@name='base_fixed']")
    if joint is None or joint.get('type') != 'fixed':
        raise ValueError('Expected fixed base_fixed joint')
    if (joint.find('parent').get('link') != 'base_link'
            or joint.find('child').get('link') != 'link1'):
        raise ValueError('Unexpected mount frames')
    origin = joint.find('origin')
    if ([float(v) for v in origin.get('xyz').split()] != [-0.092, 0, 0.091]
            or [float(v) for v in origin.get('rpy').split()] != [0, 0, 0]):
        raise ValueError('Mount differs from the audited official model')
    origin.set('xyz', '-0.092 0.0 0.116')
    return ET.tostring(root, encoding='unicode')


def prepare(output):
    """Create isolated launch copies preserving all other vendor settings."""
    output = output.resolve()
    if any(c.isspace() for c in str(output)):
        raise ValueError('Generated xacro launch path must not contain whitespace')
    desc = Path(get_package_share_directory('turtlebot3_manipulation_description'))
    bringup = Path(get_package_share_directory('turtlebot3_manipulation_bringup'))
    moveit = Path(get_package_share_directory('turtlebot3_manipulation_moveit_config'))
    name = 'turtlebot3_manipulation.urdf.xacro'
    source = desc / 'urdf' / name
    original = xacro.process_file(str(source)).toxml()
    candidate = patch_mount(original)
    # Preserve the original xacro macros, ros2_control options and mesh URIs.
    old = '<origin xyz="-0.092 0.0 0.091" rpy="0 0 0"/>'
    text = source.read_text()
    if text.count(old) != 1:
        raise ValueError('Upstream xacro changed; inspect before generating')
    output.mkdir(parents=True, exist_ok=False)
    model = output / name
    model.write_text(text.replace(old, old.replace('0.091', '0.116')))
    expanded = xacro.process_file(str(model)).toxml()
    if ET.tostring(ET.fromstring(expanded)) != ET.tostring(ET.fromstring(candidate)):
        raise ValueError('Generated model changes more than base_fixed height')
    (output / 'measured.urdf').write_text(expanded)
    sources = [source]
    for folder, filename in [
        (bringup, 'base.launch.py'), (bringup, 'hardware.launch.py'),
        (moveit, 'move_group.launch.py'), (moveit, 'moveit_rviz.launch.py'),
    ]:
        path = folder / 'launch' / filename
        body = path.read_text()
        if filename != 'hardware.launch.py':
            if body.count(repr(name)) != 1:
                raise ValueError(f'Unexpected description reference in {path}')
            body = body.replace(repr(name), repr(str(model)))
        (output / filename).write_text(body)
        sources.append(path)
    manifest = {
        'status': 'prepared_only_not_loaded',
        'change': 'base_link -> link1 z: 0.091 -> 0.116 m',
        'shoulder_ground_height_m': 0.2025,
        'measured_shoulder_ground_height_m': 0.202,
        'limitations': [
            'Deck meshes/collisions remain official; not a complete modified chassis model.',
            'Lidar approximate x/z measurements are not applied in this arm calibration model.',
            'Restart hardware and MoveIt with these copies; do not mix with old launches.',
            'Do not reuse an old +25mm postprocessing offset on data from this model.',
        ],
        'sources_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        'urdf_sha256': hashlib.sha256(expanded.encode()).hexdigest(),
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    prepare(parser.parse_args().output)
