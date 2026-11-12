"""Dict-level plan functions kept for callers that hold a stored plan; the entities live in ``mch_harness.plan``."""
from __future__ import annotations

import copy

from ..plan import (THINKING_LEVELS, GraphError, Plan, check_acyclic as _acyclic, check_configuration as validate_configuration,
                    check_id as _id, check_keys as _keys, check_text as _text, fingerprint, require)

__all__ = ['THINKING_LEVELS', 'GraphError', 'require', 'fingerprint', 'validate_configuration', 'validate', 'downstream',
           'affected']


def validate(plan):
    Plan.check(plan)
    return copy.deepcopy(plan)


def downstream(plan, seeds):
    return Plan.from_dict(plan, trusted=True).downstream(seeds)


def affected(before, after):
    return Plan.from_dict(after, trusted=True).changed_from(Plan.from_dict(before, trusted=True))
