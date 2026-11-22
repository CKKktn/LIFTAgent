import assert from 'node:assert/strict';
import test from 'node:test';
import {collectDocumentIds} from '../frontend/upload-filter.js';

test('selects all 251 files across pages',async()=>{
 const calls=[];
 const ids=await collectDocumentIds(async page=>{
  calls.push(page);return {docs:Array.from({length:Math.min(100,251-(page-1)*100)},(_,i)=>({id:String((page-1)*100+i)})),total:251};
 });
 assert.equal(ids.size,251);assert.deepEqual(calls,[1,2,3]);assert.ok(ids.has('250'));
});
test('uses the filtered total rather than requesting unrelated files',async()=>{
 let calls=0;const ids=await collectDocumentIds(async()=>{calls++;return {docs:[{id:'matched'}],total:1};});
 assert.deepEqual([...ids],['matched']);assert.equal(calls,1);
});
test('discards a response after the knowledge base or search changed',async()=>{
 let current=true;
 assert.equal(await collectDocumentIds(async()=>{current=false;return {docs:[{id:'old'}],total:1};},()=>current),null);
});
test('an incomplete list cannot silently become a partial selection',async()=>{
 await assert.rejects(collectDocumentIds(async page=>({docs:page===1?[{id:'one'}]:[],total:2})),/文件列表/);
});
test('request failure leaves selection to the caller unchanged',async()=>{
 await assert.rejects(collectDocumentIds(async()=>{throw Error('network failed');}),/network failed/);
});
test('empty knowledge base produces an empty selection',async()=>{
 assert.equal((await collectDocumentIds(async()=>({docs:[],total:0}))).size,0);
});
