"""Generation-conditional private storage and execution fencing."""
import contextlib, json, os, time, uuid
from urllib.parse import quote
import google.auth
from google.auth.transport.requests import AuthorizedSession

class Busy(RuntimeError): pass

class Store:
    def __init__(self, bucket=None, session=None):
        self.bucket=bucket or os.environ['STATE_BUCKET']
        if session is None:
            credentials,_=google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
            session=AuthorizedSession(credentials)
        self.session=session; self.lease=None; self.started=None
    def url(self,name):
        return 'https://storage.googleapis.com/storage/v1/b/'+self.bucket+'/o/'+quote(name,safe='')
    def meta(self,name):
        r=self.session.get(self.url(name),timeout=30)
        if r.status_code==404: return None
        r.raise_for_status(); return r.json()
    def read(self,name):
        meta=self.meta(name)
        if not meta: return None,None
        r=self.session.get(self.url(name),params={'alt':'media','ifGenerationMatch':meta['generation']},timeout=60)
        r.raise_for_status(); return r.content,meta['generation']
    def get(self,name):
        data,generation=self.read(name)
        return (json.loads(data) if data else None),generation
    def write(self,name,data,generation=0,content_type='application/json'):
        if not isinstance(data,bytes): data=json.dumps(data,ensure_ascii=False).encode()
        r=self.session.post('https://storage.googleapis.com/upload/storage/v1/b/'+self.bucket+'/o',
            params={'uploadType':'media','name':name,'ifGenerationMatch':generation},
            headers={'Content-Type':content_type},data=data,timeout=60)
        r.raise_for_status(); return r.json()['generation']
    def immutable(self,name,data,content_type='application/json'):
        try: return self.write(name,data,0,content_type)
        except Exception as e:
            if getattr(getattr(e,'response',None),'status_code',None)==412: return self.meta(name)['generation']
            raise
    @contextlib.contextmanager
    def locked(self):
        prior,generation=self.get('execution-lock.json')
        if prior and prior['expires']>time.time(): raise Busy('EXECUTION_BUSY')
        try:
            self.lease=self.write('execution-lock.json',{'owner':uuid.uuid4().hex,'expires':time.time()+900},generation or 0)
        except Exception as e:
            if getattr(getattr(e,'response',None),'status_code',None)==412: raise Busy('EXECUTION_BUSY') from None
            raise
        self.started=time.monotonic()
        try: yield self
        finally:
            r=self.session.delete(self.url('execution-lock.json'),params={'ifGenerationMatch':self.lease},timeout=30)
            if r.status_code not in (204,404,412): r.raise_for_status()
    def fence(self):
        if time.monotonic()-self.started>480: raise RuntimeError('EXECUTION_DEADLINE')
        meta=self.meta('execution-lock.json')
        if not meta or meta['generation']!=self.lease: raise RuntimeError('EXECUTION_LOCK_LOST')
    def save(self,state,generation):
        self.fence()
        return self.write('state.json',state,generation)
