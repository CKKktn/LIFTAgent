export const proxyUrl = path => '/proxy?path=' + encodeURIComponent(path);
export function arrayOf(data) {
  if (Array.isArray(data)) return data;
  for (const key of ['docs','chunks','chats','canvas','models','items','data']) if (Array.isArray(data?.[key])) return data[key];
  return [];
}
export async function request(method, path, body, signal) {
  const headers = {};
  if (body !== undefined && !(body instanceof FormData)) { headers['Content-Type']='application/json'; body=JSON.stringify(body); }
  const response = await fetch(proxyUrl(path), {method,headers,body,signal});
  if (response.status === 401) { location.assign('/login'+location.hash); throw new Error('登录已过期，请重新登录'); }
  return response;
}
export function checkResult(result) {
  if (result && typeof result.code === 'number' && result.code !== 0) throw new Error(result.message || `接口业务错误 ${result.code}`);
  return result;
}
export async function api(method, path, body, signal) {
  const response=await request(method,path,body,signal);
  const raw=await response.text(); let result;
  try { result=JSON.parse(raw); } catch { if (!response.ok) throw new Error(`HTTP ${response.status}：${raw.slice(0,200)}`); return {raw}; }
  if (!response.ok) throw new Error(result.message || `HTTP ${response.status}`);
  return checkResult(result);
}
// SSE 帧可能跨任意网络分片；累积解码，按空行划分事件。
export async function readSSE(response, onData) {
  if (!response.ok || !response.headers.get('content-type')?.includes('text/event-stream')) {
    const raw=await response.text(); let value;
    try { value=JSON.parse(raw); } catch { throw new Error(`非预期响应：${raw.slice(0,200)}`); }
    if (!response.ok) throw new Error(value.message || `HTTP ${response.status}`);
    checkResult(value); if(value.data && value.data!==true) onData(value.data); return;
  }
  const reader=response.body.getReader();const decoder=new TextDecoder();let buffer='';let ended=false;
  function dispatch(frame) {
    const lines=frame.split(/\r?\n/).filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trimStart());
    if(!lines.length)return;const raw=lines.join('\n').trim();if(!raw)return;
    if(raw==='[DONE]'){ended=true;return;}
    const value=checkResult(JSON.parse(raw));if(value.data===true){ended=true;return;}onData(value.data??value);
  }
  try {
    while(!ended){const {value,done}=await reader.read();buffer+=decoder.decode(value,{stream:!done});let match;
      while((match=/\r?\n\r?\n/.exec(buffer))){dispatch(buffer.slice(0,match.index));buffer=buffer.slice(match.index+match[0].length);if(ended)break;}
      if(done){if(buffer.trim()&&!ended)dispatch(buffer);break;}
    }
    if(!ended)throw new Error('回答连接提前中断，内容可能不完整，请重试。');
  } finally { await reader.cancel().catch(()=>{});reader.releaseLock(); }
}
export async function completion(body,onData,signal) { return readSSE(await request('POST','/api/v1/chat/completions',{...body,stream:true,legacy:true},signal),onData); }
