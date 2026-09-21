"""Единый владелец локального пользователя, задач и открытых диалогов."""
from dataclasses import replace
from pathlib import Path
from threading import RLock
import os

from agent import Agent, SQLiteStore, load_config
from agent.memory import MemoryStore
from agent.transport import ResponsesTransport


class Workspace:
    def __init__(self, data_dir, transport=None):
        self.lock = RLock()
        self.data_dir = Path(data_dir)
        self.memory = MemoryStore(self.data_dir / 'memory.sqlite3')
        self.transport = transport if transport is not None else ResponsesTransport(os.getenv('OPENAI_API_KEY'))
        self._agents = {}
        if self.memory.workspace()['active_dialogue'] is None:
            self.memory.create_task('Разбор графики', '', 'sliding')

    def agent(self):
        dialogue = self.memory.workspace()['active_dialogue']
        did = dialogue['id']
        if did not in self._agents:
            self._agents[did] = Agent(replace(load_config(), context_mode=dialogue['mode']), self.transport,
                SQLiteStore(self.data_dir / f'dialogue-{did}.sqlite3'), memory_store=self.memory,
                task_id=dialogue['task_id'], dialogue_id=did)
        return self._agents[did]

    def state(self):
        return {**self.agent().state(), 'workspace': self.memory.workspace()}

    def close(self):
        with self.lock:
            for agent in self._agents.values():
                agent.close()
            self.memory.close()
