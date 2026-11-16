#!/usr/bin/env python3
"""Check the actual ROS Python environment without a microphone, node or publication."""

import argparse
import sys
import threading

from voice_command_pkg.speaker_profile import SpeakerConfig
from voice_command_pkg.speaker_runtime import SpeakerRuntime


def main():
    """Exercise the runtime model/profile startup with the ROS interpreter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--allow-development-profile', action='store_true')
    args = parser.parse_args()
    if sys.prefix != sys.base_prefix:
        parser.error('source voice_env.sh, then use /usr/bin/python3 instead of .venv/bin/python')
    import coverage
    import rclpy  # noqa: F401

    print(f'Python: {sys.executable}', flush=True)
    print(f'coverage: {coverage.__version__} ({coverage.__file__})', flush=True)
    completed = threading.Event()

    def log(message):
        print(message, flush=True)
        completed.set()

    runtime = SpeakerRuntime(SpeakerConfig(
        mode='enforce', profile_path=args.profile,
        allow_development_profile=args.allow_development_profile,
    ), log, threading.Event())
    try:
        if not completed.wait(45) or not runtime.ready:
            parser.exit(1, 'FAILED: speaker runtime not ready\n')
    finally:
        runtime.close()
        runtime.worker.join(timeout=1)
    print('PASS: ROS Python model/profile ready; no microphone or ROS node started')


if __name__ == '__main__':
    main()
