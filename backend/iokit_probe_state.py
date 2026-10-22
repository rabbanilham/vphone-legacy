"""Read-only IOService-plane capture for the validated 11D257 kernel.

Offsets come from KernelMemory.bin (T20), not a general XNU ABI. Call only
while all vCPUs are stopped. Missing/corrupt reads must never mean "quiet".
"""
import struct

REGISTRY_ROOT = 0xffffff800067d090  # IORegistryEntry::getRegistryRoot, 2a31d8
SERVICE_PLANE = 0xffffff8000694668
NAME_KEY = 0xffffff80006944f0
ABI_SIGNATURES = {
    0xffffff80002ae23c: bytes.fromhex('00304139c0035fd6'),
    0xffffff80002a31d8: bytes.fromhex('c81e00b008013f91006940f9c0035fd6'),
    0xffffff80002a3b74: bytes.fromhex('c10000b4000d40f9080040f9089140f9'),
    0xffffff800028c6e0: bytes.fromhex('092140b93f01016b69000054080d40f9'),
}


class Unreadable(ValueError):
    pass


def capture_iokit(read, max_nodes=4096, max_entries=1024):
    """read(VA, size) -> bytes; no guest calls, breakpoints, or writes."""
    errors = []

    def raw(a, n):
        if a >> 56 != 0xff:
            raise Unreadable(f'invalid kernel pointer {a:#x}')
        b = read(a, n)
        if len(b) != n:
            raise Unreadable(f'short read at {a:#x}: {len(b)}/{n}')
        return b

    def number(a, n=8):
        return int.from_bytes(raw(a, n), 'little')

    def string(obj):
        ptr = number(obj + 0x18)  # OSString::getCStringNoCopy, 294ea0
        # Page-bounded reads also work when a string ends at a mapped-page edge.
        b = raw(ptr, min(128, 4096 - (ptr & 4095)))
        if b'\0' not in b:
            raise Unreadable(f'unterminated string at {ptr:#x}')
        return b.split(b'\0')[0].decode('utf-8', errors='replace')

    def dictionary(obj):
        if not obj:
            return {}
        count = number(obj + 0x20, 4)
        if count > max_entries:
            raise Unreadable(f'dictionary count {count} exceeds limit')
        if not count:
            return {}
        data = raw(number(obj + 0x18), count * 16)
        return dict(struct.iter_unpack('<QQ', data))

    def array(obj):
        if not obj:
            return []
        count = number(obj + 0x20, 4)
        if count > max_entries:
            raise Unreadable(f'array count {count} exceeds limit')
        if not count:
            return []
        return list(struct.unpack('<' + 'Q' * count,
                                  raw(number(obj + 0x18), count * 8)))

    def class_name(obj):
        # IORegistryEntry::getName's fallback calls vtable+0x38 for metaClass.
        getter = number(number(obj) + 0x38)
        adrp, add, ret = struct.unpack('<III', raw(getter, 12))
        if (adrp & 0x9f00001f != 0x90000000 or
                add & 0xffc003ff != 0x91000000 or ret != 0xd65f03c0):
            return None  # Unrecognized getter; keep its vtable for offline lookup.
        imm = ((adrp >> 5 & 0x7ffff) << 2) | (adrp >> 29 & 3)
        if imm & (1 << 20):
            imm -= 1 << 21
        meta = (getter & ~4095) + (imm << 12) + (add >> 10 & 4095)
        return string(number(meta + 0x18))

    nodes = []
    try:
        for address, expected in ABI_SIGNATURES.items():
            if raw(address, len(expected)) != expected:
                raise Unreadable(f'kernel ABI mismatch at {address:#x}')
        root = number(REGISTRY_ROOT)
        plane = number(SERVICE_PLANE)
        child_key = number(plane + 0x20)  # getChildSetReference, 2a3b64
        name_key = number(plane + 0x28)   # getName, 2a4ba8
        generic_name_key = number(NAME_KEY)
    except Unreadable as exc:
        return dict(complete=False, errors=[str(exc)], nodes=[], busy=[])
    todo = [(root, None, '')]
    seen = set()
    while todo and len(nodes) < max_nodes:
        obj, parent, path = todo.pop()
        if obj in seen:
            continue  # IORegistry is a graph, not necessarily a tree.
        seen.add(obj)
        node = dict(object=hex(obj), parent=hex(parent) if parent else None)
        try:
            node['vtable'] = hex(number(obj))
            registry = dictionary(number(obj + 0x18))
            name_obj = registry.get(name_key) or registry.get(generic_name_key)
            name = string(name_obj) if name_obj else hex(obj)
            node.update(name=name, path=path + '/' + name)
            # The registry root is IORegistryEntry, not IOService (size 0x28).
            # Do not misinterpret adjacent heap bytes as a busy count.
            node['busy'] = number(obj + 0x4c, 1) if obj != root else None
            children = array(registry.get(child_key, 0))
            node['children'] = [hex(c) for c in children]
            todo.extend((c, obj, node['path']) for c in reversed(children))
            try:
                node['class'] = class_name(obj)
            except Unreadable:
                node['class'] = None
        except Unreadable as exc:
            node['error'] = str(exc)
            errors.append(str(exc))
        nodes.append(node)
    if todo:
        errors.append(f'node limit {max_nodes} reached')
    busy = [n for n in nodes if n.get('busy')]
    # Counts propagate on child transitions; a busy ancestor is not proof that
    # its own start() is blocked. Leaves are candidates, not causal attribution.
    busy_objects = {n['object'] for n in busy}
    for n in busy:
        n['busy_child'] = any(c in busy_objects for c in n.get('children', []))
    return dict(complete=not errors, errors=errors, nodes=nodes, busy=busy)


def capture_kernel_threads(read, max_threads=512, max_frames=32):
    """Bounded kernel-task queue walk, using ca_probe_state's saved-context ABI.

    Saved contexts are stale for running threads; frames are candidates for
    symbolication, not proof of the currently executing PC on either CPU.
    """
    def num(a):
        if a >> 56 != 0xff:
            raise Unreadable(f'invalid kernel pointer {a:#x}')
        b = read(a, 8)
        if len(b) != 8:
            raise Unreadable(f'short read at {a:#x}')
        return int.from_bytes(b, 'little')

    threads, errors, seen = [], [], set()
    try:
        task = num(0xffffff8000694008)
        head = task + 0x48
        link = num(head)
        while link != head and len(threads) < max_threads:
            if link in seen:
                raise Unreadable(f'cycle in kernel thread queue at {link:#x}')
            seen.add(link)
            # Mach task_threads queue links carry the thread base pointer;
            # the next link is stored at thread+0x308 (as in ca_probe_state).
            thread = link
            item = dict(thread=hex(thread), tid=num(thread + 0x740),
                        wait_event=hex(num(thread + 0x20)),
                        continuation=hex(num(thread + 0x58)))
            context = num(thread + 0x680)
            item['saved_context'] = hex(context)
            item['frames'] = []
            try:
                fp = num(context + 0xf0)
                item['saved_lr'] = hex(num(context + 0xf8))
                item['saved_sp'] = hex(num(context + 0x100))
                visited = set()
                for _ in range(max_frames):
                    if fp >> 56 != 0xff or fp in visited:
                        break
                    visited.add(fp)
                    previous, lr = num(fp), num(fp + 8)
                    item['frames'].append(dict(fp=hex(fp), lr=hex(lr)))
                    if previous <= fp:
                        break
                    fp = previous
            except Unreadable as exc:
                item['context_error'] = str(exc)
            threads.append(item)
            link = num(thread + 0x308)
        if link != head:
            errors.append(f'thread limit {max_threads} reached')
    except Unreadable as exc:
        errors.append(str(exc))
    return dict(complete=not errors, errors=errors, threads=threads,
                saved_context_layout='inherited from ca_probe_state; validate against kernel stacks')
