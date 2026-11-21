"""One run that uses the 0.15 features together, with scripted models only.

A resident lead hires a solver agent. The solver's model fails on its first call; the failure goes to the
lead, which restarts it. The solver answers 24 (wrong); the lead sends it on to a verifier on an edge that
takes one rejection. The verifier knows the optimum is 25 and rejects; the lead, woken in ``wait``, asks the
solver again and corrects what it sent. Then the same run with a verifier that crashes once: the run fails,
and a new engine on the same journal finishes it without asking the models again for what was done.
"""
import re

import pytest

from interlacing import AgentNode, Engine, FunctionNode, GraphBuilder, Journal, Port, RunError
from interlacing.models import ChatResult, FakeProvider

PROBLEM = 'maximize x*(10-x) over the integers'
RIGHT = 'x=5 gives 25; proof: x(10-x) = 25-(x-5)^2 <= 25, equality at x=5'


def calls(*pairs):
    return ChatResult(tool_calls=[{'id': f'c{i}', 'name': name, 'args': args} for i, (name, args) in enumerate(pairs)])


def solver_model(asked):
    def chat(system, messages, tools):
        asked.append(messages[-1]['content'])
        if len(asked) == 1:
            raise ConnectionError('the model API timed out')
        return ChatResult(text=RIGHT if 'rejected' in asked[-1] else 'x=4 gives 24 (grid search)')
    return chat


def lead_model(system, messages, tools):
    said = [m['content'] for m in messages if m['role'] == 'tool']
    used = [c for m in messages if m['role'] == 'assistant' for c in (m.get('tool_calls') or [])]
    last = said[-1] if said else ''
    if not said:
        return calls(('plan', {'items': ['solve', 'verify']}),
                     ('hire', {'template': 'solver', 'name': 'solver', 'task': PROBLEM}), ('wait', {}))
    if '[failed]' in last:
        return calls(('control', {'name': 'solver', 'action': 'restart'}), ('wait', {}))
    if '[rejected]' in last:
        message = re.search(r"correct\(id='([^']+)'", last).group(1)
        reason = re.search(r'rejected by \w+: (.*)', last).group(1)
        return calls(('note', {'text': f'to correct {message}'}),
                     ('send_to_solver', {'content': f'your answer was rejected: {reason}. Check again.'}), ('wait', {}))
    if '[from_solver]' in last:
        answer = re.search(r'\[from_solver\][^\n]*\n([^\n]+)', last).group(1)
        notes = [c['args']['text'] for c in used if c['name'] == 'note']
        if notes:
            return calls(('correct', {'id': notes[-1].split()[-1], 'content': answer}), ('done', {'item': 'verify'}),
                         ('wait', {}))
        return calls(('done', {'item': 'solve'}), ('send_out', {'content': answer}), ('wait', {}))
    if 'Nothing more can come' in last:
        return calls(('finish', {'summary': 'verified'}))
    return calls(('wait', {}))


def verifier(crashes, verdicts):
    def verify(ctx, claim):
        if crashes:
            crashes.pop()
            raise RuntimeError("the verifier's machine went down")
        if int(re.search(r'gives (\d+)', claim).group(1)) != 25:
            verdict = ctx.reject('claim', 'x(10-x) reaches 25 at x=5')
            verdicts.append(verdict['answer'])
            claim = verdict.get('value', claim)
        return f'accepted: {claim}'
    return FunctionNode('verifier', verify, inputs=[Port('claim')], outputs=[Port('verdict')], context=True)


def build(asked, crashes=(), verdicts=None):
    def solver(id):
        return AgentNode(id, FakeProvider(solver_model(asked)))
    lead = AgentNode('lead', FakeProvider(lead_model), resident=True, hire=1, templates={'solver': solver})
    graph = (GraphBuilder().add(lead, verifier(list(crashes), verdicts if verdicts is not None else []))
             .connect('lead.out', 'verifier.claim', challenges=1)
             .input('task', 'lead.task').output('verdict', 'verifier.verdict').compile())
    return graph, solver, lead


@pytest.mark.parametrize('workers', [0, 1, 4])
def test_a_team_that_fails_is_rejected_and_corrects_itself(workers):
    asked, verdicts = [], []
    graph, _, lead = build(asked, verdicts=verdicts)
    engine = Engine(graph, workers=workers, time_limit=30)
    result = engine.run({'task': PROBLEM})
    assert result.complete and result.last('verdict') == f'accepted: {RIGHT}'
    assert verdicts == ['corrected'] and len(asked) == 3
    assert [(p['node'], p['handled']) for p in result.problems] == [('lead-solver', 'reported')]
    assert {node.id for node in engine.graph.nodes} == {'lead', 'verifier', 'lead-solver'}
    assert lead.board['fixed'] == {'out': 1} and all(done for _, done in lead.board['items'])


@pytest.mark.parametrize('workers', [0, 1, 4])
def test_the_same_run_taken_up_from_its_journal_after_a_crash(workers, tmp_path):
    asked = []
    path = tmp_path / 'journal.jsonl'
    graph, solver, _ = build(asked, crashes=[1])
    first = Engine(graph, workers=workers, journal=Journal(path), rebuild=lambda d: solver(d['id']), time_limit=30)
    with pytest.raises(RunError, match="verifier's machine went down"):
        first.run({'task': PROBLEM})
    graph, solver, lead = build(asked)
    again = Engine(graph, workers=workers, journal=Journal(path), rebuild=lambda d: solver(d['id']), time_limit=30)
    result = again.resume()
    assert result.complete and result.last('verdict') == f'accepted: {RIGHT}'
    assert len(asked) == 3        # the solver was not asked again for what it had already answered
    assert lead.board['fixed'] == {'out': 1} and lead.board['notes'] == ['to correct out#1']
