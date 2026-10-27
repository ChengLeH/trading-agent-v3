"""Opt-in, lossless content-addressed delivery of native task packets.

The manifest is a transport encoding only. Canonical prompts and review hashes
remain those of the original packet. All reads authenticate content by SHA-256.
"""
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

VERSION = 'v4-task-delivery/1'
_CATALOGS = ('facts', 'calcs', 'evidence', 'events', 'evidence_catalog',
             'dependency_catalog', 'binding_dimension_catalog', 'context_value_catalog', 'resolved_refs')
_OPEN = '\n\n<context-data>\n'
_CLOSE = '\n</context-data>'


def _wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _put(root, data):
    h = _hash(data)
    path = root / 'objects' / h
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError('corrupt content-addressed object: ' + h)
    else:
        with NamedTemporaryFile(dir=path.parent, delete=False) as f:
            tmp = Path(f.name)
            try:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        try:
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    return h


def _get(root, h):
    if not isinstance(h, str) or len(h) != 64 or any(c not in '0123456789abcdef' for c in h):
        raise ValueError('invalid object hash')
    data = (root / 'objects' / h).read_bytes()
    if _hash(data) != h:
        raise ValueError('corrupt content-addressed object: ' + h)
    return data


def _store_json(root, value):
    return _put(root, _wire(value))


def _load_json(root, h):
    return json.loads(_get(root, h))


def _tree_store(root, value, *, branch=True):
    # Split only structural inventories and task lists; each source stays whole.
    if branch and isinstance(value, dict):
        return {'map': [[key, _tree_store(root, child, branch=(key == 'state' or key in _CATALOGS or key in ('source_lineage', 'tasks', 'claims')))]
                        for key, child in value.items()]}
    if branch and isinstance(value, list):
        return {'list': [_tree_store(root, child, branch=False) for child in value]}
    return {'object': _store_json(root, value)}


def _tree_load(root, node):
    if 'map' in node:
        return {key: _tree_load(root, child) for key, child in node['map']}
    if 'list' in node:
        return [_tree_load(root, child) for child in node['list']]
    return _load_json(root, node['object'])


def _tree_at(node, path):
    for part in path:
        if 'map' in node:
            node = dict(node['map'])[part]
        elif 'list' in node and type(part) is int:
            node = node['list'][part]
        else:
            raise KeyError(part)
    return node


def _encode_prompt(root, prompt):
    # Role prompts have a precisely delimited pretty-printed JSON context.
    # Only decompose when re-encoding is byte-identical; otherwise retain the
    # entire prompt as an opaque object (including all review prompts).
    if isinstance(prompt, str) and _OPEN in prompt:
        prefix, rest = prompt.split(_OPEN, 1)
        try:
            payload, end = json.JSONDecoder().raw_decode(rest)
            pretty = json.dumps(payload, ensure_ascii=False, indent=2)
            compact = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
            if isinstance(payload, dict) and rest[end:].startswith(_CLOSE) and rest[:end] in (pretty, compact):
                formatting = 'pretty' if rest[:end] == pretty else 'compact'
                fields = []
                for key, value in payload.items():
                    if key == 'state' and isinstance(value, dict):
                        fields.append([key, {'tree': _tree_store(root, value)}])
                    elif key in _CATALOGS and isinstance(value, dict):
                        entries = [[k, _store_json(root, v)] for k, v in value.items()]
                        fields.append([key, {'entries': entries}])
                    elif key == 'claims' and isinstance(value, list):
                        fields.append([key, {'claims': [_store_json(root, v) for v in value]}])
                    else:
                        fields.append([key, {'object': _store_json(root, value)}])
                return {'type': 'role_context', 'prefix': _put(root, prefix.encode()),
                        'suffix': _put(root, rest[end + len(_CLOSE):].encode()), 'fields': fields, 'formatting': formatting,
                        'prompt_sha256': _hash(prompt.encode())}
        except (ValueError, TypeError):
            pass
    # Review prompts use fenced compact JSON; suffix can contain a schema.
    if isinstance(prompt, str) and '```json\n' in prompt:
        prefix, rest = prompt.split('```json\n', 1)
        try:
            payload, end = json.JSONDecoder().raw_decode(rest)
            encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
            if isinstance(payload, dict) and rest[end:].startswith('\n```') and encoded == rest[:end]:
                return {'type': 'review_context', 'prefix': _put(root, prefix.encode()),
                        'suffix': _put(root, rest[end + 4:].encode()),
                        'tree': _tree_store(root, payload),
                        'prompt_sha256': _hash(prompt.encode())}
        except (ValueError, TypeError):
            pass
    if not isinstance(prompt, str):
        raise ValueError('prompt must be a string')
    return {'type': 'opaque', 'object': _put(root, prompt.encode()), 'prompt_sha256': _hash(prompt.encode())}


def _decode_prompt(root, spec):
    if spec['type'] == 'opaque':
        raw = _get(root, spec['object'])
    elif spec['type'] == 'review_context':
        payload = _tree_load(root, spec['tree'])
        raw = (_get(root, spec['prefix']).decode() + '```json\n' +
               json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n```' +
               _get(root, spec['suffix']).decode()).encode()
    elif spec['type'] == 'role_context':
        payload = {}
        for key, value in spec['fields']:
            if 'tree' in value:
                payload[key] = _tree_load(root, value['tree'])
            elif 'entries' in value:
                payload[key] = {k: _load_json(root, h) for k, h in value['entries']}
            elif 'claims' in value:
                payload[key] = [_load_json(root, h) for h in value['claims']]
            else:
                payload[key] = _load_json(root, value['object'])
        raw = (_get(root, spec['prefix']).decode() + _OPEN +
               (json.dumps(payload, ensure_ascii=False, indent=2) if spec['formatting'] == 'pretty' else
                json.dumps(payload, ensure_ascii=False, separators=(',', ':'))) + _CLOSE +
               _get(root, spec['suffix']).decode()).encode()
    else:
        raise ValueError('unknown prompt encoding')
    if _hash(raw) != spec['prompt_sha256']:
        raise ValueError('materialized prompt hash mismatch')
    return raw.decode()


def create_delivery(directory, packet_view):
    """Store a packet and return a self-contained, authenticated manifest pointer."""
    root = Path(directory).resolve() / 'task-delivery'
    packet = dict(packet_view)
    has_items = 'items' in packet
    items = packet.pop('items', {})
    has_requests = 'requests' in packet
    requests = packet.pop('requests', [])
    specs = {}
    item_briefs = []
    for item_id, item in items.items():
        brief = {'item_id': item_id}
        if 'claim_ids' in item:
            brief['claim_ids'] = item['claim_ids']
        item_briefs.append(brief)
        remainder = dict(item)
        has_prompt = 'prompt' in remainder
        prompt = remainder.pop('prompt') if has_prompt else None
        specs[item_id] = {'item': _store_json(root, remainder),
                          'prompt': _encode_prompt(root, prompt) if has_prompt else None}
    request_specs = []
    request_briefs = []
    seen = set(specs)
    for item in requests:
        request_id = item['request_id']
        if request_id in seen:
            raise ValueError('duplicate task or request ID')
        seen.add(request_id)
        brief = {'request_id': request_id}
        if 'claim_ids' in item:
            brief['claim_ids'] = item['claim_ids']
        request_briefs.append(brief)
        remainder = dict(item)
        prompt = remainder.pop('prompt') if 'prompt' in remainder else None
        request_specs.append([request_id, {'item': _store_json(root, remainder),
                                           'prompt': _encode_prompt(root, prompt) if prompt is not None else None}])
    manifest = {'version': VERSION, 'packet': _store_json(root, packet), 'items': specs, 'has_items': has_items,
                'requests': request_specs, 'has_requests': has_requests,
                'task_brief': {'items': item_briefs, 'requests': request_briefs}}
    h = _store_json(root, manifest)
    return {'version': VERSION, 'manifest_file': str(root / 'objects' / h), 'manifest_sha256': h}


def _manifest(pointer):
    if pointer.get('version') != VERSION:
        raise ValueError('unsupported task delivery version')
    path = Path(pointer['manifest_file'])
    h = pointer['manifest_sha256']
    if path.name != h or path.parent.name != 'objects':
        raise ValueError('invalid manifest location')
    root = path.parent.parent
    manifest = _load_json(root, h)
    if manifest.get('version') != VERSION:
        raise ValueError('unsupported manifest version')
    return root, manifest


def materialize_delivery(pointer):
    """Restore the original packet exactly, including prompts and schemas."""
    root, manifest = _manifest(pointer)
    packet = _load_json(root, manifest['packet'])
    if manifest['has_items']:
        packet['items'] = {}
    for item_id, spec in manifest['items'].items():
        item = _load_json(root, spec['item'])
        if spec['prompt'] is not None:
            item['prompt'] = _decode_prompt(root, spec['prompt'])
        packet['items'][item_id] = item
    if manifest.get('has_requests'):
        packet['requests'] = []
        for _, spec in manifest['requests']:
            item = _load_json(root, spec['item'])
            if spec['prompt'] is not None:
                item['prompt'] = _decode_prompt(root, spec['prompt'])
            packet['requests'].append(item)
    return packet


def _item(manifest, item_id):
    if item_id in manifest['items']:
        return manifest['items'][item_id]
    return dict(manifest.get('requests', []))[item_id]


def query_delivery(pointer, item_id, kind, source_id):
    """Read one exact catalog ID or upstream claim ID; never infer matches."""
    root, manifest = _manifest(pointer)
    spec = _item(manifest, item_id)['prompt']
    if not spec or spec['type'] != 'role_context':
        raise ValueError('item has no indexed role context')
    fields = dict(spec['fields'])
    if kind == 'context':
        value = fields[source_id]
        if 'object' not in value:
            raise ValueError('context field is a catalog; query its exact ID')
        return _load_json(root, value['object'])
    if kind in _CATALOGS:
        entries = dict(fields[kind]['entries'])
        return _load_json(root, entries[source_id])
    if kind == 'claims':
        matches = [_load_json(root, h) for h in fields[kind]['claims']]
        matches = [claim for claim in matches if isinstance(claim, dict) and claim.get('claim_id') == source_id]
        if len(matches) != 1:
            raise KeyError(source_id)
        return matches[0]
    raise ValueError('unknown catalog kind')


def delivery_index(pointer, item_id):
    """List available exact IDs and context fields without loading catalogs."""
    root, manifest = _manifest(pointer)
    spec = _item(manifest, item_id)['prompt']
    if not spec or spec['type'] != 'role_context':
        raise ValueError('item has no indexed role context')
    fields = dict(spec['fields'])
    result = {kind: [key for key, _ in fields[kind]['entries']]
              for kind in _CATALOGS if kind in fields and 'entries' in fields[kind]}
    result['context'] = [key for key, value in spec['fields'] if 'object' in value]
    if 'claims' in fields:
        result['claims'] = [claim.get('claim_id') if isinstance(claim, dict) else None
                            for claim in (_load_json(root, h) for h in fields['claims']['claims'])]
    return result


def review_brief(pointer, item_id):
    """Return fixed instructions, schema and a navigable exact-key inventory."""
    root, manifest = _manifest(pointer)
    spec = _item(manifest, item_id)
    prompt = spec['prompt']
    if not prompt or prompt['type'] not in ('review_context', 'role_context'):
        raise ValueError('item has no structured prompt')
    result = {'instructions': _get(root, prompt['prefix']).decode(),
              'item': _load_json(root, spec['item']),
              'prompt_sha256': prompt['prompt_sha256']}
    if prompt['type'] == 'review_context':
        result['payload_keys'] = [key for key, _ in prompt['tree']['map']]
        # The remainder may contain the structured-response schema and rules.
        result['suffix'] = _get(root, prompt['suffix']).decode()
    else:
        result['payload_keys'] = [key for key, _ in prompt['fields']]
        result['suffix'] = _get(root, prompt['suffix']).decode()
    return result


def review_query(pointer, item_id, path):
    """Fetch or enumerate an exact payload path without loading sibling data.

    Use a list of exact dictionary keys and integer list indices. The returned
    subtree is unchanged; callers may inspect `review_directory` first.
    """
    root, manifest = _manifest(pointer)
    spec = _item(manifest, item_id)['prompt']
    if spec and spec['type'] == 'role_context' and path and path[0] == 'state':
        return _tree_load(root, _tree_at(dict(spec['fields'])['state']['tree'], path[1:]))
    if not spec or spec['type'] != 'review_context':
        raise ValueError('item has no indexed review context')
    return _tree_load(root, _tree_at(spec['tree'], path))


def review_directory(pointer, item_id, path=()):
    """Return only immediate exact keys or list indices at an indexed path."""
    root, manifest = _manifest(pointer)
    spec = _item(manifest, item_id)['prompt']
    if spec and spec['type'] == 'role_context' and path and path[0] == 'state':
        node = _tree_at(dict(spec['fields'])['state']['tree'], path[1:])
    elif spec and spec['type'] == 'review_context':
        node = _tree_at(spec['tree'], path)
    else:
        raise ValueError('item has no indexed review context')
    if 'map' in node:
        return [key for key, _ in node['map']]
    if 'list' in node:
        return list(range(len(node['list'])))
    return []


def review_claim(pointer, item_id, claim_id, *, global_state=False):
    """Fetch one explicitly identified review task or scoped global Claim."""
    if global_state:
        path = ['state', 'claims']
        rows = review_directory(pointer, item_id, path)
        matches = [review_query(pointer, item_id, path + [i]) for i in rows]
        matches = [row for row in matches if isinstance(row, dict) and row.get('claim_id') == claim_id]
    else:
        keys = review_directory(pointer, item_id)
        if 'tasks' in keys:
            rows = review_directory(pointer, item_id, ['tasks'])
            matches = [review_query(pointer, item_id, ['tasks', i]) for i in rows]
            matches = [row for row in matches if isinstance(row, dict)
                       and isinstance(row.get('claim'), dict)
                       and row['claim'].get('claim_id') == claim_id]
        elif 'claim' in keys:
            claim = review_query(pointer, item_id, ['claim'])
            matches = [claim] if isinstance(claim, dict) and claim.get('claim_id') == claim_id else []
        else:
            matches = []
    if len(matches) != 1:
        raise KeyError(claim_id)
    return matches[0]


def task_brief(pointer):
    """List task IDs and explicit Claim IDs by reading only the manifest."""
    _, manifest = _manifest(pointer)
    return manifest['task_brief']
