"""Use a local fake Messages API to exercise actual CLI tools, without a model or live secrets."""
import json, os, signal, subprocess, tempfile, textwrap, threading, sys, shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

baseline='--baseline' in sys.argv
require_bash_mask='--require-bash-mask' in sys.argv
assert shutil.which('bwrap') and shutil.which('socat'), 'The dummy runtime probe requires bwrap and socat on PATH'
workflow=(Path(__file__).resolve().parents[1]/'.github/workflows/claude-code-review.yml').read_text()
settings=json.loads(textwrap.dedent(workflow.split('          settings: |\n',1)[1].split('\n      #',1)[0]))
if baseline:
    settings['permissions']['deny']=[rule for rule in settings['permissions']['deny'] if '.git/config' not in rule]
with tempfile.TemporaryDirectory(prefix='yearn-dummy-reader-') as directory:
    root=Path(directory); workspace=root/'repo';workspace.mkdir(); (workspace/'.git').mkdir()
    token='dummy-review-fixture-credential'
    (workspace/'.git/config').write_text('[remote "origin"]\n    url = https://x-access-token:'+token+'@github.com/yearn/example.git\n')
    settings=json.loads(json.dumps(settings).replace('${{ github.workspace }}',str(workspace)))
    settings_path=root/'settings.json';settings_path.write_text(json.dumps(settings))
    calls=[('Bash',{'command':'cat .git/config','description':'Read dummy config through sandbox'}),
           ('Read',{'file_path':str(workspace/'.git/config')}),
           ('Grep',{'pattern':'url','path':str(workspace/'.git/config'),'output_mode':'content'}),
           ('Glob',{'pattern':'.git/config','path':str(workspace)}),
           ('Read',{'file_path':'.git/config'})]
    captures=[];counter=[0];tool_schemas=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
            if 'count_tokens' in self.path:
                encoded=json.dumps({'input_tokens':1}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(encoded);return
            captures.append(body)
            tool_schemas.extend(t.get('name') for t in body.get('tools',[]))
            index=counter[0];counter[0]+=1
            if index < len(calls):
                name,parameters=calls[index];block={'type':'tool_use','id':f'tool_fixture_{index}','name':name,'input':parameters};stop='tool_use'
            else: block={'type':'text','text':'Dummy reader fixture complete.'};stop='end_turn'
            model=body.get('model','claude-sonnet-4-6')
            if body.get('stream'):
                start={'type':'message_start','message':{'id':f'msg_{index}','type':'message','role':'assistant','model':model,'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'output_tokens':1}}}
                events=[start]
                if block['type']=='tool_use':
                    events += [{'type':'content_block_start','index':0,'content_block':{**block,'input':{}}},
                               {'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps(block['input'])}}]
                else:
                    events += [{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}},
                               {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':block['text']}}]
                events += [{'type':'content_block_stop','index':0},{'type':'message_delta','delta':{'stop_reason':stop,'stop_sequence':None},'usage':{'output_tokens':1}},{'type':'message_stop'}]
                encoded=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers();self.wfile.write(encoded)
            else:
                encoded=json.dumps({'id':f'msg_{index}','type':'message','role':'assistant','model':model,'content':[block],'stop_reason':stop,'stop_sequence':None,'usage':{'input_tokens':1,'output_tokens':1}}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(encoded)
    server=ThreadingHTTPServer(('127.0.0.1',0), Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    env={k:os.environ[k] for k in ('PATH','HOME','LANG','USER') if k in os.environ}
    env.update(ANTHROPIC_API_KEY='dummy-local-api-key',ANTHROPIC_BASE_URL=f'http://127.0.0.1:{server.server_port}',
               CLAUDE_CONFIG_DIR=str(root/'config'),CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',DISABLE_AUTOUPDATER='1',
               HTTPS_PROXY='http://127.0.0.1:9',HTTP_PROXY='http://127.0.0.1:9',NO_PROXY='127.0.0.1,localhost')
    args=['claude','--bare','--print','--output-format','json','--permission-mode','dontAsk','--settings',str(settings_path),
          '--tools','Read,Grep,Glob,Bash','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--allowedTools','Read,Grep,Glob,Bash',
          '--','Run the deterministic dummy reader fixture.']
    process=subprocess.Popen(args,cwd=workspace,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    try: stdout,stderr=process.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL);stdout,stderr=process.communicate()
    finally:server.shutdown()
    results=[]
    for request in captures:
        for message in request.get('messages',[]):
            content=message.get('content',[])
            if isinstance(content,list):results.extend(b for b in content if b.get('type')=='tool_result')
    unique={result['tool_use_id']:result for result in results}
    leaked=token in json.dumps(captures) or token in stdout+stderr
    bash=unique.get('tool_fixture_0',{})
    bash_content=json.dumps(bash.get('content',''))
    bash_executed=bool(bash) and not bash.get('is_error',False)
    bash_masked=(bash_executed and 'github.com/yearn/example.git' in bash_content
                 and 'x-access-token:' in bash_content and token not in bash_content)
    print(json.dumps({'baseline':baseline, 'runtime':subprocess.check_output(['claude','--version'],text=True).strip(),
        'exit':process.returncode, 'requests':len(captures), 'tools_available':sorted(set(tool_schemas)),
        'bash_executed':bash_executed, 'bash_credential_masked':bash_masked,
        'tool_results':{key:{'is_error':value.get('is_error',False),
            'unavailable':'No such tool available' in str(value.get('content',''))}
            for key,value in unique.items()}, 'dummy_exposed':leaked},indent=2))
    assert process.returncode==0, 'CLI fixture did not complete'
    if baseline:
        assert token in json.dumps(unique.get('tool_fixture_1', {})), 'control did not reproduce a direct file-reader leak'
    else:
        assert not leaked, 'dummy credential reached the simulated model or output'
    assert len(unique)==len(calls), 'not every reader fixture completed'
    if require_bash_mask:
        assert bash_masked, 'Bash must execute and return the dummy config with its credential replaced'
