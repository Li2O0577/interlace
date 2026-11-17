"""Plain functions as an agent's tools: no node, no edge."""
import pytest

from interlacing import AgentNode, Engine, GraphBuilder, Tool, time_left
from interlacing.base import NodeError
from interlacing.models import ChatResult, FakeProvider


def check_sum(numbers: list[int], total: int, strict: bool = False) -> str:
    """Says whether the numbers add up to the total."""
    return 'yes' if sum(numbers) == total else 'no'


def where(ctx) -> str:
    """Says which node is asking."""
    return ctx.node


def test_a_function_is_described_from_its_signature():
    tool = Tool.of(check_sum)
    assert tool.name == 'check_sum' and tool.description == 'Says whether the numbers add up to the total.'
    assert tool.parameters == {'type': 'object', 'properties': {
        'numbers': {'type': 'array', 'items': {'type': 'integer'}}, 'total': {'type': 'integer'},
        'strict': {'type': 'boolean'}}, 'required': ['numbers', 'total']}
    assert Tool.of(where).parameters == {'type': 'object', 'properties': {}}
    with pytest.raises(NodeError, match='needs a name'):
        Tool.of(lambda x: x)
    assert Tool.of(lambda x: x, 'echo').name == 'echo'


def test_an_agent_calls_its_functions_itself():
    answers = []

    def policy(system, messages, tools):
        if messages[-1]['role'] == 'tool':
            answers.append(messages[-1]['content'])
        if len(answers) == 0:
            return ChatResult(tool_calls=[{'id': '1', 'name': 'check_sum', 'args': {'numbers': [1, 2], 'total': 3}}])
        if len(answers) == 1:
            return ChatResult(tool_calls=[{'id': '2', 'name': 'where', 'args': {}}])
        if len(answers) == 2:
            return ChatResult(tool_calls=[{'id': '3', 'name': 'time_left', 'args': {}}])
        if len(answers) == 3:
            return ChatResult(tool_calls=[{'id': '4', 'name': 'check_sum', 'args': {'numbers': [1], 'total': 1, 'oops': 1}}])
        return ChatResult(text='done')
    agent = AgentNode('a', FakeProvider(policy), tools=[check_sum, where, time_left])
    assert not [port for port in agent.outputs.values() if port.call]          # nothing to wire
    graph = GraphBuilder().add(agent).input('in', 'a.task').output('out', 'a.out').compile()
    graph.node('a').time_limit = 100
    assert Engine(graph).run({'in': 'go'}).outputs['out'] == ['done']
    assert answers[:2] == ['yes', 'a'] and answers[2].endswith('seconds') and 'no argument oops' in answers[3]


def test_a_graph_file_names_a_function_tool():
    from interlacing.spec import Catalog, SpecError, dump, load
    described = {'nodes': [{'id': 'a', 'kind': 'agent', 'model': 'm',
                            'tools': [{'function': f'{__name__}.check_sum'}]}],
                 'inputs': {'in': 'a.task'}, 'outputs': {'out': 'a.out'}}
    models = lambda name: FakeProvider(lambda s, m, t: ChatResult(text='x'))     # noqa: E731
    with pytest.raises(SpecError):
        load(described, Catalog(models=models))
    graph = load(described, Catalog(allow=[__name__], models=models))
    tool = graph.node('a').tools[0]
    assert tool.name == 'check_sum' and tool.run({'numbers': [2], 'total': 2}, None) == 'yes'
    again = load(dump(graph), Catalog(allow=[__name__], models=models))
    assert again.digest == graph.digest
