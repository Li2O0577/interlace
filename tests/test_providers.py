"""The two model interfaces against a service that stands in for the real ones: what is sent, what is read,
and what happens when the service is busy, broken or slow to answer."""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from interlacing import DEFAULT, AgentNode, Engine, FunctionNode, GraphBuilder, Port, Tool
from interlacing.models import providers
from interlacing.models.providers import AnthropicProvider, OpenAICompatProvider, ProviderError


class Service:
    """Answers each request with the next of ``answers``: ``(status, body)`` or ``(status, body, headers)``.
    A body that is a list is sent as a stream of events. What was asked is kept in ``asked``."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                service.asked.append({'path': self.path, 'body': body, 'headers': {name.lower(): value for name, value in self.headers.items()}})
                status, answer, *more = service.answers.pop(0) if service.answers else (500, {'error': 'nothing left to say'})
                if isinstance(answer, list):
                    data = b''.join(f'data: {json.dumps(event)}\n\n'.encode() for event in answer)
                    kind = 'text/event-stream'
                else:
                    data, kind = json.dumps(answer).encode(), 'application/json'
                self.send_response(status)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(data)))
                for name, value in (more[0] if more else {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def service():
    made = []

    def make(*answers):
        made.append(Service(*answers))
        return made[-1]
    yield make
    for one in made:
        one.close()


@pytest.fixture
def waits(monkeypatch):
    """The waits between attempts, recorded instead of slept."""
    slept = []
    monkeypatch.setattr(providers.time, 'sleep', slept.append)
    return slept


def said(text='ok', **more):
    return {'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn', 'usage': {'input_tokens': 7, 'output_tokens': 3}, **more}


def chose(text='ok', **message):
    return {'choices': [{'message': {'content': text, **message}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 7, 'completion_tokens': 3}}


USER = [{'role': 'user', 'content': 'hello'}]


# ---- a busy or broken service ---------------------------------------------------------------------------

@pytest.mark.parametrize('make, good', [(lambda url: AnthropicProvider('key', 'm', url), said()),
                                        (lambda url: OpenAICompatProvider('key', 'm', url), chose())])
def test_a_busy_service_is_asked_again_after_the_wait_it_names(service, waits, make, good):
    there = service((429, {'error': 'slow down'}, {'Retry-After': '2'}), (529, {'error': 'overloaded'}), (200, good))
    result = make(there.url).chat('system', USER, [])
    assert result.text == 'ok' and (result.input_tokens, result.output_tokens) == (7, 3)
    assert len(there.asked) == 3 and waits == [2.0, providers.RETRY_WAITS[1]]


def test_a_wait_of_nothing_is_taken_at_its_word(service, waits):
    there = service((429, {}, {'Retry-After': '0'}), (200, said()))
    assert AnthropicProvider('key', 'm', there.url).chat('', USER, []).text == 'ok' and waits == [0.0]


def test_a_service_that_stays_busy_fails_after_the_attempts_allowed(service, waits):
    there = service(*[(503, {'error': 'down'})] * 10)
    provider = OpenAICompatProvider('key', 'm', there.url)
    with pytest.raises(ProviderError, match='answered 503'):
        provider.chat('', USER, [])
    assert len(there.asked) == provider.attempts and len(waits) == provider.attempts - 1


def test_a_request_the_service_refuses_is_not_asked_again(service, waits):
    there = service((401, {'error': 'wrong key'}), (200, said()))
    with pytest.raises(ProviderError, match='answered 401'):
        AnthropicProvider('wrong', 'm', there.url).chat('', USER, [])
    assert len(there.asked) == 1 and waits == []


def test_a_service_that_cannot_be_reached_is_tried_again_then_given_up(waits):
    provider = AnthropicProvider('key', 'm', 'http://127.0.0.1:9')
    provider.attempts = 2
    with pytest.raises(ProviderError, match='cannot reach|connection'):
        provider.chat('', USER, [])
    assert len(waits) == 1


def test_an_answer_that_is_not_json_fails_at_once(service, waits):
    there = service((200, [{'not': 'what was asked for'}]))        # a stream where a whole answer was expected
    with pytest.raises(ProviderError, match='not JSON'):
        AnthropicProvider('key', 'm', there.url).chat('', USER, [])


# ---- what is sent and what is read ----------------------------------------------------------------------

def test_the_anthropic_interface_sends_tools_and_reads_a_tool_call(service):
    there = service((200, {'content': [{'type': 'tool_use', 'id': 't1', 'name': 'fs__read', 'input': {'path': 'a'}}],
                           'stop_reason': 'tool_use', 'usage': {'input_tokens': 5, 'output_tokens': 2, 'cache_read_input_tokens': 4}}))
    tools = [{'name': 'fs.read', 'description': 'Read a file', 'parameters': {'type': 'object', 'properties': {}}}]
    result = AnthropicProvider('key', 'm', there.url).chat('be brief', USER, tools)
    asked = there.asked[0]
    assert asked['path'] == '/v1/messages' and asked['headers']['x-api-key'] == 'key'
    assert asked['body']['tools'][0]['name'] == 'fs__read' and asked['body']['system'][0]['text'] == 'be brief'
    assert result.tool_calls == [{'id': 't1', 'name': 'fs.read', 'args': {'path': 'a'}}]
    assert (result.input_tokens, result.cached_tokens) == (9, 4)


def test_the_openai_interface_sends_tools_and_reads_a_tool_call(service):
    call = {'id': 'c1', 'type': 'function', 'function': {'name': 'fs__read', 'arguments': '{"path": "a"}'}}
    there = service((200, chose('', tool_calls=[call])))
    tools = [{'name': 'fs.read', 'description': 'Read a file', 'parameters': {'type': 'object', 'properties': {}}}]
    result = OpenAICompatProvider('key', 'm', there.url + '/v1').chat('be brief', USER, tools)
    asked = there.asked[0]
    assert asked['path'] == '/v1/chat/completions' and asked['headers']['authorization'] == 'Bearer key'
    assert asked['body']['messages'][0] == {'role': 'system', 'content': 'be brief'}
    assert asked['body']['tools'][0]['function']['name'] == 'fs__read'
    assert result.tool_calls == [{'id': 'c1', 'name': 'fs.read', 'args': {'path': 'a'}}]


def test_a_picture_is_sent_in_the_form_each_interface_takes(service):
    picture = base64.b64encode(b'not really a picture').decode()
    message = [{'role': 'user', 'content': 'what is this?', 'image_data': [['image/png', picture]]}]
    one, two = service((200, said())), service((200, chose()))
    AnthropicProvider('key', 'm', one.url).chat('', message, [])
    OpenAICompatProvider('key', 'm', two.url).chat('', message, [])
    first = one.asked[0]['body']['messages'][0]['content']
    assert first[0] == {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': picture}}
    assert first[1]['text'] == 'what is this?'
    second = two.asked[0]['body']['messages'][1]['content']
    assert second[0] == {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{picture}'}}


def test_thinking_is_asked_for_read_and_given_back_between_tool_calls(service):
    thought = {'type': 'thinking', 'thinking': 'the file first', 'signature': 'sig'}
    there = service((200, {'content': [thought, {'type': 'tool_use', 'id': 't1', 'name': 'look', 'input': {}}],
                           'stop_reason': 'tool_use', 'usage': {}}),
                    (200, said('done')))
    provider = AnthropicProvider('key', 'm', there.url)
    provider.thinking = 'low'
    agent = AgentNode('agent', provider, tools=[Tool('look', 'Look')])
    look = FunctionNode('look', lambda x: 'seen', inputs=[Port('x')], outputs=[Port('y')])
    graph = (GraphBuilder().add(agent, look).connect('agent.look', 'look.x', 'call')
             .input('in', 'agent.task').output('out', 'agent.out').compile(DEFAULT))
    assert Engine(graph, DEFAULT).run({'in': 'go'}).outputs == {'out': ['done']}
    first, second = (asked['body'] for asked in there.asked)
    assert first['thinking'] == {'type': 'enabled', 'budget_tokens': 2048} and first['max_tokens'] >= 2048 + 4096
    assert second['thinking']['budget_tokens'] == 2048                      # it still thinks: its thinking came back
    assert second['messages'][1]['content'][0] == thought
    assert agent.messages[1]['thinking_blocks'] == [thought]


def test_reasoning_from_an_openai_compatible_service_is_read(service):
    there = service((200, chose('42', reasoning_content='six times seven')))
    result = OpenAICompatProvider('', 'm', there.url).chat('', USER, [])
    assert (result.text, result.reasoning) == ('42', 'six times seven') and 'authorization' not in there.asked[0]['headers']


def test_a_service_that_refuses_reasoning_effort_is_asked_again_without_it(service):
    there = service((400, {'error': 'unknown parameter reasoning_effort'}), (200, chose()))
    provider = OpenAICompatProvider('key', 'picky', there.url)
    provider.thinking = 'high'
    assert provider.chat('', USER, []).text == 'ok'
    assert there.asked[0]['body']['reasoning_effort'] == 'high' and 'reasoning_effort' not in there.asked[1]['body']


# ---- streams --------------------------------------------------------------------------------------------

def test_a_stream_is_passed_on_as_it_arrives(service):
    events = [{'type': 'message_start', 'message': {'usage': {'input_tokens': 4}}},
              {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text'}},
              {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'one '}},
              {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'two'}},
              {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 2}}]
    there = service((200, events))
    heard = []
    result = AnthropicProvider('key', 'm', there.url).chat('', USER, [], on_text=heard.append)
    assert heard == ['one ', 'two'] and result.text == 'one two' and result.output_tokens == 2
    assert there.asked[0]['body']['stream'] is True


def test_a_busy_service_is_asked_again_for_a_stream_too(service, waits):
    events = [{'choices': [{'delta': {'content': 'hi'}}]}, {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
    there = service((429, {}, {'Retry-After': '1'}), (200, events))
    heard = []
    assert OpenAICompatProvider('key', 'm', there.url).chat('', USER, [], on_text=heard.append).text == 'hi'
    assert heard == ['hi'] and waits == [1.0]


def test_a_stream_cut_short_is_not_taken_for_the_answer(service):
    cut = [{'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text'}},
           {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'half an ans'}}]
    there = service((200, cut), (200, cut), (200, said('the whole answer')))
    result = AnthropicProvider('key', 'm', there.url).chat('', USER, [], on_text=lambda text: None)
    assert result.text == 'the whole answer' and len(there.asked) == 3
