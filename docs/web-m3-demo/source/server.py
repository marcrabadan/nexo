"""Loopback-only M3 development API. Not a production server or Shield profile.

Build app.nexo to dist, then: python examples/web/data/server.py --directory dist
"""
import argparse
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading


def create_server(directory, port=8765):
    sessions={};lock=threading.Lock()
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(directory),**kwargs)
        def session(self):
            cookie=SimpleCookie()
            try:cookie.load(self.headers.get('Cookie',''))
            except Exception:pass
            key=cookie['nexo_demo'].value if 'nexo_demo' in cookie else ''
            with lock:
                if key not in sessions:
                    if len(sessions)>=100:return None,None,False
                    key=secrets.token_urlsafe(24)
                    sessions[key]={'csrf':secrets.token_urlsafe(24),'products':[{'id':1,'name':'Nexo guide'}]}
                    return key,sessions[key],True
                return key,sessions[key],False
        def reply(self,value,status=200):
            body=json.dumps(value).encode();self.send_response(status)
            self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
        def trusted_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
        def do_GET(self):
            if not self.trusted_host():return self.reply({'error':'Host denied'},403)
            if self.path not in ('/','/index.html','/api/products'):return super().do_GET()
            key,session,new=self.session()
            if session is None:return self.reply({'error':'Restart demo: session limit reached'},503)
            if self.path=='/api/products':
                if new:return self.reply({'error':'Open the application first'},403)
                with lock:products=list(session['products'])
                return self.reply(products)
            body=(directory/'index.html').read_text().replace('<title>','<meta name="nexo-csrf-token" content="'+session['csrf']+'"><title>').encode()
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            if new:self.send_header('Set-Cookie',f'nexo_demo={key}; HttpOnly; SameSite=Strict; Path=/')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_POST(self):
            if not self.trusted_host() or self.path!='/api/products':return self.reply({'error':'Denied'},403)
            key,session,new=self.session()
            origin='http://'+self.headers.get('Host','')
            if session is None or new or self.headers.get('Origin')!=origin or not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']):return self.reply({'error':'CSRF denied'},403)
            if self.headers.get('Content-Type')!='application/json':return self.reply({'error':'JSON required'},415)
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536:return self.reply({'error':'Body too large'},413)
                value=json.loads(self.rfile.read(length))
                if not isinstance(value,dict) or set(value)!={'name'} or not isinstance(value['name'],str) or not 1<=len(value['name'])<=100:return self.reply({'error':'Invalid product'},422)
            except (ValueError,UnicodeError):return self.reply({'error':'Invalid JSON'},400)
            with lock:
                if len(session['products'])>=100:return self.reply({'error':'Demo product limit'},429)
                product={'id':len(session['products'])+1,'name':value['name']};session['products'].append(product)
            self.reply(product,201)
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def serve(directory,port=8765):
    server=create_server(directory,port)
    print(f'Local development only: http://127.0.0.1:{server.server_port}/',flush=True)
    try:server.serve_forever()
    finally:server.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=Path('dist'))
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args();serve(args.directory.resolve(),args.port)
