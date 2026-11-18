(()=>{
 const groups=[
 ['丝滑动态 · 轻量',[['motion-silk','丝绸流光'],['motion-aurora','极光漫游'],['motion-tide','深海潮汐'],['motion-nebula','星云缓行'],['motion-orbit','星环流转'],['motion-haze','薄雾浮光'],['motion-grid','网格漂移'],['motion-glow','呼吸柔光']]],
 ['动态背景 · Vanta',[['vanta-waves','深蓝巨浪'],['vanta-birds','幻彩群鸟'],['vanta-globe','星球连线'],['vanta-halo','星际光晕'],['vanta-rings','光轨星环'],['vanta-fog','流动云雾'],['vanta-net','粒子网络']]],
 ['简约纹理',[['plain','纯色'],['dots','点阵'],['grid','细网格'],['blueprint','蓝图'],['diagonal','斜纹'],['lines','横纹'],['vertical','竖纹'],['cross','十字点'],['microdots','微点'],['diamonds','菱形网格'],['weave','编织'],['checker','棋盘']]],
 ['几何波纹',[['rings','同心波纹'],['ripples','涟漪'],['contour','等高线'],['arcs','弧光'],['fans','折扇'],['tiles','方砖'],['isometric','立体网格'],['spotlight','聚光']]],
 ['柔和渐变',[['mesh','柔光'],['mist','雾灰'],['aurora','极光'],['dusk','暮色'],['ocean','深海'],['lavender','薰衣草'],['sunrise','晨曦'],['silver','银幕']]]
 ];
 const choices=groups.flatMap(([,items])=>items);
 const root=document.documentElement,key='lift-background',motionKey='lift-background-animation',reducedMotion=window.matchMedia?.('(prefers-reduced-motion: reduce)');let selected='plain',motionMode='auto';try{const saved=localStorage.getItem(key),mode=localStorage.getItem(motionKey);if(choices.some(([id])=>id===saved))selected=saved;if(['auto','on','off'].includes(mode))motionMode=mode;}catch{}
 root.dataset.background=selected;root.dataset.backgroundAnimation=motionMode;
 const init=()=>{
 const syncVisibility=()=>{root.dataset.backgroundPaused=String(document.hidden);};
 document.addEventListener('visibilitychange',syncVisibility);syncVisibility();
 const host=document.querySelector('.top-actions')||document.body;
 const wrap=document.createElement('div');wrap.className='background-control'+(host===document.body?' background-login':'');
 const toggle=document.createElement('button');toggle.type='button';toggle.className='background-toggle';toggle.textContent='▧';toggle.title='更换背景';toggle.setAttribute('aria-label','更换背景');toggle.setAttribute('aria-expanded','false');toggle.setAttribute('aria-controls','backgroundPanel');
 const panel=document.createElement('div');panel.id='backgroundPanel';panel.className='background-panel';panel.hidden=true;panel.setAttribute('role','region');panel.setAttribute('aria-label','背景样式');
 const heading=document.createElement('strong');heading.textContent='背景样式 · '+choices.length+' 款';panel.append(heading);
 const grid=document.createElement('div');grid.className='background-options';
 for(const [label,items]of groups){const caption=document.createElement('div');caption.className='background-category';caption.textContent=label;grid.append(caption);for(const [id,name]of items){const b=document.createElement('button');b.type='button';b.className='background-option';b.dataset.choice=id;b.setAttribute('aria-label',name);b.title=name;b.innerHTML=`<span class="background-preview" data-background="${id}" aria-hidden="true"></span><span>${name}</span>`;b.onclick=()=>{selected=id;root.dataset.background=id;try{localStorage.setItem(key,id);}catch{}update();};grid.append(b);}}
 const motionControls=document.createElement('div');motionControls.className='background-motion-controls';
 const motionLabel=document.createElement('span');motionLabel.textContent='轻量动态';
 const motionToggle=document.createElement('button');motionToggle.type='button';motionToggle.className='background-motion-toggle';
 const motionAuto=document.createElement('button');motionAuto.type='button';motionAuto.className='background-motion-auto';motionAuto.textContent='跟随系统';
 motionControls.append(motionLabel,motionToggle,motionAuto);
 function motionEnabled(){return motionMode==='on'||(motionMode==='auto'&&!reducedMotion?.matches);}
 function setMotionMode(mode){motionMode=mode;root.dataset.backgroundAnimation=mode;try{localStorage.setItem(motionKey,mode);}catch{}updateMotion();}
 function updateMotion(){motionControls.hidden=!selected.startsWith('motion-');const enabled=motionEnabled();motionToggle.textContent=enabled?'暂停动画':'开启动画';motionToggle.setAttribute('aria-label',enabled?'暂停轻量背景动画':'开启轻量背景动画');motionToggle.setAttribute('aria-pressed',String(enabled));motionAuto.setAttribute('aria-pressed',String(motionMode==='auto'));}
 motionToggle.onclick=()=>setMotionMode(motionEnabled()?'off':'on');motionAuto.onclick=()=>setMotionMode('auto');reducedMotion?.addEventListener?.('change',updateMotion);
 panel.append(motionControls,grid);const hint=document.createElement('small');hint.textContent='即时生效 · 自动记住选择';panel.append(hint);wrap.append(toggle,panel);host.append(wrap);
 function update(){grid.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.choice===selected)));updateMotion();}update();
 function close(focus=false){panel.hidden=true;toggle.setAttribute('aria-expanded','false');if(focus)toggle.focus();}
 toggle.onclick=()=>{panel.hidden=!panel.hidden;toggle.setAttribute('aria-expanded',String(!panel.hidden));if(!panel.hidden)grid.querySelector(`[data-choice="${selected}"]`).focus();};
 document.addEventListener('pointerdown',e=>{if(!wrap.contains(e.target))close();});
 wrap.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();close(true);}});
 wrap.addEventListener('focusout',e=>{if(e.relatedTarget&&!wrap.contains(e.relatedTarget))close();});
 window.addEventListener('storage',e=>{if(e.key===key){selected=choices.some(([id])=>id===e.newValue)?e.newValue:'plain';root.dataset.background=selected;update();}if(e.key===motionKey){motionMode=['auto','on','off'].includes(e.newValue)?e.newValue:'auto';root.dataset.backgroundAnimation=motionMode;updateMotion();}});
 };
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

// Animated overlay thumbs retain native content scrolling and keyboard behavior.
(()=>{
 function init(){
  const entries=new Map();let scheduled=0,scanTimer=0;
  const isRoot=e=>e===document.scrollingElement;
  function schedule(){if(!scheduled)scheduled=requestAnimationFrame(update);}
  function isVisible(el){
   if(!el.isConnected||!el.clientWidth||!el.clientHeight||!el.getClientRects().length)return false;
   if(el.checkVisibility&&!el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true,contentVisibilityAuto:true}))return false;
   // Collapsed details can retain layout boxes: they must not leave floating rails.
   for(let p=el;p;p=p.parentElement){
    if(p.hidden||getComputedStyle(p).visibility==='hidden')return false;
    if(p.tagName==='DETAILS'&&!p.open){const summary=p.querySelector(':scope > summary');if(el!==p&&!summary?.contains(el))return false;}
   }
   return true;
  }
  function canScroll(el,axis,css=getComputedStyle(el)){
   if(el.classList.contains('chat-jump-nav')||el.closest('#chatMessages .message'))return false;
   const vertical=axis==='y',overflow=vertical?css.overflowY:css.overflowX;
   if(isRoot(el)){if(/hidden|clip/.test(overflow))return false;}
   else if(!/^(auto|scroll|overlay)$/.test(overflow))return false;
   return el[vertical?'scrollHeight':'scrollWidth']>el[vertical?'clientHeight':'clientWidth']+4;
  }
  function remove(el,item){item.rails.forEach(s=>s.rail.remove());el.classList.remove('smooth-scroll-native');entries.delete(el);ro.unobserve(el);}

  function create(el){
   const parent=el.closest('dialog')||document.body;
   const item={el,rails:[]};
   for(const axis of ['y','x']){
    const rail=document.createElement('div'),thumb=document.createElement('div');
    rail.className='smooth-scroll-rail axis-'+axis;rail.hidden=true;rail.dataset.scrollOwner=el.id||el.tagName.toLowerCase();rail.dataset.scrollUi='';rail.setAttribute('role','scrollbar');rail.tabIndex=0;rail.setAttribute('aria-label',axis==='y'?'垂直滚动条':'水平滚动条');rail.setAttribute('aria-orientation',axis==='y'?'vertical':'horizontal');rail.setAttribute('aria-valuemin','0');thumb.className='smooth-scroll-thumb';rail.append(thumb);parent.append(rail);
    const state={rail,thumb,axis,travel:0,max:0,start:0,length:0};item.rails.push(state);
    const moveTo=value=>{el[axis==='y'?'scrollTop':'scrollLeft']=Math.max(0,Math.min(state.max,value));schedule();};
    rail.addEventListener('pointerdown',e=>{
     if(e.button!==0)return;e.preventDefault();rail.focus({preventScroll:true});
     const point=axis==='y'?e.clientY:e.clientX,pos=el[axis==='y'?'scrollTop':'scrollLeft'];
     if(e.target!==thumb)moveTo((point-state.start-state.length/2)/Math.max(1,state.travel)*state.max);
     rail.classList.add('dragging');rail.setPointerCapture(e.pointerId);
     state.drag={point,pos:e.target===thumb?pos:el[axis==='y'?'scrollTop':'scrollLeft']};
    });
    rail.addEventListener('pointermove',e=>{if(!state.drag)return;const point=axis==='y'?e.clientY:e.clientX;moveTo(state.drag.pos+(point-state.drag.point)/Math.max(1,state.travel)*state.max);});
    const release=()=>{state.drag=null;rail.classList.remove('dragging');schedule();};
    rail.addEventListener('pointerup',release);rail.addEventListener('pointercancel',release);rail.addEventListener('lostpointercapture',release);
    rail.addEventListener('keydown',e=>{const pos=el[axis==='y'?'scrollTop':'scrollLeft'],size=el[axis==='y'?'clientHeight':'clientWidth'];let value;
     if(e.key==='Home')value=0;else if(e.key==='End')value=state.max;else if(e.key==='PageDown')value=pos+size*.9;else if(e.key==='PageUp')value=pos-size*.9;else if(e.key===(axis==='y'?'ArrowDown':'ArrowRight'))value=pos+40;else if(e.key===(axis==='y'?'ArrowUp':'ArrowLeft'))value=pos-40;
     if(value!==undefined){e.preventDefault();moveTo(value);}
    });
    rail.addEventListener('wheel',e=>{e.preventDefault();el.scrollBy({top:e.deltaY*(e.deltaMode===1?16:1),left:e.deltaX*(e.deltaMode===1?16:1),behavior:'instant'});schedule();},{passive:false});
   }
   el.classList.add('smooth-scroll-native');entries.set(el,item);ro.observe(el);
  }
  function scan(){
   scanTimer=0;
   for(const [el,item]of entries)if(!isVisible(el)||(!canScroll(el,'x')&&!canScroll(el,'y')))remove(el,item);
   const elements=[document.scrollingElement,...document.querySelectorAll('body *')];
   for(const el of elements){if(!el||entries.has(el)||el.closest('[data-scroll-ui]')||!isVisible(el))continue;
    const css=getComputedStyle(el);
    if(canScroll(el,'y',css)||canScroll(el,'x',css))create(el);
   }schedule();
  }
  function requestScan(){if(!scanTimer)scanTimer=setTimeout(scan,120);}
  const ro=new ResizeObserver(()=>{schedule();requestScan();});ro.observe(document.body);
  function update(){
   scheduled=0;
   const modal=document.querySelector('dialog[open]');
   for(const [el,item]of entries){
    if(!isVisible(el)){item.rails.forEach(s=>s.rail.hidden=true);continue;}
    const style=getComputedStyle(el);
    const base=isRoot(el)?{top:0,left:0,right:innerWidth,bottom:innerHeight,width:innerWidth,height:innerHeight}:el.getBoundingClientRect();
    let top=Math.max(0,base.top),bottom=Math.min(innerHeight,base.bottom),left=Math.max(0,base.left),right=Math.min(innerWidth,base.right);
    for(let p=el.parentElement;p&&p!==document.body;p=p.parentElement){const css=getComputedStyle(p);if(/auto|scroll|hidden|clip/.test(css.overflowY)){const r=p.getBoundingClientRect();top=Math.max(top,r.top);bottom=Math.min(bottom,r.bottom);}if(/auto|scroll|hidden|clip/.test(css.overflowX)){const r=p.getBoundingClientRect();left=Math.max(left,r.left);right=Math.min(right,r.right);}}
    for(const s of item.rails){
     const vertical=s.axis==='y',client=el[vertical?'clientHeight':'clientWidth'],total=el[vertical?'scrollHeight':'scrollWidth'],pos=el[vertical?'scrollTop':'scrollLeft'];
     const size=(vertical?bottom-top:right-left)-4;
     const visible=canScroll(el,s.axis,style)&&size>24&&right>left&&bottom>top&&(!modal||modal.contains(el));s.rail.hidden=!visible;if(!visible)continue;
     s.max=total-client;s.length=Math.max(24,Math.min(size,size*client/total));s.travel=size-s.length;s.start=(vertical?top:left)+2;
     Object.assign(s.rail.style,vertical?{top:top+2+'px',left:right-13+'px',height:size+'px'}:{top:bottom-13+'px',left:left+2+'px',width:size+'px'});
     const offset=Math.max(0,Math.min(s.travel,s.travel*pos/Math.max(1,s.max)));
     if(vertical){s.thumb.style.height=s.length+'px';s.thumb.style.transform=`translateY(${offset}px)`;}else{s.thumb.style.width=s.length+'px';s.thumb.style.transform=`translateX(${offset}px)`;}
     s.rail.setAttribute('aria-valuemax',String(Math.round(s.max)));s.rail.setAttribute('aria-valuenow',String(Math.round(pos)));
    }
   }
  }
  document.addEventListener('scroll',schedule,{capture:true,passive:true});window.addEventListener('resize',()=>{schedule();requestScan();});document.addEventListener('load',requestScan,true);
  new MutationObserver(records=>{if(records.some(r=>!r.target.closest?.('[data-scroll-ui]')))requestScan();}).observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['class','hidden','open']});
  document.addEventListener('toggle',()=>{schedule();requestScan();},true);
  document.addEventListener('transitionend',requestScan);scan();
 }
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();

// Vanta 0.5.24 / Three r134: local WebGL assets, one renderer at a time.
(()=>{
 const root=document.documentElement,loaded=new Map(),reducedMotion=window.matchMedia?.('(prefers-reduced-motion: reduce)');let effect=null,layer=null,generation=0;
 const kinds=['waves','birds','globe','halo','rings','fog','net'];
 function script(name){if(!loaded.has(name))loaded.set(name,new Promise((resolve,reject)=>{const s=document.createElement('script');s.src='/vendor/vanta/'+name;s.onload=resolve;s.onerror=()=>{loaded.delete(name);s.remove();reject(Error('背景资源加载失败'));};document.head.append(s);}));return loaded.get(name);}
 function status(text){const el=document.querySelector('.background-panel>small');if(el)el.textContent=text;}
 function motionStatus(){const mode=root.dataset.backgroundAnimation||'auto';status(mode==='off'?'轻量动态已暂停 · 点击开启动画':mode!=='on'&&reducedMotion?.matches?'系统已减少动态效果 · 可点击开启动画在本页播放':'轻量动态正在播放 · 自动记住选择');}
 function release(){
  const old=effect;effect=null;
  if(old){
   const renderer=old.renderer;
   // Halo feedback buffers and bird simulation textures are outside the scene.
   old.bufferTarget?.dispose();old.bufferFeedback?.dispose();
   for(const variable of old.gpuCompute?.variables||[]){variable.initialValueTexture?.dispose();variable.material?.dispose();for(const target of variable.renderTargets||[])target.dispose();}
   // Vanta removes the canvas itself when initialization fails.
   if(renderer?.domElement?.parentNode!==old.el)old.renderer=null;
   try{old.destroy();}finally{renderer?.dispose();renderer?.forceContextLoss();}
  }
  layer?.remove();layer=null;
 }
 async function sync(){
  const ticket=++generation;release();
  const kind=(root.dataset.background||'').replace('vanta-','');
  if(kind.startsWith('motion-')){motionStatus();return;}
  if(!kinds.includes(kind)){status('即时生效 · 自动记住选择');return;}
  if(document.hidden)return;
  status('正在加载 WebGL 背景…');
  try{
   await script('three.min.js');await script('vanta.'+kind+'.min.js');if(ticket!==generation)return;
   layer=document.createElement('div');layer.className='vanta-background';layer.setAttribute('aria-hidden','true');document.body.prepend(layer);
   const light=root.dataset.theme==='light',base=light?0xf1f1f1:0x111111;
   const configs={
    waves:{color:light?0x8cb9c9:0x155b7d,shininess:65,waveHeight:28,waveSpeed:1.05,zoom:.8},
    birds:{backgroundColor:light?0xeaf0f6:0x091322,color1:light?0x256f94:0x35cde3,color2:light?0x7852a3:0xa173f5,colorMode:'lerpGradient',birdSize:1.25,wingSpan:28,speedLimit:5,separation:32,alignment:28,cohesion:24,quantity:4},
    globe:{backgroundColor:light?0xecf1f4:0x0b1423,color:light?0x2881a0:0x35b9cb,color2:light?0x7585ac:0x778cdb,size:1,points:7,maxDistance:23,spacing:18,showDots:true},
    halo:{backgroundColor:light?0xdde7ef:0x090f23,baseColor:light?0x306693:0x184f9a,color2:light?0x6c72b2:0x87dced,amplitudeFactor:1.2,ringFactor:1.15,rotationFactor:1.1,size:1.25,speed:1.1,xOffset:.16,yOffset:.04},
    rings:{backgroundColor:light?0xe8edf2:0x0b1322,color:0x58d4df},
    fog:{highlightColor:light?0xdadada:0x757575,midtoneColor:light?0xbcbcbc:0x3b3b3b,lowlightColor:base,baseColor:base,blurFactor:.55,speed:1.6,zoom:1},
    net:{color:light?0x797979:0x858585,backgroundColor:base,points:7,maxDistance:22,spacing:19}
   };
   const quality=Math.max(1,devicePixelRatio)*(kind==='halo'?1.25:1);
   const instance=window.VANTA[kind.toUpperCase()]({el:layer,THREE:window.THREE,mouseControls:true,touchControls:false,gyroControls:false,minHeight:100,minWidth:100,scale:quality,scaleMobile:Math.max(1.5,quality),...configs[kind]});effect=instance;
   if(!instance.renderer||!instance.scene||!instance.camera||!layer.contains(instance.renderer.domElement))throw Error('WebGL 背景未能启动，请检查浏览器硬件加速');
   status('Vanta · 正在播放 · 移动鼠标可互动');
   // Count completed animation frames, not render passes (Halo renders twice).
   let frames=0,start=performance.now(),slow=0;const afterRender=instance.afterRender;
   instance.afterRender=()=>{afterRender?.call(instance);frames++;const now=performance.now();if(now-start>=2000){const fps=Math.round(frames*1000/(now-start));status(`Vanta · 实时 ${fps} fps · 自动性能优化`);slow=fps<45?slow+1:0;if(slow>=2&&instance.options.scale<Math.max(1,devicePixelRatio)*2.5){instance.options.scale*=1.2;instance.options.scaleMobile*=1.2;instance.resize();slow=0;}frames=0;start=now;}};
  }catch(e){if(ticket!==generation)return;if(!effect&&window.VANTA?.current?.el===layer)effect=window.VANTA.current;release();status(e.message+'；已使用纯色背景');}
 }
 const init=()=>{new MutationObserver(sync).observe(root,{attributes:true,attributeFilter:['data-background','data-theme','data-background-animation']});document.addEventListener('visibilitychange',sync);reducedMotion?.addEventListener?.('change',()=>{if(root.dataset.background?.startsWith('motion-'))motionStatus();});sync();};
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
