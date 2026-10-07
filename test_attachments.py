import io,unittest,copy
from unittest.mock import patch
from pypdf import PdfWriter
from pypdf.generic import NameObject,DictionaryObject,DecodedStreamObject
from attachments import extract,ingest,context,search_text
from app import Runtime
from operating import import_ledger
from test_controls import BASE

class Store:
    def __init__(self):self.objects={}
    def get(self,key):return self.objects.get(key),1
    def immutable(self,key,value,*args):self.objects.setdefault(key,value)
    def save(self,*args):return 1
class Response:
    ok=True;status_code=200
    def __init__(self,data):self.data=data
    def iter_content(self,size):yield self.data
    def close(self):pass
class User:
    def __init__(self,data):self.data=data;self.calls=0
    def get(self,*args,**kwargs):self.calls+=1;return Response(self.data)

def pdf_fixture():
    writer=PdfWriter()
    for text in ('Introduction','Acoustic ceiling treatment included; false ceiling construction excluded.'):
        page=writer.add_blank_page(width=400,height=400)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject();stream.set_data(('BT /F1 12 Tf 20 200 Td ('+text+') Tj ET').encode())
        page[NameObject('/Contents')]=writer._add_object(stream)
    output=io.BytesIO();writer.write(output);return output.getvalue()

class Attachments(unittest.TestCase):
    def test_spaced_pdf_words_rank_without_changing_evidence(self):
        raw='A c o u s t i c   c e i l i n g treatment'
        target={'id':'target','text':raw,'pages':[{'page':1,'text':'Introduction'},
            {'page':4,'text':raw}],'time':'2026-10-01'}
        records=[target]+[{'id':str(i),'text':'Unrelated recent document',
            'pages':[],'time':'2026-10-06'} for i in range(6)]
        original=copy.deepcopy(records)
        result=context(records,'acoustic ceiling')
        self.assertEqual(result[0]['id'],'target')
        self.assertEqual(result[0]['excerpts'][0],{'page':4,'text':raw})
        self.assertEqual(records,original)
    def test_search_normalization_preserves_normal_words_and_line_boundaries(self):
        self.assertEqual(search_text('False ceiling excluded'),'false ceiling excluded')
        self.assertEqual(search_text('A\nB\nC'),'a\nb\nc')
        self.assertEqual(search_text('ＣＥＩＬＩＮＧ'),'ceiling')

    def message(self):return {'name':'spaces/test/messages/one','thread':{'name':'spaces/test/threads/one'},'createTime':'2026-10-06T04:30:00Z'}
    def attachment(self):return {'name':'spaces/test/messages/one/attachments/one','contentName':'proposal.pdf','contentType':'application/pdf','attachmentDataRef':{'resourceName':'spaces/test/media/one'}}
    def test_pdf_extracts_text_and_page_provenance(self):
        result=extract(pdf_fixture(),'application/pdf')
        self.assertEqual(result['status'],'READ')
        self.assertIn('false ceiling',result['pages'][1]['text'])
        self.assertEqual(result['pages'][1]['page'],2)
    def test_download_cache_and_thread_provenance(self):
        store=Store();user=User(pdf_fixture())
        one=ingest(store,user,self.message(),self.attachment());two=ingest(store,user,self.message(),self.attachment())
        self.assertEqual(one,two);self.assertEqual(user.calls,1)
        self.assertEqual(one['thread'],'spaces/test/threads/one')
        evidence=context([one],'false ceiling','spaces/test/threads/one')
        self.assertIn('false ceiling',evidence[0]['excerpts'][0]['text'])
        self.assertEqual(evidence[0]['excerpts'][0]['page'],2)
    def test_attachment_only_thread_is_read_before_reply(self):
        runtime=object.__new__(Runtime);runtime.space='spaces/test';runtime.owner='users/owner'
        runtime.store=Store();runtime.__dict__['user']=User(pdf_fixture());runtime.drain=lambda s,g:(s,g)
        message={**self.message(),'sender':{'type':'HUMAN','name':'users/owner'},'attachment':[self.attachment()]}
        def result(state,text,**kwargs):
            self.assertEqual(state['attachments'][0]['status'],'READ')
            self.assertEqual(kwargs['thread'],message['thread']['name'])
            return {'reply':'Read proposal.pdf.','changes':[],'clarification_required':False}
        runtime.result=result
        with patch('app.pages',return_value=[message]):state,g,count=runtime.recover_updates(import_ledger(BASE),0)
        self.assertEqual(count,1);self.assertEqual(len(state['attachments']),1)
        self.assertEqual(state['changes'],[])
        self.assertEqual(next(iter(state['outbox'].values()))['thread'],message['thread']['name'])
    def test_download_failure_does_not_write_or_checkpoint(self):
        runtime=object.__new__(Runtime);runtime.space='spaces/test';runtime.owner='users/owner';runtime.store=Store()
        runtime.__dict__['user']=type('Failure',(),{'get':lambda *a,**k:type('Response',(),{'ok':False,'status_code':503})()})()
        message={**self.message(),'sender':{'type':'HUMAN','name':'users/owner'},'attachment':[self.attachment()]}
        state=import_ledger(BASE);checkpoint=state['operating_checkpoint']
        with patch('app.pages',return_value=[message]),self.assertRaisesRegex(RuntimeError,'ATTACHMENT_DOWNLOAD_HTTP_503'):runtime.recover_updates(state,0)
        self.assertEqual(state['operating_checkpoint'],checkpoint);self.assertEqual(state['processed'],{})
        self.assertFalse(any(k.startswith('attachments/') for k in runtime.store.objects))
    def test_blank_scan_and_unsupported_type_are_explicit_gaps(self):
        writer=PdfWriter();writer.add_blank_page(100,100);data=io.BytesIO();writer.write(data)
        self.assertEqual(extract(data.getvalue(),'application/pdf')['status'],'UNREADABLE')
        self.assertEqual(extract(b'image','image/png')['status'],'UNSUPPORTED')
    def test_file_size_is_bounded_before_durable_content(self):
        store=Store()
        with patch('attachments.LIMIT',4),self.assertRaisesRegex(RuntimeError,'ATTACHMENT_TOO_LARGE'):ingest(store,User(b'large file'),self.message(),self.attachment())
        self.assertEqual(store.objects,{})

if __name__=='__main__':unittest.main()
