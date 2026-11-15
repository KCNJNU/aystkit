"""The documentation module preserves the selected robot across navigation."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

FRONTEND = Path(__file__).resolve().parents[1] / 'cloud/frontend'


def node(code, *files):
    executable = shutil.which('node')
    if not executable:
        pytest.skip('Node is required for the navigation integration check')
    result = subprocess.run([executable, '-e', code, *map(str, files)],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('requested,expected', [('', 'robot-one'), ('robot-two', 'robot-two'), ('unknown', 'unknown')])
def test_fleet_module_link_and_return_keep_robot_context(requested, expected):
    result = node(r'''
      const fs=require('fs'), vm=require('vm');
      const requested=process.argv[3];
      const warehouse={}, library={}, back={};
      const picker={replaceChildren(){},append(){},addEventListener(){}};
      const document={getElementById:id=>id==='robot-picker'?picker:id==='observer-return'?back:null,
        createElement:()=>({}),querySelectorAll:selector=>selector==='a.dbprog-nav'?[warehouse]:selector==='a.sensor-library-nav'?[library]:[]};
      const location={href:'https://observer.example/'+(requested?'?robot='+encodeURIComponent(requested):'')};
      const window={location,fetch:async()=>({ok:true,json:async()=>({defaultRobotId:'robot-one',robots:[
        {robotId:'robot-one',name:'One',warehouseId:'w1'}, {robotId:'robot-two',name:'Two',warehouseId:'w2'}]})})};
      const context=vm.createContext({window,document,URL,URLSearchParams,Headers,Request});
      vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
      window.RobotFleet.ready.then(()=>{
        const target=new URL(library.href,location.href);
        window.location={href:target.href,search:target.search};
        vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
        console.log(JSON.stringify({library:library.href,back:back.href,warehouse:warehouse.href}));
      }).catch(e=>{console.error(e);process.exitCode=1});
    ''', FRONTEND / 'fleet.js', FRONTEND / 'sensor-library/observer-module.js', requested)
    assert result['library'] == '/sensor-library/?robot=' + expected
    assert result['back'] == '/?robot=' + expected + '#robot/sensors'
    assert 'observerRobot=' + expected in result['warehouse']


def test_return_link_treats_robot_identifier_as_data_and_keeps_same_origin():
    result = node(r'''
      const fs=require('fs'),vm=require('vm');
      const link={};
      vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{
        URL,URLSearchParams,document:{getElementById:()=>link},
        window:{location:{href:'https://observer.example/sensor-library/?robot=x%26next%3Dhttps%3A%2F%2Fevil.example',
          search:'?robot=x%26next%3Dhttps%3A%2F%2Fevil.example'}}});
      const target=new URL(link.href,'https://observer.example');
      console.log(JSON.stringify({origin:target.origin,robot:target.searchParams.get('robot'),keys:[...target.searchParams.keys()],hash:target.hash}));
    ''', FRONTEND / 'sensor-library/observer-module.js')
    assert result == {'origin': 'https://observer.example', 'robot': 'x&next=https://evil.example',
                      'keys': ['robot'], 'hash': '#robot/sensors'}
