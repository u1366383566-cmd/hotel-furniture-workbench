import subprocess
from pathlib import Path


def test_late_summary_cannot_replace_selected_batch():
    script = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('static/app.js', 'utf8');
function extract(start, end) { return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start))); }
const nodes = new Map();
const node = () => ({innerText:'', style:{}, classList:{toggle(){}}});
const context = {console, encodeURIComponent, Map, Set, JSON,
 document:{getElementById(id){ if(!nodes.has(id)) nodes.set(id,node()); return nodes.get(id); }},
 state:{currentBatchId:'A',designSummaryEtag:null,designDetails:new Map(),designSignature:'',designs:[]},
 authHeaders(){return {};}, withBase(x){return x;}, renderCurrentTab(){},
 refreshWorkbenchInfo(){}, loadTechnicalFailures(){} };
let finishA;
context.fetch = url => url.includes('/A/') ? new Promise(resolve => {finishA=resolve;}) : Promise.resolve({status:200,ok:true,headers:{get(){return 'etag-B';}},json:async()=>[{id:'B-item',status:'archived',slots:{},state_revision:'b'}]});
vm.createContext(context);
vm.runInContext(extract('async function fetchDesignSummary(', '\nasync function downloadAuthenticatedZip(') + '\n' + extract('async function loadDesigns(', '\nfunction renderCurrentTab('), context);
(async()=>{const old=context.loadDesigns(true); context.state.currentBatchId='B'; await context.loadDesigns(true); finishA({status:200,ok:true,headers:{get(){return 'etag-A';}},json:async()=>[{id:'A-item',status:'archived',slots:{},state_revision:'a'}]}); await old; if(context.state.designs[0].id!=='B-item' || context.state.designSummaryEtag!=='etag-B') throw Error('late A replaced B');})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
