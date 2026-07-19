"""
GNN Engine — sparse message passing for spreading activation.

Computes h^(l+1) = decay · Â h^(l) with seed re-injection (APPNP-style),
where Â = D^-1/2 (A + I) D^-1/2 is the symmetric-normalized adjacency
with self-loops.

The adjacency is kept as COO edge arrays (O(N+E) memory). It must never be
materialized densely: the previous N×N implementation needed >500MB and
minutes of CPU per call at ~11k nodes, and this runs per ticker per cycle.
"""

import numpy as np
from typing import Dict, List, Tuple
import logging

logger = logging.getLogger(__name__)


class GNNEngine:
    def __init__(
        self,
        nodes: List[str],
        edges: List[Tuple[str, str, float]],
    ):
        """
        nodes: list of node IDs
        edges: list of (source_id, target_id, weight)
        """
        self.node_to_idx = {node: i for i, node in enumerate(nodes)}
        self.idx_to_node = {i: node for i, node in enumerate(nodes)}
        self.n_nodes = len(nodes)

        src: list[int] = []
        tgt: list[int] = []
        wgt: list[float] = []
        for s, t, w in edges:
            i = self.node_to_idx.get(s)
            j = self.node_to_idx.get(t)
            if i is None or j is None:
                continue
            # Undirected: propagate both ways. Parallel edges (same node pair,
            # different relation) accumulate, reinforcing the connection.
            src += [i, j]
            tgt += [j, i]
            wgt += [w, w]
        # Self-loops (the +I term)
        loop = list(range(self.n_nodes))
        src += loop
        tgt += loop
        wgt += [1.0] * self.n_nodes

        self._src = np.asarray(src, dtype=np.int64)
        self._tgt = np.asarray(tgt, dtype=np.int64)
        w_arr = np.asarray(wgt, dtype=np.float32)

        deg = np.zeros(self.n_nodes, dtype=np.float32)
        np.add.at(deg, self._tgt, w_arr)
        with np.errstate(divide="ignore"):
            d_inv_sqrt = 1.0 / np.sqrt(deg)
        d_inv_sqrt[~np.isfinite(d_inv_sqrt)] = 0.0
        self._w_norm = w_arr * d_inv_sqrt[self._src] * d_inv_sqrt[self._tgt]

    def message_passing(
        self,
        initial_activations: Dict[str, float],
        layers: int = 3,
        decay: float = 0.85,
    ) -> Dict[str, float]:
        """
        Sparse graph-convolutional message passing for spreading activation.
        """
        h = np.zeros(self.n_nodes, dtype=np.float32)
        for node, act in initial_activations.items():
            if node in self.node_to_idx:
                h[self.node_to_idx[node]] = act

        for _ in range(layers):
            new_h = np.zeros(self.n_nodes, dtype=np.float32)
            np.add.at(new_h, self._tgt, self._w_norm * h[self._src])
            new_h *= decay
            # Re-inject initials (like PageRank or APPNP)
            for node, act in initial_activations.items():
                if node in self.node_to_idx:
                    idx = self.node_to_idx[node]
                    new_h[idx] = max(new_h[idx], act)
            h = new_h

        return {self.idx_to_node[i]: float(h[i]) for i in range(self.n_nodes)}
