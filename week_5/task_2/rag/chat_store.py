"""Dialogue ledger adapter for a local query embedding beside its answer."""

from agent.storage import SQLiteStore


class RagSQLiteStore(SQLiteStore):
    def __init__(self, db_path):
        super().__init__(db_path)
        self.rag_mode = None
        self.parent_request_id = None
        self.parent_metadata = None

    def begin(self, record, user_text=None):
        if record['metadata'].get('kind') == 'answer' and self.rag_mode is not None:
            record['metadata']['rag'] = {
                'mode': 'rag' if self.rag_mode else 'plain',
                'status': ('pending' if record['status'] == 'pending' else 'not_requested')
                    if self.rag_mode else 'disabled',
                'sources': [], 'context': '', 'context_budget': None,
                'embedding_request_id': None,
            }
            if record['status'] == 'pending':
                record['usage_status'] = 'not_requested'
            request_id = super().begin(record, user_text)
            self.parent_request_id = request_id
            self.parent_metadata = record['metadata']
            return request_id
        return super().begin(record, user_text)

    def mark_requested(self, request_id):
        # guarded.run_guarded marks the answer before tool_loop; query retrieval
        # must finish before the generation call becomes billable.
        if self.rag_mode is not None and request_id == self.parent_request_id:
            return
        super().mark_requested(request_id)

    def mark_generation_requested(self, request_id):
        super().mark_requested(request_id)
