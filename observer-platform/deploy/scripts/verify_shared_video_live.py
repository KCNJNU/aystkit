#!/usr/bin/env python3
"""ECS-only fixed-quality fanout validation. No credentials leave ECS."""
from __future__ import annotations
import argparse,asyncio,json,socket,time
from pathlib import Path
import aiohttp
from verify_live import credentials,require,ORIGIN

KEYS=('usbFront','usbLeft','usbHub3','usbHub2')
class Resolver(aiohttp.abc.AbstractResolver):
    async def resolve(self,host,port=0,family=socket.AF_INET):
        require(host=='aystkit.cn','hostname')
        return [{'hostname':host,'host':'127.0.0.1','port':port,'family':socket.AF_INET,'proto':socket.IPPROTO_TCP,'flags':socket.AI_NUMERICHOST}]
    async def close(self):pass

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    connector=aiohttp.TCPConnector(resolver=Resolver(),limit=24)
    timeout=aiohttp.ClientTimeout(total=30,sock_read=12)
    clients=[aiohttp.ClientSession(connector=connector,connector_owner=False,timeout=timeout) for _ in range(2)]
    stopped=[asyncio.Event(),asyncio.Event()];tasks=[];ready=[asyncio.Event() for _ in range(8)]
    rows={};authenticated=[]
    async def status():
        async with clients[0].get(ORIGIN+'/native-camera/status') as response:
            require(response.status==200,'status')
            return await response.json()
    async def stream(index,key):
        slot=index*4+KEYS.index(key);row={'bytes':0,'chunks':0,'session':None,'initial':False}
        rows[str(index)+':'+key]=row
        async with clients[index].get(ORIGIN+'/native-camera/'+key+'/video.mp4',timeout=aiohttp.ClientTimeout(total=None,sock_read=12)) as response:
            require(response.status==200,'video_status')
            require(response.content_type=='video/mp4','video_type')
            row['session']=response.headers.get('X-Video-Session');require(bool(row['session']),'video_session')
            initial=bytearray()
            async for data in response.content.iter_chunked(65536):
                row['bytes']+=len(data);row['chunks']+=1
                if len(initial)<4096:initial.extend(data[:4096-len(initial)])
                row['initial']=len(initial)>=8 and initial[4:8]==b'ftyp'
                if row['bytes']>4000:ready[slot].set()
                if stopped[index].is_set():return
    async def wait_ready(indices):
        await asyncio.wait_for(asyncio.gather(*(ready[i].wait() for i in indices)),15)
    async def logout(index):
        async with clients[index].post(ORIGIN+'/api/logout',headers={'Origin':ORIGIN}) as response:require(response.status==200,'logout')
    report={'scope':'ECS loopback HTTPS with two independent temporary sessions; real fixed-quality camera bytes; no external bandwidth load'}
    try:
        login=credentials('/run/observer-initial-access.json')
        for index,client in enumerate(clients):
            async with client.post(ORIGIN+'/api/login',json=login,headers={'Origin':ORIGIN}) as response:
                require(response.status==200,'login');require((await response.json()).get('approved') is True,'approved');authenticated.append(index)
        login.clear()
        before=await status();report['before']=before.get('fanout');require(before.get('enabled') is True,'enabled')
        expected={key:(before['cameras'][key]['width'],before['cameras'][key]['height'],before['cameras'][key]['requested_fps']) for key in KEYS}
        for key in KEYS:require(expected[key][0]==640 and expected[key][2]==10,'fixed_source_quality')
        tasks.extend(asyncio.create_task(stream(0,key)) for key in KEYS)
        await wait_ready(range(4));one_start=await status();one_clock=time.monotonic();await asyncio.sleep(3);one=await status();report['oneViewer']=one.get('fanout')
        require(one['fanout']['upstreams']==4 and one['fanout']['subscribers']==4,'one_viewer_four_sources')
        report['oneViewerUpstreamMbps']=round((one['fanout']['upstreamBytes']-one_start['fanout']['upstreamBytes'])*8/(time.monotonic()-one_clock)/1e6,3)
        tasks.extend(asyncio.create_task(stream(1,key)) for key in KEYS)
        await wait_ready(range(4,8));await asyncio.sleep(3);two=await status();report['twoViewers']=two.get('fanout')
        for key in KEYS:require(rows['0:'+key]['session']==rows['1:'+key]['session'],'same_encoder_session')
        values={key:row['bytes'] for key,row in rows.items()};started=time.monotonic();await asyncio.sleep(8)
        for key,row in rows.items():require(row['bytes']-values[key]>10000,'both_viewers_progress')
        report['bothProgressSeconds']=round(time.monotonic()-started,2)
        after=await status()
        require(after.get('fanout',{}).get('upstreams')==4,'exactly_four_upstreams')
        require(after['fanout'].get('subscribers')==8,'eight_independent_subscribers')
        for key in KEYS:
            item=after['cameras'][key];require((item['width'],item['height'],item['requested_fps'])==expected[key],'quality_unchanged')
            require(item['frames']>before['cameras'][key]['frames'] and item['encoded_frames']>before['cameras'][key]['encoded_frames'],'source_progress')
        report['twoViewersSteady']=after['fanout']
        report['twoViewersUpstreamMbps']=round((after['fanout']['upstreamBytes']-two['fanout']['upstreamBytes'])*8/report['bothProgressSeconds']/1e6,3)
        # Revocation must cancel just this test session; the second viewer keeps going.
        await logout(0);authenticated.remove(0);stopped[0].set()
        await asyncio.sleep(2)
        values={key:rows['1:'+key]['bytes'] for key in KEYS};await asyncio.sleep(2)
        for key in KEYS:require(rows['1:'+key]['bytes']>values[key]+1000,'logout_isolated')
        async with clients[1].get(ORIGIN+'/native-camera/status') as response:
            last=await response.json();require(response.status==200,'surviving_session')
        require(last['fanout']['upstreams']==4 and last['fanout']['subscribers']==4,'one_session_removed')
        report['afterFirstLogout']=last['fanout'];report['streams']=rows;report['pass']=True
    finally:
        for event in stopped:event.set()
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for index in authenticated:
            try:await logout(index)
            except (aiohttp.ClientError,asyncio.TimeoutError):pass
        for client in clients:await client.close()
        await connector.close()
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
