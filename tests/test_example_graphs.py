"""The examples under examples/graphs run as written."""
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples' / 'graphs'


@pytest.fixture(autouse=True)
def on_path(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLES))
    yield
    for name in ('chain_with_program', 'review_loop', 'governed_tree', 'camera_to_arm', 'files_checked', 'blocks_joined', 'grown_at_run_time', 'planner_and_arm', 'desks_between_blocks'):
        sys.modules.pop(name, None)


def test_chain_with_program(capsys):
    import chain_with_program
    assert chain_with_program.main().outputs == {'out': ['the x3, and x2, cat x1']}


def test_files_checked(tmp_path):
    import files_checked
    result = files_checked.main(tmp_path)
    assert result.last('out') == 'chart.svg (28 bytes) drawn from data/table.csv (25 bytes)'
    assert [(p['node'], p['handled']) for p in result.problems] == [('draw', 'rejected')] and result.rounds['draw'] == 1


def test_blocks_joined():
    import blocks_joined
    result = blocks_joined.main()
    assert result.last('out') == ' | '.join(['One design, three uses.'] * 3) and result.rounds == {'first': 1, 'panel': 1, 'join': 1}


def test_grown_at_run_time(capsys):
    import grown_at_run_time
    result, refused = grown_at_run_time.main()
    assert result.last('out')['outputs'] == {'out': [['mean 6', 'largest 9', 'spread 6']]} and result.rounds == {'plan': 1, 'grow': 1}
    assert 'exit cannot be found' in refused['error']


def test_planner_and_arm(capsys):
    import planner_and_arm
    result, bench = planner_and_arm.main()
    assert result.last('out')['block'] == planner_and_arm.GOAL and bench.moves == 3 and result.used['plans'] == 2
    assert [p['handled'] for p in result.problems] == ['rejected'] and 'must be 5 to 85 mm apart' in result.problems[0]['problem']


def test_desks_between_blocks(capsys):
    import desks_between_blocks
    assert desks_between_blocks.main().last('out') == {
        'count': 2, 'order': ['measured salt at ESTUARY', 'measured tide at HARBOUR']}


def test_review_loop():
    import review_loop
    result = review_loop.main()
    assert result.rounds == {'writer': 3, 'reviewer': 3} and result.last('out').startswith('draft 3:') and result.limited == ()


def test_governed_tree_asks_and_survives_a_restart(tmp_path):
    import governed_tree
    asked = []
    assert governed_tree.main(lambda request: (asked.append(request), {'approved': False})[1]).last('out') == 'held back'
    assert asked == [{'results': ['survey surveyed the bridge', 'build built the bridge']}]

    present = []

    def operator(request):                     # the same operator both times; the first time they are interrupted
        if not present:
            raise KeyboardInterrupt
        return {'approved': True}
    with pytest.raises(KeyboardInterrupt):
        governed_tree.main(operator, tmp_path / 'run.jsonl')
    present.append(True)
    result = governed_tree.main(operator, tmp_path / 'run.jsonl')
    assert result.last('out') == 'released' and result.rounds == {'lead': 3, 'survey': 1, 'build': 1, 'operator': 1}


def test_camera_to_arm():
    import camera_to_arm
    arm, result = camera_to_arm.main()
    assert arm.log[0] == 'connected' and arm.log[-1] == 'disconnected' and arm.position == result.outputs['at'][-1]
    assert 1 <= len(result.outputs['at']) <= 4 and all(0 < at <= 5 for at in result.outputs['at'])
    arm = camera_to_arm.Arm()
    from interlacing.engine import Engine
    from interlacing.protocols import DEFAULT
    steady = Engine(camera_to_arm.build(arm, [{'target': 5}] * 4, speed=2), DEFAULT).run()   # one thread: only the newest frame is seen
    assert steady.outputs['at'] == [2] and arm.log == ['connected', 'moved to 2', 'disconnected']
