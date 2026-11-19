"""A manager that oversees: a failure of a node it manages comes to it, and the run goes on."""
import pytest

from interlacing import Engine, FunctionNode, GraphBuilder, Port


def flaky(times):
    """Fails the first ``times`` activations, then doubles."""
    tried = []

    def run(x):
        tried.append(x)
        if len(tried) <= times:
            raise ValueError(f'broke on try {len(tried)}')
        return x * 2
    return FunctionNode('m', run, inputs=[Port('x')], outputs=[Port('y')]), tried


def boss(handle, oversees=True):
    def run(ctx, task):
        ctx.emit('work', task)
        while True:
            got = ctx.receive()
            if got is None:
                return None
            for failure in ctx.failures():
                handle(ctx, failure)
            if 'back' in got:
                return {'out': got['back']}
    node = FunctionNode('b', run, inputs=[Port('task'), Port('back', required=False)],
                        outputs=[Port('work'), Port('out')], context=True)
    node.oversees = oversees
    return node


def graph(manager, member):
    return (GraphBuilder().add(manager, member).connect('b.work', 'm.x')
            .connect('m.y', 'b.back', back=True, max_rounds=3).manage('b', 'm')
            .input('in', 'b.task').output('out', 'b.out').compile())


@pytest.mark.parametrize('workers', [0, 1, 4])
def test_the_manager_hears_of_the_failure_and_restarts_the_member(workers):
    seen = []
    member, tried = flaky(1)

    def handle(ctx, failure):
        seen.append(failure)
        ctx.control('m', 'restart')
    result = Engine(graph(boss(handle), member), workers=workers).run({'in': 5})
    assert result.outputs['out'] == [10] and result.complete and tried == [5, 5]
    assert seen == [{'node': 'm', 'round': 1, 'error': 'broke on try 1'}]
    assert result.problems[0]['handled'] == 'reported' and result.problems[0]['to'] == 'b'


def test_a_manager_that_does_not_oversee_lets_the_run_fail_as_before():
    member, _ = flaky(1)
    with pytest.raises(Exception, match='broke on try 1'):
        Engine(graph(boss(lambda ctx, f: None, oversees=False), member), workers=2).run({'in': 5})


def test_a_failure_the_manager_never_takes_up_fails_the_run():
    member, _ = flaky(1)

    def run(ctx, task):
        ctx.emit('work', task)
        ctx.receive()                      # woken by the failure, and returns without looking at it
        return {'out': 'done'}
    lazy = FunctionNode('b', run, inputs=[Port('task'), Port('back', required=False)],
                        outputs=[Port('work'), Port('out')], context=True)
    lazy.oversees = True
    with pytest.raises(Exception, match='did not take it up'):
        Engine(graph(lazy, member), workers=2).run({'in': 5})


def test_the_manager_may_fail_the_run_itself():
    member, _ = flaky(1)

    def handle(ctx, failure):
        raise RuntimeError(f"giving up: {failure['error']}")
    with pytest.raises(Exception, match='giving up: broke on try 1'):
        Engine(graph(boss(handle), member), workers=2).run({'in': 5})


def test_a_manager_that_is_not_running_cannot_be_told():
    member, _ = flaky(1)
    early = FunctionNode('b', lambda task: {'work': task}, inputs=[Port('task'), Port('back', required=False)],
                         outputs=[Port('work'), Port('out')])
    early.oversees = True
    with pytest.raises(Exception, match='broke on try 1'):
        Engine(graph(early, member), workers=2).run({'in': 5})


def test_oversees_in_a_graph_file():
    from interlacing.spec import dump, load
    member, _ = flaky(0)
    manager = boss(lambda ctx, f: None)
    described = dump(graph(manager, member))
    assert next(n for n in described['nodes'] if n['id'] == 'b')['oversees'] is True
    assert 'oversees' not in next(n for n in described['nodes'] if n['id'] == 'm')
    with pytest.raises(Exception, match='oversees is true or false'):
        load({'nodes': [{'id': 'a', 'kind': 'agent', 'model': 'x', 'oversees': 'yes'}]})
