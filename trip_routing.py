"""Small, rate-limited adapter for Valhalla's public routing demo."""

import json
import math
import os
import subprocess
import threading
import time


ROUTE_URL = os.getenv('GLOBALMAP_VALHALLA_URL', 'https://valhalla1.openstreetmap.de/route')
CLIENT_ID = os.getenv('GLOBALMAP_ROUTING_CLIENT_ID', 'globalmap-local')
MODES = {'driving': 'auto', 'walking': 'pedestrian', 'cycling': 'bicycle'}
_rate_lock = threading.Lock()
_next_request_at = 0.0


class RouteUnavailable(Exception):
    pass


class RouteNotFound(RouteUnavailable):
    pass


class RouteBusy(RouteUnavailable):
    pass


class RouteTooLong(RouteUnavailable):
    pass


def parse_point(value):
    parts = str(value or '').split(',')
    if len(parts) != 2:
        raise ValueError('Expected latitude, longitude')
    try:
        lat, lon = (float(part.strip()) for part in parts)
    except (TypeError, ValueError):
        raise ValueError('Expected latitude, longitude') from None
    if not all(math.isfinite(n) for n in (lat, lon)) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Invalid route coordinate')
    return round(lat, 6), round(lon, 6)


def decode_polyline6(encoded):
    if not isinstance(encoded, str) or len(encoded) > 500000:
        raise RouteUnavailable('Invalid route geometry')
    coordinates = []
    lat = lon = index = 0
    while index < len(encoded):
        values = []
        for _ in range(2):
            shift = result = 0
            while True:
                if index >= len(encoded) or shift > 60:
                    raise RouteUnavailable('Invalid route geometry')
                value = ord(encoded[index]) - 63
                index += 1
                if not 0 <= value <= 63:
                    raise RouteUnavailable('Invalid route geometry')
                result |= (value & 31) << shift
                shift += 5
                if value < 32:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        lat += values[0]
        lon += values[1]
        coordinates.append([round(lon / 1e6, 6), round(lat / 1e6, 6)])
        if len(coordinates) > 100000:
            raise RouteUnavailable('Route is too large')
    if len(coordinates) < 2:
        raise RouteUnavailable('Route geometry is empty')
    return coordinates


def _reserve_request():
    global _next_request_at
    with _rate_lock:
        now = time.monotonic()
        delay = max(0.0, _next_request_at - now)
        if delay > 2.5:
            raise RouteBusy('Routing service is busy')
        _next_request_at = now + delay + 1.1
    if delay:
        time.sleep(delay)


def _osrm_instruction(step):
    maneuver = step.get('maneuver') or {}
    kind = str(maneuver.get('type') or '').replace('_', ' ').lower()
    modifier = str(maneuver.get('modifier') or '').replace('_', ' ').lower()
    road = str(step.get('name') or step.get('ref') or '').strip()[:100]
    onto = f' onto {road}' if road else ''
    on = f' on {road}' if road else ''
    if kind == 'depart': return f'Start{on}.'
    if kind == 'arrive': return 'Arrive at your destination.'
    if kind in ('roundabout', 'rotary'):
        exit_number = maneuver.get('exit')
        return f'At the roundabout, take exit {exit_number}{onto}.' if exit_number else f'Continue through the roundabout{onto}.'
    if kind == 'turn' or kind == 'end of road': return f'Turn {modifier or "ahead"}{onto}.'
    if kind == 'merge': return f'Merge {modifier or "ahead"}{onto}.'
    if kind in ('on ramp', 'off ramp', 'fork'): return f'Take the {modifier + " " if modifier else ""}{kind}{onto}.'
    if kind == 'new name': return f'Continue{on}.'
    if kind == 'continue': return f'Continue {modifier}{on}.'.replace('  ', ' ').replace(' .', '.')
    return f'Continue{on}.'


def _osrm_route(start, end):
    # Valhalla's public demo caps trips at 1,500 km. Use one OSRM request for
    # long driving journeys instead of inventing straight-line road segments.
    url = (f'https://router.project-osrm.org/route/v1/driving/'
           f'{start[1]},{start[0]};{end[1]},{end[0]}'
           '?overview=full&geometries=polyline6&steps=true')
    try:
        result = subprocess.run([
            'curl', '--silent', '--show-error', '--max-time', '30', '--max-filesize', '3000000',
            '--output', '-', '--write-out', '\n%{http_code}',
            '--header', 'Accept: application/json',
            '--header', 'User-Agent: GlobalMap/1.0 (trip planner)', url,
        ], capture_output=True, timeout=35, check=True)
        body, status = result.stdout.rsplit(b'\n', 1)
        status = int(status)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RouteUnavailable('Long-distance routing service is unavailable') from error
    if status == 429:
        raise RouteBusy('Long-distance routing service is busy')
    if status != 200:
        raise RouteUnavailable('Long-distance routing service is unavailable')
    try:
        data = json.loads(body)
        if data.get('code') == 'NoRoute':
            raise RouteNotFound('No connected driving route found')
        if data.get('code') != 'Ok':
            raise RouteUnavailable('Long-distance routing service rejected the trip')
        candidate = data['routes'][0]
        coordinates = decode_polyline6(candidate['geometry'])
        steps = []
        for leg in candidate['legs']:
            for step in leg.get('steps') or []:
                location = (step.get('maneuver') or {}).get('location')
                if not isinstance(location, list) or len(location) != 2:
                    continue
                steps.append({
                    'instruction': _osrm_instruction(step),
                    'distance_km': round(float(step.get('distance') or 0) / 1000, 3),
                    'coordinate': [float(location[0]), float(location[1])],
                })
        return {
            'mode': 'driving',
            'distance_km': round(float(candidate['distance']) / 1000, 3),
            'duration_seconds': round(float(candidate['duration'])),
            'geometry': {'type': 'LineString', 'coordinates': coordinates},
            'steps': steps,
            'provider': 'OSRM / OpenStreetMap',
        }
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
        raise RouteUnavailable('Long-distance routing service returned an invalid route') from error


def route_snapshot(origin, destination, mode):
    start = parse_point(origin)
    end = parse_point(destination)
    if mode not in MODES:
        raise ValueError('Invalid travel mode')
    if start == end:
        raise ValueError('Choose two different points')
    _reserve_request()
    request = {
        'locations': [{'lat': start[0], 'lon': start[1]}, {'lat': end[0], 'lon': end[1]}],
        'costing': MODES[mode], 'units': 'kilometers', 'directions_type': 'instructions',
    }
    try:
        result = subprocess.run([
            'curl', '--silent', '--show-error', '--max-time', '25', '--max-filesize', '1048576',
            '--output', '-', '--write-out', '\n%{http_code}', '--request', 'POST',
            '--header', 'Content-Type: application/json', '--header', f'X-Client-Id: {CLIENT_ID}',
            '--header', 'User-Agent: GlobalMap/1.0 (trip planner)', '--data-binary', '@-', ROUTE_URL,
        ], input=json.dumps(request, separators=(',', ':')).encode(), capture_output=True, timeout=30, check=True)
        body, status = result.stdout.rsplit(b'\n', 1)
        status = int(status)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RouteUnavailable('Routing service is unavailable') from error
    if status == 429:
        raise RouteBusy('Routing service is busy')
    if status == 400:
        try:
            error_code = json.loads(body).get('error_code')
        except (ValueError, TypeError):
            error_code = None
        if error_code == 154:
            if mode == 'driving': return _osrm_route(start, end)
            raise RouteTooLong('Walking and cycling trips exceed the public routing range')
        raise RouteUnavailable('Routing service rejected the trip')
    if status == 404:
        raise RouteNotFound('No route found between these points')
    if status != 200:
        raise RouteUnavailable('Routing service is unavailable')
    try:
        trip = json.loads(body)['trip']
        leg = trip['legs'][0]
        coordinates = decode_polyline6(leg['shape'])
        summary = trip['summary']
        steps = []
        for maneuver in leg.get('maneuvers', []):
            instruction = str(maneuver.get('instruction') or '').strip()
            if not instruction:
                continue
            shape_index = max(0, min(len(coordinates) - 1, int(maneuver.get('begin_shape_index') or 0)))
            steps.append({
                'instruction': instruction[:300],
                'distance_km': round(float(maneuver.get('length') or 0), 3),
                'coordinate': coordinates[shape_index],
            })
        return {
            'mode': mode,
            'distance_km': round(float(summary['length']), 3),
            'duration_seconds': round(float(summary['time'])),
            'geometry': {'type': 'LineString', 'coordinates': coordinates},
            'steps': steps,
            'provider': 'Valhalla / OpenStreetMap',
        }
    except (KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
        raise RouteUnavailable('Routing service returned an invalid route') from error
