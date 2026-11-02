"""Builder checks for source HOLD repairs. No network or live acceptance."""
import datetime,json,sys,unittest
from pathlib import Path
from unittest import mock
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts'),str(Path(__file__).parent)]
import test_call_bridge as t
import call_bridge as cb
import call_lifecycle as life

class Holds(unittest.TestCase):
    def setUp(self):
        self.c=t.FakeRedis();t.open_window(self.c);self.rings=[]
    def bridge(self,seconds=0,ring=None):
        return cb.Bridge(self.c,t.LOG,lambda:True,clock=lambda:t.NOW+datetime.timedelta(seconds=seconds),ring=ring or self.rings.append)
    def claim(self,ring=None):
        t.turn(self.c,'Gemini, build the example.',seconds_ago=10)
        return self.bridge(ring=ring).tick()[0]
    def wake(self,a):return json.loads(self.c.get(cb.WAKE_KEY%a))
    def test_initial_append_failure_durably_tracked_and_never_retried(self):
        calls=[]
        def fail(s):calls.append(s);raise OSError('uncertain')
        a=self.claim(fail);self.assertEqual('dispatch_unknown',self.wake(a)['state'])
        for sec in [100,200,1000]:self.bridge(sec).tick()
        self.assertEqual(1,len(calls));self.assertFalse(self.rings)
        card=t.card(self.c,'gemini',a);self.assertEqual('wake delivery unconfirmed',card['activity'])
        self.assertFalse(cb.owns(a,card,a))
    def test_crash_after_reservation_cannot_lose_tracking_or_retry(self):
        original=self.c.set
        def crash(key,*a,**kw):
            if key.startswith('gov:callbridge:rung:'):raise RuntimeError('crash')
            return original(key,*a,**kw)
        with mock.patch.object(self.c,'set',side_effect=crash):
            with self.assertRaises(RuntimeError):self.claim()
        a=next(iter(self.c.hgetall(cb.OPEN_KEY)))
        self.assertEqual('dispatch_unknown',self.wake(a)['state']);self.bridge(100).tick();self.assertFalse(self.rings)
    def test_ack_payload_cannot_impersonate_actual_sender(self):
        a=self.claim();p='phase=ACK|from=gemini|event=worker.accepted|task_id='+a+'|receipt_id=x|answers='+cb.WAKE_PREFIX+a
        self.assertEqual([],self.bridge().acks([t.row('grok',p)],t.NOW));self.assertEqual('rung',self.wake(a)['state'])
        self.assertEqual([a],self.bridge().acks([t.row('gemini',p)],t.NOW))
    def test_passive_ack_without_consumer_receipt_rejected(self):
        a=self.claim();p='phase=ACK|from=gemini|answers='+cb.WAKE_PREFIX+a
        self.assertEqual([],self.bridge().acks([t.row('gemini',p)],t.NOW));self.assertEqual('rung',self.wake(a)['state'])
    def test_conflicting_declared_sender_rejected(self):
        a=self.claim();p='phase=RESULT|from=grok|answers='+a+'|text=done'
        self.assertEqual([],self.bridge().settle([t.row('gemini',p)]))
    def test_uncertain_second_ring_never_retried(self):
        a=self.claim();calls=[]
        def fail(s):calls.append(s);raise OSError('uncertain')
        self.bridge(100,fail).tick();self.assertEqual('dispatch_unknown',self.wake(a)['state'])
        self.bridge(1000).tick();self.assertEqual(1,len(calls));self.assertEqual(1,len(self.rings))
    def test_cancel_clears_pending_and_refusal_fallback(self):
        for phrase in ['cancel that','never mind','stop','Gemini, do not do that']:
            self.c=t.FakeRedis();t.open_window(self.c)
            t.turn(self.c,'Gemini, here is the flow.',seconds_ago=40)
            t.turn(self.c,phrase,reason='continuity',seconds_ago=30)
            t.turn(self.c,'do it',reason='continuity',seconds_ago=10)
            self.assertEqual([],self.bridge().tick(),phrase)
            self.assertIsNone(self.bridge()._pending(t.ROOM));self.assertEqual('null',self.c.get(cb.ASKED_KEY%t.ROOM))
    def test_stop_word_in_positive_task_is_not_cancellation(self):
        self.assertFalse(cb.cancellation({'kind':'turn.started','actor_id':'owner','utterance':'Build a stop button'}))
    def test_stable_identity_is_in_inbox(self):
        a=self.claim();fields=self.c.streams['fleet:inbox:gemini'][0][1]
        self.assertEqual((a,a,t.ROOM,'workshop'),tuple(fields[k] for k in ['task_id','ask','conference_room','canvas_room']))
    def test_quarantined_queue_does_not_starve_later_claims(self):
        for i in range(cb.RING_MAX+1):
            a='call-%012x'%i;v={'agent':'gemini','room':'workshop','claimed_at':cb.stamp(t.NOW)}
            self.c.hset(cb.RING_KEY,a,json.dumps(v));self.c.set(cb.WAKE_KEY%a,json.dumps(dict(v,state='dispatch_unknown',rung_at=cb.stamp(t.NOW))))
        a='call-ffffffffffff';self.c.hset(cb.RING_KEY,a,json.dumps(v))
        self.assertEqual([a],self.bridge().ring_pending());self.assertEqual(1,len(self.rings))

    def test_unknown_claim_exec_keeps_prepared_and_never_replays_delivery(self):
        """Lose EXEC's return after its effects: PREPARED is not accepted/open."""
        t.turn(self.c, 'Gemini, build the example.', seconds_ago=10)
        original = t.FakePipe.execute
        lost = []
        def unknown(pipe, *args, **kwargs):
            result = original(pipe, *args, **kwargs)
            if not lost:
                lost.append(True)
                return None
            return result
        with mock.patch.object(t.FakePipe, 'execute', unknown):
            self.assertEqual([], self.bridge().tick())
        keys = [k for k in self.c.store if k.startswith('gov:callbridge:task:')]
        self.assertEqual(1, len(keys))
        info = life.record(self.c.get(keys[0]))
        self.assertEqual('PREPARED', info['state'])
        self.assertEqual({}, self.c.hgetall(cb.OPEN_KEY))
        self.assertEqual(1, len(self.c.streams['fleet:inbox:gemini']))
        self.c.delete(cb.CURSOR_KEY % t.ROOM)
        self.assertEqual([], self.bridge().tick())
        self.assertEqual(1, len(self.c.streams['fleet:inbox:gemini']))
        self.assertEqual('PREPARED', life.get(self.c, info['ask'])['state'])

    def test_payload_only_owner_without_protected_binding_cannot_claim(self):
        t.turn(self.c, 'Gemini, build the example.', actor='owner-1')
        self.assertEqual([], self.bridge().tick())
        self.assertNotIn('fleet:inbox:gemini', self.c.streams)
        self.assertFalse(any(k.startswith('gov:callbridge:task:') for k in self.c.store))

    def test_missing_current_epoch_is_not_accepted_source(self):
        t.turn(self.c, 'Gemini, build the example.')
        self.c.delete('v1:conf:%s:state' % t.ROOM)
        self.assertEqual([], self.bridge().tick())
        self.assertNotIn('fleet:inbox:gemini', self.c.streams)


class TransactionFixtureContract(unittest.TestCase):
    def test_watched_reads_are_immediate_and_changed_key_aborts_all_writes(self):
        c = t.FakeRedis(); c.set('a', 'before')
        pipe = c.pipeline(); pipe.watch('a')
        self.assertEqual('before', pipe.get('a'))
        pipe.multi(); pipe.set('b', 'must-not-exist')
        c.set('a', 'after')
        with self.assertRaises(t.WatchConflict): pipe.execute(raise_on_error=False)
        self.assertIsNone(c.get('b'))

    def test_runtime_exec_error_does_not_roll_back_other_commands(self):
        c = t.FakeRedis(); c.set('wrong-type', 'string')
        pipe = c.pipeline(); pipe.multi()
        pipe.set('first', 'committed'); pipe.hset('wrong-type', 'x', 'bad'); pipe.set('last', 'committed')
        results = pipe.execute(raise_on_error=False)
        self.assertEqual(3, len(results)); self.assertIsInstance(results[1], TypeError)
        self.assertEqual('committed', c.get('first')); self.assertEqual('committed', c.get('last'))
        self.assertEqual('string', c.get('wrong-type'))

if __name__=='__main__':unittest.main()
