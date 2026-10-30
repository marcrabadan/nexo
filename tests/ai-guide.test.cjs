const {test} = require('node:test');
const assert = require('node:assert/strict');
const {copyPrompt} = require('../docs/assets/ai-guide.js');
function fixture() {
  return {area: {value:'Nexo prompt', focus(){this.focused=true;}, select(){this.selected=true;}}, status:{textContent:''}, button:{disabled:false}};
}
test('copy writes exact prompt and announces success', async()=>{
  const {area,status,button}=fixture(); let copied;
  await copyPrompt(area,status,button,{writeText:async text=>{copied=text;}});
  assert.equal(copied,'Nexo prompt'); assert.match(status.textContent,/Prompt copied/); assert.equal(button.disabled,false);
});
test('denied clipboard selects text and never announces success',async()=>{
  const {area,status,button}=fixture();
  await copyPrompt(area,status,button,{writeText:async()=>{throw Error('denied');}});
  assert.equal(area.selected,true);assert.equal(area.focused,true);assert.match(status.textContent,/Command\+C/);assert.doesNotMatch(status.textContent,/Prompt copied/);
});
test('missing clipboard falls back to manual copy',async()=>{
 const {area,status,button}=fixture(); await copyPrompt(area,status,button,undefined);assert.equal(area.selected,true);assert.equal(button.disabled,false);
});
test('async copy captures original selection and prevents duplicate click',async()=>{
 const {area,status,button}=fixture();let resolve,copied;
 const pending=copyPrompt(area,status,button,{writeText:text=>{copied=text;return new Promise(r=>resolve=r);}});
 assert.equal(button.disabled,true);area.value='Changed';resolve();await pending;assert.equal(copied,'Nexo prompt');assert.equal(button.disabled,false);
});
const engineering = require('../docs/assets/ai-engineering.js');
const rubric = require('../docs/ai-guide/source/readiness.json');
test('unknown/evidenceless results earn no readiness credit',()=>{
 const report=engineering.readiness(rubric,{functional:{status:'Passed'}});
 assert.equal(report.score,0);assert.ok(report.blockers.includes('functional'));
});
test('critical failure blocks despite a high score; all N/A has no score',()=>{
 const values=Object.fromEntries(rubric.categories.map(c=>[c.id,{status:'Passed',evidence:'reviewed artifact'}]));
 values.security.status='Failed';assert.equal(engineering.readiness(rubric,values).score,87.5);assert.ok(engineering.readiness(rubric,values).blockers.includes('security'));
 rubric.categories.forEach(c=>values[c.id].status='Not applicable');assert.equal(engineering.readiness(rubric,values).score,null);
});
test('costs are optional and require finite nonnegative pricing',()=>{
 assert.equal(engineering.cost(1000000,500000,2,4),4);assert.equal(engineering.cost(1,1,'',0),null);assert.equal(engineering.cost(1,1,-1,0),null);assert.equal(engineering.cost(1,1,Infinity,0),null);
});
test('ADR remains Proposed and blank onboarding preserves unknowns',()=>{
 assert.throws(()=>engineering.draftAdr({}),/Complete/);
 const values=Object.fromEntries(['context','decision','alternatives','consequences','cost_complexity','reconsider_when'].map(k=>[k,'review me']));
 assert.match(engineering.draftAdr(values),/Status: Proposed/);assert.match(engineering.onboarding([]),/Missing information/);assert.match(engineering.onboarding([]),/Assumptions: none inferred/);
});
