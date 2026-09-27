import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import trip_routing


class TripRoutingTests(unittest.TestCase):
    @patch.object(trip_routing, '_reserve_request')
    @patch.object(trip_routing.subprocess, 'run')
    def test_long_driving_trip_uses_osrm_road_route(self, run, _reserve):
        valhalla = SimpleNamespace(stdout=b'{"error_code":154,"error":"Path distance exceeds the max distance limit"}\n400')
        osrm = {'code': 'Ok', 'routes': [{'geometry': '??AA', 'distance': 4039000,
                'duration': 144000, 'legs': [{'steps': [
                    {'maneuver': {'type': 'depart', 'location': [0, 0]}, 'name': 'Main St', 'distance': 1000},
                    {'maneuver': {'type': 'arrive', 'location': [0.000001, 0.000001]}, 'distance': 0},
                ]}]}]}
        run.side_effect = [valhalla, SimpleNamespace(stdout=json.dumps(osrm).encode() + b'\n200')]
        result = trip_routing.route_snapshot('28.67617,-81.51186', '34.05223,-118.24368', 'driving')
        self.assertEqual(result['provider'], 'OSRM / OpenStreetMap')
        self.assertEqual(result['distance_km'], 4039)
        self.assertEqual(result['steps'][0]['instruction'], 'Start on Main St.')
        self.assertIn('router.project-osrm.org', run.call_args.args[0][-1])

    @patch.object(trip_routing, '_reserve_request')
    @patch.object(trip_routing.subprocess, 'run')
    def test_long_walking_trip_reports_provider_range(self, run, _reserve):
        run.return_value.stdout = b'{"error_code":154}\n400'
        with self.assertRaises(trip_routing.RouteTooLong):
            trip_routing.route_snapshot('28.67617,-81.51186', '34.05223,-118.24368', 'walking')
        run.assert_called_once()

    def test_coordinate_validation_and_polyline(self):
        self.assertEqual(trip_routing.parse_point('28.5384, -81.3789'), (28.5384, -81.3789))
        for value in ('', '91,0', '0,181', 'nan,0', '0,0,0'):
            with self.assertRaises(ValueError):
                trip_routing.parse_point(value)
        self.assertEqual(trip_routing.decode_polyline6('??AA'), [[0, 0], [0.000001, 0.000001]])

    @patch.object(trip_routing, '_reserve_request')
    @patch.object(trip_routing.subprocess, 'run')
    def test_route_response_includes_geometry_and_maneuvers(self, run, _reserve):
        response = {'trip': {'summary': {'length': 1.5, 'time': 180}, 'legs': [{
            'shape': '??AA', 'maneuvers': [
                {'instruction': 'Head north.', 'length': 1.5, 'begin_shape_index': 0},
                {'instruction': 'Arrive.', 'length': 0, 'begin_shape_index': 1},
            ],
        }]}}
        run.return_value.stdout = json.dumps(response).encode() + b'\n200'
        result = trip_routing.route_snapshot('0,0', '0.000001,0.000001', 'walking')
        self.assertEqual(result['geometry']['coordinates'], [[0, 0], [0.000001, 0.000001]])
        self.assertEqual(result['distance_km'], 1.5)
        self.assertEqual(result['steps'][1]['coordinate'], [0.000001, 0.000001])
        self.assertIn(b'"costing":"pedestrian"', run.call_args.kwargs['input'])


if __name__ == '__main__':
    unittest.main()
