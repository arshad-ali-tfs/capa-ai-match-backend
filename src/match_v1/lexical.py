"""Compact postings shared by exact, independently scoped BM25 populations."""
from collections import Counter
import math
import numpy as np
from .core import tokenize


class LexicalIndex:
    def __init__(self, chunks):
        self.positions = {chunk['chunk_id']: index for index, chunk in enumerate(chunks)}
        self.lengths = np.empty(len(chunks), dtype=np.int32)
        pending = {}
        for index, chunk in enumerate(chunks):
            tokens = tokenize(chunk['doc'])
            self.lengths[index] = len(tokens)
            for term, frequency in Counter(tokens).items():
                pending.setdefault(term, []).append((index, frequency))
        self.postings = {}
        for term in list(pending):
            values = np.asarray(pending.pop(term), dtype=np.int32)
            self.postings[term] = (values[:, 0].copy(), values[:, 1].copy())

    def scope(self, chunks):
        return ScopedBM25(self, [self.positions[chunk['chunk_id']] for chunk in chunks])


class ScopedBM25:
    """Same BM25 formula and corpus statistics as rebuilding a filtered corpus."""
    def __init__(self, index, positions):
        self.index = index
        self.n = len(positions)
        self.local = np.full(len(index.lengths), -1, dtype=np.int32)
        self.local[positions] = np.arange(self.n, dtype=np.int32)
        self.lengths = index.lengths[positions]
        self.avg_length = int(self.lengths.sum()) / self.n if self.n else 1.0
        self.k1, self.b = 1.5, 0.75

    def scores(self, query):
        scores = np.zeros(self.n, dtype=np.float64)
        if not self.n:
            return scores
        for word in set(query):
            posting = self.index.postings.get(word)
            if posting is None:
                continue
            positions, frequencies = posting
            local = self.local[positions]
            valid = local >= 0
            matched = local[valid]
            if not len(matched):
                continue
            frequency = frequencies[valid].astype(np.float64)
            idf = math.log1p((self.n - len(matched) + 0.5) / (len(matched) + 0.5))
            scale = self.k1 * (1 - self.b + self.b * self.lengths[matched] / max(1, self.avg_length))
            scores[matched] += idf * (frequency * (self.k1 + 1)) / (frequency + scale)
        return scores
