"""Independent fail-closed regression contract for PR336; all dependencies are fakes."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
# Works both in this isolated bundle and as tests.test_gate_regressions in a checkout.
if Path(__file__).resolve().parent.name == 'tests':
    sys.path[:0] = [str(Path(__file__).resolve().parent),
                   str(Path(__file__).resolve().parents[1] / 'scripts')]
import bus_verify as bv
import bus_reconcile as rec
import bus
from test_bus_verify import HEADER, BOARD, FakeStore, row, gateway, seeded, full
from test_bus_reconcile import FakeRedis

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cloud_verify_board', Path(bv.__file__).parents[1] / 'cloud/bus-reconciler/verify_board.py')
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)


def capture_main(entrypoint, reader=None, load_error=None, conn=None):
    conn = seeded(BOARD) if conn is None else conn
    out = io.StringIO()
    status = {key: False for key in bv.redis_dual.STATUS_FOR_LOG}
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(bv.redis_dual, 'Settings', return_value=object()))
        stack.enter_context(patch.object(bv.redis_dual, 'status', return_value=status))
        stack.enter_context(patch.object(bv.redis_dual, 'client', return_value=conn))
        stack.enter_context(patch.object(bus, 'load_env', side_effect=load_error, return_value={}))
        stack.enter_context(patch.object(bus, 'read_range', side_effect=reader or (lambda env, f, c: gateway(BOARD)(f,c))))
        stack.enter_context(contextlib.redirect_stdout(out))
        escaped = None
        try:
            code = entrypoint()
        except (Exception, SystemExit) as error:
            code, escaped = None, type(error).__name__
    receipts = []
    for line in out.getvalue().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and 'verdict' in record:
            receipts.append(record)
    return {'exit_code': code, 'escaped': escaped, 'receipts': receipts, 'stdout': out.getvalue()}


class ExemptionRegression(unittest.TestCase):
    def test_future_occurrence_of_exempt_id_is_not_eligible(self):
        dup = sorted(bv.ratified()['ids'])[0]
        board = [list(HEADER)] + [row(i) for i in range(1, 3736)]
        board[1][0] = dup
        later = list(board[1]); later[8] = 'NEW DUPLICATE CONTENT NOT MIRRORED'
        board.append(later) # appended at 3737, beyond the explicit ratification snapshot
        result = full(seeded(board), board)
        self.assertFalse(bv.gate_eligible(result), {k:result[k] for k in ['verdict','duplicate_row_ids','duplicates_ratified','duplicates_unratified','matched','unique_row_ids']})

    def test_idless_positions_tamper_is_rejected(self):
        data = json.loads(bv.RATIFIED.read_text())
        data['idless_positions'].append(3)
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / 'tampered.json'; path.write_text(json.dumps(data))
            exemption = bv.ratified(path)
        self.assertFalse(exemption['census_present'], 'changed idless positions cannot establish an approved census')

    def test_idless_tamper_cannot_make_a_record_eligible(self):
        data = json.loads(bv.RATIFIED.read_text()); data['idless_positions'].append(3)
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / 'tampered.json'; path.write_text(json.dumps(data)); exemption = bv.ratified(path)
        board = [list(HEADER), row(1), ['', 'unreviewed idless content']]
        result = full(seeded(board), board, exempt=exemption)
        self.assertFalse(bv.gate_eligible(result), result)




class ReceiptRegression(unittest.TestCase):
    def assert_unknown_receipt(self, outcome):
        self.assertIsNone(outcome['escaped'], outcome)
        self.assertEqual(1, len(outcome['receipts']), outcome)
        receipt = outcome['receipts'][0]
        self.assertEqual(bv.UNKNOWN, receipt['verdict'])
        self.assertFalse(receipt['gate_eligible'])
        self.assertGreater(receipt['read_failures'], 0)

    def test_cli_initial_read_systemexit_has_receipt(self):
        self.assert_unknown_receipt(capture_main(lambda: bv.main([]), reader=lambda *args: (_ for _ in ()).throw(SystemExit('flap'))))

    def test_cloud_initial_read_systemexit_has_receipt(self):
        self.assert_unknown_receipt(capture_main(cloud.main, reader=lambda *args: (_ for _ in ()).throw(SystemExit('flap'))))

    def test_cli_load_env_systemexit_has_receipt(self):
        self.assert_unknown_receipt(capture_main(lambda: bv.main([]), load_error=SystemExit('missing env')))

    def test_cloud_load_env_systemexit_has_receipt(self):
        self.assert_unknown_receipt(capture_main(cloud.main, load_error=SystemExit('missing env')))

    def test_cli_recheck_systemexit_has_receipt(self):
        calls = 0
        def reader(env, first, count):
            nonlocal calls
            calls += 1
            if calls > 2: # initial head, then one full walk chunk, then recheck
                raise SystemExit('recheck flap')
            return gateway(BOARD)(first,count)
        self.assert_unknown_receipt(capture_main(lambda: bv.main([]), reader=reader))

    def test_cloud_recheck_systemexit_has_receipt(self):
        calls = 0
        def reader(env, first, count):
            nonlocal calls
            calls += 1
            if calls > 2:
                raise SystemExit('recheck flap')
            return gateway(BOARD)(first,count)
        self.assert_unknown_receipt(capture_main(cloud.main, reader=reader))


class RecheckRegression(unittest.TestCase):
    def test_unobserved_recheck_does_not_qualify(self):
        result = bv.verify(seeded(BOARD), gateway(BOARD), len(BOARD), total_at_end=len(BOARD), remember=bv.recheck_points(len(BOARD)))
        bv.apply_recheck(result, {999: 'not a walked position'})
        self.assertFalse(bv.gate_eligible(result), result)

    def test_header_only_recheck_does_not_qualify(self):
        result = bv.verify(seeded(BOARD), gateway(BOARD), len(BOARD), total_at_end=len(BOARD), remember=bv.recheck_points(len(BOARD)))
        bv.apply_recheck(result, {1: HEADER[0]})
        self.assertFalse(bv.gate_eligible(result), result)

    def test_short_header_recheck_invalidates_cli_receipt(self):
        calls = 0
        def reader(env, first, count):
            nonlocal calls
            calls += 1
            if calls == 3:
                return {'rows': [], 'start': 1, 'count': 0, 'total': len(BOARD)}
            return gateway(BOARD)(first,count)
        self.assert_unknown(capture_main(lambda: bv.main([]), reader=reader))

    def test_short_header_recheck_invalidates_cloud_receipt(self):
        calls = 0
        def reader(env, first, count):
            nonlocal calls
            calls += 1
            if calls == 3:
                return {'rows': [], 'start': 1, 'count': 0, 'total': len(BOARD)}
            return gateway(BOARD)(first,count)
        self.assert_unknown(capture_main(cloud.main, reader=reader))

    def assert_unknown(self, outcome):
        self.assertEqual(1, len(outcome['receipts']), outcome)
        self.assertFalse(outcome['receipts'][0]['gate_eligible'], outcome)
        self.assertEqual(bv.UNKNOWN, outcome['receipts'][0]['verdict'])


class LegacyReadRegression(unittest.TestCase):
    def test_missing_rows_reply_is_rejected_not_no_sample(self):
        with self.assertRaises(ValueError):
            rec.board_rows(None, reader=lambda **kwargs: {'ok': True, 'service': 'bus'})

    def test_explicit_empty_rows_remains_no_sample(self):
        rows = rec.board_rows(None, reader=lambda **kwargs: {'rows': []})
        self.assertEqual(rec.NO_SAMPLE, rec.compare(FakeRedis(), rows)['verdict'])


class PositiveControlsAndShape(unittest.TestCase):
    def test_clean_cli_is_eligible(self):
        outcome = capture_main(lambda: bv.main([]))
        self.assertIsNone(outcome['escaped'], outcome)
        self.assertEqual(0, outcome['exit_code'])
        self.assertEqual(1, len(outcome['receipts']))
        self.assertTrue(outcome['receipts'][0]['gate_eligible'])

    def test_clean_cloud_is_eligible(self):
        outcome = capture_main(cloud.main)
        self.assertEqual(0, outcome['exit_code'])
        self.assertEqual(1, len(outcome['receipts']))
        self.assertTrue(outcome['receipts'][0]['gate_eligible'])

    def test_explicit_empty_board_is_no_sample(self):
        for entrypoint in (lambda: bv.main([]), cloud.main):
            with self.subTest(entrypoint=entrypoint):
                outcome = capture_main(entrypoint, reader=lambda *args: {'rows': [], 'total': 0, 'count': 0, 'start': 1})
                self.assertIsNone(outcome['escaped'], outcome)
                self.assertEqual(1, len(outcome['receipts']), outcome)
                self.assertEqual(bv.NO_SAMPLE, outcome['receipts'][0]['verdict'])
                self.assertFalse(outcome['receipts'][0]['gate_eligible'])
                self.assertEqual(0, outcome['receipts'][0]['read_failures'])

    def test_header_only_is_no_sample(self):
        board = [HEADER]
        for entrypoint in (lambda: bv.main([]), cloud.main):
            with self.subTest(entrypoint=entrypoint):
                outcome = capture_main(entrypoint, conn=FakeStore(), reader=lambda env, f, c: gateway(board)(f,c))
                self.assertEqual(1, len(outcome['receipts']), outcome)
                self.assertEqual(bv.NO_SAMPLE, outcome['receipts'][0]['verdict'])
                self.assertFalse(outcome['receipts'][0]['gate_eligible'])

    def test_bad_initial_shapes_are_unknown_receipts(self):
        for reply in (None, {}, {'total':0}, {'rows':None,'total':0,'count':0,'start':1}, {'rows': [], 'total':0,'count':0,'start':2}, {'rows':[HEADER], 'start':True,'total':1,'count':1}, {'rows':[HEADER], 'start':1,'total':True,'count':1}):
            with self.subTest(reply=reply):
                outcome = capture_main(lambda: bv.main([]), reader=lambda *args: reply)
                self.assertIsNone(outcome['escaped'], outcome)
                self.assertEqual(1, len(outcome['receipts']), outcome)
                self.assertEqual(bv.UNKNOWN, outcome['receipts'][0]['verdict'])
                self.assertFalse(outcome['receipts'][0]['gate_eligible'])

    def test_underspecified_recheck_schedule_is_ineligible(self):
        result = bv.verify(seeded(BOARD), gateway(BOARD), len(BOARD), total_at_end=len(BOARD), remember=[1])
        bv.apply_recheck(result, {1: HEADER[0]})
        self.assertFalse(bv.gate_eligible(result), result)

    def test_existing_v1_duplicate_scope_without_exact_boundaries_is_ineligible(self):
        dup = sorted(bv.ratified()['ids'])[0]
        item = row(1); item[0] = dup
        result = full(seeded([HEADER,item,item]), [HEADER,item,item])
        self.assertIn(result['verdict'], (bv.UNKNOWN, bv.DIVERGE))
        self.assertFalse(bv.gate_eligible(result))
        self.assertEqual(1, result['duplicate_row_ids'])

    def test_existing_v1_idless_scope_without_content_boundaries_is_ineligible(self):
        board = [list(HEADER)] + [row(i) for i in range(1, 1563)]
        board[1562] = ['', 'modified content at a declared idless position']
        result = full(seeded(board), board)
        self.assertEqual(bv.UNKNOWN, result['verdict'])
        self.assertFalse(bv.gate_eligible(result))

    def test_real_read_range_exhaustion_has_receipt(self):
        real_read_range = bus.read_range
        env = {'BUS_URL': 'https://offline.invalid', 'BUS_SECRET':'fake-test-placeholder'}
        with patch.object(bus, 'fetch', return_value='{"ok":true}'), contextlib.redirect_stderr(io.StringIO()):
            outcome = capture_main(lambda: bv.main([]), reader=lambda _env,f,c: real_read_range(env,f,c))
        self.assertIsNone(outcome['escaped'], outcome)
        self.assertEqual(1, len(outcome['receipts']), outcome)
        self.assertEqual(bv.UNKNOWN, outcome['receipts'][0]['verdict'])

    def test_legacy_only_idless_rows_are_unknown(self):
        result = rec.compare(FakeRedis(), [['', 'some content']])
        self.assertEqual(rec.UNKNOWN, result['verdict'])

    def test_legacy_malformed_rows_are_not_no_sample(self):
        for reply in ({'rows':None}, {'rows':['junk']}, {'rows':[[]]}, None):
            with self.subTest(reply=reply):
                with self.assertRaises(ValueError):
                    rec.board_rows(None, reader=lambda **kwargs: reply)

    def test_legacy_cli_missing_rows_has_unknown_receipt(self):
        out=io.StringIO()
        with patch.object(rec.redis_dual,'Settings',return_value=object()), patch.object(rec.redis_dual,'client',return_value=FakeRedis()), patch.object(bus,'load_env',return_value={}), patch.object(bus,'read_rows',return_value={'ok':True}), contextlib.redirect_stdout(out):
            code=rec.main(['--limit','1','--json'])
        self.assertEqual(2,code)
        records=[json.loads(line) for line in out.getvalue().splitlines() if line.startswith('{')]
        self.assertEqual(1,len(records))
        self.assertEqual(rec.UNKNOWN,records[0]['verdict'])

    def test_cloud_malformed_chunk_has_unknown_receipt(self):
        import os
        with patch.dict(os.environ, {'VERIFY_CHUNK':'bad-value'}):
            outcome=capture_main(cloud.main)
        self.assertIsNone(outcome['escaped'],outcome)
        self.assertEqual(1,len(outcome['receipts']),outcome)
        self.assertEqual(bv.UNKNOWN,outcome['receipts'][0]['verdict'])

    def test_stream_failure_keeps_one_durable_receipt(self):
        conn=seeded(BOARD); conn.fail_xadd=True
        outcome=capture_main(lambda:bv.main([]),conn=conn)
        self.assertEqual(1,len(outcome['receipts']),outcome)
        self.assertTrue(outcome['receipts'][0]['gate_eligible'])
        self.assertIn('stream_write_failed',outcome['stdout'])


class TransportPipelineRegression(unittest.TestCase):
    ENV = {'BUS_URL':'https://offline.invalid', 'BUS_SECRET':'fake-test-placeholder'}

    def test_real_range_reader_cannot_hide_empty_physical_row(self):
        real_reader=bus.read_range
        def fetch(url,payload):
            reply=gateway(BOARD)(payload['start'],payload['count'])
            reply['rows']=[[]]+reply['rows']
            return json.dumps(reply)
        with patch.object(bus,'fetch',side_effect=fetch):
            outcome=capture_main(lambda:bv.main([]),reader=lambda _,f,c: real_reader(self.ENV,f,c))
        self.assertEqual(1,len(outcome['receipts']),outcome)
        self.assertEqual(bv.UNKNOWN,outcome['receipts'][0]['verdict'])
        self.assertFalse(outcome['receipts'][0]['gate_eligible'])

    def test_real_filtered_reader_rejects_null_and_malformed_rows(self):
        for rows in (None, [None], [[]], ['junk'], [{'error':'unavailable'}]):
            with self.subTest(rows=rows), patch.object(bus,'fetch',return_value=json.dumps({'rows':rows,'filtered':0,'total':0})):
                with self.assertRaises(SystemExit):
                    bus.read_rows(self.ENV,limit=1)

    def test_real_range_reader_clean_pipeline_is_eligible(self):
        real_reader=bus.read_range
        with patch.object(bus,'fetch',side_effect=lambda url,payload:json.dumps(gateway(BOARD)(payload['start'],payload['count']))):
            outcome=capture_main(lambda:bv.main([]),reader=lambda _,f,c: real_reader(self.ENV,f,c))
        self.assertEqual(1,len(outcome['receipts']),outcome)
        self.assertTrue(outcome['receipts'][0]['gate_eligible'])

    def test_real_range_reader_empty_board_is_no_sample(self):
        real_reader=bus.read_range
        with patch.object(bus,'fetch',return_value=json.dumps({'rows':[],'total':0,'count':0,'start':1})):
            outcome=capture_main(lambda:bv.main([]),reader=lambda _,f,c: real_reader(self.ENV,f,c))
        self.assertEqual(1,len(outcome['receipts']),outcome)
        self.assertEqual(bv.NO_SAMPLE,outcome['receipts'][0]['verdict'])
        self.assertFalse(outcome['receipts'][0]['gate_eligible'])
