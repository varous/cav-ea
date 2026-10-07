import contextlib, datetime as dt, json, pathlib, unittest,os
from unittest.mock import patch, Mock
from operating import import_ledger,validate_changes,apply_changes,export_ledger,referenced_tasks,explicit_completion,digest,now
from app import record_gap,pending_gaps,coverage_for_model,latest_gap_text,brief_run,brief_slot_status,task_evidence
from app import add_outbox,app,Runtime,VisibleHTML

BASE=(pathlib.Path(__file__).parent/'fixtures'/'baseline.md').read_text(encoding='utf-8-sig')
class Controls(unittest.TestCase):
    def completion_state(self):
        state=import_ledger(BASE)
        state['tasks']['T-113']={'id':'T-113','title':'OrgA transfer','status':'OUTSTANDING'}
        return state
    def test_completion_exchange_and_repeat(self):
        state=self.completion_state();text='OrgA transfer is done T-113 is closed'
        result,evidence=explicit_completion(state,text,'thread/one')
        state=apply_changes(state,validate_changes(state,result,evidence),'owner/source')
        self.assertEqual(state['tasks']['T-113']['status'],'COMPLETED')
        self.assertEqual(state['changes'][-1]['evidence_quote'],text)
        self.assertEqual(explicit_completion(state,text,'thread/one')[0]['changes'],[])
    def test_bare_id_resolves_only_same_thread_completion(self):
        state=self.completion_state()
        state['conversation']=[{'thread':'thread/one','clarification_required':True,'owner_message':'OrgA transfer is done T-113 is closed'}]
        state['conversation'].append({'thread':'thread/one','clarification_required':True,'owner_message':'T-113'})
        result,evidence=explicit_completion(state,'T-113','thread/one')
        self.assertEqual(validate_changes(state,result,evidence)[0]['value'],'COMPLETED')
        self.assertIsNone(explicit_completion(state,'T-113','thread/two'))
        state['conversation']=[]
        self.assertIsNone(explicit_completion(state,'T-113','thread/one'))
    def test_completion_does_not_accept_questions_negation_reports_or_future(self):
        state=self.completion_state()
        for text in ('T-113 done?','T-113 is not done','T-113 isn’t done','T-113 will be done','I need T-113 done','PERSON_D says T-113 done','T-113 done if bank approves','T-113 and T-101 done','T-113 done, reassign owner'):
            self.assertIsNone(explicit_completion(state,text,'thread/one'),text)
    def test_processing_completion_bypasses_model_and_deduplicates(self):
        state=self.completion_state()
        runtime=object.__new__(Runtime);runtime.space='spaces/test';runtime.owner='users/owner'
        runtime.__dict__['user']=object()
        runtime.store=type('MemoryStore',(),{'immutable':lambda *a:None,'save':lambda *a:1})()
        runtime.result=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Model should not parse explicit completion'))
        runtime.drain=lambda s,g:(s,g)
        message={'name':'spaces/test/messages/one','sender':{'name':'users/owner','type':'HUMAN'},'thread':{'name':'thread/one'},'text':'OrgA transfer is done T-113 is closed','createTime':'2026-10-06T05:15:00Z'}
        with patch('app.pages',return_value=[message]):
            state,g,count=runtime.recover_updates(state,0)
            self.assertEqual(count,1)
            state,g,count=runtime.recover_updates(state,g)
        self.assertEqual(count,0)
        self.assertEqual(len(state['changes']),1)
        self.assertEqual(len(state['outbox']),1)
    def test_bad_model_format_is_repaired_without_owner_rephrase(self):
        state=self.completion_state();runtime=object.__new__(Runtime)
        runtime.space='spaces/test';runtime.owner='users/owner';runtime.__dict__['user']=object()
        runtime.store=type('MemoryStore',(),{'immutable':lambda *a:None,'save':lambda *a:1})()
        runtime.drain=lambda s,g:(s,g)
        text='T-113 reassign to PERSON_E'
        invalid={'reply':'Changed','clarification_required':False,'changes':[{'kind':'update','task_id':'T-113','field':'status','value':'CLOSED','evidence_quote':text,'reason':'bad format'}]}
        valid={'reply':'T-113 assigned to PERSON_E.','clarification_required':False,'changes':[{'kind':'update','task_id':'T-113','field':'owner','value':'PERSON_E','evidence_quote':text,'reason':'Owner instruction'}]}
        runtime.result=unittest.mock.Mock(side_effect=[invalid,valid])
        message={'name':'spaces/test/messages/two','sender':{'name':'users/owner','type':'HUMAN'},'thread':{'name':'thread/one'},'text':text,'createTime':'2026-10-06T05:15:00Z'}
        with patch('app.pages',return_value=[message]):state,g,count=runtime.recover_updates(state,0)
        self.assertEqual(state['tasks']['T-113']['owner'],'PERSON_E')
        self.assertEqual(state['tasks']['T-113']['status'],'OUTSTANDING')
        self.assertEqual(runtime.result.call_count,2)
        self.assertEqual(runtime.result.call_args.kwargs['validation_error'],'INVALID_TASK_STATUS')
    def test_new_task_keeps_fields_and_allocates_id(self):
        state=import_ledger(BASE);text='Follow up tomorrow morning with PERSON_M for OrgA transfer'
        change={'kind':'create','task_id':None,'field':'title','value':'OrgA transfer follow-up','evidence_quote':text,'reason':'New owner to-do','new_task':{'owner':'PERSON_A','deadline':'2026-10-06 morning, time unknown','priority':'UNKNOWN','details':'Follow up with PERSON_M'}}
        changes=validate_changes(state,{'clarification_required':False,'changes':[change]},text)
        updated=apply_changes(state,changes,'owner message')
        self.assertEqual(len(updated['tasks']),14)
        self.assertEqual(updated['tasks']['T-113']['owner'],'PERSON_A')
        self.assertIn('2026-10-06',updated['tasks']['T-113']['deadline'])
        self.assertEqual(updated['tasks']['T-113']['status'],'OUTSTANDING')
    def test_authority_uses_owner_display_name(self):
        state=import_ledger(BASE);text='Add a follow-up with PERSON_M to my list'
        change={'kind':'create','task_id':None,'field':'title','value':'Follow up with PERSON_M','evidence_quote':text,'reason':'New owner to-do','new_task':{'owner':'NOT YET ASSIGNED','deadline':'UNKNOWN','priority':'UNKNOWN','details':''}}
        changes=validate_changes(state,{'clarification_required':False,'changes':[change]},text)
        named=apply_changes(state,changes,'owner message','Alex Example')
        self.assertEqual(named['changes'][-1]['authority'],'confirmed/instructed by Alex Example')
        generic=apply_changes(state,changes,'owner message')
        self.assertEqual(generic['changes'][-1]['authority'],'confirmed/instructed by the owner')
    def test_shorthand_clarification_resolves_111b(self):
        state=import_ledger(BASE)
        for text in ('it meant 111B','PERSON_D tickets online update meant 111B, 111A remains as is'):
            result={'clarification_required':False,'changes':[{'kind':'update','task_id':'T-111B','field':'details','value':'Owner instructed PERSON_D to get tickets online by 7 October 2026.','evidence_quote':text,'reason':'Resolved attribution'}]}
            self.assertEqual(validate_changes(state,result,text)[0]['task_id'],'T-111B')
        self.assertEqual(referenced_tasks(state,'1110B 111BB x111B'),set())
    def test_numbered_restatement_does_not_resolve_or_complete(self):
        # Exact wording that caused the false C-019 completion.
        state=import_ledger(BASE)
        text='3. New task: ProjectX SiteX — PERSON_F to sit with PERSON_C and get the show layout done for 20 December.'
        self.assertEqual(referenced_tasks(state,text),set())
        self.assertIsNone(explicit_completion(state,text,'thread/one'))
    def test_bare_integers_do_not_reference_tasks(self):
        state=import_ledger(BASE)
        for text in ('3. New task','3) layout done','3- done','• 3 done','3','11 done','2. create a group'):
            self.assertEqual(referenced_tasks(state,text),set(),text)
            self.assertIsNone(explicit_completion(state,text,'thread/one'),text)
    def test_numbered_list_creates_task_without_completing_another(self):
        state=import_ledger(BASE)
        text='3. New task: ProjectX SiteX — PERSON_F to get the show layout done for 20 December.'
        self.assertIsNone(explicit_completion(state,text,'thread/one'))
        create={'kind':'create','task_id':None,'field':'title','value':'ProjectX SiteX — show layout',
            'evidence_quote':text,'reason':'New owner task',
            'new_task':{'owner':'PERSON_F','deadline':'2026-12-20, time unknown','priority':'UNKNOWN','details':'Sit with PERSON_C'}}
        after=apply_changes(state,validate_changes(state,{'clarification_required':False,'changes':[create]},text),'owner message')
        self.assertEqual(after['tasks']['T-103']['status'],'OUTSTANDING')
        self.assertEqual(len(after['tasks']),14)
    def test_alias_resolves_owner_shorthand(self):
        state=import_ledger(BASE)
        text='add T-111C which is launch post for EventA workshop needs to go on Instagram'
        create={'kind':'create','task_id':None,'field':'title','value':'EventA workshop launch post',
            'evidence_quote':text,'reason':'New owner task',
            'new_task':{'owner':'UNKNOWN','deadline':'UNKNOWN','priority':'UNKNOWN','details':'Instagram launch post'}}
        after=apply_changes(state,validate_changes(state,{'clarification_required':False,'changes':[create]},text),'owner message')
        new_id='T-113'
        self.assertEqual(after['tasks'][new_id]['alias'],'T-111C')
        self.assertIn(new_id,referenced_tasks(after,'Owner is PERSON_B for 111C'))
        self.assertIn(new_id,referenced_tasks(after,'T-111C'))
        update={'kind':'update','task_id':new_id,'field':'owner','value':'PERSON_B',
            'evidence_quote':'Owner is PERSON_B for 111C','reason':'Owner assignment'}
        self.assertEqual(validate_changes(after,{'clarification_required':False,'changes':[update]},'Owner is PERSON_B for 111C')[0]['task_id'],new_id)
    def test_gmail_message_skips_404(self):
        from app import gmail_message
        class R:
            def __init__(self,status,body=None): self.status_code=status;self._b=body or {};self.ok=status<400;self.content=b'x';self.url='https://gmail.googleapis.com/gmail/v1/users/me/messages/id'
            def json(self): return self._b
        class S:
            def __init__(self,response): self.response=response
            def get(self,*a,**k): return self.response
        self.assertIsNone(gmail_message(S(R(404)),'gone'))
        self.assertEqual(gmail_message(S(R(200,{'id':'id','labelIds':[]})),'id'),{'id':'id','labelIds':[]})
    def test_routine_brief_does_not_unfurl_raw_drive_file(self):
        state=import_ledger(BASE)
        key=add_outbox(state,'spaces/test','brief:test-preview','Check T-101. [source](https://drive.google.com/file/d/abc/view)')
        self.assertNotIn('drive.google.com',state['outbox'][key]['text'])
        self.assertIn('T-101',state['outbox'][key]['text'])
    def test_html_mail_omits_scripts_and_style(self):
        parser=VisibleHTML()
        parser.feed('<head><style>private-css</style></head><body>Task &amp; update<script>code</script></body>')
        self.assertEqual(' '.join(parser.parts),'Task & update')
    def setUp(self): self.state=import_ledger(BASE)
    def result(self,quote='T-101 done',task='T-101'):
        return {'reply':'Recorded','clarification_required':False,'changes':[{'kind':'update','task_id':task,'field':'status','value':'COMPLETED','evidence_quote':quote,'reason':'Owner confirmed'}]}
    def test_ambiguous_completion_withheld(self):
        with self.assertRaises(RuntimeError): validate_changes(self.state,self.result('done'),'done')
    def test_fabricated_evidence_withheld(self):
        with self.assertRaises(RuntimeError): validate_changes(self.state,self.result(),'What is pending?')
    def test_question_cannot_complete(self):
        with self.assertRaises(RuntimeError): validate_changes(self.state,self.result('T-101 update please'),'T-101 update please')
    def test_completion_question_and_negation_withheld(self):
        for text in ('T-101 done?','T-101 is not done'):
            with self.assertRaises(RuntimeError): validate_changes(self.state,self.result(text),text)
    def test_append_only_change_preserves_baseline(self):
        changes=validate_changes(self.state,self.result(),'T-101 done')
        after=apply_changes(self.state,changes,'owner test evidence')
        self.assertEqual(self.state['tasks']['T-101']['status'],'OUTSTANDING')
        self.assertEqual(after['changes'][0]['old'],'OUTSTANDING')
        self.assertEqual(set(after['tasks']),set(self.state['tasks']))
        exported=export_ledger(BASE,after)
        for marker in ('### C-001','### C-007','T-111A','T-111B','VALIDATION PROPOSAL'):
            self.assertIn(marker,exported)
    def test_retry_same_message_has_one_outbox(self):
        key=add_outbox(self.state,'spaces/test','reply:one','First')
        self.assertEqual(key,add_outbox(self.state,'spaces/test','reply:one','Retry'))
        self.assertEqual(len(self.state['outbox']),1)
        self.assertEqual(self.state['outbox'][key]['text'],'First')
    def test_export_separates_live_status_from_unchanged_history(self):
        after=apply_changes(self.state,validate_changes(self.state,self.result(),'T-101 done'),'owner test evidence')
        after['tasks']['T-101']['details']='one | two\nthree'
        after['mode']='active'
        exported=export_ledger(BASE,after)
        current,historical=exported.split('## Frozen historical baseline',1)
        self.assertIn('Authoritative cloud task list',current)
        self.assertIn('Current cloud commit: C-008',current)
        self.assertNotIn('No task is confirmed complete',current)
        self.assertNotIn('VALIDATION PROPOSAL',current)
        self.assertIn('| COMPLETED |',current)
        self.assertIn('one &#124; two<br>three',current)
        self.assertIn(BASE,historical)
        self.assertEqual(after['changes'][0]['old'],'OUTSTANDING')
    def test_other_identity_cannot_call_service(self):
        with patch('app.id_token.verify_oauth2_token',return_value={'email':'someone@example.com','email_verified':True}):
            response=app.test_client().post('/process',headers={'Authorization':'Bearer test'})
            self.assertEqual(response.status_code,403)
    def test_scheduler_cannot_use_owner_admin_routes(self):
        with patch('app.id_token.verify_oauth2_token',return_value={'email':'ea-trigger@your-project.iam.gserviceaccount.com','email_verified':True}):
            response=app.test_client().post('/admin/test-brief',headers={'Authorization':'Bearer test'})
            self.assertEqual(response.status_code,403)
    def test_membership_expansion_withholds_post(self):
        runtime=object.__new__(Runtime)
        runtime.space='spaces/test';runtime.owner='users/owner';runtime.config={'bot_id':'bot'}
        runtime.__dict__['bot']=type('Bot',(),{'get':lambda *a,**k:None})()
        runtime.__dict__['user']=object()
        members=[{'state':'JOINED','member':{'name':'users/'+name}} for name in ('owner','bot','another-bot')]
        with patch('app.checked',return_value={'accessSettings':{'accessState':'PRIVATE'}}),patch('app.pages',return_value=iter(members)):
            with self.assertRaisesRegex(RuntimeError,'SPACE_MEMBERSHIP_CHANGED'): runtime.guard_space()
    def test_nonexistent_403_then_retry_uses_receipt(self):
        class Response:
            def __init__(self,status,body): self.status_code=status;self.body=body;self.ok=status<400;self.content=b'body'
            def json(self): return self.body
        class Bot:
            def __init__(self): self.sent=0;self.receipt=None
            def get(self,*a,**k): return Response(200,self.receipt) if self.receipt else Response(403,{})
            def post(self,url,params,json,timeout):
                self.sent+=1;self.receipt={'name':'spaces/test/messages/server-id','text':json['text']}
                return Response(200,self.receipt)
        runtime=object.__new__(Runtime);runtime.space='spaces/test';runtime.__dict__['bot']=Bot()
        runtime.store=type('Store',(),{'fence':lambda self:None})()
        entry={'space':'spaces/test','message_id':'client-cw-test','request_id':'stable','text':'Validation'}
        with patch.object(runtime,'guard_space'):
            first=runtime.post(entry);second=runtime.post(entry)
        self.assertEqual(first,second);self.assertEqual(runtime.bot.sent,1)
    def test_model_failure_is_fail_closed_per_message(self):
        state=import_ledger(BASE)
        runtime=object.__new__(Runtime); runtime.space='spaces/test'; runtime.owner='users/owner'
        runtime.__dict__['user']=object()
        runtime.store=type('MemStore',(),{'immutable':lambda *a:None,'save':lambda *a:1,
            'meta':lambda *a:None,'write':lambda *a:None})()
        runtime.drain=lambda s,g:(s,g)
        ok={'reply':'Noted.','clarification_required':False,'changes':[]}
        runtime.result=Mock(side_effect=[RuntimeError('MODEL_OUTPUT_INCOMPLETE'),ok])
        m1={'name':'spaces/test/messages/one','sender':{'name':'users/owner','type':'HUMAN'},'thread':{'name':'thread/one'},'text':'first update','createTime':'2026-10-06T05:15:00Z'}
        m2={'name':'spaces/test/messages/two','sender':{'name':'users/owner','type':'HUMAN'},'thread':{'name':'thread/two'},'text':'second update','createTime':'2026-10-06T05:16:00Z'}
        with patch('app.pages',return_value=[m1,m2]):
            state,g,count=runtime.recover_updates(state,0)
        self.assertEqual(count,2)
        k1=digest('spaces/test/messages/one')
        self.assertEqual(state['processed'][k1]['model_error'],'MODEL_OUTPUT_INCOMPLETE')
        self.assertEqual(state['processed'][k1]['changes'],0)
        self.assertIn(digest('reply:spaces/test/messages/one'),state['outbox'])
        self.assertIn('process that',state['outbox'][digest('reply:spaces/test/messages/one')]['text'])
        k2=digest('spaces/test/messages/two')
        self.assertNotIn('model_error',state['processed'][k2])
        self.assertIn(digest('reply:spaces/test/messages/two'),state['outbox'])
    def test_gap_records_are_stable_and_nonconsequential_suppressed(self):
        state={'coverage':{}}
        first=record_gap(state,'gmail_skipped_message','a message was skipped',consequential=False)
        stamp=first['first_seen']
        again=record_gap(state,'gmail_skipped_message','a message was skipped',consequential=False)
        self.assertEqual(again['first_seen'],stamp)
        self.assertEqual(pending_gaps(state),[])
        record_gap(state,'gmail_expired_history','history expired',consequential=True)
        self.assertEqual(len(pending_gaps(state)),1)
        state['coverage']['gmail']={'status':'SUCCESS','gap':'a message was skipped'}
        self.assertNotIn('gap',coverage_for_model(state['coverage']).get('gmail',{}))
        self.assertEqual(latest_gap_text(state,'gmail_'),'history expired')
    def test_surfaced_or_unchanged_gap_does_not_resurface(self):
        state={'coverage':{}}
        gap=record_gap(state,'big_gap','real gap',consequential=True)
        gap['surfaced']=True
        self.assertEqual(pending_gaps(state),[])
        record_gap(state,'big_gap','real gap',consequential=True)
        self.assertEqual(pending_gaps(state),[])
        changed=record_gap(state,'big_gap','a materially different gap',consequential=True)
        self.assertFalse(changed['surfaced'])
        self.assertEqual(len(pending_gaps(state)),1)

    def test_brief_slot_status_claim_covered_and_stale(self):
        self.assertEqual(brief_slot_status({},'x'),'claim')
        self.assertEqual(brief_slot_status({'x':{'status':'done'}},'x'),'covered')
        self.assertEqual(brief_slot_status({'x':{'status':'error'}},'x'),'covered')
        self.assertEqual(brief_slot_status({'x':{'outbox':'o','snapshot_time':'t'}},'x'),'covered')
        self.assertEqual(brief_slot_status({'x':{'status':'pending','claimed':now()}},'x'),'covered')
        old=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(minutes=31)).isoformat().replace('+00:00','Z')
        self.assertEqual(brief_slot_status({'x':{'status':'pending','claimed':old}},'x'),'claim')
    def _mem_runtime(self,result_fn):
        class MemStore:
            def __init__(s,state): s.state=state; s.gen=1
            @contextlib.contextmanager
            def locked(s): yield s
            def get(s,name): return json.loads(json.dumps(s.state)),s.gen
            def save(s,state,gen): s.state=json.loads(json.dumps(state)); s.gen=gen+1; return s.gen
            def meta(s,name): return None
            def write(s,*a,**k): pass
            def read(s,name): return (BASE.encode(),1)
            def immutable(s,*a,**k): pass
        rt=object.__new__(Runtime); rt.space='spaces/test'; rt.owner='users/owner'
        rt.store=MemStore(import_ledger(BASE))
        rt.guard_space=lambda:None
        rt.intake=lambda s,g:(s,g)
        rt.recover_updates=lambda s,g:(s,g,0)
        rt.drain=lambda s,g:(s,g)
        rt.result=result_fn
        return rt
    def test_brief_slot_claim_prevents_duplicate_and_error_blocks_retry(self):
        ts=dt.datetime(2026,10,7,15,0)
        ok=Mock(return_value={'reply':'Brief.','clarification_required':False,'changes':[]})
        rt=self._mem_runtime(ok)
        r1=brief_run(rt,'test-brief','test:one',ts)
        r2=brief_run(rt,'test-brief','test:one',ts)
        self.assertEqual(r1['status'],'SUCCESS')
        self.assertEqual(r2['status'],'ALREADY_COVERED')
        self.assertEqual(ok.call_count,1)
        self.assertIn(digest('brief:test:one'),rt.store.state['outbox'])
        fail=Mock(side_effect=RuntimeError('MODEL_OUTPUT_INCOMPLETE'))
        rt2=self._mem_runtime(fail)
        e1=brief_run(rt2,'test-brief','test:two',ts)
        self.assertEqual(e1['status'],'BRIEF_MODEL_ERROR')
        self.assertEqual(rt2.store.state['briefs']['test:two']['status'],'error')
        e2=brief_run(rt2,'test-brief','test:two',ts)
        self.assertEqual(e2['status'],'ALREADY_COVERED')
        self.assertEqual(fail.call_count,1)
    def test_brief_evidence_surfaces_owner_progress_as_proposal(self):
        state=import_ledger(BASE)
        state['tasks']['T-119']={'id':'T-119','title':'EventA workshop — publish launch post on HandleX Instagram',
            'owner':'PERSON_B','deadline':'UNKNOWN','status':'OUTSTANDING'}
        state['sources']=[{'id':'google_chat:spaces/X/messages/1','source':'google_chat','time':'2026-10-07T08:52:00Z',
            'sender':'users/owner','text':'@PERSON_B Panday IG post is live EventA and KIDF has accepted collab. Please ask Blooper to accept as well.'}]
        ev=task_evidence(state,'users/owner')
        self.assertIn('T-119',ev)
        self.assertTrue(ev['T-119']['evidence'][0]['owner'])
        self.assertIn('IG post is live',ev['T-119']['evidence'][0]['text'])
        rt=object.__new__(Runtime); rt.owner='users/owner'
        rt.store=type('S',(),{'read':lambda s,n:(BASE.encode(),1),'get':lambda s,n:(None,1)})()
        captured={}
        def fake_headroom(system,user):
            captured['system']=system; captured['user']=user
            return {'reply':'x','clarification_required':False,'changes':[]}
        with patch('app.complete_with_headroom',side_effect=fake_headroom):
            rt.result(state,'Current operating brief.','brief')
        user=json.loads(captured['user'])
        self.assertIn('changelog',user)
        self.assertTrue(any('Suggestion' in line and 'IG post is live' in line for line in user['changelog']))
        self.assertIn('Suggestion',captured['system'])

    def _capture_runtime(self):
        rt=object.__new__(Runtime); rt.owner='users/owner'
        rt.store=type('S',(),{'read':lambda s,n:(BASE.encode(),1),'get':lambda s,n:(None,1)})()
        return rt
    def test_context_is_trimmed_and_bounded(self):
        state=import_ledger(BASE)
        state['sources']=[{'id':f'google_chat:x/{i}','source':'google_chat','time':f'2026-10-07T00:{i:02d}:00Z','sender':'users/x','text':'word '*300} for i in range(50)]
        rt=self._capture_runtime(); cap={}
        def fake(system,user): cap['user']=user; return {'reply':'x','clarification_required':False,'changes':[]}
        with patch('app.complete_with_headroom',side_effect=fake):
            rt.result(state,'Current operating brief.','brief')
        user=json.loads(cap['user'])
        self.assertLessEqual(len(user['evidence']),12)
        self.assertNotIn('baseline_excerpt',user)
        self.assertLess(len(user['company_and_operating_baseline']),6000)
        self.assertLess(len(cap['user']),70000)
    def test_baseline_excerpt_only_on_history_reference(self):
        state=import_ledger(BASE)
        rt=self._capture_runtime(); caps={}
        def run(text):
            def fake(system,user): caps['user']=user; return {'reply':'x','clarification_required':False,'changes':[]}
            with patch('app.complete_with_headroom',side_effect=fake): rt.result(state,text,'conversation')
            return json.loads(caps['user'])
        self.assertNotIn('baseline_excerpt',run('What needs my attention now?'))
        self.assertTrue(run('Remind me what the baseline history says about T-111A').get('baseline_excerpt'))
    def test_enforce_context_budget_caps_size(self):
        from app import enforce_context_budget
        ctx={'evidence':[{'text':'x'*500} for _ in range(50)],'retrieved_background':{'a':'y'*5000,'b':'z'*5000},
            'recent_changes':[{'new':'q'*300} for _ in range(50)],'keep':'small'}
        enforce_context_budget(ctx,2000)
        self.assertLessEqual(len(json.dumps(ctx,ensure_ascii=False)),2000)

    def test_current_tasks_bounded_by_reference(self):
        from app import tasks_for_context
        state=import_ledger(BASE)
        ctx=tasks_for_context(state,'T-110 is done','conversation')
        self.assertIsInstance(ctx['T-110'],dict)
        self.assertIsInstance(ctx['T-101'],str)
        self.assertIn('T-101',ctx['T-101'])
        state['tasks']['T-110']['dependency']='Blocked by T-109'
        ctx=tasks_for_context(state,'T-110 is done','conversation')
        self.assertIsInstance(ctx['T-109'],dict)
    def test_operating_brief_cached(self):
        import app as appmod
        calls={'get':0,'immutable':0}
        class S:
            def get(s,n): calls['get']+=1; return (None,1)
            def immutable(s,*a,**k): calls['immutable']+=1
        appmod._BRIEF_CACHE.clear()
        t1=appmod.operating_brief(S(),BASE)
        t2=appmod.operating_brief(S(),BASE)
        self.assertEqual(t1,t2)
        self.assertEqual(calls['get'],1)
        self.assertEqual(calls['immutable'],1)
        self.assertLess(len(t1),len(BASE))
    def test_model_history_ring_buffer(self):
        import app as appmod
        appmod.MODEL_TELEMETRY.clear(); appmod.MODEL_TELEMETRY.update({'seq':1,'finish_reason':'stop'})
        state={}
        self.assertTrue(appmod.record_model_history(state))
        self.assertFalse(appmod.record_model_history(state))
        for i in range(2,30):
            appmod.MODEL_TELEMETRY.clear(); appmod.MODEL_TELEMETRY.update({'seq':i,'finish_reason':'stop'})
            appmod.record_model_history(state)
        self.assertEqual(len(state['model_history']),20)
        self.assertEqual(state['model_history'][-1]['seq'],29)

    def test_rules_clean_and_signal_style(self):
        from operating import RULES
        self.assertNotIn('Noted in my-ea',RULES)
        self.assertNotIn("hasn't been switched over",RULES)
        self.assertNotIn('in validation',RULES.lower())
        self.assertNotIn('imported ledger',RULES.lower())
        for marker in ('🟢','🟠','🔴'): self.assertIn(marker,RULES)
        self.assertIn('status is at stake',RULES)
        self.assertIn('ACT NOW',RULES)
        self.assertIn('UP NEXT',RULES)
    def test_brief_instruction_changelog_and_recap(self):
        state=import_ledger(BASE)
        rt=self._capture_runtime(); cap={}
        def fake(system,user): cap['system']=system; cap['user']=user; return {'reply':'x','clarification_required':False,'changes':[]}
        with patch('app.complete_with_headroom',side_effect=fake):
            rt.result(state,'Current operating brief.','brief',hour=11)
        u=json.loads(cap['user'])
        self.assertIn('changelog',u); self.assertNotIn('recap',u)
        self.assertIn('changelog-only',u['instruction']); self.assertNotIn('blank line',u['instruction'])
        cap.clear()
        with patch('app.complete_with_headroom',side_effect=fake):
            rt.result(state,'Current operating brief.','brief',hour=12)
        u=json.loads(cap['user'])
        self.assertIn('recap',u); self.assertIn('ACT NOW',u['instruction'])
        self.assertIn('🔴 ACT NOW',u['recap']); self.assertIn('🟠 UP NEXT',u['recap'])
        self.assertNotIn('Noted in my-ea',cap['system'])
    def test_changelog_delta_and_empty(self):
        from app import changelog_lines
        state=import_ledger(BASE)
        state['last_brief']={'time':'2026-10-07T00:00:00Z'}
        self.assertEqual(changelog_lines(state,'users/owner'),['No changes since last brief.'])
        state['changes']=[{'commit':'C-008','time':'2026-10-07T01:00:00Z','kind':'update','task_id':'T-101','field':'status','old':'OUTSTANDING','new':'COMPLETED'}]
        self.assertTrue(any(l.startswith('🟢') and 'T-101' in l for l in changelog_lines(state,'users/owner')))
        state['tasks']['T-113']={'id':'T-113','title':'Book venue for Friday','owner':'PERSON_F','deadline':'2026-10-09','status':'OUTSTANDING'}
        state['changes'].append({'commit':'C-009','time':'2026-10-07T02:00:00Z','kind':'create','task_id':'T-113','field':'title','new':'Book venue for Friday'})
        lines=changelog_lines(state,'users/owner')
        self.assertTrue(any(l.startswith('➕') and 'T-113' in l for l in lines))
        self.assertTrue(any(l.startswith('🟢') for l in lines))
    def test_recap_lines_headers(self):
        from app import recap_lines
        state=import_ledger(BASE)
        state['tasks']['T-101']={'id':'T-101','title':'Overdue thing','owner':'PERSON_A','deadline':'2020-01-01','status':'OUTSTANDING'}
        state['tasks']['T-102']={'id':'T-102','title':'Soon thing','owner':'PERSON_A','deadline':dt.date.today().isoformat(),'status':'OUTSTANDING'}
        lines=recap_lines(state)
        self.assertIn('🔴 ACT NOW',lines)
        self.assertIn('🟠 UP NEXT',lines)
        self.assertTrue(any(l.startswith('🔴') and 'T-101' in l for l in lines))
        self.assertFalse(any(l.startswith('🟢') for l in lines))
    def test_recap_uses_full_subject_without_ellipsis(self):
        from app import recap_lines
        state=import_ledger(BASE)
        long_title='Confirm final load-in schedule and crew call times with the venue manager before the weekend'
        state['tasks']['T-101']={'id':'T-101','title':long_title,'owner':'PERSON_A','deadline':'2020-01-01','status':'OUTSTANDING'}
        lines=recap_lines(state)
        line=[l for l in lines if 'T-101' in l][0]
        self.assertIn('venue manager before the weekend',line)
        self.assertNotIn('…',line)
    def test_create_reply_gets_signal(self):
        from app import with_create_signals
        state=import_ledger(BASE); before=set(state['tasks'])
        state['tasks']['T-113']={'id':'T-113','title':'Book venue for Friday','owner':'PERSON_F','deadline':'2026-10-09','status':'OUTSTANDING'}
        out=with_create_signals(state,before,'Noted.')
        self.assertIn('➕ T-113 created',out)
        self.assertIn('due 9 Oct 2026',out)
    def test_format_due_rules(self):
        from app import format_due
        self.assertEqual(format_due('2026-10-09, time unknown'),'due 9 Oct 2026')
        self.assertEqual(format_due('2026-10-08 12:00 PM IST'),'due 8 Oct 2026 12:00 PM')
        self.assertEqual(format_due('5 October 2026'),'due 5 Oct 2026')
        self.assertEqual(format_due('today EOD, exact time unknown'),'due today')
        self.assertIsNone(format_due('UNKNOWN'))
        self.assertIsNone(format_due('NOT YET ASSIGNED'))
        self.assertIsNone(format_due(''))
        self.assertNotIn('exact time UNKNOWN',format_due('2026-10-09, time unknown'))
    def test_unknown_owner_surfaced_only_as_blocker(self):
        from app import recap_lines
        state=import_ledger(BASE)
        state['tasks']={'T-101':{'id':'T-101','title':'Overdue unowned','owner':'NOT YET ASSIGNED','deadline':'2020-01-01','status':'OUTSTANDING'},
            'T-102':{'id':'T-102','title':'Future unowned','owner':'UNKNOWN','deadline':'2099-01-01','status':'OUTSTANDING'}}
        lines=recap_lines(state)
        self.assertTrue(any('needs owner' in l and 'T-101' in l for l in lines))
        self.assertFalse(any('needs owner' in l and 'T-102' in l for l in lines))
        self.assertFalse(any('NOT YET ASSIGNED' in l or 'UNKNOWN' in l for l in lines))
    def test_short_subject_word_boundary_ellipsis(self):
        from app import short_subject
        title='This is a very long task title that goes on and on well beyond the limit'
        sub=short_subject(title)
        self.assertTrue(sub.endswith('…'))
        base=sub[:-1]
        self.assertTrue(title.startswith(base))
        self.assertTrue(len(base)==len(title) or title[len(base)]==' ')
        self.assertLessEqual(len(sub),49)
    def test_brief_preview_text_no_unknown_noise(self):
        from app import brief_preview_text
        state=import_ledger(BASE)
        p11=brief_preview_text(state,'users/owner',hour=11)
        self.assertFalse(p11['is_recap']); self.assertNotIn('ACT NOW',p11['preview'])
        p12=brief_preview_text(state,'users/owner',hour=12)
        self.assertTrue(p12['is_recap']); self.assertIn('ACT NOW',p12['preview'])
        for p in (p11,p12):
            self.assertNotIn('UNKNOWN',p['preview']); self.assertNotIn('NOT YET ASSIGNED',p['preview'])

    def test_build_settings_and_validation(self):
        import app as appmod
        s=appmod.build_settings({'owner_user':'users/x','space':'spaces/y','bot_id':123})
        self.assertEqual(s['space_id'],'spaces/y'); self.assertEqual(s['bot_id'],'123')
        self.assertEqual(s['assistant_name'],'my-ea'); self.assertTrue(s['owner_email'])
        with self.assertRaises(appmod.MissingConfig) as cm:
            appmod.validate_settings({})
        for field in ('owner_email','owner_user','space_id','bot_id'): self.assertIn(field,str(cm.exception))
        appmod.validate_settings({'owner_email':'a','owner_user':'u','space_id':'s','bot_id':'b'})
    def test_rules_template_substitution(self):
        from operating import RULES, render_rules, RULES_TEMPLATE
        r=render_rules('helper','Alex')
        self.assertIn('helper',r); self.assertIn('Alex',r)
        self.assertNotIn('{assistant_name}',r); self.assertNotIn('{owner_name}',r)
        self.assertIn('advisory context only',r)
        self.assertIn('my-ea',RULES); self.assertIn('PERSON_A',RULES)
        self.assertEqual(RULES,render_rules())
    def test_knowledge_pack_generic_bundle(self):
        from app import load_knowledge_pack
        class S:
            def read(s,n): return (BASE.encode(),1)
            def get(s,n):
                if n.endswith('capabilities.json'): return ({'version':1,'team':[{'name':'X','role':'Y','capabilities':['z']}]},1)
                if n.endswith('background.json'): return ({'company_documents':[{'title':'Doc','text':'hello'}],'role_evidence':[],'ledger_conversation':[],'gaps':[]},1)
                return (None,1)
        pack=load_knowledge_pack(S())
        self.assertTrue(pack['operating_context'])
        self.assertEqual(pack['people']['team'][0]['name'],'X')
        self.assertEqual(pack['documents'][0]['title'],'Doc')
    def test_retrieve_relevant_golden_keyword_order(self):
        from app import retrieve_relevant, trim_evidence
        state={'sources':[{'id':'a','source':'google_chat','time':'t1','sender':'s','text':'alpha beta gamma'},
            {'id':'b','source':'gmail','time':'t2','sender':'s','text':'alpha only'}]}
        res=retrieve_relevant(state,'alpha beta',limit=5)
        self.assertTrue({'text','source','kind','score'} <= set(res[0]))
        self.assertEqual(res[0]['id'],'a'); self.assertGreaterEqual(res[0]['score'],res[1]['score'])
        self.assertEqual(res[0]['kind'],'source')
        ev=trim_evidence(state,'alpha beta')
        self.assertEqual(ev[0]['source'],'google_chat')

if __name__=='__main__': unittest.main()
