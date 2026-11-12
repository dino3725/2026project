"""Reject missing/ambiguous clock and command evidence instead of making latency claims."""

from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from latency_analysis import chrony_bound, summarize_trial  # noqa: E402


TRACKING = '''Reference ID : 01020304 (server)
Stratum : 2
Ref time (UTC) : Thu Jan 01 00:00:00 1970
System time : 0.001 seconds slow of NTP time
Root delay : 0.002 seconds
Root dispersion : 0.001 seconds
Leap status : Normal
'''


def fixture():
    snapshot = {'tracking': {'returncode': 0, 'stdout': TRACKING}}
    controller = [{'name': 'control', 'namespace': '/'}]
    voice = [{'name': 'voice', 'namespace': '/'}]
    trace = {'event': 'published', 'command_stamp_ns': 1000000000,
             'action': 'stop', 'target_class': '', 'detector': 'transcript',
             'clock_domain': 'laptop:boot', 'publish_started_monotonic_ns': 2000000000,
             'unix_ns': 1001000000, 'vad_end_to_publish_call_seconds': 1.7}
    events = [
        {'event': 'state', 'state': 'IDLE', 'unix_ns': 900000000,
         'monotonic_ns': 1900000000, 'publishers': controller},
        {'event': 'command', 'stamp_ns': 1000000000, 'action': 'stop', 'target_class': '',
         'unix_ns': 1010000000, 'monotonic_ns': 2010000000, 'publishers': voice},
        {'event': 'state', 'state': 'ERROR', 'unix_ns': 1020000000,
         'monotonic_ns': 2020000000, 'publishers': controller},
        {'event': 'status', 'stamp_ns': 1025000000, 'action': 'stop', 'target_class': '',
         'accepted': True, 'unix_ns': 1030000000, 'monotonic_ns': 2030000000,
         'publishers': controller},
    ]
    laptop = {'metadata': {'role': 'laptop', 'clock_domain': 'laptop:boot',
                           'use_sim_time': False, 'chrony_start': snapshot,
                           'chrony_end': snapshot, 'start_unix_ns': 800000000,
                           'end_unix_ns': 1100000000}, 'events': events}
    jetson = deepcopy(laptop)
    jetson['metadata'].update(role='jetson', clock_domain='jetson:boot')
    return trace, laptop, jetson


def test_normal_trial_reports_software_and_observation_never_physical_stop():
    trace, laptop, jetson = fixture()
    result = summarize_trial([trace], laptop, jetson)
    assert result['physical_stop_seconds'] is None
    assert result['metrics'][
        'publish_call_to_status_observed_laptop_seconds'] == pytest.approx(.03)
    assert result['metrics']['header_to_command_observed_jetson_seconds'] == pytest.approx(.01)
    assert result['metrics']['header_to_state_observed_jetson_seconds'] == pytest.approx(.02)
    assert result['estimated_cross_host_error_bound_seconds'] == pytest.approx(.006)


@pytest.mark.parametrize('text', [
    '', TRACKING.replace('Normal', 'Not synchronised'),
    TRACKING.replace('Stratum : 2', 'Stratum : 0'),
    TRACKING.replace('01020304', '7F7F0101'),
    TRACKING.replace('0.001 seconds slow', 'nan seconds slow'),
])
def test_bad_chrony_reports_are_not_sync_proof(text):
    assert chrony_bound({'tracking': {'returncode': 0, 'stdout': text}}) is None


def test_missing_chrony_keeps_only_same_host_monotonic_metric():
    trace, laptop, jetson = fixture()
    jetson['metadata']['chrony_end'] = {}
    result = summarize_trial([trace], laptop, jetson)
    assert list(result['metrics']) == ['publish_call_to_status_observed_laptop_seconds']


@pytest.mark.parametrize('mutation', ['step', 'stale', 'sim', 'wide'])
def test_clock_problems_do_not_produce_cross_host_metrics(mutation):
    trace, laptop, jetson = fixture()
    if mutation == 'step':
        jetson['events'][-1]['unix_ns'] += 1000000000
    elif mutation == 'stale':
        jetson['metadata']['end_unix_ns'] = 2000 * 1000000000
    elif mutation == 'sim':
        jetson['metadata']['use_sim_time'] = True
    else:
        jetson['metadata']['chrony_end']['tracking']['stdout'] = TRACKING.replace('0.002', '1.002')
    result = summarize_trial([trace], laptop, jetson)
    assert 'header_to_command_observed_jetson_seconds' not in result['metrics']


@pytest.mark.parametrize('mutation', ['repeat', 'mismatch', 'multiple_publishers'])
def test_ambiguous_command_not_paired(mutation):
    trace, laptop, jetson = fixture()
    if mutation == 'repeat':
        jetson['events'].append(deepcopy(jetson['events'][1]))
    elif mutation == 'mismatch':
        jetson['events'][1]['stamp_ns'] += 1
    else:
        jetson['events'][1]['publishers'] *= 2
    result = summarize_trial([trace], laptop, jetson)
    assert 'header_to_command_observed_jetson_seconds' not in result['metrics']


def test_retained_error_not_a_new_stop_transition():
    trace, laptop, jetson = fixture()
    jetson['events'][0]['state'] = 'ERROR'
    result = summarize_trial([trace], laptop, jetson)
    assert 'header_to_state_observed_jetson_seconds' not in result['metrics']


def test_dry_run_is_not_a_publication_and_no_latency_is_fabricated():
    trace, laptop, jetson = fixture()
    trace.update(event='dry_run', command_stamp_ns=None)
    result = summarize_trial([trace], laptop, jetson)
    assert result['metrics'] == {}
    assert result['dry_run_count'] == 1


def test_wrong_status_source_and_wrong_host_not_used():
    trace, laptop, jetson = fixture()
    laptop['events'][-1]['publishers'] = [{'name': 'other', 'namespace': '/'}]
    assert 'publish_call_to_status_observed_laptop_seconds' not in summarize_trial(
        [trace], laptop, jetson)['metrics']
    laptop['metadata']['clock_domain'] = 'other:boot'
    assert 'publish_call_to_status_observed_laptop_seconds' not in summarize_trial(
        [trace], laptop, jetson)['metrics']


def test_recorder_has_no_command_publication_calls():
    # Inspect executable syntax: a read-only measurement tool must not publish robot commands.
    import ast
    source = Path(__file__).resolve().parents[1] / 'record_latency.py'
    tree = ast.parse(source.read_text())
    assert not any(isinstance(n, ast.Attribute) and n.attr in ('publish', 'create_publisher')
                   for n in ast.walk(tree))


def test_missing_measurement_not_zero_and_routes_not_mixed():
    from summarize_latency import summarize
    rows = [
        {'action': 'stop', 'detector': 'transcript', 'physical_stop_seconds': 2.0},
        {'action': 'stop', 'detector': 'transcript', 'physical_stop_seconds': None},
        {'action': 'stop', 'detector': 'stream', 'physical_stop_seconds': .5},
    ]
    groups = summarize(rows)
    transcript = next(g for g in groups if g['detector'] == 'transcript')
    assert transcript['metrics']['physical_stop_seconds'] == {
        'measured': 1, 'missing': 1, 'median': 2.0, 'p95_nearest_rank': 2.0, 'max': 2.0,
    }


def test_video_endpoints_use_slower_part_and_do_not_invent_missing_motion():
    from latency_analysis import video_measurement
    markers = {'command_stamp_ns': 42, 'speech_end_seconds': 1.0,
               'time_resolution_seconds': .02, 'base_stopped_seconds': 1.4,
               'arm_stopped_seconds': 1.8}
    assert video_measurement(markers, 42)['physical_stop_seconds'] == pytest.approx(.8)
    markers['arm_stopped_seconds'] = None
    assert video_measurement(markers, 42)['physical_stop_seconds'] is None
    with pytest.raises(ValueError):
        video_measurement(markers, 43)
    markers['base_stopped_seconds'] = float('nan')
    with pytest.raises(ValueError):
        video_measurement(markers, 42)


def test_same_host_observers_not_claimed_as_jetson_network_measurement():
    trace, laptop, jetson = fixture()
    jetson['metadata']['clock_domain'] = laptop['metadata']['clock_domain']
    result = summarize_trial([trace], laptop, jetson)
    assert 'header_to_command_observed_jetson_seconds' not in result['metrics']
    assert 'observers_not_on_distinct_hosts' in result['issues']


def test_all_missing_physical_endpoints_are_reported_as_missing():
    from summarize_latency import summarize
    groups = summarize([{'action': 'stop', 'detector': 'transcript'}])
    assert groups[0]['metrics']['physical_stop_seconds'] == {
        'measured': 0, 'missing': 1, 'median': None, 'p95_nearest_rank': None, 'max': None,
    }
