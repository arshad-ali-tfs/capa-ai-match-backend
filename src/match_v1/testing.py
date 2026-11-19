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
        def matches(metadata, clause):
            if '$and' in clause:
                return all(matches(metadata, value) for value in clause['$and'])
            return all(metadata.get(field) in value['$in'] if isinstance(value, dict)
                       else metadata.get(field) == value for field, value in clause.items())
        out_ids, out_distances, out_docs, out_meta = [], [], [], []
        for q in unit_rows(query_embeddings):
            eligible = [(k, val) for k, val in self.items.items()
                        if matches(val['metadata'], where)]
            ranked = sorted(eligible, key=lambda pair: -float(q @ pair[1]['vec']))[:n_results]
            out_ids.append([key for key, _ in ranked])
            out_distances.append([max(0., 1 - float(q @ val['vec'])) for _, val in ranked])
            out_docs.append([val['doc'] for _, val in ranked])
            out_meta.append([val['metadata'] for _, val in ranked])
        return {'ids': out_ids, 'distances': out_distances,
                'documents': out_docs, 'metadatas': out_meta}
