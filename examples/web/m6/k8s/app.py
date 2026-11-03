"""Public integration fixture for a local cluster, not an authenticated backend."""
import atexit
import hashlib
from pathlib import Path
import runpy
import threading
from nexo.web_ssr import SSRApplication
from nexo.web_subscriptions import EventChannel

FOLDER=Path(__file__).resolve().parents[1]
backend=runpy.run_path(str(FOLDER/'server.py'))
channel=EventChannel({'kind':'record','fields':{'notice':{'kind':'str'}}})
channel.publish({'notice':'Public event stream connected'})
stop=threading.Event()
def publish():
    sequence=0
    while not stop.wait(2):
        sequence+=1
        channel.publish({'notice':f'Public snapshot {sequence}'})
thread=threading.Thread(target=publish,daemon=True);thread.start()
boot=(FOLDER/'host.js').read_text().replace('@@SHA256@@',hashlib.sha256(backend['ADD']).hexdigest())
application=SSRApplication.from_file(FOLDER/'app.nexo',hydrate=True,loader=backend['load'],wasm=True,
    channels={'/events':channel},assets={'/m6-host.js':('text/javascript',boot),'/add.wasm':('application/wasm',backend['ADD'])},client_boot='/m6-host.js')
def close():
    stop.set();channel.close();thread.join(timeout=3)
atexit.register(close)
