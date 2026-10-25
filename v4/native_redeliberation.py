"""Durable, scoped original-role reconsideration without changing finding routes."""
from copy import copy, deepcopy
from types import SimpleNamespace
from uuid import uuid4

from .agent_queue import submit_items_in_state
from .journal import RunJournal
from .schema import Claim, claim_index, descendants, digest
from .atomic_phase6 import build_atomic_tasks


class NativeResolutionJournal(RunJournal):
    """Expose received proof inside a queue transaction without writing checkpoints."""
    def checkpoint(self, runner):
        pass


class NativeRedeliberation:
    @staticmethod
    def _redeliberation_view(packet):
        manual = packet['manual_only']
        return dict(kind='role_redeliberation',
            status='blocked:revision_limit' if manual else 'role_redeliberation',
            blocked=manual, manual_only=manual,
            next_action='submit-redeliberation', packet_id=packet['packet_id'],
            input_hash=packet['input_hash'], allowed_claim_ids=list(packet['target_claim_ids']),
            finding_ids=[f['finding_id'] for f in packet['findings']],
            items=deepcopy(packet['items']))

    def _prepare_redeliberation(self, state, runner, findings):
        wanted = [f for f in findings if f.get('resolution_route') == 'role_redeliberation']
        if not wanted:
            return None
        known = {f.get('finding_id') for f in wanted}
        wanted.extend(deepcopy(f) for f in runner.deferred_global_findings
                      if f.get('resolution_route') == 'role_redeliberation'
                      and f.get('finding_id') not in known)
        runner._require_atomic()
        index = claim_index(runner.run.claims)
        first_ids = runner._finding_claim_ids(wanted[0])
        if not first_ids or any(cid not in index for cid in first_ids):
            raise ValueError('role reconsideration requires explicit current Claim IDs')
        reviewed = {cid for record in runner._role_redeliberation_candidates(wanted)
                    for cid in record['claim_ids']}
        unreviewed = [cid for finding in wanted for cid in runner._finding_claim_ids(finding)
                      if cid not in reviewed and cid in index]
        role = index[(unreviewed or first_ids)[0]]['author_role']
        selected, ids = [], []
        for finding in wanted:
            targets = runner._finding_claim_ids(finding)
            if not finding.get('finding_id') or not targets or any(cid not in index for cid in targets):
                raise ValueError('role reconsideration requires stable findings and current targets')
            local = [cid for cid in targets if index[cid]['author_role'] == role]
            if local:
                selected.append(deepcopy(finding))
                ids.extend(cid for cid in local if cid not in ids)
        binding = dict(input_hash=runner._hash(), history_hash=digest(runner.history),
                       verification_epoch=runner._verification_epoch,
                       role=role, target_claim_ids=ids, findings_hash=digest(selected),
                       finding_ids=[f['finding_id'] for f in selected])
        active = state['packets'].get(state.get('active_packet_id'))
        if (active and not active.get('retired') and active.get('kind') == 'role_redeliberation'
                and active.get('binding') == binding):
            return self._redeliberation_view(active)
        from .runner import json_prompt, ROLE_CONTRACT
        tasks = runner._tasks()
        prompt = json_prompt(
            '你是原作者角色，針對所列 finding 重新裁決指定 Claim，不是替審查者照抄修改。'
            '只回指定 target_claim_ids 的完整 Claim objects，保留原 ID 與 author_role；不要重生其他章節。'
            '重新檢視來源、因果、期間及反證，修訂不成立的結論；若維持原結論，必須提供理由。'
            '回 JSON object：claims（每個 target 恰一個完整物件）、reason（重新裁決理由）。'
            '不得刪除真正依賴來掩蓋衝突；本回覆不會自行結案，仍須重新語義驗證及 Global。\n' + ROLE_CONTRACT,
            dict(role=role, target_claim_ids=ids,
                 findings=[{k: deepcopy(v) for k, v in finding.items()
                            if k not in {'resolution_baseline', 'resolution_state_hash'}}
                           for finding in selected],
                 tasks=[tasks[cid].review_dict() for cid in ids]), compact=True)
        packet_id = uuid4().hex
        state['packets'][packet_id] = dict(packet_id=packet_id, kind='role_redeliberation',
            input_hash=state['input_hash'], binding=binding, target_claim_ids=ids,
            findings=selected, manual_only=runner.revision_round >= runner.max_revisions,
            items={role: dict(prompt=prompt)}, received={})
        state['active_packet_id'] = packet_id
        self._save(state, runner)
        return self._redeliberation_view(state['packets'][packet_id])

    def submit_redeliberation(self, packet_id, responses, executor, input_hash):
        with self.queue.transaction() as state:
            runner = self._load(state)
            packet = state['packets'].get(packet_id)
            if not packet or packet.get('kind') != 'role_redeliberation':
                raise ValueError('unknown original-role reconsideration packet')
            role = packet['binding']['role']
            binding = packet['binding']
            if not isinstance(responses, dict) or set(responses) != {role}:
                raise ValueError('reconsideration response must use the original role item ID')
            raw = responses[role]
            journal = runner.journal
            context = dict(redeliberation=deepcopy(binding))
            accepted = state.get('redeliberation_outcomes', {}).get(packet_id)
            if accepted is not None:
                context = deepcopy(packet['accepted_context'])
                row = packet.get('received', {}).get(role, {})
                if (packet['input_hash'] != input_hash
                        or packet['accepted_after_hash'] != state['input_hash']
                        or packet['accepted_history_hash'] != digest(runner.history)
                        or binding['verification_epoch'] != runner._verification_epoch):
                    raise ValueError('stale accepted original-role reconsideration')
                if (row.get('executor') != executor or row.get('response') != raw
                        or journal.response_provenance(runner, SimpleNamespace(identity=executor),
                            role, packet['items'][role]['prompt'], raw, review_context=context) is None):
                    raise ValueError('conflicting or unproved reconsideration redelivery')
                return {**deepcopy(accepted), 'idempotent': True}
            if (input_hash != state['input_hash'] or packet['input_hash'] != input_hash
                    or binding['verification_epoch'] != runner._verification_epoch
                    or binding['history_hash'] != digest(runner.history)):
                raise ValueError('stale original-role reconsideration')
            if runner.pending_patch is not None:
                raise ValueError('finish pending Claim repairs before original-role reconsideration')
            runner._require_atomic()
            if not isinstance(raw, dict) or not {'claims', 'reason'} <= raw.keys():
                raise ValueError('reconsideration requires claims and reason')
            if not isinstance(raw['reason'], str) or not raw['reason'].strip():
                raise ValueError('reconsideration requires the original author reasoning')
            if not isinstance(raw['claims'], list):
                raise ValueError('reconsideration claims must be an array')
            claims = [Claim.from_dict(c).to_dict() for c in raw['claims']]
            ids = [c['claim_id'] for c in claims]
            if (len(set(ids)) != len(ids) or set(ids) != set(packet['target_claim_ids'])
                    or any(c['author_role'] != role for c in claims)):
                raise ValueError('reconsideration must preserve original author and exact target scope')
            shadow_orch = copy(runner.orch)
            shadow_orch.runs = {runner.run_id: deepcopy(runner.run)}
            shadow_orch.patch(runner.run_id, claims, packet['target_claim_ids'])
            context['redeliberation']['accepted_task_hashes'] = {
                task.claim_id: digest(task.to_dict())
                for task in build_atomic_tasks(shadow_orch.runs[runner.run_id]) if task.claim_id in ids}
            submit_items_in_state(state, packet_id, responses, executor, input_hash)
            proof = journal.archive_external_submission(runner, model=executor, task_id=role,
                prompt=packet['items'][role]['prompt'], response=deepcopy(raw), review_context=context)
            current = claim_index(runner.run.claims)
            changed = any(c != current[c['claim_id']] for c in claims)
            findings = deepcopy(packet['findings'])
            if changed:
                runner.pending_patch = dict(input_hash=runner._hash(),
                    ids=list(packet['target_claim_ids']), findings=findings)
                outcome = runner.collect_phase6_revision(claims, enforce_budget=False)
            else:
                affected = descendants(runner.run.claims, ids)
                existing = {f['finding_id']: deepcopy(f) for f in runner.deferred_global_findings}
                existing.update({f['finding_id']: f for f in findings})
                runner.deferred_global_findings = list(existing.values())
                for cid in affected:
                    runner.receipts.pop(cid, None)
                    runner.deep_receipts.pop(cid, None)
                runner.global_receipt = runner.pending_global = runner.global_review_progress = None
                runner.revision_round += 1
                runner.status = runner.run.stage = 'atomic'
                outcome = dict(revalidate_claim_ids=affected,
                               **runner.prepare_phase6_atomic(enforce_budget=False))
            packet.update(retired=True, original_role_proof=proof, accepted_context=context,
                          accepted_after_hash=runner._hash(), accepted_history_hash=digest(runner.history))
            state['active_packet_id'] = None
            state.setdefault('redeliberation_outcomes', {})[packet_id] = deepcopy(outcome)
            self._save(state, runner)
            return outcome
