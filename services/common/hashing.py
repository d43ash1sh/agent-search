from __future__ import annotations

import hashlib
import re


def compute_sha256(text: str) -> str:
    """Compute deterministic SHA-256 hexadecimal hash for exact deduplication."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def compute_simhash(text: str, hash_bits: int = 64) -> int:
    """Compute a 64-bit SimHash fingerprint for near-duplicate text detection.

    Similar documents will produce fingerprints with low Hamming distance.
    """
    tokens = re.findall(r"\w+", text.lower())
    if not tokens:
        return 0

    # 3-gram character shingles or token frequency
    v = [0] * hash_bits
    for token in tokens:
        # 64-bit integer hash for each token
        token_hash = int(hashlib.md5(token.encode("utf-8")).hexdigest()[:16], 16)
        for i in range(hash_bits):
            bit = (token_hash >> i) & 1
            if bit == 1:
                v[i] += 1
            else:
                v[i] -= 1

    fingerprint = 0
    for i in range(hash_bits):
        if v[i] > 0:
            fingerprint |= 1 << i

    return fingerprint


def simhash_hamming_distance(hash1: int, hash2: int) -> int:
    """Calculate the Hamming distance (number of differing bits) between two SimHashes.

    A distance <= 3 indicates near-duplicate or republished content.
    """
    xor_val = (int(hash1) ^ int(hash2)) & 0xFFFFFFFFFFFFFFFF
    return bin(xor_val).count("1")


def to_signed_int64(val: int | None) -> int:
    """Convert an unsigned 64-bit integer to a signed 64-bit integer (two's complement).

    Guarantees the value fits strictly in PostgreSQL BIGINT (-2^63 to 2^63-1),
    preventing asyncpg int8_encode OverflowErrors while preserving all 64 bits.
    """
    if val is None:
        return 0
    v = int(val) & 0xFFFFFFFFFFFFFFFF
    return v if v < (1 << 63) else v - (1 << 64)


def to_unsigned_uint64(val: int | None) -> int:
    """Convert a signed 64-bit integer (e.g., from PostgreSQL BIGINT) back to unsigned 64-bit."""
    if val is None:
        return 0
    return int(val) & 0xFFFFFFFFFFFFFFFF
