import json, os, unittest
from unittest.mock import patch
import app as appmod
from app import extract_json, deepseek_complete, complete_with_headroom

class FakeResponse:
    def __init__(self,status=200,body=None,url='https://api.deepseek.com/chat/completions'):
        self.status_code=status; self._body=body if body is not None else {}
        self.url=url; self.ok=status<400; self.content=b'x' if body is not None else b''
    def json(self): return self._body

def content_response(text):
    return FakeResponse(200,{'choices':[{'message':{'content':text}}]})

def choice_response(text,finish='stop'):
    return FakeResponse(200,{'choices':[{'message':{'content':text},'finish_reason':finish}]})

class DeepSeek(unittest.TestCase):
    def test_request_shape_parse_and_absent_openai_fields(self):
        body=json.dumps({'reply':'ok','clarification_required':False,'changes':[]})
        with patch('app.requests.post',return_value=content_response(body)) as post:
            out=deepseek_complete('SYS','USER',base='https://api.deepseek.com',model='deepseek-v4-pro',key='k')
        self.assertEqual(out,{'reply':'ok','clarification_required':False,'changes':[]})
        args,kwargs=post.call_args
        self.assertEqual(args[0],'https://api.deepseek.com/chat/completions')
        self.assertEqual(kwargs['headers']['Authorization'],'Bearer k')
        payload=kwargs['json']
        self.assertEqual(payload['model'],'deepseek-v4-pro')
        self.assertEqual(payload['response_format'],{'type':'json_object'})
        self.assertEqual(payload['max_tokens'],1600)
        self.assertEqual(payload['temperature'],0)
        self.assertEqual([m['role'] for m in payload['messages']],['system','user'])
        self.assertEqual(payload['messages'][1]['content'],'USER')
        for absent in ('store','reasoning','max_output_tokens','text'):
            self.assertNotIn(absent,payload)
    def test_fenced_json_extraction(self):
        body='```json\n{"reply":"hi","clarification_required":false,"changes":[]}\n```'
        with patch('app.requests.post',return_value=content_response(body)):
            self.assertEqual(deepseek_complete('s','u',base='b',model='m',key='k')['reply'],'hi')
    def test_prose_wrapped_balanced_json(self):
        body='Here is the result: {"reply":"yo","clarification_required":false,"changes":[]} thanks.'
        self.assertEqual(json.loads(extract_json(body))['reply'],'yo')
    def test_unfenced_direct_json(self):
        self.assertEqual(extract_json('{"reply":"x"}'),'{"reply":"x"}')
    def test_non_200_raises_deepseek_provider_code(self):
        with patch('app.requests.post',return_value=FakeResponse(500,{'error':'x'})):
            with self.assertRaisesRegex(RuntimeError,'DEEPSEEK_HTTP_500'):
                deepseek_complete('s','u',base='https://api.deepseek.com',model='m',key='k')
    def test_error_payload_without_choices(self):
        with patch('app.requests.post',return_value=FakeResponse(200,{'error':{'message':'bad'}})):
            with self.assertRaisesRegex(RuntimeError,'MODEL_OUTPUT_INCOMPLETE'):
                deepseek_complete('s','u',base='b',model='m',key='k')
    def test_invalid_json_content(self):
        with patch('app.requests.post',return_value=content_response('not json at all')):
            with self.assertRaisesRegex(RuntimeError,'MODEL_OUTPUT_INCOMPLETE'):
                deepseek_complete('s','u',base='b',model='m',key='k')
    def test_max_tokens_env_override_honoured(self):
        body=json.dumps({'reply':'ok','clarification_required':False,'changes':[]})
        with patch.dict(os.environ,{'DEEPSEEK_MAX_TOKENS':'3200'}):
            with patch('app.requests.post',return_value=content_response(body)) as post:
                deepseek_complete('s','u',base='b',model='m',key='k')
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'],3200)
    def test_max_tokens_defaults_to_1600_when_unset(self):
        env=dict(os.environ); env.pop('DEEPSEEK_MAX_TOKENS',None)
        body=json.dumps({'reply':'ok','clarification_required':False,'changes':[]})
        with patch.dict(os.environ,env,clear=True):
            with patch('app.requests.post',return_value=content_response(body)) as post:
                deepseek_complete('s','u',base='b',model='m',key='k')
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'],1600)
    def test_explicit_max_tokens_override_wins(self):
        body=json.dumps({'reply':'ok','clarification_required':False,'changes':[]})
        with patch.dict(os.environ,{'DEEPSEEK_MAX_TOKENS':'3200'}):
            with patch('app.requests.post',return_value=content_response(body)) as post:
                deepseek_complete('s','u',base='b',model='m',key='k',max_tokens=8000)
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'],8000)
    def test_finish_reason_length_raises_truncated(self):
        with patch('app.requests.post',return_value=choice_response('',  'length')):
            with self.assertRaisesRegex(RuntimeError,'MODEL_OUTPUT_TRUNCATED'):
                deepseek_complete('s','u',base='b',model='m',key='k')
    def test_empty_content_raises_truncated(self):
        with patch('app.requests.post',return_value=choice_response('   ','stop')):
            with self.assertRaisesRegex(RuntimeError,'MODEL_OUTPUT_TRUNCATED'):
                deepseek_complete('s','u',base='b',model='m',key='k')
    def test_headroom_retries_once_on_truncation(self):
        ok={'reply':'ok','clarification_required':False,'changes':[]}
        with patch.dict(os.environ,{'DEEPSEEK_RETRY_MAX_TOKENS':'16000'}):
            with patch('app.deepseek_complete',side_effect=[RuntimeError('MODEL_OUTPUT_TRUNCATED'),ok]) as call:
                out=complete_with_headroom('s','u')
        self.assertEqual(out,ok)
        self.assertEqual(call.call_count,2)
        self.assertEqual(call.call_args_list[1].kwargs.get('max_tokens'),16000)
    def test_headroom_does_not_retry_other_errors(self):
        with patch('app.deepseek_complete',side_effect=RuntimeError('MODEL_OUTPUT_INCOMPLETE')) as call:
            with self.assertRaisesRegex(RuntimeError,'MODEL_OUTPUT_INCOMPLETE'):
                complete_with_headroom('s','u')
        self.assertEqual(call.call_count,1)

    def test_telemetry_recorded_on_success(self):
        body={'choices':[{'message':{'content':'{"reply":"ok","clarification_required":false,"changes":[]}'},'finish_reason':'stop'}],
            'usage':{'completion_tokens':12,'completion_tokens_details':{'reasoning_tokens':5}}}
        with patch('app.requests.post',return_value=FakeResponse(200,body)):
            deepseek_complete('s','u',base='b',model='m',key='k')
        self.assertEqual(appmod.MODEL_TELEMETRY['finish_reason'],'stop')
        self.assertEqual(appmod.MODEL_TELEMETRY['reasoning_tokens'],5)
        self.assertEqual(appmod.MODEL_TELEMETRY['completion_tokens'],12)
        self.assertIn('latency_seconds',appmod.MODEL_TELEMETRY)
        self.assertIsNone(appmod.MODEL_TELEMETRY['error'])
    def test_context_tokens_recorded(self):
        body={'choices':[{'message':{'content':'{"reply":"ok","clarification_required":false,"changes":[]}'},'finish_reason':'stop'}]}
        with patch('app.requests.post',return_value=FakeResponse(200,body)):
            deepseek_complete('SYS'*10,'USER'*20,base='b',model='m',key='k')
        self.assertEqual(appmod.MODEL_TELEMETRY['context_tokens'],(len('SYS'*10)+len('USER'*20))//4)
    def test_telemetry_recorded_on_failure(self):
        with patch('app.requests.post',return_value=choice_response('','length')):
            with self.assertRaisesRegex(RuntimeError,'MODEL_OUTPUT_TRUNCATED'):
                deepseek_complete('s','u',base='b',model='m',key='k')
        self.assertEqual(appmod.MODEL_TELEMETRY['error'],'MODEL_OUTPUT_TRUNCATED')
        self.assertEqual(appmod.MODEL_TELEMETRY['finish_reason'],'length')

if __name__=='__main__': unittest.main()
