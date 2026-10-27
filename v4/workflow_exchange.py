"""Durable, idempotent submission followed by optional next-work delivery."""
from copy import deepcopy
from .schema import digest


def submit_and_next(workflow, packet_id, responses, executor, input_hash):
    identity = dict(packet_id=packet_id, responses=responses, executor=executor, input_hash=input_hash)
    key = digest(identity)
    with workflow.queue.transaction() as state:
        saved = state.get('exchange_outcomes', {}).get(key)
        if saved is not None:
            if saved['state_hash'] != state['input_hash']:
                raise ValueError('stale submission exchange; use next for current work')
            outcome = deepcopy(saved['outcome'])
        else:
            outcome = None
            packet = state.get('packets', {}).get(packet_id, {})
            received = packet.get('received', {})
            if packet.get('input_hash') == input_hash and isinstance(responses, dict) and responses:
                exact = all(item in packet.get('items', {}) and
                            received.get(item, {}).get('executor') == executor and
                            received.get(item, {}).get('response') == answer
                            for item, answer in responses.items())
                if exact:
                    # A crash may occur after raw receipt publication, before
                    # our convenience outcome. Recover receipt, never a verdict;
                    # next still runs ordinary archive/epoch/semantic checks.
                    outcome = dict(status='submission_recovered', recovered_from_received=True,
                                   verification_status='requires_current_workflow_validation')
    if outcome is None:
        # Commit before attempting delivery. A lost reply must not lose proof.
        outcome = workflow.submit(packet_id, responses, executor, input_hash)
        with workflow.queue.transaction() as state:
            state.setdefault('exchange_outcomes', {})[key] = dict(
                state_hash=state['input_hash'], outcome=deepcopy(outcome))
    result = dict(status=outcome.get('status', 'submitted'),
                  blocked=bool(outcome.get('blocked')), submission_saved=True,
                  submission=outcome)
    if outcome.get('blocked') and not outcome.get('needs_retry'):
        return result
    try:
        result['next'] = workflow.next()
        result['blocked'] = bool(result['next'].get('blocked'))
    except (ValueError, OSError, RuntimeError) as exc:
        result.update(blocked=True, next_error=dict(type=type(exc).__name__, reason=str(exc)),
                      next_action='retry_next_without_resubmitting')
    return result
