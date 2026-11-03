"""Trusted backend configuration for the integrated experimental M6 example."""
import asyncio
import hashlib
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from nexo.web_ssr import local_server
from nexo.web_subscriptions import EventChannel

ADD = bytes.fromhex('0061736d0100000001070160027f7f017f030201000707010361646400000a09010700200020016a0b')


async def load(request):
    await asyncio.sleep(0)  # Replace with an independently authorized backend query.
    return {'title':'Async server snapshot', 'products':[{'id':1,'name':'One'},{'id':2,'name':'Two'}]}


def main():
    folder = Path(__file__).parent
    channel = EventChannel({'kind':'record','fields':{'notice':{'kind':'str'}}})
    channel.publish({'notice':'Public event stream connected'})
    boot = (folder/'host.js').read_text().replace('@@SHA256@@', hashlib.sha256(ADD).hexdigest())
    assets = {'/m6-host.js':('text/javascript',boot),'/add.wasm':('application/wasm',ADD)}
    stop = threading.Event()
    def publish():
        sequence = 0
        while not stop.wait(2):
            sequence += 1
            channel.publish({'notice':f'Public snapshot {sequence}'})
    publisher = threading.Thread(target=publish, daemon=True);publisher.start()
    try:
        with local_server(folder/'app.nexo',4174,hydrate=True,loader=load,wasm=True,
                          channels={'/events':channel},assets=assets,client_boot='/m6-host.js') as server:
            print(f'M6 integrated example http://127.0.0.1:{server.server_port}/',flush=True)
            try:server.serve_forever()
            except KeyboardInterrupt:pass
    finally:
        stop.set();channel.close();publisher.join()


if __name__ == '__main__':
    main()
