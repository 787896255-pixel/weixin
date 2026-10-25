'use strict';
const $=id=>document.getElementById(id),token=document.querySelector('meta[name="app-token"]').content;
const state={settings:{},people:[],selected:null,job:null,lastJob:null,report:null,offset:0,rawTotal:0,request:0,allPeople:[],checked:new Set(),checkedMessages:new Set(),rawItems:[],page:'home',panel:'chat',rawLoading:0};let timer,toastTimer;
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,5500);}
async function api(path,data){const response=await fetch(path,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-App-Token':token},body:data===undefined?undefined:JSON.stringify(data)});const value=await response.json();if(!response.ok)throw new Error(value.error||'操作失败');return value;}
const guard=fn=>async(...a)=>{try{await fn(...a);}catch(e){toast(e.message);}};
// Local API resources only: never fetch remote profile images or invent identities.
function localAvatarURL(value){
  if(typeof value!=='string'||!value.trim())return '';
  try{
    const url=new URL(value,location.href);
    if(!['http:','https:'].includes(url.protocol)||url.origin!==location.origin||url.username||url.password)return '';
    // HTTP respond owns avatar signing; preserve the supplied session query unchanged.
    return url.pathname+url.search;
  }catch(_){return '';}
}
function makeAvatar(url,cls='avatar',name=''){
  const node=el('span',cls),img=el('img');img.alt='';img.loading='lazy';img.decoding='async';
  img.src=localAvatarURL(url)||'/icons/user.svg';
  img.classList.toggle('avatar-placeholder',!localAvatarURL(url));
  img.onerror=()=>{img.onerror=null;img.src='/icons/user.svg';img.classList.add('avatar-placeholder');};
  node.append(img);if(name)node.setAttribute('aria-label',name+'的头像');return node;
}
function setAvatar(target,url,name=''){
  const avatar=makeAvatar(url,target.className,name);target.replaceChildren(...avatar.childNodes);
}
function isGroup(person){return !!(person?.is_group||person?.type==='group'||String(person?.peer_id||'').endsWith('@chatroom'));}
Object.assign(state,{peopleRequest:0,rawStart:0,chatRefreshJob:null,viewport:null});
const liveUI={source:null,epoch:0,polling:false,loadedRevision:null,refreshing:false,value:null,configuring:false};
function renderConversationHeading(){
  const person=current();if(!person)return;
  $('person-name').textContent=person.name||person.peer_id||'未命名会话';
  $('person-meta').textContent='消息数 '+Number(person.message_count||0).toLocaleString();
  $('person-meta').title=[person.peer_id,person.first_time,person.last_time].filter(Boolean).join(' · ');
  setAvatar($('person-avatar'),person.avatar_url,person.name);busy(state.job?.status==='running');
  $('raw-summary').textContent='聊天记录';
}
function messageIsSelf(message){return message.is_self===true||message.is_self===1||message.is_self==='1';}
function captureMessageScroll(){
  const box=$('raw-messages');
  if(!box.getClientRects().length)return state.viewport||{atBottom:true,top:0,anchor:null,anchorTop:0};
  const top=box.getBoundingClientRect().top;
  const first=[...box.querySelectorAll('.raw-message')].find(node=>node.getBoundingClientRect().bottom>top+2);
  return {atBottom:box.scrollHeight-box.scrollTop-box.clientHeight<80,top:box.scrollTop,
    anchor:first?.dataset.messageId,anchorTop:first?first.getBoundingClientRect().top-top:0};
}
function restoreMessageScroll(snapshot,toBottom=false){
  const box=$('raw-messages');
  if(!box.getClientRects().length){state.viewport=toBottom?{atBottom:true,top:0,anchor:null,anchorTop:0}:snapshot;return;}
  if(toBottom||snapshot?.atBottom)box.scrollTop=box.scrollHeight;
  else if(snapshot){
    const anchor=[...box.querySelectorAll('.raw-message')].find(node=>node.dataset.messageId===snapshot.anchor);
    box.scrollTop=anchor?box.scrollTop+anchor.getBoundingClientRect().top-box.getBoundingClientRect().top-snapshot.anchorTop:snapshot.top;
  }
  state.viewport=captureMessageScroll();$('jump-latest').hidden=state.viewport.atBottom&&state.offset>=state.rawTotal;
}
// Only visible chat images enter the existing bounded media preparation queue.
const inlineImages={observer:null,waiting:[],running:0,active:new Set(),attempted:new Set()};
function prepareVisibleImages(){
  inlineImages.observer?.disconnect();inlineImages.waiting=[];
  const root=$('raw-messages');
  if(!('IntersectionObserver' in window))return; // Explicit preview buttons remain available.
  inlineImages.observer=new IntersectionObserver(entries=>{
    for(const entry of entries){
      const wrap=entry.target,task=wrap.inlineImage;
      if(!entry.isIntersecting||!task||inlineImages.attempted.has(task.key)||inlineImages.active.has(task.key)||
        inlineImages.waiting.some(next=>next.key===task.key))continue;
      inlineImages.waiting.push(task);
    }
    drainImagePreviews();
  },{root,rootMargin:'0px',threshold:0});
  root.querySelectorAll('.media-preview').forEach(wrap=>{if(wrap.inlineImage)inlineImages.observer.observe(wrap);});
}
function drainImagePreviews(){
  while(inlineImages.running<2&&inlineImages.waiting.length){
    const task=inlineImages.waiting.shift(),root=$('raw-messages');
    if(!task.wrap.isConnected||task.source!==accountUI.active?.id||task.button.disabled||inlineImages.active.has(task.key))continue;
    const rect=task.wrap.getBoundingClientRect(),view=root.getBoundingClientRect();
    if(!root.clientHeight||rect.bottom<view.top||rect.top>view.bottom)continue;
    inlineImages.running++;inlineImages.active.add(task.key);inlineImages.attempted.add(task.key);
    if(inlineImages.attempted.size>512)inlineImages.attempted.delete(inlineImages.attempted.values().next().value);
    // appendMedia already POSTs /api/media/prepare and polls only this asset's status.
    Promise.resolve().then(()=>task.load()).catch(()=>{}).finally(()=>{
      inlineImages.running--;inlineImages.active.delete(task.key);
      if(!task.parent.isConnected)inlineImages.attempted.delete(task.key);
      else{
        task.parent.querySelectorAll('img').forEach(img=>img.addEventListener('load',()=>{
          if(state.viewport&&img.isConnected)restoreMessageScroll(state.viewport);
        },{once:true}));
        if(state.viewport)restoreMessageScroll(state.viewport);
      }
      prepareVisibleImages();
    });
  }
}
function appendChatMedia(item,parent){
  appendMedia(item,parent);
  if(item.kind!=='image'||item.status==='ready')return;
  const wrap=parent.lastElementChild,button=wrap?.querySelector('button');
  if(!button?.onclick)return;
  const source=accountUI.active?.id;
  wrap.inlineImage={wrap,parent,button,source,key:source+'|'+item.id,load:button.onclick};
}
function renderMessages(snapshot=null,toBottom=false){
  const box=$('raw-messages'),fragment=document.createDocumentFragment(),person=current();let lastTime='',lastStamp=null;
  for(const message of state.rawItems){
    const self=messageIsSelf(message),row=el('div','raw-message'+(self?' self':''));row.dataset.messageId=String(message.id);
    // Source timestamps are displayed verbatim; no generated date or guessed sender.
    const time=typeof message.time==='string'&&message.time!=='未知'?message.time:'';
    const timestamp=time?Date.parse(time.replace(' ','T')):NaN;
    if(time&&time!==lastTime&&(lastStamp===null||!Number.isFinite(timestamp)||Math.abs(timestamp-lastStamp)>=300000)){
      const stamp=el('time','message-time',time);stamp.dateTime=time;fragment.append(stamp);
      lastTime=time;lastStamp=Number.isFinite(timestamp)?timestamp:null;
    }
    if(time)row.title=time;
    const label=el('label','message-select'),check=el('input');check.type='checkbox';check.checked=state.checkedMessages.has(message.id);
    check.setAttribute('aria-label','选择'+(time?' '+time:'')+' 的消息');check.dataset.messageId=message.id;
    check.onchange=()=>{check.checked?state.checkedMessages.add(message.id):state.checkedMessages.delete(message.id);updateRawCount();};label.append(check);
    const avatar=self?(message.self_avatar_url||message.avatar_url||accountUI.active?.avatar_url):
      (message.sender_avatar_url||message.avatar_url||(!isGroup(person)?person?.avatar_url:''));
    const body=el('div','message-body');
    if(!self&&isGroup(person)&&message.sender)body.append(el('div','message-sender',message.sender));
    const attachments=Array.isArray(message.attachments)?message.attachments:[];
    const content=typeof message.content==='string'?message.content:'';
    const mediaOnly=attachments.some(item=>['image','audio','video'].includes(item.kind))&&
      (!content.trim()||/^\[(图片|语音|视频|表情|动画表情)\]$/.test(content.trim()));
    if(!mediaOnly&&content)body.append(el('div','chat-bubble',content));
    for(const item of attachments)appendChatMedia(item,body);
    row.append(label,makeAvatar(avatar,'message-avatar'),body);fragment.append(row);
  }
  if(!state.rawItems.length)fragment.append(el('p','messages-empty','没有符合条件的本地消息'));
  box.replaceChildren(fragment);$('raw-more').hidden=state.rawStart<=0;
  box.querySelectorAll('img').forEach(img=>img.addEventListener('load',()=>{
    if(state.viewport&&box.contains(img))restoreMessageScroll(state.viewport);
  },{once:true}));
  restoreMessageScroll(snapshot,toBottom);updateRawCount();prepareVisibleImages();
}
async function messagePage(id,offset,filters=rawFilters()){
  const result=await api('/api/messages?'+new URLSearchParams({id,offset,...filters,types:filters.types.join(',')}));
  return {...result,items:Array.isArray(result.items)?result.items:[]};
}
async function reloadCurrentConversation(){
  if(!state.selected||liveUI.refreshing||state.rawLoading===state.request)return false;
  const id=state.selected,source=accountUI.active?.id,seq=++state.request;
  const filters=state.appliedFilters?JSON.parse(state.appliedFilters):rawFilters();
  const before=captureMessageScroll(),size=Math.max(100,state.rawItems.length),originalStart=state.rawStart;
  let start=originalStart;
  liveUI.refreshing=true;
  try{
    let first=await messagePage(id,start,filters);
    if(seq!==state.request||source!==accountUI.active?.id)return false;
    const follow=before.atBottom&&captureMessageScroll().atBottom;
    const target=follow?Math.max(0,Number(first.total||0)-size):Math.min(start,Math.max(0,Number(first.total||0)-1));
    if(target!==start){start=target;first=await messagePage(id,start,filters);}
    const messages=[...first.items];let offset=start+messages.length,hasMore=first.has_more;
    while(hasMore&&messages.length<size){
      const next=await messagePage(id,offset,filters);
      if(seq!==state.request||source!==accountUI.active?.id)return false;
      if(!next.items.length)break;
      messages.push(...next.items);offset+=next.items.length;hasMore=next.has_more;
    }
    if(seq!==state.request||id!==state.selected||source!==accountUI.active?.id)return false;
    let viewport=captureMessageScroll();
    // A user can scroll upward while the tail request is in flight. Keep their older window.
    if(!viewport.atBottom&&viewport.anchor&&!messages.some(message=>String(message.id)===viewport.anchor)&&start!==originalStart){
      start=originalStart;const original=await messagePage(id,start,filters);
      messages.splice(0,messages.length,...original.items);let nextOffset=start+messages.length;
      let more=original.has_more;
      while(more&&messages.length<size){
        const next=await messagePage(id,nextOffset,filters);
        if(seq!==state.request||source!==accountUI.active?.id)return false;
        if(!next.items.length)break;messages.push(...next.items);nextOffset+=next.items.length;more=next.has_more;
      }
      if(seq!==state.request||source!==accountUI.active?.id)return false;viewport=captureMessageScroll();
    }
    state.rawItems=messages;state.rawStart=start;state.rawTotal=Number(first.total)||0;state.offset=start+messages.length;
    renderMessages(viewport);renderConversationHeading();return true;
  }finally{liveUI.refreshing=false;}
}
async function applyMessageFilters(){
  ++state.request;state.rawLoading=0;state.rawStart=0;state.offset=0;state.rawItems=[];state.checkedMessages.clear();
  $('raw-messages').replaceChildren();updateRawCount();await loadRaw();
}
function resetLiveSource(source){
  if(liveUI.source===source)return;
  liveUI.source=source||null;++liveUI.epoch;liveUI.loadedRevision=null;liveUI.value=null;liveUI.configuring=false;
  inlineImages.observer?.disconnect();inlineImages.waiting=[];inlineImages.attempted.clear();
  $('live-enabled').checked=false;$('live-enabled').disabled=true;
  $('live-status').textContent=source?'正在获取同步状态…':'未选择微信源';$('live-status').title='';
  $('live-error').hidden=true;
}
function liveRevision(value){
  if(value.revision!==undefined&&value.revision!==null)return 'revision:'+String(value.revision);
  if(value.changed!==undefined&&typeof value.changed!=='boolean')return 'changed:'+String(value.changed);
  return 'sync:'+String(value.last_sync||'')+':'+String(value.new_messages||0)+':'+String(!!value.changed);
}
function renderLiveState(value){
  liveUI.value=value;if(!liveUI.configuring)$('live-enabled').checked=!!value.enabled;
  $('live-enabled').disabled=liveUI.configuring;
  $('live-status').textContent=value.error?'同步未完成':value.running?'正在同步…':value.enabled?'自动同步已开启':'自动同步已暂停';
  $('live-status').classList.toggle('sync-error',!!value.error);
  $('live-status').title=[value.last_sync?'上次同步：'+value.last_sync:'',value.interval_seconds?'同步间隔：'+value.interval_seconds+' 秒':'',value.error||''].filter(Boolean).join('\n');
  $('live-error').hidden=!value.error;$('live-error').textContent=value.error||'';
}
async function pollLiveState(){
  const source=accountUI.active?.id;
  resetLiveSource(source);
  if(!source||!accountUI.active?.ready||state.page==='home'||liveUI.polling)return;
  const epoch=liveUI.epoch;liveUI.polling=true;
  try{
    const value=await api('/api/live-state?'+new URLSearchParams({source_id:source}));
    if(epoch!==liveUI.epoch||source!==accountUI.active?.id||value.source_id&&value.source_id!==source)return;
    renderLiveState(value);
    const revision=liveRevision(value),explicit=value.revision!=null||typeof value.changed==='number'||typeof value.changed==='string';
    const changed=explicit?liveUI.loadedRevision!==null&&revision!==liveUI.loadedRevision:!!value.changed||Number(value.new_messages)>0;
    if(value.running)return;
    if((changed||liveUI.loadedRevision===null&&(!!value.changed||Number(value.new_messages)>0))&&revision!==liveUI.loadedRevision){
      await refreshPeople({autoSelect:false});
      if(epoch!==liveUI.epoch||source!==accountUI.active?.id)return;
      if(state.selected){if(!await reloadCurrentConversation())return;}
      else if(state.people.length)await select(state.people[0].id);
    }
    if(epoch===liveUI.epoch)liveUI.loadedRevision=revision;
  }catch(error){
    if(epoch!==liveUI.epoch||source!==accountUI.active?.id)return;
    $('live-enabled').disabled=true;$('live-status').textContent='实时状态暂不可用';
    $('live-status').title=error.message;$('live-error').textContent='实时读取状态：'+error.message;$('live-error').hidden=false;
  }finally{liveUI.polling=false;}
}
async function requestChatRefresh(mode){
  if(!state.selected||!accountUI.active?.id)throw new Error('先选择微信账号和会话');
  if(state.job?.status==='running')throw new Error('正在读取，请等待当前任务完成');
  const source=accountUI.active.id,id=state.selected;
  busy(true);
  try{
    const job=await api('/api/chat-refresh',{source_id:source,conversation_id:id,mode});
    state.chatRefreshJob=job.id;await updateJob(job);
  }finally{busy(state.job?.status==='running');}
}

function current(){return state.people.find(p=>p.id===state.selected);}
function conversationPreview(value,limit=50){
  if(typeof value!=='string')return '';
  // Presentation only. Bound work before cleaning large XML payloads; never alter the source message.
  let text=value.slice(0,16384);
  const entities={amp:'&',lt:'<',gt:'>',quot:'"',apos:"'",nbsp:' ',
    ldquo:'“',rdquo:'”',lsquo:'‘',rsquo:'’',mdash:'—',ndash:'–',hellip:'…'};
  for(let pass=0;pass<3;pass++){
    text=text.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi,(entity,key)=>{
      if(key[0]!=='#')return entities[key.toLowerCase()]??entity;
      const code=key[1]?.toLowerCase()==='x'?parseInt(key.slice(2),16):parseInt(key.slice(1),10);
      return code>0&&code<=0x10ffff&&!(code>=0xd800&&code<=0xdfff)?String.fromCodePoint(code):' ';
    }).replace(/<!\[CDATA\[|\]\]>/gi,' ')
      .replace(/<!--[\s\S]*?(?:-->|$)/g,' ')
      .replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi,' ')
      .replace(/<[^>]*>/g,' ')
      .replace(/<[!?/a-z_][^>]*$/gi,' ');
  }
  text=text.replace(/[\s\u0000-\u001f\u007f-\u009f\u200b\ufeff]+/g,' ').trim();
  const chars=Array.from(text);
  return chars.length>limit?chars.slice(0,Math.max(0,limit-1)).join('')+'…':text;
}
function renderPeople(){
  const list=$('people-list'),scroll=list.scrollTop;list.replaceChildren();
  $('people-count').textContent=state.people.length;
  $('batch-count').textContent='已选 '+state.checked.size;
  $('select-visible').checked=!!state.people.length&&state.people.every(p=>state.checked.has(p.id));
  list.classList.toggle('batch-mode',$('batch-mode').checked);
  if(!state.people.length){list.append(el('p','session-empty','没有符合条件的会话'));return;}
  for(const p of state.people){
    const row=el('div','person-row'+(p.id===state.selected?' selected':''));row.setAttribute('role','listitem');
    const check=el('input');check.type='checkbox';check.checked=state.checked.has(p.id);
    const accessibleName=conversationPreview(p.name||p.peer_id||'此会话');
    check.setAttribute('aria-label','选择 '+accessibleName);
    check.onchange=()=>{check.checked?state.checked.add(p.id):state.checked.delete(p.id);renderPeople();};
    const button=el('button','person'+(p.id===state.selected?' selected':''));button.type='button';
    button.setAttribute('aria-current',p.id===state.selected?'true':'false');
    if(p.updated)button.title='最近更新：'+conversationPreview(String(p.updated),32);
    button.append(makeAvatar(p.avatar_url,'avatar'));
    const info=el('span','person-info'),title=el('span','person-title');
    title.append(el('strong','',p.name||p.peer_id||'未命名会话'));
    if(isGroup(p))title.append(el('span','group-tag','群聊'));
    else if(p.type==='official'||p.is_official)title.append(el('span','group-tag','公众号'));
    else if(p.type==='work')title.append(el('span','group-tag','企聊'));
    const preview=conversationPreview(p.preview);
    info.append(title,el('small','session-preview',preview));
    button.setAttribute('aria-label',[accessibleName,isGroup(p)?'群聊':'',preview].filter(Boolean).join('，'));
    button.append(info);button.onclick=guard(()=>select(p.id));row.append(check,button);list.append(row);
  }
  list.scrollTop=scroll;
}
function filterPeople(){
  state.people=state.allPeople.filter(p=>{
    const kind=$('chat-filter').value;
    return (!accountUI.active?.account_ids?.length||accountUI.active.account_ids.includes(p.account))&&
      (!$('account-filter').value||p.account===$('account-filter').value)&&
      (!kind||(kind==='group'?isGroup(p):kind==='official'?p.type==='official'||p.is_official:
        kind==='work'?p.type==='work'||String(p.peer_id||'').endsWith('@openim'):
        !isGroup(p)&&p.type!=='official'&&!p.is_official&&p.type!=='work'&&!String(p.peer_id||'').endsWith('@openim')));
  });
  if(!state.people.some(p=>p.id===state.selected)){
    if(state.selected)++state.request;
    state.selected=null;$('person-view').hidden=true;$('empty-state').hidden=false;
  }
  renderPeople();
}
function settingsOpen(){const s=state.settings;$('setting-vault').value=s.vault||'';$('ai-url').value=s.ai_url||'';$('ai-model').value=s.ai_model||'';$('setting-owner').value=s.owner||'';$('settings-dialog').showModal();}
async function refreshPeople({autoSelect=true}={}){
  const request=++state.peopleRequest,source=accountUI.active?.id;
  const people=await api('/api/conversations?'+new URLSearchParams({q:$('search-people').value,source_id:source||''}));
  if(request!==state.peopleRequest||source!==accountUI.active?.id)return;
  state.allPeople=Array.isArray(people)?people:people.items||[];
  const previous=$('account-filter').value;
  const accounts=[...new Set(state.allPeople.filter(p=>!accountUI.active?.account_ids?.length||accountUI.active.account_ids.includes(p.account)).map(p=>p.account).filter(Boolean))];
  $('account-filter').replaceChildren(new Option('全部账号',''),...accounts.map(a=>new Option(a,a)));
  if(accounts.includes(previous))$('account-filter').value=previous;
  filterPeople();renderConversationHeading();
  if(autoSelect&&!state.selected&&state.people.length)await select(state.people[0].id);
}
async function refresh(){
  const value=await api('/api/state');state.settings=value.settings;state.runtime_platform=value.runtime_platform;
  state.demo=value.demo;state.stats=value.stats;receiveAccountFlow(value);
  await refreshAccounts();await initializeAccountDiscovery();updateHome();
  $('runtime-info').textContent=`v${value.version} · ${value.runtime_platform==='win32'?'Windows':value.runtime_platform==='darwin'?'macOS':value.runtime_platform}`;
  $('source-path').value=value.settings.source_paths?.[0]||$('source-path').value;
  $('demo-banner').hidden=!value.demo;
  $('stats').textContent=value.stats.conversations?`已读取 ${value.stats.conversations} 个联系人／群聊 · ${value.stats.messages.toLocaleString()} 条消息`:'还没有读取聊天记录';
  $('vault-status').textContent=value.settings.vault?'Obsidian：'+value.settings.vault:'尚未选择 Obsidian 库';
  $('vault-status').title=value.settings.vault||'';
  if(accountUI.active?.ready)await refreshPeople();
  if(value.job)await updateJob(value.job);
}
async function select(id){
  const seq=++state.request;state.selected=id;state.offset=0;state.rawStart=0;
  state.rawItems=[];state.checkedMessages.clear();state.rawTotal=0;state.viewport=null;
  $('raw-messages').replaceChildren();$('raw-more').hidden=true;$('jump-latest').hidden=true;
  $('empty-state').hidden=true;$('person-view').hidden=false;
  renderPeople();renderConversationHeading();updateRawCount();
  if(state.panel==='chat')await loadRaw();
  const result=await api('/api/insight?id='+encodeURIComponent(id));
  if(seq!==state.request)return;
  renderInsight(result.insight);
}
function renderInsight(insight){const container=$('insight-content');container.replaceChildren();$('save-one').disabled=!insight||state.job?.status==='running';if(!insight&&!current()?.message_count){container.append(el('p','not-analyzed','这个会话暂无本地聊天消息；读到消息后可以提炼。'));return;}if(!insight){const note=el('div','not-analyzed');note.append(el('strong','','还没有提炼这个人的聊天'),el('p','','点击「提炼此人并保存」，AI 会梳理值得长期记住的内容。'));container.append(note);return;}const profile=insight.profile;const summary=el('div','summary-card');summary.append(el('h2','','人物与对话概述'),el('p','',profile.summary||'本次没有发现需要长期保存的重要内容。'),el('div','summary-meta',`已分析 ${profile.message_count.toLocaleString()} 条聊天 · ${profile.items.length} 个重要要点 · ${insight.updated.slice(0,10)}`));container.append(summary);for(const category of ['工作','生活','其他']){const items=profile.items.filter(i=>i.category===category);if(!items.length)continue;const section=el('section','insight-group');const title=el('h2','group-title',category+'中的重要内容');title.append(el('span','',items.length+' 条'));section.append(title);for(const item of items){const card=el('article','memory-card');const header=el('div','memory-title');header.append(el('span','kind '+(category==='工作'?'work':''),item.kind),el('h3','',item.title));card.append(header,el('p','',item.detail));const meta=el('div','memory-meta');meta.append(el('span','','涉及：'+item.subject));if(item.date)meta.append(el('span','',item.date));if(item.status)meta.append(el('span','','原文状态：'+item.status));card.append(meta);const detail=el('details');detail.append(el('summary','','查看原文依据'));for(const evidence of item.evidence||[]){detail.append(el('p','',evidence.quote),el('small','',evidence.time+' · '+(evidence.is_self?'我':evidence.sender)));}card.append(detail);section.append(card);}container.append(section);}}
function busy(value){
  ['fetch','analyze-all','archive-start','contacts-export','all-contacts-export'].forEach(id=>$(id).disabled=value);
  $('analyze-one').disabled=value||!current()?.message_count;
  $('save-one').disabled=value||!current()?.analyzed;
  for(const id of ['chat-full','chat-incremental'])$(id).disabled=value||!state.selected||!accountUI.active?.id;
  renderDiscoveryControls();
}
async function updateJob(job){
  if(!job)return;state.job=job;const running=job.status==='running';busy(running);
  if(job.kind==='live-sync'){
    // Background completion is presented by the revision poll, without replacing a manual report.
    $('job-bar').hidden=true;
    if(job.status==='error'){
      $('live-error').textContent=job.message||'自动同步未完成，请查看同步状态';
      $('live-error').hidden=false;
    }
    return;
  }
  $('job-bar').hidden=!running;$('job-text').textContent=job.message||'';
  $('cancel-job').hidden=job.kind==='export';
  if(!running&&state.lastJob!==job.id){
    state.lastJob=job.id;state.report=job;
    if(job.kind==='account-read'){await finishAccountJob(job);return;}
    const chatRefresh=job.id===state.chatRefreshJob||job.kind==='chat-refresh';
    if(chatRefresh)showReport(job);
    else if(job.status==='error')toast(job.message||'读取未完成，请查看处理结果');
    else if(job.status==='cancelled'){toast('已停止，已经完成的结果会保留。');if(job.result)showReport(job);}
    else if(job.kind.startsWith('media')||['analyze','export','archive'].includes(job.kind)||
      job.result?.sources?.some(source=>source.errors?.length)||job.result?.sources?.every(source=>!source.read))showReport(job);
    const value=await api('/api/state');state.settings=value.settings;state.stats=value.stats;updateHome();
    $('stats').textContent=`已读取 ${value.stats.conversations} 个联系人／群聊 · ${value.stats.messages.toLocaleString()} 条消息`;
    await refreshPeople({autoSelect:false});
    if(state.selected)await reloadCurrentConversation();
    else if(state.people.length)await select(state.people[0].id);
  }
}
async function analyze(ids){if(!state.settings.ai_url||!state.settings.ai_model||!state.settings.vault){settingsOpen();toast('先设置 AI 模型与 Obsidian 保存位置');return;}showPage('memories');await updateJob(await api('/api/analyze',{ids,save:true}));}
function showReport(job=state.report){const body=$('report-body');body.replaceChildren();
if(job&&(job.kind==='chat-refresh'||job.id===state.chatRefreshJob)){
  body.append(el('p','',job.message||('会话刷新状态：'+job.status)));
  if(Number.isFinite(job.result?.new_messages))body.append(el('p','',`本次新增 ${job.result.new_messages} 条消息`));
}
if(job?.kind?.startsWith('media')&&job.status!=='error'){const r=job.result||{};body.append(el('p','',job.kind==='media-export'?`已导出 ${r.files||0} 个媒体文件`:`已登记 ${r.files||0} 个媒体文件 · 可查看 ${r.ready||0} 个 · 待预览 ${r.queued||0} 个 · 尚未读出 ${r.unread||0} 个`));if(r.folder)body.append(el('p','',r.folder));const b=el('button','button primary','查看媒体');b.onclick=guard(async()=>{$('report-dialog').close();await openMedia('');});body.append(b);}else if(!job){body.append(el('p','','还没有处理结果。'));}else if(job.status==='error'){body.append(el('p','warning',job.message));}else if(job.kind==='analyze'){const r=job.result;body.append(el('p','',`已完成 ${r.people} 位联系人／群聊的提炼，本次新增 ${r.important_items} 个重要要点。`));if(r.reused?.length)body.append(el('p','',`${r.reused.length} 位没有新聊天，已复用已有笔记。`));if(r.export)renderExport(r.export,body);for(const error of r.errors||[]){const section=el('div','report-section');section.append(el('strong','',error.person),el('p','warning',error.error));body.append(section);}}else if(job.kind==='export')renderExport(job.result,body);else if(job.kind==='archive'){const r=job.result;body.append(el('p','',`已导出 ${r.people} 个会话 · ${r.messages} 条消息`),el('p','',r.folder));if(r.scope)body.append(el('p','',r.scope));for(const error of r.errors||[])body.append(el('p','warning',error.person+'：'+error.error));if(r.cancelled)body.append(el('p','warning','任务已停止，已完成文件保留，增量进度未推进。'));}else{for(const source of job.result?.sources||[])renderSourceReport(source,body);if(job.result?.capture_diagnostics){body.append(el('p','warning',job.result.capture_diagnostics.message));renderConnectionDiagnostics(job.result.capture_diagnostics,body);}}$('connect-v4').hidden=state.runtime_platform!=='win32'||!['import','connect-v4'].includes(job?.kind);if(!$('report-dialog').open)$('report-dialog').showModal();}
function renderSourceReport(source,body){
  const section=el('div','report-section');
  section.append(el('strong','',source.path),el('p','',`读取 ${source.read} 条 · 新增 ${source.imported} 条 · 已存在 ${source.duplicates} 条`));
  if(source.source_info){const info=source.source_info;section.append(el('p','',`${info.layout} · 数据库 ${info.database_files} 个 · 明文 ${info.plaintext_databases} 个 · 加密或待识别 ${info.encrypted_or_unknown_databases} 个`));}
  if(source.preflight?.copy_after_connection)section.append(el('p','','已先检查读取连接；尚未通过连接校验的加密库未复制全文。'));
  for(const cache of source.cache_sources||[]){
    section.append(el('p','',`${cache.name} · 缓存读取 ${cache.read_messages.toLocaleString()} 条 · 已保存名称 ${cache.contact_names} 个`));
    if(!cache.complete)section.append(el('p','warning',cache.known?(cache.synced_sessions===cache.sessions?`原程序的同步标记与缓存实际行数不一致：预期 ${cache.expected_messages??'未知'} 条，实际 ${cache.source_rows??0} 行。`:`原程序仅 ${cache.synced_sessions}/${cache.sessions} 个会话标记同步完成，本次只包含缓存已有记录。`):'这个缓存没有可确认的完整同步状态，本次只读取实际保存的记录。'));
  }
  if(source.encrypted_databases)section.append(el('p','',`已识别 ${source.encrypted_databases} 个加密库 · 成功解密 ${source.decrypted_databases||0} 个`));
  if(source.wal_databases)section.append(el('p','',`其中 ${source.wal_databases} 个数据库已合并日志中的完整提交`));
  if(source.database_results?.length){
    const detail=el('details');detail.append(el('summary','','逐库读取与日志结果'));
    const labels={decrypted:'校验通过',no_matching_material:'尚未连接',wal_validation_failed:'日志校验失败',
      page_authentication_failed:'页面校验失败',incomplete_file:'文件不完整',snapshot_validation_failed:'副本校验失败',
      snapshot_changed:'记录正在变化',snapshot_unreadable:'文件无法复制',rollback_journal:'存在回滚日志'};
    for(const item of source.database_results){
      let text=`${item.name} · ${labels[item.status]||item.status}`;
      if(item.wal?.transactions)text+=` · 日志 ${item.wal.transactions} 次提交 / ${item.wal.overlay_pages} 页`;
      detail.append(el('p','',text));
      if(item.message)detail.append(el('p','warning',item.message));
      if(item.wal?.ignored_uncommitted_frames||item.wal?.tail)detail.append(el('p','','未提交或旧代日志尾部未纳入记录'));
    }
    section.append(detail);
  }
  if(source.reused_files)section.append(el('p','',`复用 ${source.reused_files} 个未变化文件 · ${source.cached_messages.toLocaleString()} 条已缓存聊天`));
  if(source.media?.deferred)section.append(el('p','','聊天已先行读取；媒体将在查看或导出时处理。'));
  if(source.media){section.append(el('p','',`本地媒体：已登记 ${source.media.files} 个 · 可查看 ${source.media.ready} 个 · 尚未读出 ${source.media.unread} 个 · 已关联 ${source.media.linked_messages||0} 条聊天`));const b=el('button','button','查看本目录媒体');b.onclick=guard(async()=>{$('report-dialog').close();await openMedia(source.id);});section.append(b);}
  for(const error of source.errors||[])section.append(el('p','warning',error));
  for(const note of source.notes||[])section.append(el('p','',note));
  if(source.unread_files?.length){const detail=el('details');detail.append(el('summary','','查看尚未读出的文件'));for(const path of source.unread_files)detail.append(el('p','',path));section.append(detail);}
  if(source.diagnostics)renderConnectionDiagnostics(source.diagnostics,section);
  body.append(section);
}
function renderConnectionDiagnostics(d,parent){
  const detail=el('details');detail.append(el('summary','','查看连接检查'));
  if(d.message)detail.append(el('p','',d.message));
  for(const p of d.processes||[]){detail.append(el('p','',`${p.name} · ${p.version||'版本未获取'} · ${p.opened?'可读取':p.same_user?'进程访问失败':'其他 Windows 用户'}${p.open_error?' · Windows 错误码 '+p.open_error:''}`));}
  if(d.target_count!==undefined&&d.mode!=='windows-v4-startup')detail.append(el('p','',`数据库首页校验 ${d.matched_targets||0} / ${d.target_count} 个`));
  if(d.layout_warning)detail.append(el('p','warning',d.layout_warning));
  if(d.scanned_bytes!==undefined)detail.append(el('p','',`已检查 ${(d.scanned_bytes/1024/1024).toFixed(1)} MB · 候选 ${d.candidates_checked||0} 个 · 耗时 ${d.elapsed_seconds||0} 秒`));
  if(d.mode==='windows-v4-startup'){
    const method={combined:'数据库入口 + 计算入口', 'direct-key-object':'数据库入口', 'sha512-fallback':'计算入口', pending:'等待连接'}[d.capture_method]||'等待连接';
    detail.append(el('p','',`${method} · 已连接 ${d.attached_processes||0} 个进程 · 校验 ${d.matched_targets||0} / ${d.target_count||0} 个数据库`),el('p','',`定位 ${d.signature_matches||0} 个数据库入口 · 连接 ${d.direct_key_entries||0} 个数据库入口 / ${d.sha512_entries||0} 个计算入口 · 候选 ${d.candidates_checked||0} 个 · 耗时 ${d.elapsed_seconds||0} 秒`));
    if(d.attach_error)detail.append(el('p','warning','微信进程连接未成功：'+d.attach_error));
    if(d.queue_full)detail.append(el('p','warning','本次候选过多，部分未能处理；当前读取结果可能不完整。'));
  }
  parent.append(detail);
}
function renderExport(r,body){body.append(el('p','',`已把 ${r.people||0} 位联系人的重要笔记保存到：`),el('p','',r.folder));for(const path of r.conflicts||[])body.append(el('p','warning','保留手动修改的笔记：'+path));if(r.no_important_content?.length)body.append(el('p','',`${r.no_important_content.length} 位未发现需要长期保存的重要内容，未生成空笔记。`));if(r.needs_analysis?.length)body.append(el('p','warning','以下联系人有新聊天，重新提炼后再保存：'+r.needs_analysis.join('、')));}
function rawFilters(){return {q:$('raw-query').value,since:$('raw-since').value,until:$('raw-until').value,types:$('raw-type').value?[$('raw-type').value]:[],direction:$('raw-direction').value};}
async function loadRaw(){
  if(!state.selected||state.rawLoading===state.request)return;
  const id=state.selected,seq=state.request,source=accountUI.active?.id,filters=rawFilters();
  const earlier=state.rawItems.length>0;
  if(earlier&&state.rawStart<=0)return;
  const start=earlier?Math.max(0,state.rawStart-100):0;
  state.rawLoading=seq;$('raw-more').disabled=true;
  try{
    let page=await messagePage(id,start,filters),offset=start;
    if(!earlier&&Number(page.total)>page.items.length){
      offset=Math.max(0,Number(page.total)-100);page=await messagePage(id,offset,filters);
    }
    if(seq!==state.request||id!==state.selected||source!==accountUI.active?.id)return;
    const snapshot=earlier?captureMessageScroll():null;
    state.rawItems=earlier?[...page.items.filter(m=>!state.rawItems.some(old=>old.id===m.id)),...state.rawItems]:page.items;
    state.rawStart=offset;state.rawTotal=Number(page.total)||0;state.offset=offset+state.rawItems.length;
    state.appliedFilters=JSON.stringify(filters);
    renderMessages(snapshot, !earlier);updateRawCount();
  }finally{
    if(state.rawLoading===seq)state.rawLoading=0;
    $('raw-more').disabled=false;
  }
}
function updateRawCount(){
  const selected=state.checkedMessages.size;
  $('raw-count').textContent=`符合 ${state.rawTotal.toLocaleString()} 条 · 已选 ${selected} 条`;
  $('message-selection-bar').hidden=!selected&&!$('message-selection-mode').checked;
  $('raw-messages').classList.toggle('selection-mode',$('message-selection-mode').checked||selected>0);
  $('message-filter-open').classList.toggle('has-filters',!!($('raw-since').value||$('raw-until').value||$('raw-type').value||$('raw-direction').value));
}
$('browse-source').onclick=guard(async()=>{const r=await api('/api/pick-folder',{});if(r.path)$('source-path').value=r.path;});
$('fetch').onclick=guard(async()=>{$('source-dialog').close();await startAccountRead($('source-path').value.trim());});
$('analyze-all').onclick=guard(async()=>{$('source-dialog').close();await analyze(state.people.map(p=>p.id));});$('analyze-one').onclick=guard(()=>analyze([state.selected]));
$('save-one').onclick=guard(async()=>{if(!state.settings.vault)return settingsOpen();await updateJob(await api('/api/export',{ids:[state.selected],vault:state.settings.vault}));});
$('settings-open').onclick=settingsOpen;$('vault-status').onclick=settingsOpen;
$('browse-vault').onclick=guard(async()=>{const r=await api('/api/pick-folder',{});if(r.path)$('setting-vault').value=r.path;});
$('settings-save').onclick=guard(async()=>{const settings={vault:$('setting-vault').value.trim(),ai_url:$('ai-url').value.trim(),ai_model:$('ai-model').value.trim(),owner:$('setting-owner').value.trim()};if($('ai-key').value)settings.api_key=$('ai-key').value;await api('/api/settings',settings);delete settings.api_key;Object.assign(state.settings,settings);$('ai-key').value='';$('settings-dialog').close();$('vault-status').textContent=settings.vault?'Obsidian：'+settings.vault:'尚未选择 Obsidian 库';toast('已保存，点击 AI 提炼即可逐人整理重要内容');});
$('search-people').oninput=()=>{clearTimeout(timer);timer=setTimeout(guard(refreshPeople),220);};
$('raw-details').ontoggle=null;$('raw-more').onclick=guard(loadRaw);
$('cancel-job').onclick=guard(()=>api('/api/cancel',{}));$('report-open').onclick=()=>showReport();
for(const id of ['report-close','report-done'])$(id).onclick=()=>$('report-dialog').close();
$('open-folder').onclick=guard(()=>api('/api/open-export',{}));
let jobPolling=false;
setInterval(guard(async()=>{if(state.job?.status==='running'&&!jobPolling){jobPolling=true;try{const s=await api('/api/job-state');receiveAccountFlow(s);await updateJob(s.job);}finally{jobPolling=false;}}}),1000);
guard(refresh)();

$('copy-diagnostics').onclick=guard(async()=>{const text=JSON.stringify(await api('/api/read-diagnostics'),null,2);try{await navigator.clipboard.writeText(text);}catch(_){const field=el('textarea');field.value=text;document.body.append(field);field.select();const ok=document.execCommand('copy');field.remove();if(!ok)throw new Error('复制失败，请从本地资料库取得 read-diagnostics.json');}toast('读取诊断已复制，可粘贴到问题反馈中。');});
$('connect-v4').onclick=guard(()=>accountUI.readContext?.kind==='advanced'?startAccountRead(accountUI.readContext.path,'capture'):retryAccountRead());

function archiveIds(){const scope=$('archive-scope').value;const ids=scope==='current'?(state.selected?[state.selected]:[]):scope==='checked'?[...state.checked]:state.people.map(p=>p.id);if(!ids.length)throw new Error('请先选择会话或勾选联系人');return ids;}
function archiveOptions(){const filters=$('archive-use-filters').checked?rawFilters():{};if($('archive-selected-messages').checked){if($('archive-scope').value!=='current')throw new Error('勾选消息仅适用于当前会话');if(!state.checkedMessages.size)throw new Error('先在原始聊天中勾选消息');filters.message_ids=[...state.checkedMessages];}return {format:$('archive-format').value,split:Number($('archive-split').value),filters};}
async function archiveOpen(){if(state.checked.size)$('archive-scope').value='checked';else $('archive-scope').value='current';$('archive-folder').value=state.settings.export_dir||'';$('archive-selected-messages').checked=state.checkedMessages.size>0;if(state.checkedMessages.size)$('archive-scope').value='current';if(!$('archive-dialog').open)$('archive-dialog').showModal();const history=await api('/api/archive-history');$('archive-history').replaceChildren();for(const h of history.slice(0,10)){$('archive-history').append(el('p','',`${h.created} · ${h.detail.format} · ${h.detail.messages} 条 · ${h.status}`),el('small','',h.detail.folder));}}
$('archive-open').onclick=guard(archiveOpen);$('export-one').onclick=guard(async()=>{await archiveOpen();if(!state.checked.size)$('archive-scope').value='current';});$('archive-close').onclick=()=>$('archive-dialog').close();
$('browse-archive').onclick=guard(async()=>{const r=await api('/api/pick-folder',{});if(r.path)$('archive-folder').value=r.path;});
$('archive-start').onclick=guard(async()=>{const ids=archiveIds(),options=archiveOptions();const job=await api('/api/archive',{ids,options,folder:$('archive-folder').value.trim()});state.settings.export_dir=$('archive-folder').value.trim();$('archive-dialog').close();await updateJob(job);});
$('contacts-export').onclick=guard(async()=>{const job=await api('/api/contacts-export',{ids:archiveIds(),format:'xlsx',folder:$('archive-folder').value.trim()});state.settings.export_dir=$('archive-folder').value.trim();$('archive-dialog').close();await updateJob(job);});
$('all-contacts-export').onclick=guard(async()=>{const job=await api('/api/contacts-export',{format:'xlsx',folder:$('archive-folder').value.trim()});$('archive-dialog').close();await updateJob(job);});
$('archive-folder-open').onclick=guard(()=>api('/api/open-archive',{}));
for(const [id,label] of [['raw-type','文本'],['raw-type','图片'],['raw-type','语音'],['raw-type','视频'],['raw-type','表情'],['raw-type','链接/文件'],['raw-type','转账/红包'],['raw-type','位置'],['raw-type','名片'],['raw-type','通话'],['raw-type','系统'],['raw-type','其他']])$(id).append(new Option(label,label));
$('raw-apply').onclick=guard(async()=>{
  $('message-filter-dialog').close();
  if(JSON.stringify(rawFilters())!==state.appliedFilters)await applyMessageFilters();else updateRawCount();
});
$('raw-select-page').onclick=()=>{const boxes=$('raw-messages').querySelectorAll('input[data-message-id]'),all=[...boxes].every(c=>c.checked);for(const box of boxes){box.checked=!all;all?state.checkedMessages.delete(box.dataset.messageId):state.checkedMessages.add(box.dataset.messageId);}updateRawCount();};
$('select-visible').onchange=()=>{for(const p of state.people)$('select-visible').checked?state.checked.add(p.id):state.checked.delete(p.id);renderPeople();};
for(const id of ['account-filter','chat-filter'])$(id).onchange=guard(async()=>{filterPeople();if(!state.selected&&state.people.length)await select(state.people[0].id);});
$('contact-edit').onclick=()=>{const p=current();if(!p)return;$('contact-alias').value=p.name;$('contact-note').value=p.note||'';$('contact-dialog').showModal();};
$('contact-close').onclick=()=>$('contact-dialog').close();$('contact-save').onclick=guard(async()=>{await api('/api/contact',{id:state.selected,alias:$('contact-alias').value,note:$('contact-note').value});$('contact-dialog').close();await refreshPeople();if(state.selected)await select(state.selected);});
$('detect-source').onclick=guard(async()=>{const r=await api('/api/detect-sources');$('sources-list').replaceChildren();if(!r.items.length)$('sources-list').append(el('p','','常见位置没有找到数据目录，请使用「选择文件夹」指定记录位置。'));for(const source of r.items){const button=el('button','source-choice',source.account+' · '+source.layout+'\n'+source.path);button.onclick=()=>{$('source-path').value=source.path;$('sources-dialog').close();};$('sources-list').append(button);}$('sources-dialog').showModal();});$('sources-close').onclick=()=>$('sources-dialog').close();
async function schedulesOpen(){const rows=await api('/api/schedules');$('schedules-list').replaceChildren();if(!rows.length)$('schedules-list').append(el('p','','尚未创建定时任务。'));for(const task of rows){const card=el('div','report-section');card.append(el('strong','',task.name),el('p','',task.next_run+' · '+({once:'一次',daily:'每天',weekly:'每周'}[task.config.repeat])+' · '+task.config.options.format),el('small','',task.last_result||'等待运行'));const actions=el('div','task-actions');const toggle=el('button','button',task.enabled?'暂停':'启用');toggle.onclick=guard(async()=>{await api('/api/schedule-enabled',{id:task.id,enabled:!task.enabled});await schedulesOpen();});const run=el('button','button','立即执行');run.onclick=guard(async()=>{const job=await api('/api/schedule-run',{id:task.id});$('schedules-dialog').close();await updateJob(job);});actions.append(toggle,run);card.append(actions);$('schedules-list').append(card);}if(!$('schedules-dialog').open)$('schedules-dialog').showModal();}
$('schedules-open').onclick=guard(schedulesOpen);$('schedules-close').onclick=()=>$('schedules-dialog').close();$('schedule-new').onclick=guard(async()=>{$('schedules-dialog').close();await archiveOpen();$('schedule-fields').open=true;});
$('schedule-save').onclick=guard(async()=>{await api('/api/schedule-save',{name:$('schedule-name').value,ids:archiveIds(),folder:$('archive-folder').value.trim(),options:archiveOptions(),next_run:$('schedule-time').value,repeat:$('schedule-repeat').value,incremental:$('schedule-incremental').checked});$('archive-dialog').close();toast('定时导出已创建，应用保持打开时执行。');await schedulesOpen();});
const tomorrow=new Date(Date.now()+3600000);$('schedule-time').value=new Date(tomorrow.getTime()-tomorrow.getTimezoneOffset()*60000).toISOString().slice(0,16);

function updateHome(){renderDiscoveryControls();}
function showPage(page){
  state.page=page;$('home-page').hidden=page==='home'?false:true;$('work-page').hidden=page==='home';
  document.querySelector('.shell').classList.toggle('chat-mode',page!=='home');
  for(const id of ['nav-home','nav-chats','nav-memories'])$(id).classList.toggle('active',id===(page==='home'?'nav-home':page==='memories'?'nav-memories':'nav-chats'));
  if(page==='memories')guard(()=>showPanel('memory'))();else if(page==='chats')guard(()=>showPanel('chat'))();
  if(page!=='home')guard(pollLiveState)();
}
async function showPanel(panel){
  state.panel=panel;
  $('person-view').classList.toggle('memory-mode',panel==='memory');
  $('insight-content').hidden=panel!=='memory';$('raw-details').hidden=panel!=='chat';$('raw-details').open=panel==='chat';
  $('panel-chat').classList.toggle('active',panel==='chat');$('panel-memory').classList.toggle('active',panel==='memory');
  $('panel-note').textContent=panel==='chat'?'':'逐人提炼 · 保存到 Obsidian';
  if(panel==='chat'&&state.selected){
    if(!state.rawItems.length)await loadRaw();
    else {restoreMessageScroll(state.viewport);prepareVisibleImages();}
  }
  if(panel==='memory'&&state.selected){
    const id=state.selected,result=await api('/api/insight?id='+encodeURIComponent(id));
    if(state.selected===id&&state.panel==='memory')renderInsight(result.insight);
  }
}
$('brand-home').onclick=()=>showPage('home');$('nav-home').onclick=()=>showPage('home');
$('nav-chats').onclick=guard(openChatWorkspace);
$('nav-memories').onclick=guard(async()=>{if(accountUI.active?.ready)showPage('memories');else await openAccountPicker();});
$('panel-chat').onclick=guard(()=>showPanel('chat'));$('panel-memory').onclick=guard(()=>showPanel('memory'));

initAccounts();initMedia();

$('chat-full').onclick=guard(()=>requestChatRefresh('full'));
$('chat-incremental').onclick=guard(()=>requestChatRefresh('incremental'));
$('message-filter-open').onclick=()=>{$('message-filter-dialog').showModal();};
$('message-filter-close').onclick=()=>$('message-filter-dialog').close();
$('raw-search').onclick=guard(applyMessageFilters);
$('raw-query').onkeydown=guard(async event=>{if(event.key==='Enter'){event.preventDefault();await applyMessageFilters();}});
$('raw-clear-filters').onclick=guard(async()=>{
  for(const id of ['raw-query','raw-since','raw-until','raw-type','raw-direction'])$(id).value='';
  $('message-filter-dialog').close();await applyMessageFilters();
});
$('message-selection-mode').onchange=updateRawCount;
$('selection-clear').onclick=()=>{state.checkedMessages.clear();$('message-selection-mode').checked=false;
  $('raw-messages').querySelectorAll('input[data-message-id]').forEach(node=>node.checked=false);updateRawCount();};
$('batch-mode').onchange=renderPeople;
$('raw-messages').onscroll=()=>{state.viewport=captureMessageScroll();$('jump-latest').hidden=state.viewport.atBottom&&state.offset>=state.rawTotal;};
$('jump-latest').onclick=guard(async()=>{
  // Explicitly returning to the latest page is the only action that forces a new tail window.
  ++state.request;state.rawLoading=0;state.rawItems=[];state.rawStart=0;await loadRaw();
});
$('voice-open').onclick=()=>$('voice-dialog').showModal();
for(const id of ['voice-close','voice-done'])$(id).onclick=()=>$('voice-dialog').close();
$('chat-home').onclick=()=>showPage('home');
for(const id of ['contact-edit','analyze-one','save-one'])$(id).addEventListener('click',()=>{document.querySelector('.chat-extra').open=false;});
$('chat-switch').onclick=guard(openAccountPicker);$('empty-accounts').onclick=guard(openAccountPicker);
$('chat-source').onclick=openAdvancedImport;$('source-close').onclick=()=>$('source-dialog').close();
$('chat-memories').onclick=()=>showPage('memories');
$('live-enabled').onchange=guard(async()=>{
  const source=accountUI.active?.id,enabled=$('live-enabled').checked,epoch=liveUI.epoch;
  if(!source)return;
  liveUI.configuring=true;$('live-enabled').disabled=true;
  try{
    await api('/api/live-config',{source_id:source,enabled});
    if(epoch!==liveUI.epoch||source!==accountUI.active?.id)return;
    await pollLiveState();
  }catch(error){
    if(epoch===liveUI.epoch){$('live-enabled').checked=!!liveUI.value?.enabled;$('live-status').textContent='同步设置未保存';}
    throw error;
  }finally{if(epoch===liveUI.epoch){liveUI.configuring=false;$('live-enabled').disabled=!liveUI.value;}}
});
setInterval(()=>{void pollLiveState();},3000);
