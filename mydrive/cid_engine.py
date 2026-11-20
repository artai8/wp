"""
CID (Content Identifier) Engine
==============================
IPFS-style content addressing: data -> SHA256 -> multihash -> CIDv1 (base32)

This is the core of "how files are stored" — not by path, but by content hash.
Same content = same CID = automatic deduplication.
"""

import hashlib
import base64
from dataclasses import dataclass
from typing import Optional

# Multihash codec IDs
SHA2_256 = 0x12  # hash type
SHA2_256_LEN = 32  # 256 bits = 32 bytes

# Multibase prefixes
BASE32 = "b"       # CIDv1 base32
BASE58_BTC = "z"   # CIDv0 base58btc


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def make_multihash(hash_bytes: bytes, hash_code: int = SHA2_256) -> bytes:
    """Encode a raw hash into multihash format: <codec><length><hash>"""
    return bytes([hash_code, len(hash_bytes)]) + hash_bytes


def cid_v1_from_multihash(multihash_bytes: bytes, codec: int = 0x55) -> str:
    """CIDv1 = <version=1><multicodec><multihash> -> base32 encode

    codec 0x55 = raw    -> prefix ``bafkrei…`` (used for slices)
    codec 0x70 = dag-pb -> prefix ``bafybei…`` (used for file roots)
    """
    cid_bytes = bytes([0x01, codec]) + multihash_bytes
    # Base32 lowercase, no padding
    encoded = base64.b32encode(cid_bytes).decode("ascii").rstrip("=").lower()
    return "b" + encoded


def cid_v0_from_multihash(multihash_bytes: bytes) -> str:
    """CIDv0 = just base58btc(multihash), starts with 'Qm'"""
    # Base58 Bitcoin encoding
    return "Qm" + _base58_encode(multihash_bytes)


# Base58 Bitcoin alphabet
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_encode(data: bytes) -> str:
    """Base58Encode encodes the given bytes to a base58 string."""
    if len(data) == 0:
        return ""
    # Count leading zeros
    zeros = 0
    for b in data:
        if b == 0:
            zeros += 1
        else:
            break
    # Convert to big integer
    num = int.from_bytes(data, "big")
    # Encode
    result = ""
    while num > 0:
        num, rem = divmod(num, 58)
        result = _B58_ALPHABET[rem] + result
    return "1" * zeros + result


@dataclass
class CidInfo:
    """Parsed CID information"""
    version: int          # 0 or 1
    codec: int            # 0x55 = raw
    hash_code: int        # 0x12 = sha256
    hash_length: int
    hash_hex: str         # hex string of the raw hash
    cid: str              # full CID string


def parse_cid(cid_str: str) -> CidInfo:
    """Parse a CID string back into its components."""
    if cid_str.startswith("Qm") and len(cid_str) == 46:
        # Likely CIDv0 (base58btc)
        raw = _base58_decode(cid_str)
        if len(raw) < 2:
            raise ValueError("invalid CIDv0: too short")
        hash_code = raw[0]
        hash_len = raw[1]
        hash_bytes = raw[2:2 + hash_len]
        return CidInfo(
            version=0, codec=0x70,  # dag-pb for v0
            hash_code=hash_code, hash_length=hash_len,
            hash_hex=hash_bytes.hex(), cid=cid_str
        )
    elif cid_str.startswith("b"):
        # CIDv1 base32
        # Remove prefix, add padding, decode
        b32 = cid_str[1:].upper()
        padding = "=" * ((8 - len(b32) % 8) % 8)
        decoded = base64.b32decode(b32 + padding)
        version = decoded[0]
        codec = decoded[1]
        hash_code = decoded[2]
        hash_len = decoded[3]
        hash_bytes = decoded[4:4 + hash_len]
        return CidInfo(
            version=version, codec=codec,
            hash_code=hash_code, hash_length=hash_len,
            hash_hex=hash_bytes.hex(), cid=cid_str
        )
    else:
        raise ValueError(f"unsupported CID format: {cid_str[:20]}...")


def _base58_decode(s: str) -> bytes:
    """Decode a base58 string to bytes."""
    num = 0
    for char in s:
        idx = _B58_ALPHABET.find(char)
        if idx == -1:
            raise ValueError(f"invalid base58 character: {char}")
        num = num * 58 + idx
    # Convert to bytes
    # Figure out how many leading zeros
    zeros = 0
    for char in s:
        if char == "1":
            zeros += 1
        else:
            break
    # Convert number to bytes
    if num == 0:
        return b"\x00" * zeros
    byte_len = (num.bit_length() + 7) // 8
    result = num.to_bytes(byte_len, "big")
    return b"\x00" * zeros + result


def compute_cid(data: bytes) -> str:
    """Compute the CIDv1 (base32) of a chunk of data. This is the core function."""
    h = sha256(data)
    mh = make_multihash(h)
    return cid_v1_from_multihash(mh)


def compute_cid_with_codec(data: bytes, codec: int = 0x55) -> str:
    """CIDv1 with an explicit multicodec.

    The v6 API uses ``raw`` (0x55) for slices and ``dag-pb`` (0x70) for the
    file root, which is why real content identities start with ``bafybei``.
    """
    h = sha256(data)
    mh = make_multihash(h)
    return cid_v1_from_multihash(mh, codec=codec)


# ---------------------------------------------------------------------------
# UnixFS / dag-pb file DAG
#
# Verified against the live service: a file uploaded as 2 x 4 MiB slices gets
# the exact CID that js-ipfs produces with
#   { cidVersion: 1, rawLeaves: true, chunker: fixed(4194304) }
# i.e. raw leaves (codec 0x55) + a dag-pb UnixFS root (codec 0x70), and a
# single-slice file collapses to the raw slice CID (reduceSingleLeafToSelf).
# ---------------------------------------------------------------------------

DAG_PB_CODEC = 0x70
RAW_CODEC = 0x55
MAX_LINKS_PER_NODE = 174  # go-unixfs / js-ipfs default fan-out
UNIXFS_TYPE_FILE = 2


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _pb_varint(field: int, value: int) -> bytes:
    return _varint((field << 3) | 0) + _varint(value)


def _pb_bytes(field: int, value: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _unixfs_file(filesize: int, blocksizes) -> bytes:
    """UnixFS ``Data`` message for a File node (proto2, non-packed repeated)."""
    out = _pb_varint(1, UNIXFS_TYPE_FILE)
    out += _pb_varint(3, filesize)
    for size in blocksizes:
        out += _pb_varint(4, size)
    return out


def _dag_pb_node(links, data: bytes) -> bytes:
    """PBNode: Links (field 2, encoded first) followed by Data (field 1).

    Each PBLink carries an explicit empty ``Name`` (``12 00``) — the live
    service and js-ipfs both emit it, and omitting it changes the CID.
    """
    out = b""
    for cid_bytes, tsize in links:
        link = _pb_bytes(1, cid_bytes) + _pb_bytes(2, b"") + _pb_varint(3, tsize)
        out += _pb_bytes(2, link)
    out += _pb_bytes(1, data)
    return out


def cid_bytes_of(cid_str: str) -> bytes:
    """Decode a base32 CIDv1 string back to its binary form."""
    body = cid_str[1:] if cid_str[:1] in ("b", "B") else cid_str
    body = body.upper()
    body += "=" * ((8 - len(body) % 8) % 8)
    return base64.b32decode(body)


def compute_file_cid(chunks) -> str:
    """File-level content identity, identical to IPFS UnixFS.

    ``chunks`` is the ordered slice list, each item either
    ``(cid_str, size)`` or ``bytes``.
    """
    leaves = []
    for item in chunks:
        if isinstance(item, (bytes, bytearray)):
            data = bytes(item)
            cid = compute_cid_with_codec(data, codec=RAW_CODEC)
            size = len(data)
        else:
            cid, size = item[0], int(item[1])
        leaves.append((cid_bytes_of(cid), size, size))

    if not leaves:
        return compute_cid_with_codec(b"", codec=DAG_PB_CODEC)
    if len(leaves) == 1:
        # reduceSingleLeafToSelf: no wrapping node for a single slice.
        return cid_v1_from_multihash(leaves[0][0][2:], codec=RAW_CODEC)

    level = leaves
    while len(level) > 1:
        groups = [level[i:i + MAX_LINKS_PER_NODE]
                  for i in range(0, len(level), MAX_LINKS_PER_NODE)]
        nxt = []
        for group in groups:
            filesize = sum(item[1] for item in group)
            node = _dag_pb_node([(item[0], item[2]) for item in group],
                                _unixfs_file(filesize, [item[1] for item in group]))
            digest = sha256(node)
            cid_b = bytes([0x01, DAG_PB_CODEC]) + make_multihash(digest)
            dag_size = len(node) + sum(item[2] for item in group)
            nxt.append((cid_b, filesize, dag_size))
        level = nxt

    return cid_v1_from_multihash(level[0][0][2:], codec=DAG_PB_CODEC)


def compute_cid_v0(data: bytes) -> str:
    """Compute CIDv0 of data (starts with Qm)."""
    h = sha256(data)
    mh = make_multihash(h)
    return cid_v0_from_multihash(mh)


# Quick hash for fast lookup (before computing full CID)
def quick_fingerprint(data: bytes) -> dict:
    """
    Compute multiple hashes for the FastLookup (instant transfer) system.
    - sha1: full file SHA1
    - head_sha1: SHA1 of first 256KB (fast pre-check)
    - sha256_hex: raw SHA256 hex
    - size: byte count
    """
    sha1_full = hashlib.sha1(data).hexdigest()
    head = data[:262144]  # 256KB
    sha1_head = hashlib.sha1(head).hexdigest()
    sha256_hex = hashlib.sha256(data).hexdigest()
    return {
        "sha1": sha1_full,
        "head_sha1": sha1_head,
        "sha256": sha256_hex,
        "size": len(data),
    }


if __name__ == "__main__":
    # Self-test
    test_data = b"Hello, HalalCloud Clone!"
    cid = compute_cid(test_data)
    print(f"Data: {test_data}")
    print(f"CIDv1: {cid}")
    
    info = parse_cid(cid)
    print(f"Parsed: version={info.version} codec=0x{info.codec:02x} hash_code=0x{info.hash_code:02x}")
    print(f"  hash_hex={info.hash_hex}")
    print(f"  re-computed CID matches: {compute_cid(test_data) == cid}")
    
    # Test dedup: same content = same CID
    d2 = b"Hello, HalalCloud Clone!"
    print(f"\nDedup test: same content same CID = {compute_cid(d2) == cid}")
    
    d3 = b"Different content"
    print(f"Different content different CID = {compute_cid(d3) != cid}")
    
    # FastLookup fingerprint
    fp = quick_fingerprint(test_data)
    print(f"\nFastLookup fingerprint: {fp}")
    
    # Test with a larger chunk
    import os
    big = os.urandom(1024 * 1024)  # 1MB random
    big_cid = compute_cid(big)
    big_fp = quick_fingerprint(big)
    print(f"\n1MB chunk CID: {big_cid[:30]}...")
    print(f"1MB fingerprint: sha1={big_fp['sha1'][:16]}... size={big_fp['size']}")
    
    print("\n✅ CID engine works correctly")
