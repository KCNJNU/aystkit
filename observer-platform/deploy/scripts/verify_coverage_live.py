#!/usr/bin/env python3
"""Read-only ECS validation. Secrets stay in ECS memory; only login/logout mutate sessions."""
import argparse,asyncio,hashlib,json,re,socket
from pathlib import Path
import aiohttp
from verify_live import credentials,require,ORIGIN

class Resolver(aiohttp.abc.AbstractResolver):
    async def resolve(self,host,port=0,family=socket.AF_INET):
        require(host=='aystkit.cn','hostname')
        return [{'hostname':host,'host':'127.0.0.1','port':port,'family':socket.AF_INET,'proto':socket.IPPROTO_TCP,'flags':socket.AI_NUMERICHOST}]
    async def close(self):pass

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--manifest',required=True);args=parser.parse_args()
    manifest=json.loads(Path(args.manifest).read_text()); checks=[]; transfers=[]
    asset_names={name for name in manifest['files'] if name.startswith('cloud/frontend/coverage/assets/')}
    require(all(re.fullmatch(r'cloud/frontend/coverage/assets/[A-Za-z0-9_-]+\.(?:js|css)',name) for name in asset_names),'safe_asset_names')
    assets={'/coverage-assets/assets/'+name.rsplit('/',1)[1]:name for name in asset_names}
    workers=[path for path in assets if re.fullmatch(r'/coverage-assets/assets/camera-optimizer-[A-Za-z0-9_-]+\.js',path)]
    require(len(workers)==1,'dedicated_camera_worker')
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=Resolver()),timeout=aiohttp.ClientTimeout(total=30)) as session:
        async def get(path,status=200):
            async with session.get(ORIGIN+path,allow_redirects=False) as response:
                require(response.status==status,'status_'+path)
                body=await response.read();require(len(body)<4*1024*1024,'bounded_body')
                return response,body
        for path in ('/coverage','/coverage/'):
            response,_=await get(path,302);require(response.headers.get('Location')=='/login','login_redirect')
        for path in sorted(assets):await get(path,401)
        checks.append('anonymous_denied')
        async with session.post(ORIGIN+'/api/login',json=credentials('/run/observer-initial-access.json'),headers={'Origin':ORIGIN}) as response:
            require(response.status==200,'login');require((await response.json()).get('approved') is True,'approved')
        try:
            _,index=await get('/');require(b'href="/coverage"' in index,'navigation')
            response,html=await get('/coverage');csp=response.headers.get('Content-Security-Policy','')
            require("script-src 'self'" in csp and "default-src 'self'" in csp,'csp_worker_self')
            require(hashlib.sha256(html).hexdigest()==manifest['files']['cloud/frontend/coverage/index.html'],'page_digest')
            linked=set(re.findall(r'(?:src|href)="(/coverage-assets/assets/[A-Za-z0-9_-]+\.(?:js|css))"',html.decode()))
            require(len(linked)>=2 and linked.issubset(assets),'html_assets')
            worker_referenced=False
            for path in sorted(assets):
                response,body=await get(path);name=assets[path]
                require(hashlib.sha256(body).hexdigest()==manifest['files'][name],'asset_digest')
                require(response.headers.get('Content-Encoding')=='gzip','asset_gzip')
                if path.endswith('.js'):require('javascript' in response.headers.get('Content-Type',''),'javascript_mime')
                if path not in workers and path.endswith('.js') and workers[0].rsplit('/',1)[1].encode() in body:worker_referenced=True
                transfers.append({'file':path.rsplit('/',1)[1],'bytes':len(body),'transferBytes':int(response.headers['Content-Length'])})
            require(worker_referenced,'worker_referenced_by_bundle')
            for name in ('native-player.js','media.js'):
                _,body=await get('/'+name)
                require(hashlib.sha256(body).hexdigest()==manifest['files']['cloud/frontend/'+name],'preserved_'+name)
            await get('/snapshot.json'); checks+=['approved_page_assets_and_worker','release_digests','gzip_and_csp','video_assets_preserved']
        finally:
            async with session.post(ORIGIN+'/api/logout',headers={'Origin':ORIGIN}) as response:require(response.status==200,'logout')
        await get('/coverage',302)
        for path in sorted(assets):await get(path,401)
        checks.append('logout_revoked')
    print(json.dumps({'pass':True,'checks':checks,'assets':transfers},ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
