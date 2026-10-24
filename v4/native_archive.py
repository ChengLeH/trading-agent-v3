"""Lossless content-addressed cold payloads for the native work queue.

The queue lock covers hydration and publication. Immutable objects are fsynced
before their references reach the atomic state write; interrupted publication
may leave harmless reusable objects, never a reference to unwritten evidence.
This reduces hot-state writes, not logical read volume or model context size.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

_METADATA = '_native_archive'
_FORMAT = 'v4-native-archive/1'
_OBJECT_FORMAT = 'v4-native-payload/1'
_PROMPT_FRAME = '\n\n```json\n'
_MARKER = '$native_archive'


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class NativeArchive:
    def __init__(self, directory):
        self.root = Path(directory)
        self.directory = self.root / 'native-archive'

    def _check_directory(self, create=False):
        if create:
            try:
                self.directory.mkdir()
            except FileExistsError:
                pass
            else:
                _sync_directory(self.root)
        try:
            mode = self.directory.lstat().st_mode
        except OSError as exc:
            raise ValueError('native archive directory is missing or unreadable') from exc
        if not stat.S_ISDIR(mode):
            raise ValueError('native archive directory must be a real directory, not a symlink')

    def _read(self, digest):
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in '0123456789abcdef' for char in digest)):
            raise ValueError('native archive reference has an unsafe content hash')
        self._check_directory()
        try:
            fd = os.open(self.directory / f'{digest}.json', os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError('native archive object must be a regular file')
                payload = stream.read()
        except OSError as exc:
            raise ValueError(f'native archive object {digest} is missing or unsafe') from exc
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f'native archive object {digest} failed content hash validation')
        try:
            envelope = json.loads(payload)
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f'native archive object {digest} is not valid JSON') from exc
        if (not isinstance(envelope, dict) or envelope.get('format') != _OBJECT_FORMAT
                or 'value' not in envelope or not isinstance(envelope.get('logical_sha256'), str)):
            raise ValueError('native archive object encoding is unsupported')
        value = envelope['value']
        prefixes = envelope.get('prompt_prefixes', [])
        if not isinstance(prefixes, list):
            raise ValueError('native archive prompt prefix table is malformed')
        seen = set()
        for row in prefixes:
            if (not isinstance(row, dict) or set(row) != {'item_id', 'sha256'}
                    or not isinstance(row['item_id'], str) or row['item_id'] in seen):
                raise ValueError('native archive prompt prefix reference is malformed')
            item_id = row['item_id']
            seen.add(item_id)
            prefix = self._read(row['sha256'])
            if (not isinstance(prefix, str) or not isinstance(value, dict)
                    or not isinstance(value.get(item_id), dict)
                    or not isinstance(value[item_id].get('prompt'), str)):
                raise ValueError('native archive prompt prefix cannot be reconstructed')
            value[item_id]['prompt'] = prefix + value[item_id]['prompt']
        if hashlib.sha256(_json_bytes(value)).hexdigest() != envelope['logical_sha256']:
            raise ValueError('native archive reconstructed payload failed logical content hash validation')
        return value

    def _store(self, value, pool_prompts=False):
        # Never sort keys: packet/item iteration order is part of replay behavior.
        logical_digest = hashlib.sha256(_json_bytes(value)).hexdigest()
        encoded = value
        prefixes = []
        if pool_prompts and isinstance(value, dict):
            encoded = dict(value)
            for item_id, item in value.items():
                prompt = item.get('prompt') if isinstance(item, dict) else None
                if not isinstance(prompt, str) or _PROMPT_FRAME not in prompt:
                    continue
                boundary = prompt.index(_PROMPT_FRAME) + len(_PROMPT_FRAME)
                # A framing split is storage-only: no JSON or task semantics are parsed.
                encoded[item_id] = dict(item)
                encoded[item_id]['prompt'] = prompt[boundary:]
                prefixes.append(dict(item_id=item_id, sha256=self._store(prompt[:boundary])))
        envelope = dict(format=_OBJECT_FORMAT, logical_sha256=logical_digest, value=encoded)
        if prefixes:
            envelope['prompt_prefixes'] = prefixes
        payload = _json_bytes(envelope)
        digest = hashlib.sha256(payload).hexdigest()
        self._check_directory(create=True)
        target = self.directory / f'{digest}.json'
        if target.exists() or target.is_symlink():
            self._read(digest)  # Existing objects are verified, never replaced.
            return digest
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(dir=self.directory, prefix='.pending-')
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                self._read(digest)
            _sync_directory(self.directory)
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
        return digest

    @staticmethod
    def _slot(state, path):
        if (not isinstance(path, list) or not all(isinstance(part, str) for part in path)
                or not ((len(path) == 3 and path[0] == 'packets' and path[2] in ('items', 'received', 'wire_requests', 'batch_sources', 'wire_outcomes'))
                        or (len(path) == 2 and path[0] == 'patch_outcomes'))):
            raise ValueError('native archive reference has an unsupported path')
        try:
            container = state
            for part in path[:-1]:
                container = container[part]
            if not isinstance(container, dict) or path[-1] not in container:
                raise KeyError(path[-1])
        except (KeyError, TypeError) as exc:
            raise ValueError('native archive reference path is missing') from exc
        return container, path[-1]

    @staticmethod
    def _payload_slots(state):
        for packet_id, packet in state.get('packets', {}).items():
            if isinstance(packet, dict):
                for field in ('items', 'received', 'wire_requests', 'batch_sources', 'wire_outcomes'):
                    if field in packet:
                        yield ['packets', packet_id, field], packet, field
        for packet_id in state.get('patch_outcomes', {}):
            yield ['patch_outcomes', packet_id], state['patch_outcomes'], packet_id

    def hydrate(self, state):
        metadata = state.pop(_METADATA, None)
        restored = set()
        if metadata is not None:
            if (not isinstance(metadata, dict) or metadata.get('format') != _FORMAT
                    or not isinstance(metadata.get('refs'), list)):
                raise ValueError('native archive manifest is unsupported')
            for reference in metadata['refs']:
                if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
                    raise ValueError('native archive reference is malformed')
                path, digest = reference['path'], reference['sha256']
                container, key = self._slot(state, path)
                identity = tuple(path)
                if identity in restored or container[key] != {_MARKER: digest}:
                    raise ValueError('native archive reference differs from its payload marker')
                container[key] = self._read(digest)
                restored.add(identity)
        # A damaged/removed manifest must not silently expose references as evidence.
        for path, container, key in self._payload_slots(state):
            if (tuple(path) not in restored and isinstance(container[key], dict)
                    and set(container[key]) == {_MARKER} and isinstance(container[key][_MARKER], str)):
                raise ValueError('native archive payload marker has no manifest reference')
        return state

    def compact(self, state):
        if _METADATA in state:
            raise ValueError('native archive metadata is reserved for disk storage')
        compact = dict(state)
        compact['packets'] = {key: dict(value) if isinstance(value, dict) else value
                              for key, value in state.get('packets', {}).items()}
        if 'patch_outcomes' in state:
            compact['patch_outcomes'] = dict(state['patch_outcomes'])
        references = []
        for path, container, key in self._payload_slots(compact):
            if path[0] == 'packets' and not container.get('retired'):
                continue
            digest = self._store(container[key], pool_prompts=path[0] == 'packets' and key == 'items')
            container[key] = {_MARKER: digest}
            references.append(dict(path=path, sha256=digest))
        if references:
            compact[_METADATA] = dict(format=_FORMAT, refs=references)
        return compact
