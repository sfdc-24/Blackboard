"""Builder regressions: receipt-gated ACKs and durable, fail-closed deduplication.
These are offline fake-worker tests, not actual worker acceptance or a browser test.
"""
import datetime
import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'scripts'), str(Path(__file__).parent)]
import call_bridge as cb
import fleet_doorbell as fd
import wake_ack as wa
import test_call_bridge as t
ASK='call-0123456789ab'

def row(ask=ASK, sender='bus-reconciler', agent='grok'):
    s=cb.wake_row(ask,agent,'workshop',t.NOW)
    return [s['row_id'],t.NOW.isoformat(),sender,agent,'ASK',s['payload']]

def accepted(ask=ASK):
    return {'task_id':ask,'agent':'grok','state':'accepted','receipt_id':'turn-started-fixture'}

def process_ack(path, output):
    def post(spec):
        with open(output,'a') as f: f.write(spec['row_id']+'\n')
    wa.Acker('grok',{'grok'},post,path=path,log=lambda s:None)(row(),accepted())

class Ack(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'acked.json';self.posts=[]
    def acker(self,post=None,path=True):
        return wa.Acker('grok',{'grok'},post or self.posts.append,path=self.path if path else None,
                        clock=lambda:t.NOW,log=lambda s:None)
    def test_passive_ring_never_acks(self):
        self.assertIsNone(self.acker()(row()));self.assertFalse(self.posts);self.assertFalse(self.path.exists())
    def test_correlated_acceptance_acks(self):
        s=self.acker()(row(),accepted());self.assertIn('|event=worker.accepted|task_id='+ASK,s['payload'])
        self.assertIn('|receipt_id=turn-started-fixture|',s['payload']);self.assertNotIn('working on it now',s['payload'])
    def test_mismatched_acceptance_rejected(self):
        for field,value in [('task_id','call-ffffffffffff'),('agent','gemini'),('state','dispatched'),('receipt_id','x|from=evil')]:
            self.assertIsNone(self.acker()(row(),dict(accepted(),**{field:value})))
        self.assertFalse(self.posts)
    def test_wrong_ring_sender_or_target_rejected(self):
        for r in [row(sender='grok'),row(agent='gemini')]: self.assertIsNone(self.acker()(r,accepted()))
        self.assertFalse(self.posts)
    def test_replay_restart_and_r2_not_retried(self):
        self.assertIsNotNone(self.acker()(row(),accepted()))
        r=row();r[0]+='-R2';r[5]=r[5].replace('|id='+cb.WAKE_PREFIX+ASK+'|','|id='+cb.WAKE_PREFIX+ASK+'-R2|')
        self.assertIsNone(self.acker()(row(),accepted()));self.assertIsNone(self.acker()(r,accepted()))
        self.assertEqual(1,len(self.posts))
    def test_uncertain_append_quarantined(self):
        calls=[]
        def fail(s):calls.append(s);raise OSError('unknown')
        self.acker(post=fail)(row(),accepted());self.acker()(row(),accepted())
        self.assertEqual(1,len(calls));self.assertFalse(self.posts)
    def test_corrupt_state_fails_closed(self):
        for raw in ['{', '{}','[42]','["not-an-ask"]']:
            self.path.write_text(raw);self.assertIsNone(self.acker()(row(),accepted()))
        self.assertFalse(self.posts)
    def test_unreadable_state_fails_closed(self):
        self.path.mkdir();self.assertIsNone(self.acker()(row(),accepted()));self.assertFalse(self.posts)
    def test_write_failure_never_posts(self):
        with mock.patch.object(wa.os,'replace',side_effect=OSError('disk')):
            self.assertIsNone(self.acker()(row(),accepted()))
        self.assertFalse(self.posts)
    def test_missing_durable_path_never_posts(self):
        self.assertIsNone(self.acker(path=False)(row(),accepted()));self.assertFalse(self.posts)
    def test_abandoned_lock_fails_closed(self):
        self.path.with_name(self.path.name+'.lock').mkdir()
        self.assertIsNone(self.acker()(row(),accepted()));self.assertFalse(self.posts)
    def test_concurrent_processes_only_one_append(self):
        output=str(Path(self.temp.name)/'posts');ctx=multiprocessing.get_context('spawn')
        ps=[ctx.Process(target=process_ack,args=(str(self.path),output)) for _ in range(4)]
        for p in ps:p.start()
        for p in ps:p.join(10);self.assertEqual(0,p.exitcode)
        self.assertEqual(1,len(Path(output).read_text().splitlines()))
    def test_no_pruning_prior_acceptance(self):
        old=['call-%012x'%i for i in range(700)];self.path.write_text(json.dumps(old))
        self.acker()(row(),accepted());self.assertTrue(set(old)<=set(json.loads(self.path.read_text())))
    def test_private_words_do_not_enter_ack(self):
        r=row();r[5]+='|private=secret client bank';s=self.acker()(r,accepted())
        for w in ['secret','client','bank']:self.assertNotIn(w,s['payload'])
    def test_doorbell_never_calls_supplied_acker(self):
        bm=Path(self.temp.name)/'bm';bm.write_text((t.NOW-datetime.timedelta(seconds=30)).isoformat())
        ack=mock.Mock();out=[]
        self.assertEqual(1,fd.check(lambda since:[row()],'grok',path=bm,out=out.append,ack=ack))
        ack.assert_not_called();self.assertTrue(out[-1].startswith('RING '));self.assertIsNone(fd.acker_for('grok'))

if __name__=='__main__':unittest.main()
