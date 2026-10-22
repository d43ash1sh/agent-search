from __future__ import annotations

from services.common.hashing import compute_sha256, compute_simhash, simhash_hamming_distance


class DeduplicationFilter:
    """Detects exact and near-duplicate content before index ingestion."""

    def __init__(self, hamming_threshold: int = 3) -> None:
        self.exact_hashes: set[str] = set()
        self.simhashes: list[int] = []
        self.hamming_threshold = hamming_threshold

    def is_duplicate(self, text: str) -> tuple[bool, str]:
        """Check if text is exact duplicate or near duplicate.

        Returns (is_dup, reason).
        """
        sha = compute_sha256(text)
        if sha in self.exact_hashes:
            return True, "exact_duplicate"

        sim = compute_simhash(text)
        for existing in self.simhashes:
            if simhash_hamming_distance(sim, existing) <= self.hamming_threshold:
                return True, "near_duplicate"

        return False, ""

    def register(self, text: str) -> None:
        """Register seen text into deduplication filter."""
        self.exact_hashes.add(compute_sha256(text))
        self.simhashes.append(compute_simhash(text))
