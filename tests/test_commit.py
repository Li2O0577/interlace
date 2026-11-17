"""Progress saved in the middle of a long activation: ctx.commit, and going on from it."""
import threading
import time

from interlacing import Engine, GraphBuilder, Journal, Node, Port


class Worker(Node):
    """Does ``steps`` items, sending each one out and saving its progress after each."""

    kind = 'worker'

    def __init__(self, steps, gate=None, save=True):
        super().__init__('w', inputs=[Port('x', required=False)], outputs=[Port('y')])
        self.steps, self.gate, self.save = steps, gate, save
        self.done, self.resumed, self.resets = 0, [], 0

    def run(self, ctx, inputs):
        self.resumed.append(ctx.resumed)
        while self.done < self.steps:
            if not ctx.checkpoint():
                return None
            if self.gate is not None and self.done == 2:
                self.gate.wait(5)
                if not ctx.checkpoint():
                    return None
            ctx.emit('y', self.done)
            self.done += 1
            if self.save:
                ctx.commit()
        return None

    def snapshot(self):
        return {'done': self.done}

    def restore(self, state):
        self.done = state['done']

    def reset(self):
        self.done, self.resets = 0, self.resets + 1


def graph_of(node):
    return GraphBuilder().add(node).input('in', 'w.x').output('out', 'w.y').compile()


def test_a_run_taken_up_goes_on_from_the_progress_saved(tmp_path):
    gate = threading.Event()
    first = Worker(5, gate)
    engine = Engine(graph_of(first), journal=Journal(tmp_path / 'j.jsonl'), workers=1)
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=engine.run({'in': 1})))
    thread.start()
    deadline = time.monotonic() + 5
    while first.done < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    engine.stop()                                   # as if the process ended here
    gate.set()
    thread.join(10)
    assert not box['result'].complete and box['result'].outputs['out'] == [0, 1]
    second = Worker(5)
    result = Engine(graph_of(second), journal=Journal(tmp_path / 'j.jsonl'), workers=1).resume()
    assert result.complete and result.outputs['out'] == [0, 1, 2, 3, 4]      # nothing sent twice
    assert second.resumed == [True] and result.rounds == {'w': 2}      # the one cut short, and this one


def test_without_a_journal_nothing_is_saved_but_nothing_changes():
    node = Worker(3)
    assert Engine(graph_of(node)).run({'in': 1}).outputs['out'] == [0, 1, 2] and node.resumed == [False]


def test_a_stopped_node_that_saved_goes_on_when_switched_on_again():
    gate = threading.Event()
    node = Worker(5, gate)
    keeper = Keeper()
    graph = (GraphBuilder().add(node, keeper).input('in', 'w.x').input('k', 'k.x')
             .output('out', 'w.y').compile())
    engine = Engine(graph, workers=2)
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=engine.run({'in': 1, 'k': 1})))
    thread.start()
    deadline = time.monotonic() + 5
    while node.done < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    engine.control('w', 'stop')
    gate.set()
    time.sleep(0.2)
    engine.control('w', 'resume')
    deadline = time.monotonic() + 5
    while node.done < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    keeper.go.set()
    thread.join(10)
    assert box['result'].outputs['out'] == [0, 1, 2, 3, 4] and node.resumed == [False, True]


def test_a_restart_does_not_go_on_from_what_was_saved():
    gate = threading.Event()
    node = Worker(4, gate)
    keeper = Keeper()
    graph = GraphBuilder().add(node, keeper).input('in', 'w.x').input('k', 'k.x').output('out', 'w.y').compile()
    engine = Engine(graph, workers=2)
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=engine.run({'in': 1, 'k': 1})))
    thread.start()
    deadline = time.monotonic() + 5
    while node.done < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    node.gate = None
    engine.control('w', 'restart')
    gate.set()
    deadline = time.monotonic() + 5
    while (node.resets < 1 or node.done < 4) and time.monotonic() < deadline:
        time.sleep(0.01)
    keeper.go.set()
    thread.join(10)
    assert node.resets == 1 and node.resumed == [False, False]
    assert box['result'].outputs['out'] == [0, 1, 0, 1, 2, 3]


class Keeper(Node):
    """Keeps the run going until told."""

    kind = 'keeper'

    def __init__(self):
        super().__init__('k', inputs=[Port('x')], outputs=[])
        self.go = threading.Event()

    def run(self, ctx, inputs):
        while not self.go.is_set() and ctx.checkpoint():
            time.sleep(0.01)
