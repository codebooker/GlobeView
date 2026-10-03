import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import armenia_enforcement as feed
import proxy
from scripts.update_armenia_enforcement import (
    address_streets, match_inventory, qualified_road, read_inventory, street_descriptor)


def road(way_id, name, nodes, english=None):
    tags = {'name': name, 'highway': 'primary'}
    if english:
        tags['name:en'] = english
    return {'id': way_id, 'tags': tags,
            'nodes': [{'id': node_id, 'lat': lat, 'lon': lon} for node_id, lat, lon in nodes]}


class ArmeniaEnforcementTests(unittest.TestCase):
    def test_recorded_old_names_find_the_same_physical_junction(self):
        entry = 'Կասյան-Գյուլբենկյան փողոցների խաչմերուկ (անշարժ տեսախցիկներ).'
        renamed = road(1, 'Վազգեն I փողոց', [(10, 40.20, 44.49)])
        renamed['tags']['old_name'] = 'Կասյան փողոց'
        matched, omitted = match_inventory([entry], [renamed,
            road(2, 'Գյուլբենկյան փողոց', [(10, 40.20, 44.49)])])
        self.assertFalse(omitted)
        self.assertEqual(matched[0]['osmNode'], 10)
        self.assertIn('Կասյան փողոց', matched[0]['matchedRoadNames'])
        self.assertIn('Վազգեն I փողոց', matched[0]['roadNames'])

    def test_aliases_cannot_count_one_way_as_two_junction_roads(self):
        entry = 'Կասյան-Գյուլբենկյան փողոցների խաչմերուկ (անշարժ տեսախցիկներ).'
        renamed = road(1, 'Գյուլբենկյան փողոց', [(10, 40.20, 44.49)])
        renamed['tags']['old_name'] = 'Կասյան փողոց'
        self.assertFalse(match_inventory([entry], [renamed])[0])

    def test_square_and_spelling_variants_preserve_personal_qualifiers(self):
        entry = 'Տ.Մեծ պողոտա-Հանրապետության Հրապարակ խաչմերուկ (անշարժ տեսախցիկներ).'
        roads = [road(1, 'Տիգրան Մեծի պողոտա', [(10, 40.18, 44.51)]),
                 road(2, 'Հանրապետության հրապարակ', [(10, 40.18, 44.51)])]
        self.assertEqual(match_inventory([entry], roads)[0][0]['osmNode'], 10)
        descriptor = street_descriptor('Փ․Բյուզանդ փողոց')
        self.assertTrue(qualified_road(descriptor, 'Փավստոս Բուզանդի փողոց'))
        self.assertFalse(qualified_road(descriptor, 'Էլեն Բյուզանդի փողոց'))
        entry = 'Մ.Խորենացի-Զաքիյան փողոցների խաչմերուկ (անշարժ տեսախցիկներ).'
        self.assertTrue(match_inventory([entry], [
            road(1, 'Մովսես Խորենացու փողոց', [(10, 40.18, 44.51)]),
            road(2, 'Զաքյան փողոց', [(10, 40.18, 44.51)])])[0])

    def test_reference_requires_shared_node_on_all_explicit_roads(self):
        entry = 'Մ.Մաշտոցի պողոտա-Ամիրյան փողոց խաչմերուկ (անշարժ տեսախցիկներ).'
        roads = [road(1, 'Մեսրոպ Մաշտոցի պողոտա', [(10, 40.18, 44.51)], 'Mashtots Avenue'),
                 road(2, 'Ամիրյան փողոց', [(10, 40.18, 44.51)], 'Amiryan Street')]
        matched, omitted = match_inventory([entry], roads)
        self.assertEqual(omitted, [])
        self.assertEqual(matched[0]['osmNode'], 10)
        self.assertEqual(matched[0]['osmWays'], [1, 2])
        self.assertEqual(matched[0]['locationKind'], 'junction_reference')
        self.assertEqual(matched[0]['roadNames'], ['Amiryan Street', 'Mashtots Avenue'])
        roads[1]['nodes'][0]['id'] = 11  # overlapping coordinates do not prove a connected junction
        self.assertEqual(match_inventory([entry], roads)[0], [])
        three_roads = entry.replace('Ամիրյան փողոց', 'Ամիրյան փողոց-Հանրապետության փողոց')
        roads.append(road(3, 'Հանրապետության փողոց', [(12, 40.18, 44.51)]))
        self.assertEqual(match_inventory([three_roads], roads)[0], [])

    def test_initials_and_full_names_cannot_match_different_people(self):
        paruyr = address_streets('Պ.Սևակ-Դրո փողոցների խաչմերուկ')[0]
        self.assertTrue(qualified_road(paruyr, 'Պարույր Սևակի փողոց'))
        self.assertFalse(qualified_road(paruyr, 'Ռուբեն Սևակի փողոց'))
        self.assertFalse(qualified_road(paruyr, 'Սևակի փողոց'))
        david = address_streets('Դավիթ Բեկի փողոց-Հ.Ավետիսյան փողոց խաչմերուկ')[0]
        self.assertTrue(qualified_road(david, 'Դավիթ Բեկի փողոց'))
        self.assertFalse(qualified_road(david, 'Տիգրան Բեկի փողոց'))
        admiral = address_streets('Ծ.Իսակովի պողոտա-Աթենքի փողոց խաչմերուկ')[0]
        self.assertTrue(qualified_road(admiral, 'Ադմիրալ Իսակովի պողոտա'))
        self.assertFalse(qualified_road(admiral, 'Իսակովի փողոց'))

    def test_compounds_genitives_and_numbered_streets_retain_identity(self):
        pairs = [('Մ.Խորենացի փողոց', 'Մովսես Խորենացու փողոց'),
                 ('Էրեբունի փողոց', 'Էրեբունու փողոց'),
                 ('Սեբաստիա փողոց', 'Սեբաստիայի փողոց'),
                 ('Կ.Ուլնեցի փողոց', 'Կարապետ Ուլնեցու փողոց')]
        for source, name in pairs:
            with self.subTest(source=source):
                self.assertTrue(qualified_road(street_descriptor(source), name))
        compound = address_streets('Մ.Մաշտոց-Սայաթ-Նովա պողոտաների խաչմերուկ')
        self.assertEqual(len(compound), 2)
        self.assertTrue(qualified_road(compound[1], 'Սայաթ-Նովայի պողոտա'))
        numbered = address_streets('Սեբաստիա-Շահումյան 1-ին փողոցների խաչմերուկ')[1]
        self.assertTrue(qualified_road(numbered, 'Շահումյան 1-ին փողոց'))
        self.assertFalse(qualified_road(numbered, 'Շահումյան 14-րդ փողոց'))
        upper = street_descriptor('Վերին Շենգավիթ 2-րդ փողոց')
        self.assertFalse(qualified_road(upper, 'Ներքին Շենգավիթ 2-րդ փողոց'))

    def test_mismatched_or_extended_junctions_are_omitted(self):
        entry = 'Մ.Մաշտոցի պողոտա-Ամիրյան փողոց խաչմերուկ (անշարժ տեսախցիկներ).'
        nodes = [(10, 40.18, 44.51), (11, 40.20, 44.53)]
        roads = [road(1, 'Մեսրոպ Մաշտոցի պողոտա', nodes), road(2, 'Ամիրյան փողոց', nodes)]
        matched, omitted = match_inventory([entry], roads)
        self.assertFalse(matched)
        self.assertEqual(omitted[0]['reason'], 'Multiple or extended junction matches')
        roads[1]['tags']['name'] = 'Չարենց փողոց'
        self.assertFalse(match_inventory([entry], roads)[0])

    def test_nearby_carriageway_junction_nodes_use_an_actual_osm_node(self):
        entry = 'Մ.Մաշտոցի պողոտա-Ամիրյան փողոց խաչմերուկ (անշարժ տեսախցիկներ).'
        nodes = [(10, 40.18000, 44.51), (11, 40.18005, 44.51), (12, 40.18010, 44.51)]
        roads = [road(1, 'Մեսրոպ Մաշտոցի պողոտա', nodes), road(2, 'Ամիրյան փողոց', nodes)]
        matched, omitted = match_inventory([entry], roads)
        self.assertFalse(omitted)
        self.assertEqual(matched[0]['osmNode'], 11)
        self.assertEqual(matched[0]['lat'], 40.18005)
        self.assertLess(matched[0]['matchSpreadMetres'], 100)

    def test_only_named_junctions_with_explicit_fixed_cameras_are_mapped(self):
        entries = ['Մ.Մաշտոցի պողոտա-Ամիրյան փողոց խաչմերուկ (շարժական տեսախցիկ).',
                   'Մ.Մաշտոցի պողոտա դպրոց ճանապարհահատված (անշարժ տեսախցիկներ).',
                   'Մ.Մաշտոցի պողոտա (անշարժ տեսախցիկներ).']
        matched, omitted = match_inventory(entries, [])
        self.assertFalse(matched)
        self.assertEqual(len(omitted), len(entries))

    def test_primary_document_provenance_and_entry_count_are_required(self):
        def document(modified=feed.DOCUMENT_MODIFIED, count=158):
            stream = io.BytesIO()
            paragraphs = ['Երևան քաղաքի խաչմերուկներում գործող տեսադիտարկման սարքերի ցանկ']
            paragraphs += ['Մաշտոցի-Ամիրյան փողոցների խաչմերուկ (անշարժ տեսախցիկներ).'] * count
            with zipfile.ZipFile(stream, 'w') as archive:
                archive.writestr('docProps/core.xml',
                                 '<core xmlns:dcterms="http://purl.org/dc/terms/">'
                                 f'<dcterms:modified>{modified}</dcterms:modified></core>')
                archive.writestr('word/document.xml',
                                 '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                                 + ''.join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs)
                                 + '</w:document>')
            return stream.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.docx'
            path.write_bytes(document())
            self.assertEqual(len(read_inventory(path)), 158)
            path.write_bytes(document(modified='2022-01-01T00:00:00Z'))
            with self.assertRaisesRegex(ValueError, 'document changed'):
                read_inventory(path)
            path.write_bytes(document(count=157))
            with self.assertRaisesRegex(ValueError, 'count changed'):
                read_inventory(path)

    def test_committed_catalog_and_bbox_are_inventory_references_without_streams(self):
        feed.enforcement_catalog.cache_clear()
        rows = feed.enforcement_catalog()
        self.assertEqual(len(rows), 106)
        visible = feed.enforcement_for_bbox((44.40, 40.08, 44.63, 40.28))
        self.assertEqual(len(visible), 106)
        self.assertFalse(feed.enforcement_for_bbox((44.8, 40.1, 45.0, 40.3)))
        for item in visible:
            self.assertEqual(item['record_kind'], 'road_surveillance_inventory')
            self.assertIn('Approximate junction', item['detail'])
            self.assertNotIn('Plate reader', item['title'])
            self.assertIn('Public video unavailable', item['detail'])
            self.assertIn('status unverified', item['source'])
            self.assertEqual(item['source_url'], feed.SOURCE_URL)
            self.assertFalse({'video_url', 'image_url', 'stream_url'} & set(item))

    def test_catalog_rejects_invalid_coordinates_duplicates_and_provenance(self):
        catalog = json.loads(feed.CATALOG_PATH.read_text(encoding='utf-8'))
        for kind in ('duplicate', 'outside', 'nan', 'node', 'source'):
            with self.subTest(kind=kind):
                candidate = copy.deepcopy(catalog)
                if kind == 'duplicate':
                    candidate['locations'][1]['id'] = candidate['locations'][0]['id']
                elif kind == 'outside':
                    candidate['locations'][0]['lon'] = 45
                elif kind == 'nan':
                    candidate['locations'][0]['lat'] = float('nan')
                elif kind == 'node':
                    candidate['locations'][0]['osmNode'] = None
                else:
                    candidate['sourceUrl'] = 'https://example.test/'
                feed.enforcement_catalog.cache_clear()
                fake_path = MagicMock()
                fake_path.open.return_value = io.StringIO(json.dumps(candidate))
                with patch.object(feed, 'CATALOG_PATH', fake_path):
                    with self.assertRaises(ValueError):
                        feed.enforcement_catalog()
        feed.enforcement_catalog.cache_clear()

    def test_official_references_survive_a_deflock_failure_and_filter_viewport(self):
        bbox = (44.5, 40.17, 44.52, 40.19)
        expected = feed.enforcement_for_bbox(bbox)
        self.assertTrue(expected)
        with patch.object(proxy, 'cached_deflock_json', side_effect=OSError('unavailable')):
            data = json.loads(proxy.fetch_deflock_lpr_content(bbox))
        self.assertEqual({row['id'] for row in data['elements']}, {row['id'] for row in expected})
        self.assertTrue(data['viewport_filtered'])
        self.assertTrue(data['sourceErrors'])

    def test_views_elsewhere_do_not_load_the_armenia_inventory(self):
        with patch.object(proxy, 'cached_deflock_json', return_value={
                'tile_size_degrees': 20, 'regions': [],
                'tile_url': 'https://example.test/{lat}/{lon}.json'}), \
                patch.object(proxy, 'armenia_enforcement_for_bbox') as read:
            proxy.fetch_deflock_lpr_content((-80, 35, -75, 40))
        read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
