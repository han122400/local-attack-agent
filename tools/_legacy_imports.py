"""Scoped compatibility imports for historical loop sources."""

import builtins
import importlib

_ENGINE = ('session_log', 'command_runtime', 'harness', 'decision_provider', 'model_providers',
           'model_settings', 'planning', 'risk_rubric', 'code_checks', 'scorer_session',
           'scorer_state', 'event_classification', 'experiment_config', 'lab', 'scorecard')
_MAPPING = {name: 'engine.' + name for name in _ENGINE}
_MAPPING.update({'v4_' + name: 'engine.' + name for name in
                 ('tree', 'contracts', 'reporting', 'usage', 'policy', 'discovery')})
_MAPPING.update({'v4_loop': 'strategies.greedy', 'explorer': 'orchestrator'})


def baseline_builtins() -> dict:
    original_import = builtins.__import__

    def scoped_import(name, globals=None, locals=None, fromlist=(), level=0):
        mapped = _MAPPING.get(name) if level == 0 else None
        if mapped:
            if not fromlist:
                return importlib.import_module(mapped)
            return original_import(mapped, globals, locals, fromlist, level)
        return original_import(name, globals, locals, fromlist, level)

    namespace = vars(builtins).copy()
    namespace['__import__'] = scoped_import
    return namespace
