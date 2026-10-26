"""FMOD Studio strings bank (MasterBank.strings.bank) STDT string table: read, look up, rebuild.

Layout (FEV format 0x2e as shipped with FH2): STDT = u32 type (0 = radix tree, 32-bit) | node list | GUID list | blob.
  list  = X16 (count << 1) + u16 element size + elements;  X16 = u16, if bit 15 set: (lo & 0x7fff) | (hi u16 << 15)
  node  = u32 KeyInfo (low 24 bit: offset of the node's segment in the blob, 0xffffff = none; high 8: its first byte)
          u32 ChildInfo (low 24: first child node index, high 8: child count); children are contiguous and sorted by
          their first byte; a node without children: low 24 = index into the GUID list
  guids = 16-byte FMOD GUIDs, sorted (data1 as u32 LE first)
  blob  = X16 byte count + NUL-terminated segments
"""
import struct

SENT = 0xFFFFFF


def x16_read(d, o):
    lo = struct.unpack_from("<H", d, o)[0]
    if lo & 0x8000:
        hi = struct.unpack_from("<H", d, o + 2)[0]
        return (lo & 0x7FFF) | (hi << 15), o + 4
    return lo, o + 2


def x16_write(v):
    if v < 0x8000:
        return struct.pack("<H", v)
    return struct.pack("<HH", (v & 0x7FFF) | 0x8000, v >> 15)


class Table:
    def __init__(self, stdt):
        self.type = struct.unpack_from("<I", stdt, 0)[0]
        o = 4
        raw, o = x16_read(stdt, o)
        n, esz = raw >> 1, struct.unpack_from("<H", stdt, o)[0]
        o += 2
        assert esz == 8, esz
        self.nodes = [list(struct.unpack_from("<II", stdt, o + 8 * i)) for i in range(n)]
        o += 8 * n
        raw, o = x16_read(stdt, o)
        n, esz = raw >> 1, struct.unpack_from("<H", stdt, o)[0]
        o += 2
        assert esz == 16, esz
        self.guids = [stdt[o + 16 * i:o + 16 * i + 16] for i in range(n)]
        o += 16 * n
        bl, o = x16_read(stdt, o)
        self.blob = bytearray(stdt[o:o + bl])
        o += bl
        self.tail = stdt[o:]

    # --- helpers
    def seg(self, i):
        off = self.nodes[i][0] & 0xFFFFFF
        if off == SENT:
            return b""
        e = self.blob.index(b"\0", off)
        return bytes(self.blob[off:e])

    def children(self, i):
        c = self.nodes[i][1]
        cnt = c >> 24
        first = c & 0xFFFFFF
        return list(range(first, first + cnt)) if cnt else []

    def paths(self):
        """[(path, guid index)] of every leaf"""
        out, stack = [], [(0, b"")]
        while stack:
            i, pre = stack.pop()
            p = pre + self.seg(i)
            ch = self.children(i)
            if not ch:
                out.append((p.decode("latin-1"), self.nodes[i][1] & 0xFFFFFF))
            for c in ch:
                stack.append((c, p))
        return out

    def lookup(self, path):
        p = path.encode("latin-1")
        i = 0
        while True:
            s = self.seg(i)
            if not p.startswith(s):
                return None
            p = p[len(s):]
            ch = self.children(i)
            if not p:
                if not ch:
                    return self.nodes[i][1] & 0xFFFFFF
                term = [c for c in ch if (self.nodes[c][0] & 0xFFFFFF) == SENT]
                return self.nodes[term[0]][1] & 0xFFFFFF if term else None
            nxt = [c for c in ch if self.seg(c) and p.startswith(self.seg(c))]
            if not nxt:
                return None
            i = nxt[0]

    def write(self):
        o = struct.pack("<I", self.type)
        o += x16_write((len(self.nodes) << 1) | 1) + struct.pack("<H", 8)     # list count: (n << 1) | 1
        o += b"".join(struct.pack("<II", *n) for n in self.nodes)
        o += x16_write((len(self.guids) << 1) | 1) + struct.pack("<H", 16)
        o += b"".join(self.guids)
        o += x16_write(len(self.blob)) + bytes(self.blob)
        return o + self.tail


def path_guids(table):
    """[(path, guid)] of a table (leaf order = guid index order)"""
    out = [None] * len(table.guids)
    for p, g in table.paths():
        out[g] = (p, table.guids[g])
    return out


def build(pairs):
    """STDT bytes for [(path, 16-byte guid)]: radix tree (node key = lowercased first byte, children contiguous, a path
    that is also a prefix ends in a segment-less child), GUIDs sorted as 4 x u32 LE, leaf / parent index arrays"""
    pairs = sorted(pairs, key=lambda pg: struct.unpack("<IIII", pg[1]))
    guids = [g for _, g in pairs]
    # trie of dicts: node = {"seg": bytes, "kids": [node], "guid": index or None}
    root = {"seg": b"", "kids": [], "guid": None}

    def insert(n, p, gi):
        if not p:
            n["guid"] = gi
            return
        for c in n["kids"]:
            s = c["seg"]
            k = 0
            while k < len(s) and k < len(p) and s[k] == p[k]:
                k += 1
            if k == 0:
                continue
            if k < len(s):                       # split c at k
                tail = {"seg": s[k:], "kids": c["kids"], "guid": c["guid"]}
                c["seg"], c["kids"], c["guid"] = s[:k], [tail], None
            insert(c, p[k:], gi)
            return
        n["kids"].append({"seg": p, "kids": [], "guid": gi})

    for gi, (p, _) in enumerate(pairs):
        insert(root, p.encode("latin-1"), gi)
    # flatten: BFS, children contiguous; a node with both a guid and kids gets a segment-less first child
    for n in _walk(root):
        if n["guid"] is not None and n["kids"]:
            n["kids"].insert(0, {"seg": None, "kids": [], "guid": n["guid"]})
            n["guid"] = None
    order, parent = [root], [None]
    i = 0
    first = {}
    while i < len(order):
        n = order[i]
        if n["kids"]:
            first[id(n)] = len(order)
            for c in n["kids"]:
                order.append(c)
                parent.append(i)
        i += 1
    blob, boff = bytearray(), {}
    nodes = []
    for n in order:
        s = n["seg"]
        if s:
            boff_ = len(blob)                   # every segment stored on its own (as the stock table does)
            blob += s + b"\0"
            key = (s[0] | 0x20) if 0x41 <= s[0] <= 0x5A else s[0]
            ki = boff_ | (key << 24)
        else:
            ki = SENT
        ci = (first[id(n)] | (len(n["kids"]) << 24)) if n["kids"] else n["guid"]
        nodes.append([ki, ci])
    leaf = [0] * len(guids)
    for idx, n in enumerate(order):
        if not n["kids"] and n["guid"] is not None:
            leaf[n["guid"]] = idx
    o = struct.pack("<I", 0)
    o += x16_write((len(nodes) << 1) | 1) + struct.pack("<H", 8) + b"".join(struct.pack("<II", *n) for n in nodes)
    o += x16_write((len(guids) << 1) | 1) + struct.pack("<H", 16) + b"".join(guids)
    o += x16_write(len(blob)) + bytes(blob)
    o += x16_write(len(leaf)) + struct.pack("<%dI" % len(leaf), *leaf)
    o += x16_write(len(order)) + struct.pack("<%dI" % len(order), *[0xFFFFFFFF if p is None else p for p in parent])
    return o


def _walk(n):
    st = [n]
    while st:
        x = st.pop()
        yield x
        st.extend(x["kids"])


def replace_stdt(bank, stdt):
    """strings bank bytes with its STDT chunk replaced (STDT is the last chunk of LIST PROJ / the RIFF)"""
    o = bank.find(b"STDT")
    old = struct.unpack_from("<I", bank, o + 4)[0]
    end = o + 8 + old + (old & 1)
    suffix = bank[end:]                      # FH2: an empty STBL chunk after STDT (inside LIST PROJ)
    p = next(k for k in range(12, o) if bank[k:k + 4] == b"LIST" and bank[k + 8:k + 12] == b"PROJ")
    assert p + 8 + struct.unpack_from("<I", bank, p + 4)[0] == len(bank), "LIST PROJ does not end the file"
    body = stdt + (b"\0" if len(stdt) & 1 else b"")
    out = bytearray(bank[:o] + b"STDT" + struct.pack("<I", len(stdt)) + body + suffix)
    struct.pack_into("<I", out, p + 4, len(out) - p - 8)              # LIST PROJ
    struct.pack_into("<I", out, 4, len(out) - 8)                       # RIFF
    return bytes(out)


def bank_table(bank):
    """Table of a strings bank's STDT chunk"""
    o = bank.find(b"STDT")
    return Table(bank[o + 8:o + 8 + struct.unpack_from("<I", bank, o + 4)[0]])


def bank_paths(bank):
    """{event / bus / snapshot path: GUID hex} of a strings bank"""
    return {p: g.hex() for p, g in path_guids(bank_table(bank))}


def bank_with_paths(bank, paths):
    """the strings bank rebuilt with exactly {path: GUID hex}"""
    return replace_stdt(bank, build([(p, bytes.fromhex(g)) for p, g in paths.items()]))
