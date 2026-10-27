from __future__ import annotations

import collections
import logging
from typing import Any

logger = logging.getLogger(__name__)


class PageRankCalculator:
    """Computes directed graph PageRank and domain authority for autonomous search ranking.

    Implements the standard power iteration algorithm with dangling node compensation
    and damping factor d = 0.85.
    """

    def __init__(self, damping: float = 0.85, max_iterations: int = 25, tolerance: float = 1e-5) -> None:
        self.damping = damping
        self.max_iterations = max_iterations
        self.tolerance = tolerance

    def compute(
        self,
        edges: list[tuple[str, str]],
        nodes: list[str] | None = None,
    ) -> dict[str, float]:
        """Compute PageRank dictionary {node_id: score} given directed edges (source, target)."""
        # Collect all unique nodes
        all_nodes: set[str] = set(nodes or [])
        out_edges: dict[str, list[str]] = collections.defaultdict(list)
        in_edges: dict[str, list[str]] = collections.defaultdict(list)

        for src, dst in edges:
            all_nodes.add(src)
            all_nodes.add(dst)
            out_edges[src].append(dst)
            in_edges[dst].append(src)

        n = len(all_nodes)
        if n == 0:
            return {}

        node_list = list(all_nodes)
        initial_score = 1.0 / n
        ranks: dict[str, float] = {node: initial_score for node in node_list}

        out_degrees: dict[str, int] = {node: len(out_edges[node]) for node in node_list}
        dangling_nodes = [node for node in node_list if out_degrees[node] == 0]

        teleport = (1.0 - self.damping) / n

        for it in range(self.max_iterations):
            # Compute total dangling rank
            dangling_sum = sum(ranks[node] for node in dangling_nodes)
            dangling_contrib = self.damping * (dangling_sum / n)

            new_ranks: dict[str, float] = {}
            diff = 0.0

            for node in node_list:
                incoming_sum = 0.0
                for in_neighbor in in_edges[node]:
                    out_deg = out_degrees[in_neighbor]
                    if out_deg > 0:
                        incoming_sum += ranks[in_neighbor] / out_deg

                new_rank = teleport + dangling_contrib + (self.damping * incoming_sum)
                diff += abs(new_rank - ranks[node])
                new_ranks[node] = new_rank

            ranks = new_ranks
            if diff < self.tolerance:
                logger.debug("PageRank converged in %d iterations (diff=%.6f)", it + 1, diff)
                break

        # Normalize so maximum score is 1.0 and minimum is non-zero
        max_score = max(ranks.values()) if ranks else 1.0
        normalized = {node: (score / max_score) for node, score in ranks.items()}
        return normalized

    @staticmethod
    def extract_domain_edges(raw_edges: list[dict[str, Any]]) -> list[tuple[str, str]]:
        """Filter and extract unique inter-domain directed edges."""
        unique_edges: set[tuple[str, str]] = set()
        for edge in raw_edges:
            src_domain = edge.get("source_domain", "")
            tgt_domain = edge.get("target_domain", "")
            if src_domain and tgt_domain and src_domain != tgt_domain:
                unique_edges.add((src_domain, tgt_domain))
        return list(unique_edges)
