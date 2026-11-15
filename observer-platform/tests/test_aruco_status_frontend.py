"""Execute the actual status-card renderer using synthetic observation summaries."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / 'cloud/frontend'


def test_aruco_cards_keep_marker_ids_separate_from_pose_and_never_retain_stale_ids():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is required for ArUco status rendering checks')
    script = r'''
      const fs=require('fs'),vm=require('vm');global.window={};
      vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
      const F=window.ObserverFormat;
      const source=fs.readFileSync(process.argv[2],'utf8');
      const effectiveStatus=value=>F.normalizeStatus(value);
      vm.runInThisContext(source.slice(source.indexOf('  function statusCard('),source.indexOf('  function kpiCard(')));
      vm.runInThisContext(source.slice(source.indexOf('  function arucoPreviewCards('),source.indexOf('  function renderArucoPreview(')));
      const keys=['usbFront','usbLeft','usbHub3','usbHub2','color'];
      const dictionary='DICT_5X5_100';
      const value={status:'AVAILABLE',dictionary,ageMs:100,cameras:Object.fromEntries(keys.map(
        key=>[key,{status:'AVAILABLE',dictionary,ageMs:250,ids:[11,0,99,10,11]}]))};
      const output={live:arucoPreviewCards(value),missing:arucoPreviewCards(),
        expired:arucoPreviewCards(value,4750),badElapsed:arucoPreviewCards(value,NaN)};
      const one=structuredClone(value);one.cameras.usbFront.status='STALE';
      output.one=arucoPreviewCards(one);
      const noMarkers=structuredClone(value);keys.forEach(key=>noMarkers.cameras[key].ids=[]);
      output.empty=arucoPreviewCards(noMarkers);
      const invalidRows=[{ids:['<script>SECRET</script>']},{ids:[100]},{ids:[true]},
        {ids:[10.5]},{ids:Array(13).fill(10)},{dictionary:'DICT_4X4_100'},
        {ageMs:null},{ageMs:-1},{ageMs:Infinity}];
      output.invalid=invalidRows.map(bad=>{
        const copy=structuredClone(value);Object.assign(copy.cameras.usbFront,bad);
        return arucoPreviewCards(copy);
      });
      const old=structuredClone(value);old.status='STALE';output.old=arucoPreviewCards(old);
      const error=structuredClone(value);error.cameras.usbFront.status='ERROR';output.error=arucoPreviewCards(error);
      console.log(JSON.stringify(output));
    '''
    result = subprocess.run([node, '-e', script, str(FRONTEND / 'formatters.js'), str(FRONTEND / 'app.js')],
                            capture_output=True, text=True, check=True)
    values = json.loads(result.stdout)
    assert values['live'].count('ID 0 · 10 · 11 · 99') == 5
    assert all(label in values['live'] for label in ('前视相机', '后视相机', '左视相机', '右视相机', '深度相机 · 彩色'))
    assert values['empty'].count('未发现码') == 5 and 'ID ' not in values['empty']
    assert values['empty'].count('检测中') == 5
    for key in ('missing', 'expired', 'badElapsed', 'old'):
        assert 'ID ' not in values[key]
        assert values[key].count('>等待<') == 5
    assert values['one'].count('ID 0 · 10 · 11 · 99') == 4
    assert values['error'].count('ID 0 · 10 · 11 · 99') == 4 and '识别异常' in values['error']
    for invalid in values['invalid']:
        assert invalid.count('ID 0 · 10 · 11 · 99') == 4
        assert 'SECRET' not in invalid and '<script>' not in invalid
    html = (FRONTEND / 'index.html').read_text()
    assert html.count('id="aruco-preview-cameras"') == 1
    assert 'ArUco 标记识别' in html and 'id="vision-docking-summary"' in html
