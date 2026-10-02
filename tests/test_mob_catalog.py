import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import mob_catalog as catalog
import server_sync as sync

def mob(name='Carabok', hp=10, level=1):
    return dict(name=name, type='Animal', planets=['Arkadia'], maturities={'Puny': {'hp': hp, 'level': level}})

class MobCatalogTests(unittest.TestCase):
    def test_server_duplicates_win_entire_record_and_inputs_are_not_mutated(self):
        local = [mob(hp=50), mob('Local only')]; remote = [mob('carabok', hp=None, level=9), mob('Server only')]
        before = copy.deepcopy((local, remote)); merged = catalog.merge_mobs(local, remote)
        self.assertEqual(merged['carabok']['maturities']['Puny'], {'hp': None, 'level': 9})
        self.assertEqual(set(merged), {'carabok', 'Local only', 'Server only'}); self.assertEqual((local, remote), before)
    def test_saved_catalog_survives_restart_and_preserves_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'mobs.json'; catalog.save_mobs({'Carabok': {k:v for k,v in mob(hp=66).items() if k != 'name'}}, path)
            loaded = catalog.load_mobs({'Other': {k:v for k,v in mob('Other').items() if k != 'name'}}, path)
            self.assertEqual(loaded['Carabok']['maturities']['Puny']['hp'], 66); self.assertIn('Other', loaded)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)
    def test_sync_saves_before_upload_and_rereads_server_to_resolve_races(self):
        client = sync.ServerClient('https://example.test', 'test-token'); calls, saves = [], []
        remote = [mob(hp=20), mob('Server only')]
        def request(path, body=None, method='GET'):
            calls.append((path, copy.deepcopy(body), method))
            if path.startswith('mobs/?'):
                return {'rows': remote, 'total': len(remote)}
            self.assertEqual(len(saves), 1)
            self.assertEqual(next(r for r in body['mobs'] if r['name']=='Carabok')['maturities']['Puny']['hp'], 20)
            remote[0] = mob(hp=30); remote.append(mob('Concurrent server'))
            return {'added': 1}
        with patch.object(client, 'request', side_effect=request):
            result = client.sync_mobs([mob(hp=5), mob('Local only')], lambda data: saves.append(copy.deepcopy(data)))
        self.assertEqual(result['Carabok']['maturities']['Puny']['hp'], 30); self.assertIn('Concurrent server', result)
        self.assertEqual(len(saves), 2); self.assertEqual(calls[1][0], 'mobs/sync')
    def test_bad_remote_does_not_save_or_upload(self):
        client = sync.ServerClient('https://example.test', 'test-token')
        with patch.object(client, 'request', return_value={'rows': [mob(hp=-1)], 'total': 1}) as request:
            with self.assertRaises(sync.UploadError): client.sync_mobs([mob()], lambda _: self.fail('Must not save'))
            self.assertEqual(request.call_count, 1)
    def test_duplicates_bad_prices_and_empty_names_are_rejected(self):
        for rows in ([mob(), mob('carabok')], [mob(hp=float('nan'))], [mob(name='')], [mob(hp=-5)]):
            with self.assertRaises(sync.UploadError): catalog.mob_rows(rows)
    def test_equipment_and_utc_loot_clocks_survive_upload_without_private_fields(self):
        raw = dict(id='a', mob='Carabok', weapon='Gun', amplifier='Amp', attachments=['Sight', 'Scope'], damage_total=100,
            started_at='2026-10-01T10:00:00+03:00', ended_at='2026-10-01T11:00:00+03:00', ped_cycled=10,
            loot_events=[dict(started_at='2026-10-01 07:05:00', ended_at='2026-10-01T10:05:01+03:00', items={'Oil':1}, value_ped=1, secret='private')])
        before = copy.deepcopy(raw); payload = sync.session_payload(raw)
        self.assertEqual(payload['amplifier'],'Amp'); self.assertEqual(payload['attachments'],['Sight','Scope'])
        self.assertEqual(payload['loot_events'][0]['started_at'],'2026-10-01T07:05:00+00:00')
        self.assertEqual(payload['loot_events'][0]['ended_at'],'2026-10-01T07:05:01+00:00'); self.assertEqual(raw,before)
        self.assertNotIn('secret',payload['loot_events'][0])
    @unittest.skipUnless(hasattr(time, 'tzset'), 'Requires timezone control')
    def test_naive_loot_clock_is_utc_even_when_computer_is_in_moscow(self):
        previous = os.environ.get('TZ')
        try:
            os.environ['TZ']='Europe/Moscow'; time.tzset()
            raw=dict(id='a',mob='Carabok',started_at='2026-10-01T10:00:00',ended_at='2026-10-01T11:00:00',ped_cycled=10,loot_events=[dict(timestamp='2026-10-01 07:05:00')])
            payload=sync.session_payload(raw)
            self.assertEqual(payload['started_at'],'2026-10-01T07:00:00+00:00')
            self.assertEqual(payload['loot_events'][0]['timestamp'],'2026-10-01T07:05:00+00:00')
        finally:
            if previous is None: os.environ.pop('TZ',None)
            else: os.environ['TZ']=previous
            time.tzset()
