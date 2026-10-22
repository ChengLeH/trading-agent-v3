"""Real native queue/checkpoint/archive IO, with an independent Claim-cone oracle."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from v4.agent_reviews import NativeReviewWorkflow
from v4.checkpoint import load_checkpoint, save_checkpoint
from v4.test_agent_reviews import response
from v4.test_integration import global_fixture, seeded


TARGETS = ('a', 'b', 'c')
NOTES = st.sampled_from(('omitted', 'empty', 'one', 'two'))


def candidate(cid, item, notes):
    row = response(cid, item)
    if notes != 'omitted':
        row['clarity_notes'] = [dict(issue=f'wording {i}', reason='Meaning and decision remain unchanged')
                                for i in range({'empty': 0, 'one': 1, 'two': 2}[notes])]
    return row


class NativeLifecycle(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        runner = seeded()
        # Three mutable nodes; the remaining eight minimal role Claims are
        # required by the real native role-completion guard and stay immutable.
        runner.run.claims = [c for c in runner.run.claims if c['claim_id'] != 'independent']
        runner.max_revisions = 1000  # exploration bounds operations, not repair authority
        self.claims = {c['claim_id']: deepcopy(c) for c in runner.run.claims}
        self.epoch = runner._verification_epoch
        self.accepted = {}
        self.results = {}
        self.sent = []
        self.revision = 0
        source = self.root / 'initial.json'
        save_checkpoint(runner, source)
        self.workflow = NativeReviewWorkflow.from_checkpoint(source, self.root / 'native')
        # Establish real archived receipts before exploring invalidation/reload.
        for _ in range(3):
            packet = self.workflow.next()
            if packet.get('kind') != 'atomic':
                break
            rows = {cid: candidate(cid, item, 'omitted') for cid, item in packet['items'].items()}
            self.workflow.submit(packet['packet_id'], rows, 'host/property', packet['input_hash'])
            self._record_submission(packet, rows)
            if set(self.accepted) == set(self.claims):
                break
        assert set(self.accepted) == set(self.claims)

    def _record_submission(self, packet, rows):
        """Independent receipt oracle, shared by bootstrap and live reviews."""
        for cid in rows:
            self.accepted[cid] = self.fingerprint(cid)
            self.results[cid] = deepcopy(rows[cid])
        self.sent.append((deepcopy(packet), deepcopy(rows)))

    def fingerprint(self, cid):
        # This reference model does not call production task/hash/descendant helpers.
        cone = {}

        def visit(key):
            if key in cone:
                return
            cone[key] = self.claims[key]
            for parent in self.claims[key]['dependencies']:
                visit(parent)

        visit(cid)
        wire = json.dumps([self.epoch, cone], sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(wire).hexdigest()

    def snapshot(self):
        with self.workflow.queue.transaction() as state:
            runner = self.workflow._load(state)
            return (deepcopy(runner.run.claims), deepcopy(runner.receipts), runner._verification_epoch)

    @rule()
    def reload(self):
        before = self.snapshot()
        self.workflow = NativeReviewWorkflow(self.workflow.directory)
        assert self.snapshot() == before

    @rule(data=st.data(), notes=NOTES)
    def submit_pending_review(self, data, notes):
        if self.all_verified():
            return
        packet = self.workflow.next()
        if packet.get('kind') != 'atomic':
            return
        cid = data.draw(st.sampled_from(sorted(packet['items'])), label='reviewed Claim')
        selected = list(packet['items']) if data.draw(st.booleans(), label='whole packet') else [cid]
        rows = {key: candidate(key, packet['items'][key], notes) for key in selected}
        outcome = self.workflow.submit(packet['packet_id'], rows, 'host/property', packet['input_hash'])
        assert not outcome.get('blocked'), outcome
        self._record_submission(packet, rows)

    def all_verified(self):
        return all(self.accepted.get(cid) == self.fingerprint(cid) for cid in self.claims)

    @precondition(lambda self: self.all_verified())
    @rule(cid=st.sampled_from(TARGETS), add_edge=st.booleans())
    def patch_claim(self, cid, add_edge):
        self.revision += 1
        change = dict(claim_id=cid, text=f'Explicitly revised premise {self.revision}')
        if cid == 'b':
            # Exercise dependency-only changes too, always preserving an acyclic graph.
            change = dict(claim_id=cid, dependencies=['a', 'role.market_analyst'] if add_edge else ['a'])
        with self.workflow.queue.transaction() as state:
            runner = self.workflow._load(state)
            prepared = runner.prepare_phase6_revision_v4([
                dict(claim_id=cid, issue='Scripted targeted repair', suggested_fix='Revise this Claim only')])
            state['patch_prompt'] = prepared['revision_prompts'][0][1]
            self.workflow._save(state, runner)
        packet = self.workflow.next()
        assert packet.get('kind') == 'patch', packet
        outcome = self.workflow.submit_patch(packet['packet_id'], [change], 'host/property', packet['input_hash'])
        if all(self.claims[cid].get(k) == v for k, v in change.items() if k != 'claim_id'):
            assert outcome.get('status') == 'blocked:revision_no_changes', outcome
            # Finish this explicit repair rather than silently erasing a pending request.
            packet = self.workflow.next()
            change = dict(claim_id=cid, text=f'Actual targeted repair {self.revision}')
            outcome = self.workflow.submit_patch(packet['packet_id'], [change], 'host/property', packet['input_hash'])
        assert not outcome.get('blocked'), outcome
        self.claims[cid].update({k: deepcopy(v) for k, v in change.items() if k != 'claim_id'})

    @rule()
    def fresh_epoch_from_disk(self):
        previous_epoch = self.epoch
        with self.workflow.queue.transaction() as state:
            runner = load_checkpoint(self.workflow.checkpoint_path, continue_verification=False)
            assert runner._verification_epoch != previous_epoch
            assert not runner.receipts and not runner.deep_receipts
            self.workflow._save(state, runner)
            self.epoch = runner._verification_epoch
        self.workflow = NativeReviewWorkflow(self.workflow.directory)

    @precondition(lambda self: self.all_verified())
    @rule()
    def global_finding_survives_reload_and_routes_to_real_patch(self):
        packet = self.workflow.next()
        assert packet.get('kind') == 'global', packet
        finding_id = f'property.global.{self.revision}'
        finding = dict(finding_id=finding_id, claim_id='a', issue='Scripted premise requires repair',
                       suggested_fix='Revise the premise', resolution_route='claim_patch')
        result = global_fixture(list(self.claims.values()), [finding])
        outcome = self.workflow.submit(packet['packet_id'],
            {next(iter(packet['items'])): result}, 'host/global-property', packet['input_hash'])
        assert outcome.get('status') == 'revision', outcome
        self.reload()
        patch_packet = self.workflow.next()
        assert patch_packet.get('kind') == 'patch', patch_packet
        self.revision += 1
        text = f'Global premise repair {self.revision}'
        outcome = self.workflow.submit_patch(patch_packet['packet_id'],
            [dict(claim_id='a', text=text)], 'host/property', patch_packet['input_hash'])
        assert not outcome.get('blocked'), outcome
        self.claims['a']['text'] = text
        with self.workflow.queue.transaction() as state:
            runner = self.workflow._load(state)
            assert finding_id not in {f.get('finding_id') for f in runner.deferred_global_findings}
            assert runner.global_receipt is None

    @rule(data=st.data(), stale_hash=st.booleans())
    def resubmit_old_packet(self, data, stale_hash):
        packet, rows = data.draw(st.sampled_from(self.sent), label='historical packet')
        before = self.workflow.queue.state_path.read_bytes()
        fingerprint = packet['input_hash'] + '.stale' if stale_hash else packet['input_hash']
        try:
            self.workflow.submit(packet['packet_id'], rows, 'host/property', fingerprint)
        except ValueError:
            assert self.workflow.queue.state_path.read_bytes() == before
        else:
            assert not stale_hash, 'Stale input_hash was accepted'

    @precondition(lambda self: not self.all_verified())
    @rule(data=st.data())
    def stale_hash_on_current_unretired_packet(self, data):
        packet = self.workflow.next()
        assert packet.get('kind') == 'atomic', packet
        cid = data.draw(st.sampled_from(sorted(packet['items'])), label='stale-hash Claim')
        before = self.workflow.queue.state_path.read_bytes()
        try:
            self.workflow.submit(packet['packet_id'],
                {cid: candidate(cid, packet['items'][cid], 'one')},
                'host/property', packet['input_hash'] + '.stale')
        except ValueError as exc:
            assert 'stale packet input version' in str(exc), str(exc)
        else:
            raise AssertionError('An active packet accepted a stale input_hash')
        assert self.workflow.queue.state_path.read_bytes() == before

    @invariant()
    def receipts_match_current_claim_cone_and_epoch_after_real_reload(self):
        claims, receipts, epoch = self.snapshot()
        assert {c['claim_id']: c for c in claims} == self.claims
        assert epoch == self.epoch
        expected = {cid for cid, token in self.accepted.items() if token == self.fingerprint(cid)}
        assert set(receipts) == expected, (set(receipts), expected)
        for cid, receipt in receipts.items():
            assert receipt['result'] == self.results[cid]
            assert receipt['result']['claim_id'] == cid
            assert receipt['result']['verdict'] == 'PASS'
            assert receipt['model'] == 'host/property'

    def teardown(self):
        self.temp.cleanup()


TestNativeLifecycle = NativeLifecycle.TestCase
