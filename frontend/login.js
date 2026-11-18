let initialized=true,ready=false;
const form=document.getElementById('login'),button=document.getElementById('submit'),error=document.getElementById('error');
button.disabled=true;
fetch('/auth/status').then(r=>r.json()).then(status=>{
 initialized=status.initialized;
 if(!initialized){document.getElementById('loginHint').textContent='首次使用，请设置本地管理员账号与密码';document.getElementById('username').value='admin';document.getElementById('password').minLength=8;document.getElementById('password').autocomplete='new-password';document.getElementById('confirmPasswordField').hidden=false;button.textContent='创建管理员并进入';}
 ready=true;button.disabled=false;
}).catch(()=>{error.textContent='无法连接后端，请确认服务已启动';});
form.addEventListener('submit',async event=>{
 event.preventDefault();button.disabled=true;error.textContent='';
 try{if(!ready)throw Error('正在连接本地服务，请稍候');const password=document.getElementById('password').value;if(!initialized&&password!==document.getElementById('confirmPassword').value)throw Error('两次输入的密码不一致');
 const response=await fetch(initialized?'/auth/login':'/auth/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.getElementById('username').value,password})});
 const result=await response.json();if(!response.ok)throw Error(result.message||'登录失败');location.replace('/'+location.hash);
 }catch(e){error.textContent=e.message;}finally{button.disabled=false;}
});
