"""What a node sends is checked, and the port says what happens when a check fails: the node fails, the
problem is let through with a warning, or the node is told and puts it right. Files travel as paths."""
import io
import json

import pytest

from inter1ace import cli
from inter1ace.base import GraphBuilder, GraphError, Invalid, Port, PortError, ProtocolError
from inter1ace.engine import Engine, Journal, RunError
from inter1ace.methods import Chain
from inter1ace.models import ChatResult, FakeProvider
from inter1ace.nodes import AgentNode, FunctionNode, Tool
from inter1ace.protocols import DEFAULT, File, FileOf, standard, with_files
from inter1ace.runs import Run
from inter1ace.spec import Catalog, dump, load

HERE = __name__


def fn(id, function, inputs=('x',), outputs=('y',), **options):
    ports = lambda names: [p if isinstance(p, Port) else Port(p) for p in names]   # noqa: E731
    return FunctionNode(id, function, inputs=ports(inputs), outputs=ports(outputs), **options)


def positive(value):
    return None if value > 0 else f'{value} is not positive'


def even(value):
    return value % 2 == 0


def strict(value):
    if value > 100:
        raise ValueError('too large')


def echo(x):
    return x


def say(text='', **calls):
    return ChatResult(text=text, tool_calls=[{'id': f'c{i}', 'name': name, 'args': args} for i, (name, args) in enumerate(calls.items())])


def scripted(*answers):
    left, seen = list(answers), []

    def policy(system, messages, tools):
        seen.append([dict(m) for m in messages])
        answer = left.pop(0)
        return answer(messages) if callable(answer) else answer
    provider = FakeProvider(policy)
    provider.seen = seen
    return provider


# ---- the three ways a port deals with a problem -----------------------------------------------------------

def test_by_default_a_failed_check_fails_the_node_and_the_message_says_which_port_and_why():
    graph = Chain(fn('a', echo, outputs=[Port('y', int, check=(positive, even, strict))]), fn('b', echo)).build(DEFAULT)
    assert Engine(graph, DEFAULT).run({'in': 4}).last('out') == 4
    for value, why in ((-2, 'y: -2 is not positive'), (3, 'y: even does not hold'), (200, 'y: too large'), ('x', 'y: expected int, got str'), (2.5, 'y: expected int, got float')):
        with pytest.raises(RunError, match=f'node a failed in round 1: {why}') as failure:
            Engine(graph, DEFAULT).run({'in': value})
        assert failure.value.node == 'a' and failure.value.problems == []


def test_a_port_that_warns_lets_the_payload_through_and_the_problem_is_on_record(tmp_path):
    seen = []
    graph = Chain(fn('a', echo, outputs=[Port('y', int, check=positive, invalid='warn')]), fn('b', echo)).build(DEFAULT)
    result = Engine(graph, DEFAULT, journal=Journal(tmp_path / 'j'), observe=seen.append).run({'in': -5})
    problem = {'node': 'a', 'round': 1, 'port': 'y', 'problem': '-5 is not positive', 'handled': 'warned'}
    assert result.last('out') == -5 and result.problems == [problem]
    assert {'event': 'warned', 'node': 'a', 'round': 1, 'port': 'y', 'problem': '-5 is not positive'} in seen
    assert [e['event'] for e in seen].index('warned') < [e['event'] for e in seen].index('emitted')
    assert Engine(graph, DEFAULT, journal=Journal(tmp_path / 'j')).resume().problems == [problem]      # it is in the journal
    assert Engine(graph, DEFAULT).run({'in': 5}).problems == []
    wrong_shape = Chain(fn('a', echo, outputs=[Port('y', int, invalid='warn')])).build(DEFAULT)
    result = Engine(wrong_shape, DEFAULT).run({'in': 'text'})
    assert result.last('out') == 'text' and result.problems[0]['problem'] == 'expected int, got str'


def test_a_node_that_lets_the_problem_go_is_run_again_and_told_what_was_wrong():
    tries = []

    def count(ctx, x):
        tries.append(list(ctx.problems))
        return x + len(tries)
    seen = []
    graph = Chain(FunctionNode('a', count, context=True, inputs=[Port('x')], outputs=[Port('y', check=even, repairs=3)])).build(DEFAULT)
    result = Engine(graph, DEFAULT, observe=seen.append).run({'in': 2})                    # 3 is odd, 4 is even
    assert result.last('out') == 4 and tries == [[], ['y: even does not hold']] and result.rounds == {'a': 1}
    assert result.problems == [{'node': 'a', 'round': 1, 'port': 'y', 'problem': 'even does not hold', 'handled': 'rejected'}]
    assert [e['event'] for e in seen] == ['started', 'rejected', 'emitted', 'finished', 'ended']

    always_odd = Chain(fn('a', lambda x: 3, outputs=[Port('y', check=even, repairs=2)])).build(DEFAULT)
    with pytest.raises(RunError, match=r'node a failed in round 1: y: even does not hold \(after 2 corrections\)') as failure:
        Engine(always_odd, DEFAULT).run({'in': 0})
    assert [p['handled'] for p in failure.value.problems] == ['rejected', 'rejected']
    and_then_warn = Chain(fn('a', lambda x: 3, outputs=[Port('y', check=even, repairs=1, invalid='warn')])).build(DEFAULT)
    result = Engine(and_then_warn, DEFAULT).run({'in': 0})
    assert result.last('out') == 3 and [p['handled'] for p in result.problems] == ['rejected', 'warned']


def test_a_node_that_has_already_sent_something_is_not_run_again():
    def two(ctx, x):
        ctx.emit('first', x)
        ctx.emit('second', x)
    node = FunctionNode('a', two, context=True, inputs=[Port('x')], outputs=[Port('first'), Port('second', check=even, repairs=5)])
    graph = GraphBuilder().add(node).input('in', 'a.x').output('one', 'a.first').output('two', 'a.second').compile(DEFAULT)
    with pytest.raises(RunError, match='node a failed in round 1: second: even does not hold'):
        Engine(graph, DEFAULT).run({'in': 3})

    def careful(ctx, x):
        try:
            ctx.emit('second', x)
        except Invalid as problem:                     # a node that can put it right does so itself
            assert problem.again and problem.port == 'second' and problem.problem == 'even does not hold'
            ctx.emit('second', x + 1)
    node = FunctionNode('a', careful, context=True, inputs=[Port('x')], outputs=[Port('first'), Port('second', check=even, repairs=1)])
    graph = GraphBuilder().add(node).input('in', 'a.x').output('two', 'a.second').compile(DEFAULT)
    assert Engine(graph, DEFAULT).run({'in': 3}).last('two') == 4


def test_what_a_port_may_be_told():
    for wrong in ({'invalid': 'ignore'}, {'repairs': -1}, {'repairs': 1.5}, {'check': 'positive'}, {'check': (positive, 3)}):
        with pytest.raises(PortError):
            Port('y', **wrong)
    described = Port('y', int, check=positive, invalid='warn', repairs=2).describe()
    assert described['check'] == [f'{HERE}.positive'] and described['invalid'] == 'warn' and described['repairs'] == 2
    assert set(Port('y', int).describe()) == {'name', 'schema', 'required', 'many', 'call'}      # nothing new unless used


# ---- files travel as paths ----------------------------------------------------------------------------------

def writer(root):
    def write(x):
        if x.get('text') is not None:
            (root / x['path']).parent.mkdir(parents=True, exist_ok=True)
            (root / x['path']).write_text(x['text'], encoding='utf-8')
        return x['path']
    return write


def file_chain(root, source=Port('y'), target=Port('x'), protocols=None, reader=echo):
    protocols = protocols or with_files(DEFAULT, root)
    graph = (GraphBuilder().add(fn('write', writer(root), outputs=[source]), fn('read', reader, inputs=[target]))
             .connect('write.y', 'read.x', 'file').input('in', 'write.x').output('out', 'read.y').compile(protocols))
    return graph, protocols


def test_a_file_travels_as_its_path_with_what_it_was_when_it_was_sent(tmp_path):
    graph, protocols = file_chain(tmp_path)
    got = Engine(graph, protocols, journal=Journal(tmp_path / 'j.jsonl')).run({'in': {'path': 'out/report.md', 'text': 'hello'}}).last('out')
    assert got == {'path': 'out/report.md', 'size': 5, 'media': 'text/markdown',
                   'sha256': '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'}
    assert protocols.get('file').locate(got) == tmp_path / 'out' / 'report.md'
    assert '"sent":[["write.y>read.x",1,1,["v",{"path":"out/report.md"' in (tmp_path / 'j.jsonl').read_text()   # plain data
    note = fn('write', lambda x: {'path': 'a.txt', 'note': 'first draft'})
    (tmp_path / 'a.txt').write_text('x')
    graph = GraphBuilder().add(note, fn('read', echo)).connect('write.y', 'read.x', 'file').input('in', 'write.x').output('out', 'read.y').compile(protocols)
    assert Engine(graph, protocols).run({'in': 1}).last('out')['note'] == 'first draft'           # what else was in the mapping goes along


@pytest.mark.parametrize('sent, why', [
    ({'path': 'missing.txt'}, r'there is no file missing.txt in the working directory \(the working directory holds: here.txt, sub\)'),
    ({'path': 'sub/missing.txt'}, r'there is no file sub/missing.txt in the working directory \(sub is empty\)'),
    ({'path': 'nowhere/x.txt'}, 'there is no directory nowhere either'),
    ({'path': '../outside.txt'}, 'leads outside the working directory'),
    ({'path': '/etc/hostname'}, 'is an absolute path: give the path relative to the working directory'),
    ({'path': 'C:\\\\x.txt'}, 'is an absolute path'),
    ({'path': 'sub'}, 'sub is a directory, not a file'),
    ({'path': 'empty.txt', 'text': ''}, 'empty.txt is empty'),
    ({'path': ''}, 'expected the path of a file'),
])
def test_a_path_that_does_not_lead_to_a_file_is_a_problem_and_the_message_helps(tmp_path, sent, why):
    root = tmp_path / 'work'
    (root / 'sub').mkdir(parents=True)
    (root / 'here.txt').write_text('x')
    (tmp_path / 'outside.txt').write_text('x')
    graph, protocols = file_chain(root)
    with pytest.raises(RunError, match=f'node write failed in round 1: y: .*{why}'):
        Engine(graph, protocols).run({'in': sent})


def test_a_link_out_of_the_working_directory_is_refused(tmp_path):
    root = tmp_path / 'work'
    root.mkdir()
    (tmp_path / 'secret.txt').write_text('x')
    (root / 'link.txt').symlink_to(tmp_path / 'secret.txt')
    graph, protocols = file_chain(root)
    with pytest.raises(RunError, match='link.txt leads outside the working directory'):
        Engine(graph, protocols).run({'in': {'path': 'link.txt'}})


def test_a_port_says_what_file_it_carries(tmp_path):
    pictures = Port('y', FileOf('.png', '.jpg', max_size=10, min_size=2))
    graph, protocols = file_chain(tmp_path, source=pictures)
    run = lambda **sent: Engine(graph, protocols).run({'in': sent}).last('out')   # noqa: E731
    assert run(path='a.png', text='12345')['media'] == 'image/png'
    for sent, why in (({'path': 'a.txt', 'text': '12345'}, r'a.txt is not a .png or .jpg file'),
                      ({'path': 'b.png', 'text': '1' * 11}, 'b.png is 11 bytes, more than the 10 allowed'),
                      ({'path': 'c.png', 'text': '1'}, 'c.png is 1 bytes, fewer than the 2 required')):
        with pytest.raises(RunError, match=why):
            run(**sent)
    assert FileOf('.png', '.jpg').accepts(FileOf('.png')) and not FileOf('.png').accepts(FileOf('.png', '.gif'))
    assert FileOf(media='image/*').accepts(FileOf('.png')) and not FileOf(media='image/*').accepts(FileOf('.txt'))
    assert FileOf('.png', max_size=5).describe() == {'file': {'suffixes': ['.png'], 'max_size': 5}}
    with pytest.raises(GraphError, match='carries int, and a file edge carries the path of a file'):
        file_chain(tmp_path, source=Port('y', int))
    with pytest.raises(GraphError, match='does not accept the files'):
        file_chain(tmp_path, source=Port('y', FileOf('.txt')), target=Port('x', FileOf('.png')))


def test_a_missing_file_can_be_let_through_with_a_warning_and_the_receiver_is_told(tmp_path):
    graph, protocols = file_chain(tmp_path, source=Port('y', invalid='warn'))
    result = Engine(graph, protocols).run({'in': {'path': 'later.txt'}})
    assert result.last('out') == {'path': 'later.txt', 'media': 'text/plain', 'unchecked': True}
    assert result.problems[0]['handled'] == 'warned' and 'there is no file later.txt' in result.problems[0]['problem']
    not_a_path = Chain(fn('a', lambda x: 7, outputs=[Port('y', invalid='warn')]), fn('b', echo))
    graph = (GraphBuilder().add(fn('a', lambda x: 7, outputs=[Port('y', invalid='warn')]), fn('b', echo))
             .connect('a.y', 'b.x', 'file').input('in', 'a.x').output('out', 'b.y').compile(protocols))
    with pytest.raises(RunError, match='and it cannot be sent on as it is: expected the path of a file'):
        Engine(graph, protocols).run({'in': 1})                    # a warning cannot make a path of a number
    assert not_a_path is not None


def test_a_file_that_changed_on_the_way_is_noticed_where_it_arrives(tmp_path):
    def spoil(x):
        (tmp_path / 'a.txt').write_text('changed!')
        return x
    middle = fn('spoil', spoil)

    def build(target):
        return (GraphBuilder().add(fn('write', writer(tmp_path)), middle, fn('read', lambda x, go: x, inputs=[target, Port('go')]))
                .connect('write.y', 'read.x', 'file').connect('write.y', 'spoil.x', 'file').connect('spoil.y', 'read.go', 'value')
                .input('in', 'write.x').output('out', 'read.y').compile(with_files(DEFAULT, tmp_path)))
    protocols = with_files(DEFAULT, tmp_path)
    with pytest.raises(RunError, match='node read failed in round 1: x: a.txt has changed since it was sent: it was 5 bytes and is 8'):
        Engine(build(Port('x')), protocols).run({'in': {'path': 'a.txt', 'text': 'first'}})
    result = Engine(build(Port('x', invalid='warn')), protocols).run({'in': {'path': 'a.txt', 'text': 'first'}})
    assert result.last('out')['unchecked'] is True and result.problems[0]['node'] == 'read' and result.problems[0]['handled'] == 'warned'
    same_size = File(tmp_path)
    body = same_size.encode('a.txt', Port('y'))
    (tmp_path / 'a.txt').write_text('CHANGED!')
    with pytest.raises(ProtocolError, match='its content is different'):
        same_size.decode(body, Port('x'))
    (tmp_path / 'a.txt').unlink()
    with pytest.raises(ProtocolError, match='was there when it was sent and is gone now'):
        same_size.decode(body, Port('x'))
    assert 'sha256' not in File(tmp_path, hash=False, empty=True).encode({'path': 'j'}, Port('y')) if (tmp_path / 'j').write_text('') == 0 else False
    assert standard(files=tmp_path).get('file').root == tmp_path.resolve() and standard().get('file') is None


# ---- an agent puts its own output right ---------------------------------------------------------------------

def test_an_agent_is_told_what_is_wrong_with_its_file_and_writes_it(tmp_path):
    def write(x):
        (tmp_path / x['path']).write_text(x['text'])
        return 'written'
    model = scripted(say('report.md'), say(write={'path': 'report.md', 'text': '# done'}), say('report.md'))
    agent = AgentNode('agent', model, tools=[Tool('write')])
    protocols = with_files(DEFAULT, tmp_path)
    graph = (GraphBuilder().add(agent, fn('disk', write), fn('reader', echo)).connect('agent.write', 'disk.x', 'call')
             .connect('agent.out', 'reader.x', 'file').input('in', 'agent.task').output('out', 'reader.y').compile(protocols))
    result = Engine(graph, protocols).run({'in': 'write the report'})
    assert result.last('out')['path'] == 'report.md' and result.last('out')['size'] == 6
    told = model.seen[1][-1]
    assert told['role'] == 'user' and 'there is no file report.md in the working directory' in told['content']
    assert [p['handled'] for p in result.problems] == ['rejected'] and result.rounds['agent'] == 1


def test_an_agent_gets_two_corrections_unless_told_otherwise_and_json_counts_too(tmp_path):
    protocols = with_files(DEFAULT, tmp_path)

    def build(agent):
        return (GraphBuilder().add(agent, fn('reader', echo)).connect('agent.out', 'reader.x', 'file')
                .input('in', 'agent.task').output('out', 'reader.y').compile(protocols))
    with pytest.raises(RunError, match=r'node agent failed in round 1: out: there is no file nope.md .*\(after 2 corrections\)'):
        Engine(build(AgentNode('agent', scripted(*[say('nope.md')] * 3))), protocols).run({'in': 'go'})
    with pytest.raises(RunError, match='node agent failed in round 1: out: there is no file nope.md'):
        Engine(build(AgentNode('agent', scripted(say('nope.md')), repairs=0)), protocols).run({'in': 'go'})
    model = scripted(say('seven'), say('{"score": 7}'))
    result = Engine(Chain(AgentNode('agent', model, json=True)).build(DEFAULT), DEFAULT).run({'in': 'rate'})
    assert result.last('out') == {'score': 7} and "not JSON" in model.seen[1][-1]['content']
    chooser = scripted(say(send_good={'content': 3}), say(send_good={'content': 4}), say('done'))
    agent = AgentNode('agent', chooser, outputs=[Port('good', check=even), Port('other')])
    graph = GraphBuilder().add(agent).input('in', 'agent.task').output('out', 'agent.good').compile(DEFAULT)
    assert Engine(graph, DEFAULT).run({'in': 'go'}).last('out') == 4
    assert chooser.seen[1][-1]['content'].startswith('not accepted: even does not hold')


def test_an_agent_is_shown_a_picture_that_arrives_as_a_file(tmp_path):
    (tmp_path / 'plot.png').write_bytes(b'\x89PNG fake')
    model = scripted(say('a plot'))
    protocols = with_files(DEFAULT, tmp_path)
    graph = (GraphBuilder().add(fn('draw', lambda x: 'plot.png'), AgentNode('eye', model, files=tmp_path))
             .connect('draw.y', 'eye.task', 'file').input('in', 'draw.x').output('out', 'eye.out').compile(protocols))
    assert Engine(graph, protocols).run({'in': 1}).last('out') == 'a plot'
    message = model.seen[0][-1]
    assert message['image_data'][0][0] == 'image/png' and 'plot.png' in message['content'] and '[image 1]' in message['content']


# ---- written as data, and run from the command line -----------------------------------------------------------

def make(x):
    return 'out/result.csv'


def test_checks_and_files_can_be_written_as_data_and_a_run_has_its_working_directory(tmp_path):
    description = {
        'nodes': [{'id': 'make', 'kind': 'function', 'function': f'{HERE}.make', 'inputs': ['x'],
                   'outputs': [{'name': 'y', 'schema': {'file': {'suffixes': ['.csv'], 'max_size': 1000}}, 'invalid': 'warn', 'repairs': 1}]},
                  {'id': 'use', 'kind': 'function', 'function': f'{HERE}.echo', 'inputs': [{'name': 'x', 'invalid': 'warn'}],
                   'outputs': [{'name': 'y', 'check': f'{HERE}.positive', 'invalid': 'warn'}]}],
        'edges': [{'from': 'make.y', 'to': 'use.x', 'protocol': 'file'}],
        'inputs': {'in': 'make.x'}, 'outputs': {'out': 'use.y'}}
    catalog = Catalog(allow=[HERE], workspace=tmp_path / 'w')
    graph = load(description, catalog)
    out = graph.node('make').outputs['y']
    assert isinstance(out.schema, FileOf) and (out.invalid, out.repairs) == ('warn', 1)
    assert graph.node('use').outputs['y'].check == (positive,)
    assert load(dump(graph), catalog).digest == graph.digest

    run = Run.create(tmp_path / 'run', description, allow=[HERE])
    assert run.workspace() == tmp_path / 'run' / 'files' and run.workspace().is_dir()
    state = run.start({'in': 1})                                    # nothing wrote the file: warned twice over, and it went on
    assert state['status'] == 'failed' or state['status'] == 'done'
    handled = [(p['node'], p['handled']) for p in state['problems']]
    assert ('make', 'rejected') in handled and ('make', 'warned') in handled
    (tmp_path / 'g.json').write_text(json.dumps(description), encoding='utf-8')
    (tmp_path / 'mine' / 'out').mkdir(parents=True)
    (tmp_path / 'mine' / 'out' / 'result.csv').write_text('a,b\n1,2\n')
    out = io.StringIO()
    code = cli.main(['run', str(tmp_path / 'g.json'), '--dir', str(tmp_path / 'r2'), '--allow', HERE, '--input', 'in=1',
                     '--workspace', str(tmp_path / 'mine')], out)
    said = json.loads(out.getvalue())
    assert code == 0 and said['status'] == 'done' and said['outputs']['out'][0]['path'] == 'out/result.csv'
    assert [(p['node'], p['handled']) for p in said['problems']] == [('use', 'warned')]      # positive() cannot judge a mapping
    assert 'positive could not judge it: TypeError' in said['problems'][0]['problem']
    out = io.StringIO()
    assert cli.main(['check', str(tmp_path / 'g.json'), '--allow', HERE], out) == 0


def test_a_called_node_whose_answer_fails_a_check_fails_the_call_not_the_caller_twice():
    calls = []

    def caller(ctx, x):
        calls.append(x)
        return ctx.call('ask', x)
    asked = fn('asked', lambda x: x, outputs=[Port('y', check=even, repairs=3)])
    node = FunctionNode('caller', caller, context=True, inputs=[Port('x')], outputs=[Port('y'), Port('ask', call=True)])
    graph = (GraphBuilder().add(node, asked).connect('caller.ask', 'asked.x', 'call')
             .input('in', 'caller.x').output('out', 'caller.y').compile(DEFAULT))
    assert Engine(graph, DEFAULT).run({'in': 2}).last('out') == 2
    with pytest.raises(RunError, match='node caller failed in round 1: asked could not answer: y: even does not hold'):
        Engine(graph, DEFAULT).run({'in': 3})
    assert calls == [2, 3]                                          # the caller ran once each time


def test_a_sender_cannot_say_that_its_file_needs_no_checking(tmp_path):
    from inter1ace.base import Port
    from inter1ace.protocols import File
    (tmp_path / 'a.txt').write_text('abc')
    body = File(tmp_path).encode({'path': 'a.txt', 'unchecked': True, 'size': 999, 'sha256': 'no', 'note': 'kept'}, Port('y'))
    assert 'unchecked' not in body and body['size'] == 3 and body['sha256'] != 'no' and body['note'] == 'kept'
