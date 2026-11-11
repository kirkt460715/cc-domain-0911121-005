import json,hashlib,urllib.request
from source import start,ROOT
server,base=start()
try:
    entry=json.load(urllib.request.urlopen(base+'/approved/catalog'))
    assert entry['@graph'][0]['grant']['@id']=='/approved/license/a'
    license=json.load(urllib.request.urlopen(base+'/approved/license/a'))
    assert license['purposes']==['research']
    assert json.load(urllib.request.urlopen(base+'/approved/conflict'))['title']!='合成观测集'
finally:
    server.shutdown();server.server_close()
print('本地输入协议与故障样本检查通过')
