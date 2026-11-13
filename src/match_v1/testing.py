"""Zero-network vector-store double, used ONLY by unit tests and offline selfcheck."""
from __future__ import annotations

import numpy as np

from .core import unit_rows


class MemoryCollection:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def upsert(self, *, ids, documents, metadatas, embeddings):
        for key, doc, meta, vector in zip(ids, documents, metadatas, embeddings):
            self.items[key] = {'doc': doc, 'metadata': meta, 'vec': unit_rows(vector)[0]}

    def count(self):
        return len(self.items)

    def get(self, *, ids, include=None):
        return {'ids': [key for key in ids if key in self.items],
                'embeddings': [self.items[key]['vec'].tolist() for key in ids if key in self.items]}

    def query(self, *, query_embeddings, n_results, where, include=None):
        out_ids, out_distances, out_docs, out_meta = [], [], [], []
        for q in unit_rows(query_embeddings):
            eligible = [(k, val) for k, val in self.items.items()
                        if all(val['metadata'].get(field) in value['$in'] if isinstance(value, dict)
                               else val['metadata'].get(field) == value
                               for field, value in where.items())]
            ranked = sorted(eligible, key=lambda pair: -float(q @ pair[1]['vec']))[:n_results]
            out_ids.append([key for key, _ in ranked])
            out_distances.append([max(0., 1 - float(q @ val['vec'])) for _, val in ranked])
            out_docs.append([val['doc'] for _, val in ranked])
            out_meta.append([val['metadata'] for _, val in ranked])
        return {'ids': out_ids, 'distances': out_distances,
                'documents': out_docs, 'metadatas': out_meta}
