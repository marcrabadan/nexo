"""Read-only views of operator configuration using Shield's existing loaders."""
import os
from pathlib import Path

from . import authorization, security_context
from .errors import NexoError
from .modules import shown_path


class Configuration:
    def __init__(self, root, dependencies=None, grants=None):
        self.root = Path(root).resolve()
        self.dependencies = None
        self.authorization = None
        dep = dependencies if dependencies is not None else os.environ.get('NEXO_SECURITY_DEPENDENCIES')
        auth = grants if grants is not None else os.environ.get('NEXO_SECURITY_AUTHORIZATION')
        if dep is not None:
            try:
                self.dependencies = security_context.load(dep, root)
            except (OSError, ValueError, TypeError, RecursionError):
                raise NexoError('security_context_config') from None
        if auth is not None:
            try:
                self.authorization = authorization.load(auth)
            except (OSError, ValueError, TypeError, RecursionError):
                raise NexoError('security_authorization') from None
        self.active = dep is not None or auth is not None
        self.has_authorization = auth is not None

    def approval(self, file, symbol):
        if self.authorization is None:
            return False
        grant = self.authorization[1].get((Path(file).resolve(), symbol))
        return bool(grant and grant[1])

    def registrations(self, file, symbol):
        if self.authorization is None:
            return set()
        key = Path(file).resolve(), symbol
        if key not in self.authorization[1]:
            return set()
        return {('tool', *key)} | {('agent', *agent) for agent, (_,tools) in self.authorization[0].items() if key in tools}

    def contexts(self, file, symbol):
        if not self.active:
            return set()
        identity = Path(file).resolve()
        result = set()
        if self.dependencies is not None and identity != self.root:
            result.add((file, ''))
        if self.authorization is not None and (identity, symbol) in self.authorization[1]:
            result.add((file, symbol))
            for (agent_file, _), (_, tools) in self.authorization[0].items():
                if (identity, symbol) in tools:
                    result.add((shown_path(agent_file), '#registration'))
        return result

    def checks(self, contexts, capability, target):
        rows = {}
        for file, symbol in sorted(contexts):
            identity = Path(file).resolve()
            if self.dependencies is not None and identity != self.root:
                allowed = capability not in ('shell', 'c', 'python') and security_context.permits(
                    self.dependencies.get(identity, []), capability, target)
                rows['dependency', shown_path(identity)] = dict(scope='dependency', identity=shown_path(identity),
                    status='allowed' if allowed else 'denied', approval_required=False)
            if self.authorization is not None:
                agents, tools = self.authorization
                key = identity, symbol
                if key not in tools:
                    continue
                rules, confirm = tools[key]
                allowed = capability not in ('shell', 'c', 'python') and security_context.permits(rules, capability, target)
                name = shown_path(identity) + '::' + symbol
                rows['tool', name] = dict(scope='tool', identity=name, status='allowed' if allowed else 'denied', approval_required=confirm)
                matched_agent = False
                for (agent_file, agent_name), (rules, permitted) in agents.items():
                    if key in permitted:
                        matched_agent = True
                        allowed = capability not in ('shell', 'c', 'python') and security_context.permits(rules, capability, target)
                        name = shown_path(agent_file) + '::' + agent_name
                        rows['agent', name] = dict(scope='agent', identity=name, status='allowed' if allowed else 'denied', approval_required=False)
                if not matched_agent:
                    name = shown_path(identity) + '::' + symbol
                    rows['dispatch', name] = dict(scope='dispatch', identity=name, status='denied', approval_required=False)
        return list(rows.values())
