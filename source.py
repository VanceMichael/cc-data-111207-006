import json

class Source:
    def __init__(self, fixture):
        self.fixture=fixture
        self.fail_path=None
        self.requests=[]

    def handle(self,path,headers):
        self.requests.append(path)
        if path==self.fail_path:
            return 503,{},b''
        if path==self.fixture['expired']:
            return 410,{'Content-Type':'application/json'},b'{"restart":"/full"}'
        payload=self.fixture['pages'].get(path)
        if payload is None:
            return 404,{},b''
        return 200,{'Content-Type':'application/json'},json.dumps(payload).encode()
