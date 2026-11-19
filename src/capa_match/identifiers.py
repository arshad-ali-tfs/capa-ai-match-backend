"""Preserve exact shared-source IDs around MATCH v1's normalized index keys."""
from dataclasses import dataclass

from match_v1.core import normalize_pr_id


@dataclass(frozen=True)
class SourceIdentity:
    pr_id: str
    site: str
    record_type: str


class SourceIdentifiers:
    def __init__(self, repository):
        self.repository = repository
        self.snapshot_id = repository.metadata()['snapshot_id']
        self.by_match_id: dict[str, SourceIdentity] = {}
        # Read identity columns once; never load source narratives to map IDs.
        with repository._connect() as database:
            for row in database.execute('SELECT pr_id,site,record_type FROM records ORDER BY source_row'):
                internal = normalize_pr_id(row['pr_id'])
                if not internal:
                    continue
                identity = SourceIdentity(row['pr_id'], row['site'], row['record_type'])
                previous = self.by_match_id.get(internal)
                if previous is not None and previous.pr_id != identity.pr_id:
                    raise ValueError(
                        f'Ambiguous MATCH identifier normalization: source PR_ID {previous.pr_id!r} '
                        f'and {identity.pr_id!r} both normalize to {internal!r}. '
                        'Resolve the source identifiers and import a new snapshot before building MATCH.')
                self.by_match_id[internal] = identity

    def literal_documents(self, match_ids):
        """Load only investigation text for known indexed deviations, on demand."""
        external = {self.identity(key, record_type='deviation').pr_id: key for key in match_ids}
        identifiers = list(external)
        result = {}
        with self.repository._connect() as database:
            for offset in range(0, len(identifiers), 400):
                batch = identifiers[offset:offset + 400]
                query = "SELECT pr_id,json_extract(fields,'$.investigation') AS investigation FROM records WHERE pr_id IN (" + ','.join('?' for _ in batch) + ')'
                for row in database.execute(query, batch):
                    text = str(row['investigation'] or '')
                    if text.strip():
                        result[external[row['pr_id']]] = {'INVESTIGATION': text}
        return result

    def identity(self, match_id, *, site=None, record_type=None):
        identity = self.by_match_id.get(str(match_id))
        if identity is None or (site is not None and identity.site != site) or (
                record_type is not None and identity.record_type.strip().lower() != record_type):
            raise ValueError('MATCH identifier does not resolve to the expected source record and site.')
        return identity

    def validate_index(self, service):
        for record in service.records.values():
            self.identity(record['pr_id'], site=record['site'], record_type='deviation')
        for capas in service.capas.values():
            for capa in capas:
                self.identity(capa['pr_id'], record_type='capa')

    def external_capa(self, capa):
        identity = self.identity(capa['pr_id'], record_type='capa')
        if identity.pr_id == capa['pr_id']:
            return dict(capa)
        return {**capa, 'pr_id': identity.pr_id, 'source_pr_id': identity.pr_id, 'match_pr_id': capa['pr_id']}

    def external_record(self, record):
        identity = self.identity(record['pr_id'], site=record['site'], record_type='deviation')
        return {**record, 'pr_id': identity.pr_id, 'source_pr_id': identity.pr_id,
                'match_pr_id': record['pr_id'], 'source_snapshot_id': self.snapshot_id,
                'related_capas': [self.external_capa(capa) for capa in record.get('related_capas', [])]}


class SnapshotMatchService:
    """HTTP boundary adapter; candidate retrieval and scoring stay in MATCH v1."""
    def __init__(self, service, identifiers):
        identifiers.validate_index(service)
        self.service = service
        self.identifiers = identifiers
        self.service.source_ids = {key: identifiers.identity(key, site=record['site'], record_type='deviation').pr_id
                                   for key, record in service.records.items()}
        self.service.additional_documents_loader = lambda: identifiers.literal_documents(service.records)

    def __getattr__(self, name):
        return getattr(self.service, name)

    def search(self, query='', *, site=None, all_sites=False, top_k=10, filters=None, mode='auto', offset=0):
        result = self.service.search(query, site=site, all_sites=all_sites, top_k=top_k,
                                     filters=filters, mode=mode, offset=offset)
        return {**result, 'source_snapshot_id': self.identifiers.snapshot_id,
                'results': [self.identifiers.external_record(record) for record in result['results']]}

    def facets(self, *, site=None, all_sites=False, filters=None):
        return {**self.service.facets(site=site, all_sites=all_sites, filters=filters),
                'source_snapshot_id': self.identifiers.snapshot_id}

    def search_legacy(self, query, *, site=None, all_sites=False, top_k=10):
        result = self.service.search_legacy(query, site=site, all_sites=all_sites, top_k=top_k)
        return {**result, 'source_snapshot_id': self.identifiers.snapshot_id,
                'results': [self.identifiers.external_record(record) for record in result['results']]}

    def record(self, source_pr_id, site):
        # The original service checks authorization and index site before any
        # source record is returned. Only exact external source IDs are accepted.
        internal = normalize_pr_id(source_pr_id)
        record = self.service.record(internal, site)
        external = self.identifiers.external_record(record)
        if external['pr_id'] != source_pr_id:
            raise KeyError('Record not found for this exact source identifier.')
        additional = self.identifiers.literal_documents([internal]).get(internal, {})
        additional = {name: text for name, text in additional.items() if name not in record.get('indexed_field_texts', {})}
        if additional:
            external['literal_field_texts'] = additional
        return external
