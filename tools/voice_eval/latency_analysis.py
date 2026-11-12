"""Conservative single-trial timing analysis with explicit clock limitations."""

from datetime import datetime, timezone
import math
import re


def chrony_bound(snapshot, now_ns=None):
    """Estimate clock error from synchronized chrony tracking; reject absent evidence."""
    tracking = snapshot.get('tracking', {})
    text = tracking.get('stdout', '')
    if tracking.get('returncode') != 0:
        return None
    fields = dict(re.findall(r'^([^:\n]+)\s*:\s*(.*)$', text, re.MULTILINE))
    fields = {key.strip(): value.strip() for key, value in fields.items()}
    try:
        if fields['Leap status'] != 'Normal' or not 1 <= int(fields['Stratum']) <= 15:
            return None
        if fields['Reference ID'].startswith(('7F7F0101', '00000000')):
            return None
        if now_ns is not None:
            reference = datetime.strptime(fields['Ref time (UTC)'], '%a %b %d %H:%M:%S %Y')
            age = now_ns / 1e9 - reference.replace(tzinfo=timezone.utc).timestamp()
            if not 0 <= age <= 1800:
                return None
        offset, delay, dispersion = [float(fields[key].split()[0]) for key in (
            'System time', 'Root delay', 'Root dispersion'
        )]
        if not all(math.isfinite(x) for x in (offset, delay, dispersion)):
            return None
        if min(delay, dispersion) < 0:
            return None
        return abs(offset) + delay / 2 + dispersion
    except (KeyError, ValueError):
        return None


def observer_bound(observer):
    """Use both snapshots and reject simulated time or observed wall clock steps."""
    meta = observer['metadata']
    bounds = [chrony_bound(meta.get('chrony_' + side, {}), meta.get(side + '_unix_ns'))
              for side in ('start', 'end')]
    if not all(side + '_unix_ns' in meta for side in ('start', 'end')):
        return None
    if meta.get('use_sim_time', True) or any(b is None for b in bounds):
        return None
    offsets = [(e['unix_ns'] - e['monotonic_ns']) / 1e9 for e in observer['events']]
    if not offsets or max(offsets) - min(offsets) > 0.01:
        return None
    return max(bounds)


def sole_publisher(event):
    """Require a unique observed source; absence of graph evidence is not success."""
    sources = event.get('publishers', [])
    if len(sources) != 1:
        return None
    return sources[0]['namespace'], sources[0]['name']


def summarize_trial(traces, laptop=None, jetson=None):
    """Never infer complete robot stopping from an ERROR state or an accepted status."""
    result = {'schema': 1, 'issues': [], 'metrics': {},
              'physical_stop_seconds': None,
              'correlation': 'single isolated command; status/state have no command ID',
              'dry_run_count': sum(t.get('event') == 'dry_run' for t in traces)}
    sent = [t for t in traces if t.get('event') == 'published']
    if len(sent) != 1:
        result['issues'].append('expected_exactly_one_successful_publication_in_voice_log')
        return result
    trace = sent[0]
    stamp = trace['command_stamp_ns']
    if type(stamp) is not int or stamp <= 0:
        result['issues'].append('invalid_command_stamp')
        return result
    result['command_stamp_ns'] = stamp
    result['action'] = trace['action']
    result['detector'] = trace['detector']
    result['software'] = {key: value for key, value in trace.items()
                          if key.endswith('_seconds')}
    valid = {}
    for role, observer in [('laptop', laptop), ('jetson', jetson)]:
        if observer is None:
            result['issues'].append(role + '_observer_missing')
            continue
        meta = observer['metadata']
        if meta.get('role') != role or meta.get('use_sim_time', True):
            result['issues'].append(role + '_invalid_role_or_simulated_time')
            continue
        commands = [e for e in observer['events'] if e['event'] == 'command']
        if (len(commands) != 1 or commands[0]['stamp_ns'] != stamp
                or commands[0]['action'] != trace['action']
                or commands[0]['target_class'] != trace['target_class']
                or sole_publisher(commands[0]) is None):
            result['issues'].append(role + '_command_missing_multiple_or_mismatched')
            continue
        valid[role] = (observer, commands[0])
    if laptop is not None and laptop['metadata'].get('clock_domain') == trace['clock_domain']:
        start = trace['publish_started_monotonic_ns']
        if 'laptop' in valid:
            events = laptop['events']
            matches = [e for e in events if e['event'] == 'status'
                       and e['action'] == trace['action']
                       and e['target_class'] == trace['target_class']
                       and 0 <= (e['monotonic_ns'] - start) / 1e9 <= 10
                       and sole_publisher(e) is not None]
            state_sources = {sole_publisher(e) for e in events if e['event'] == 'state'}
            if (len(matches) == 1 and state_sources == {sole_publisher(matches[0])}):
                result['metrics']['publish_call_to_status_observed_laptop_seconds'] = (
                    matches[0]['monotonic_ns'] - start) / 1e9
                result['status_accepted'] = matches[0]['accepted']
            else:
                result['issues'].append('status_missing_or_ambiguous')
    elif laptop is not None:
        result['issues'].append('voice_and_laptop_monotonic_clock_domain_mismatch')
    if len(valid) == 2:
        if laptop['metadata'].get('clock_domain') != trace['clock_domain']:
            return result
        if jetson['metadata'].get('clock_domain') == laptop['metadata'].get('clock_domain'):
            result['issues'].append('observers_not_on_distinct_hosts')
            return result
        bounds = [observer_bound(valid[role][0]) for role in ('laptop', 'jetson')]
        bound = sum(bounds) if all(b is not None for b in bounds) else None
        result['estimated_cross_host_error_bound_seconds'] = bound
        if bound is None or bound > 0.02:
            result['issues'].append('cross_host_clock_evidence_missing_or_bound_over_20ms')
        elif abs(trace['unix_ns'] - stamp) / 1e9 > 1:
            result['issues'].append('command_header_not_near_system_time')
        else:
            remote, command = valid['jetson']
            delay = (command['unix_ns'] - stamp) / 1e9
            if delay < -bound or delay > 10:
                result['issues'].append('invalid_cross_host_command_delta')
            else:
                result['metrics']['header_to_command_observed_jetson_seconds'] = delay
                result['metrics']['header_to_command_interval_seconds'] = [
                    delay - bound, delay + bound]
                expected = 'ERROR' if trace['action'] == 'stop' else 'MOVE_TO_RECOGNITION'
                previous = None
                previous_source = None
                transitions = []
                for event in remote['events']:
                    if event['event'] != 'state':
                        continue
                    if (previous is not None and previous != expected
                            and event['state'] == expected
                            and 0 <= (event['monotonic_ns'] - command['monotonic_ns']) / 1e9 <= 10
                            and sole_publisher(event) is not None
                            and sole_publisher(event) == previous_source):
                        transitions.append(event)
                    previous = event['state']
                    previous_source = sole_publisher(event)
                if len(transitions) == 1:
                    state_delta = (transitions[0]['unix_ns'] - stamp) / 1e9
                    result['metrics']['header_to_state_observed_jetson_seconds'] = state_delta
                    result['metrics']['header_to_state_interval_seconds'] = [
                        state_delta - bound, state_delta + bound]
                    result['observed_state'] = expected
                else:
                    result['issues'].append('new_state_transition_missing_or_ambiguous')
    return result


def video_measurement(markers, command_stamp_ns):
    """Measure manual endpoints on one common video timeline, including resolution."""
    if markers['command_stamp_ns'] != command_stamp_ns:
        raise ValueError('video annotation does not match the published command')
    speech = markers['speech_end_seconds']
    resolution = markers['time_resolution_seconds']
    for value in (speech, resolution):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid video timeline value')
    if resolution == 0:
        raise ValueError('video time resolution must be positive')
    deltas = {}
    for part in ('base', 'arm'):
        endpoint = markers.get(part + '_stopped_seconds')
        if endpoint is not None:
            if type(endpoint) not in (int, float) or not math.isfinite(endpoint) or endpoint < 0:
                raise ValueError('invalid stop endpoint on video timeline')
            deltas[part] = endpoint - speech
    physical = max(deltas.values()) if len(deltas) == 2 else None
    return {'physical_stop_seconds': physical,
            'human_video_measurement': {
                'latencies_seconds': deltas, 'time_resolution_seconds': resolution,
                'endpoint_quantization_interval_seconds': (
                    [physical - 2 * resolution, physical + 2 * resolution]
                    if physical is not None else None),
                'notes': markers.get('notes', ''),
            }}
