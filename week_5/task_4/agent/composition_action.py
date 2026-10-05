"""Model-selected host action composing three separate MCP calls."""
from .composition import CompositionAgent
from composition.contracts import SearchInput
from composition.service import CompositionError


class CompositionAction:
    def __init__(self, store, pipeline=None):
        self.store = store
        self.pipeline = pipeline or CompositionAgent(store)

    def __call__(self, agent, request_id, arguments):
        value = SearchInput.model_validate(arguments)
        branch = agent.composition_context()['branch_id']
        run = self.store.create(agent.profile_id, agent._dialogue_id, value.question, value.query,
                                branch, parent_request_id=request_id, source=value.source)

        def before_save(content):
            rules = agent._invariants()
            if not rules or not rules['rules']:
                return None
            from .guarded import check
            context = dict(task_state=agent._task_state(), messages=[
                dict(role='user', content=value.question)])
            result = check(agent, 'output', content, rules, request_id, context)
            return result.text if result.status != 'ok' else None

        self.pipeline.execute(run['id'], before_save=before_save)
        completed = self.store.get(run['id'])
        agent.accept_composition(completed)
        if completed['status'] != 'success':
            raise CompositionError(completed['error'] or 'Цепочка остановлена без сохранения.')
        return dict(run_id=run['id'], saved=completed['saved'],
                    source_limitation=completed['materials']['limitation'],
                    download_url='/api/composition/'+run['id']+'/file')
