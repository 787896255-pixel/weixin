'use strict';
// Account snapshots are cheap. Only an explicit discovery request uses refresh=1.
const accountUI={items:[],environment:{},discovery:{status:'idle'},selectedId:null,selectionLost:false,
  detail:null,detailAdvanced:false,active:null,readPath:'',loading:false,activating:false,
  catalogPromise:null,catalogLoaded:false,catalogSignature:null,catalogError:'',discoveryPending:false,discoveryStarted:false,
  readContext:null,flowDismissed:false,enteredJob:null,pendingExportDir:''};

function defaultAccounts(){return accountUI.items.filter(item=>item.automatic===true||item.ready);}
function usableAccount(item){return !!item&&(item.ready||item.exists!==false&&item.available!==false);}
function accountReadBusy(){return accountUI.loading||accountUI.activating||state.job?.kind==='account-read'&&state.job.status==='running';}
function chosenAccount(){return defaultAccounts().find(item=>item.id===accountUI.selectedId)||null;}
function accountName(item){return item?.name||item?.alias||item?.wxid||'微信账号';}
function accountSummary(item){
  const identity=item.alias||item.wxid||item.account_ids?.[0]||'';
  if(!item.ready)return [identity,item.exists===false?'记录暂不可用':'已发现 · 点击下一步读取'].filter(Boolean).join(' · ');
  const counts=[];
  if(Number.isFinite(item.conversations))counts.push(item.conversations+' 个会话');
  if(Number.isFinite(item.messages))counts.push(item.messages.toLocaleString()+' 条消息');
  return [identity,counts.join(' · ')||'已保存，可直接进入'].filter(Boolean).join(' · ');
}
function discoveryText(){
  if(accountUI.catalogError)return accountUI.catalogError;
  if(!accountUI.catalogLoaded||accountUI.discoveryPending)return '正在自动查找本机微信账号，请稍候…';
  if(accountUI.discovery.status==='scanning')return accountUI.discovery.message?conversationPreview(String(accountUI.discovery.message),140):'正在查找本机微信数据；发现的账号会自动显示。';
  if(accountUI.selectionLost)return '刚才选择的账号暂不可用，请重新选择；不会自动切换到其他账号。';
  if(accountUI.discovery.error)return String(accountUI.discovery.error);
  const items=defaultAccounts();
  if(items.length)return items.length===1?'已发现微信账号，点击下一步继续。':'已发现 '+items.length+' 个微信账号，请选择后继续。';
  return '尚未发现本机账号。请先登录电脑版微信，再点击“重新检测”。';
}

async function refreshAccounts({rescan=false}={}){
  if(accountUI.catalogPromise)return accountUI.catalogPromise;
  const trigger=rescan&&accountUI.discovery.status!=='scanning'&&!accountUI.discoveryPending;
  accountUI.discoveryPending=trigger;accountUI.catalogError='';renderDiscoveryControls();
  const request=(async()=>{
    try{
      const value=await api('/api/accounts'+(trigger?'?refresh=1':''));
      accountUI.discoveryPending=false;
      const signature=JSON.stringify([value.items,value.environment,value.discovery,value.active_id]);
      const changed=signature!==accountUI.catalogSignature;accountUI.catalogSignature=signature;
      const oldActive=accountUI.active;
      accountUI.items=Array.isArray(value.items)?value.items:[];
      accountUI.environment=value.environment||{};
      accountUI.discovery=value.discovery||{status:'done'};accountUI.catalogLoaded=true;
      const items=defaultAccounts(),previous=accountUI.selectedId;
      const active=items.find(item=>item.id===accountUI.active?.id&&item.ready);
      if(accountUI.active)accountUI.active=active||null;
      else if(!accountUI.selectionLost)accountUI.active=items.find(item=>item.id===value.active_id&&item.ready)||null;
      if(previous&&!items.some(item=>item.id===previous)){
        accountUI.selectedId=null;accountUI.selectionLost=true;
      }else if(!previous&&!accountUI.selectionLost){
        const persisted=items.find(item=>item.id===value.active_id&&item.ready);
        const ready=items.filter(item=>item.ready),usable=items.filter(usableAccount);
        // Never choose the first of several accounts, or replace a disappeared selection.
        accountUI.selectedId=persisted?.id||(ready.length===1?ready[0].id:usable.length===1?usable[0].id:null);
      }
      $('active-account-label').textContent=accountUI.active?accountName(accountUI.active):'选择微信';
      $('active-account-label').title=accountUI.active?.alias||accountUI.active?.wxid||'选择或切换微信账号';
      if(changed||previous!==accountUI.selectedId){renderHomeAccounts();renderAccountPicker();renderAdvancedAccounts();}
      else renderDiscoveryControls();
      renderActiveAccount();
      if(oldActive&&!accountUI.active){
        ++state.request;state.selected=null;state.people=[];state.allPeople=[];state.checked.clear();state.checkedMessages.clear();
        $('person-view').hidden=true;$('empty-state').hidden=false;renderPeople();
      }
      resetLiveSource(accountUI.active?.id);if(state.accountFlow)renderAccountFlow();
      return value;
    }catch(error){
      accountUI.catalogError='暂时无法获取账号：'+error.message;
      renderHomeAccounts();renderAccountPicker();throw error;
    }finally{
      accountUI.discoveryPending=false;accountUI.catalogPromise=null;renderDiscoveryControls();
    }
  })();
  accountUI.catalogPromise=request;return request;
}
async function initializeAccountDiscovery(){
  if(accountUI.discoveryStarted)return;
  accountUI.discoveryStarted=true;
  if(!accountUI.catalogLoaded)await refreshAccounts();
  if(accountUI.discovery.status==='idle')await refreshAccounts({rescan:true});
}
async function redetectAccounts(){
  if(accountUI.discovery.status==='scanning'||accountUI.discoveryPending)return;
  await refreshAccounts({rescan:true});
}
async function pollAccountDiscovery(){
  if(accountUI.catalogPromise)return;
  try{await refreshAccounts();await initializeAccountDiscovery();}catch(_){/* The discovery status shows the actual error without repeated popups. */}
}
function renderDiscoveryControls(){
  const scanning=accountUI.discoveryPending||accountUI.discovery.status==='scanning'||!accountUI.catalogLoaded;
  const item=chosenAccount(),busy=accountReadBusy();
  $('home-state').textContent=discoveryText();$('home-state').classList.toggle('warning',!!accountUI.catalogError||!!accountUI.discovery.error);
  $('account-discovery-status').textContent=discoveryText();
  for(const id of ['home-accounts','account-list'])$(id).setAttribute('aria-busy',String(scanning));
  for(const id of ['home-redetect','accounts-refresh']){
    $(id).disabled=scanning||busy;$(id).textContent=scanning?'正在检测…':'重新检测';
  }
  for(const id of ['home-read','account-continue']){
    $(id).disabled=busy||!usableAccount(item);
    $(id).textContent=busy?(accountUI.activating?'正在进入…':'正在读取…'):item?'下一步':scanning?'正在查找账号…':defaultAccounts().length?'请选择账号':'等待微信登录';
  }
  $('account-detail-open').disabled=!item||busy;
  $('home-add-account').disabled=busy;
  const env=accountUI.environment,canLaunch=env.installed&&(env.windows||state.runtime_platform==='win32');
  for(const id of ['home-launch','account-launch','prepare-launch'])$(id).disabled=!canLaunch||accountUI.loading;
  $('home-workspace').textContent=item?.ready?'进入聊天工作台':'选择账号并继续';
}
function renderActiveAccount(){
  const account=accountUI.active;
  $('chat-account-name').textContent=account?accountName(account):'选择微信账号';
  $('chat-account-id').textContent=account?.alias||account?.wxid||account?.account||account?.account_ids?.[0]||'';
  $('chat-account-name').title=account?accountName(account):'';$('chat-account-id').title=$('chat-account-id').textContent;
  setAvatar($('chat-account-avatar'),account?.avatar_url,account?.name);
}
function accountRow(item,compact=false,advanced=false){
  const selected=!advanced&&item.id===accountUI.selectedId;
  const button=el('button','account-choice'+(selected?' selected':''));button.type='button';
  button.dataset.accountId=item.id;button.setAttribute('aria-pressed',String(selected));button.disabled=accountReadBusy()||!usableAccount(item);
  button.append(makeAvatar(item.avatar_url,'account-avatar',accountName(item)));
  const info=el('span','account-choice-info');info.append(el('strong','',accountName(item)),el('small','',accountSummary(item)));
  const label=item.ready?(item.status==='partial'?'部分已读':'已就绪'):item.exists===false?'暂不可用':'待读取';
  button.append(info,el('span','account-chip'+(item.ready?' ready':''),label));
  button.onclick=guard(async()=>{
    if(accountReadBusy())return;
    if(advanced){$('source-dialog').close();showAccountDetail(item,true);return;}
    accountUI.selectedId=item.id;accountUI.selectionLost=false;renderHomeAccounts();renderAccountPicker();
  });
  return button;
}
function emptyAccountList(compact=false){
  const box=el('div',compact?'account-home-empty':'account-picker-empty');
  const scanning=accountUI.discoveryPending||accountUI.discovery.status==='scanning'||!accountUI.catalogLoaded;
  if(scanning)box.append(el('span','spinner'),el('strong','','正在自动查找微信账号'),el('p','','请稍候，发现后会显示在这里。'));
  else box.append(el('strong','','请先登录电脑版微信'),el('p','','登录后点击“重新检测”，无需选择记录文件夹。'));
  return box;
}
function renderHomeAccounts(){
  const list=$('home-accounts'),scroll=list.scrollTop,focused=list.contains(document.activeElement)?document.activeElement.dataset.accountId:null;list.replaceChildren();
  const items=defaultAccounts();if(!items.length)list.append(emptyAccountList(true));
  for(const item of items)list.append(accountRow(item,true));list.scrollTop=scroll;
  if(focused)[...list.children].find(node=>node.dataset.accountId===focused)?.focus({preventScroll:true});
  renderDiscoveryControls();
}
function renderAccountPicker(){
  const env=accountUI.environment,status=$('client-status');status.replaceChildren();
  const title=env.installed?`电脑版微信 ${env.version||''} · ${env.running?'正在运行':'请登录微信'}`:
    env.windows?'请安装并登录电脑版微信；已保存账号仍可离线查看。':'本机账号自动发现 · 已保存记录可离线查看';
  status.append(el('span','client-status-dot'+(env.installed?' available':'')),el('span','',title));
  const list=$('account-list'),scroll=list.scrollTop,focused=list.contains(document.activeElement)?document.activeElement.dataset.accountId:null;list.replaceChildren();
  const items=defaultAccounts();if(!items.length)list.append(emptyAccountList());
  for(const item of items)list.append(accountRow(item));list.scrollTop=scroll;
  if(focused)[...list.children].find(node=>node.dataset.accountId===focused)?.focus({preventScroll:true});
  renderDiscoveryControls();
}
function renderAdvancedAccounts(){
  const list=$('advanced-accounts');list.replaceChildren();
  const items=accountUI.items.filter(item=>!item.automatic&&!item.ready);
  $('advanced-sources').hidden=!items.length;
  for(const item of items)list.append(accountRow(item,false,true));
}
function openAdvancedImport(){
  $('account-dialog').close();$('prepare-dialog').close();$('account-detail-dialog').close();
  if(!$('source-dialog').open)$('source-dialog').showModal();
}
async function openAccountPicker(){
  try{await refreshAccounts();}catch(_){/* Keep the selector open with the discovery error and retry controls. */}
  if(!$('account-dialog').open)$('account-dialog').showModal();
}
async function proceedWithAccount(){
  if(accountReadBusy())return;
  const item=chosenAccount();
  if(!usableAccount(item)){await openAccountPicker();return;}
  if(item.ready)await activateAccount(item);
  else await startAutoAccountRead(item.id);
}
async function openChatWorkspace(){
  if(accountUI.active?.ready&&(!accountUI.selectedId||accountUI.selectedId===accountUI.active.id)&&!accountUI.selectionLost){
    showPage('chats');return;
  }
  await proceedWithAccount();
}
function showAccountDetail(item,advanced=false){
  accountUI.detail=item;accountUI.detailAdvanced=advanced;$('account-dialog').close();
  setAvatar($('account-detail-avatar'),item.avatar_url,item.name);$('account-detail-name').textContent=accountName(item);
  $('account-detail-sub').textContent=accountSummary(item);
  $('account-ready-note').textContent=item.ready?(item.status==='partial'?'部分记录已经保存，可直接查看；处理结果中保留未读项目。':'此账号已有保存的记录，可以直接进入。'):
    advanced?'这是高级导入来源，确认后读取。':'已自动发现此微信账号，点击下一步开始读取。';
  $('account-ready-note').classList.toggle('pending',!item.ready||item.status==='partial');
  $('account-client-version').textContent=accountUI.environment.version||'未检测到';
  $('account-detail-layout').textContent=item.layout||'自动发现';$('account-detail-path').textContent=item.path||'由软件自动定位';
  $('account-export-dir').value=item.export_dir||'';$('account-export-dir').placeholder='使用此账号的默认导出目录';
  $('account-next').textContent=item.ready?'直接进入':'读取并进入聊天';
  $('account-refresh-data').hidden=!item.ready;$('account-refresh-data').disabled=item.exists===false;
  if(!$('account-detail-dialog').open)$('account-detail-dialog').showModal();
}
async function activateAccount(item,folder=''){
  if(accountUI.activating)return;
  accountUI.activating=true;renderDiscoveryControls();
  try{
    const account=await api('/api/accounts/open',{id:item.id,folder});
    if(account.id!==item.id)throw new Error('打开结果与所选账号不一致，已停止自动进入，请重新选择账号。');
    accountUI.active=account;accountUI.selectedId=account.id;accountUI.selectionLost=false;
    state.settings.export_dir=account.export_dir;++state.request;state.selected=null;state.rawItems=[];state.rawStart=0;
    state.offset=0;state.rawLoading=0;state.checked.clear();state.checkedMessages.clear();state.viewport=null;state.rawTotal=0;
    $('person-view').hidden=true;$('empty-state').hidden=false;resetLiveSource(account.id);renderActiveAccount();
    $('account-partial-note').hidden=item.status!=='partial';
    $('account-partial-note').textContent=item.status==='partial'?'此账号已有部分记录可查看，未完成项目请查看处理结果。':'';
    $('account-filter').value='';$('search-people').value='';$('source-path').value=account.path||'';
    for(const id of ['raw-query','raw-since','raw-until','raw-type','raw-direction'])$(id).value='';
    $('message-selection-mode').checked=false;$('raw-messages').replaceChildren();updateRawCount();
    for(const id of ['account-detail-dialog','account-flow-dialog','account-dialog','source-dialog'])$(id).close();
    $('active-account-label').textContent=accountName(account);$('active-account-label').title=account.alias||account.wxid||'';
    showPage('chats');await refreshPeople();renderHomeAccounts();await pollLiveState();
  }finally{accountUI.activating=false;renderHomeAccounts();renderAccountPicker();}
}
function prepareAccountFlow(context){
  accountUI.readContext=context;accountUI.flowDismissed=false;accountUI.readPath=context.path||'';
  $('flow-stop').disabled=false;$('flow-stop').textContent='停止读取';
  for(const id of ['account-detail-dialog','account-dialog','report-dialog','source-dialog'])$(id).close();
  state.accountFlow={phase:'checking',active:true,message:context.kind==='auto'?'正在准备所选微信账号，请稍候…':'正在准备导入所选记录…'};
  renderAccountFlow();if(!$('account-flow-dialog').open)$('account-flow-dialog').showModal();
}
async function startAutoAccountRead(id,mode='read'){
  if(accountReadBusy())return;
  if(state.job?.status==='running')throw new Error('当前任务尚未完成，请稍候再读取账号');
  const confirmed=mode==='restart'&&$('flow-restart-confirm').checked;
  if(mode==='restart'&&!confirmed)throw new Error('请先确认已经完成微信同步并保存编辑内容');
  const item=defaultAccounts().find(candidate=>candidate.id===id);
  if(!usableAccount(item))throw new Error('所选账号暂不可用，请重新检测并选择账号');
  accountUI.loading=true;accountUI.pendingExportDir='';
  prepareAccountFlow({kind:'auto',id:item.id,path:item.path||'',jobId:null});
  $('flow-restart-confirm').checked=confirmed;renderHomeAccounts();renderAccountPicker();
  try{
    const job=await api('/api/accounts/auto-read',{id:item.id,mode,owner:state.settings.owner||'',restart_confirmed:confirmed});
    accountUI.readContext.jobId=job.id;await updateJob(job);
    const value=await api('/api/state');receiveAccountFlow(value);
    if(!value.job||value.job.id===job.id)await updateJob(value.job);
  }catch(error){
    state.accountFlow={...(state.accountFlow||{}),phase:'error',active:false,message:error.message};renderAccountFlow();throw error;
  }finally{accountUI.loading=false;renderHomeAccounts();renderAccountPicker();}
}
// Explicit advanced import retains the existing folder/backup and confirmed restart paths.
async function startAccountRead(path,mode='read'){
  if(accountReadBusy())return;
  if(state.job?.status==='running')throw new Error('当前任务尚未完成，请稍候再导入');
  if(!path?.trim()){openAdvancedImport();return;}
  if(mode==='restart'&&!$('flow-restart-confirm').checked)throw new Error('请先确认已经完成微信同步并保存编辑内容');
  accountUI.loading=true;
  accountUI.pendingExportDir=accountUI.detail?.path===path?$('account-export-dir').value:'';
  const item=accountUI.items.find(candidate=>candidate.path===path);
  prepareAccountFlow({kind:'advanced',id:item?.id||null,path,jobId:null});
  $('source-path').value=path;state.settings.source_paths=[path];
  $('flow-restart-confirm').checked=mode==='restart'&&$('flow-restart-confirm').checked;
  try{
    const job=await api('/api/accounts/read',{path,mode,owner:state.settings.owner||'',
      backup_password:$('backup-password').value,restart_confirmed:mode==='restart'});
    accountUI.readContext.jobId=job.id;await updateJob(job);
    const value=await api('/api/state');receiveAccountFlow(value);
    if(!value.job||value.job.id===job.id)await updateJob(value.job);
  }catch(error){
    state.accountFlow={...(state.accountFlow||{}),phase:'error',active:false,message:error.message};renderAccountFlow();throw error;
  }finally{accountUI.loading=false;renderHomeAccounts();renderAccountPicker();}
}
function receiveAccountFlow(value){
  if(!value.account_flow)return;
  if(!accountUI.readContext&&value.job?.kind==='account-read'&&value.job.status==='running'){
    accountUI.readContext={kind:value.account_flow.automatic===false?'advanced':'auto',id:value.account_flow.account?.id||null,
      path:value.account_flow.automatic===false?value.account_flow.path||'':'',jobId:value.job.id,resumed:true};
  }
  accountUI.readPath=value.account_flow.path||accountUI.readPath;state.accountFlow=value.account_flow;renderAccountFlow();
  if(value.account_flow.active&&value.job?.kind==='account-read'&&!accountUI.flowDismissed&&!$('account-flow-dialog').open)
    $('account-flow-dialog').showModal();
}
function renderAccountFlow(){
  const flow=state.accountFlow;if(!flow)return;
  const titles={checking:'正在准备账号',waiting_login:'等待微信登录',connecting:'正在连接聊天记录',reading:'正在读取聊天记录',
    confirm:flow.partial?'部分记录已读取':'正在进入聊天',needs_connection:'暂时无法读取此账号',needs_restart:'需要重新登录微信',
    close_wechat:'退出微信后继续读取',retry_read:'需要重新读取记录',error:'读取需要你的操作',cancelled:'本次读取已停止'};
  $('flow-title').textContent=titles[flow.phase]||'正在准备账号';
  $('flow-subtitle').textContent=flow.active?'请稍候，读取完成后会自动进入聊天。':flow.phase==='confirm'?
    '账号已准备好，正在进入聊天。':'可以重试读取、打开微信或重新检测账号。';
  const elapsed=state.job?.started?Math.max(0,Math.floor(Date.now()/1000-state.job.started)):0;
  const elapsedText=elapsed>=60?`${Math.floor(elapsed/60)} 分 ${elapsed%60} 秒`:`${elapsed} 秒`;
  $('flow-message').textContent=(flow.message||'')+(flow.active?` · 已用时 ${elapsedText}`:'');
  $('flow-path').hidden=accountUI.readContext?.kind!=='advanced';$('flow-path').textContent=flow.path||'';
  const icon=$('flow-state-icon');icon.replaceChildren();icon.className='flow-state-icon '+(flow.active?'working':flow.phase==='confirm'?'complete':'attention');
  if(flow.active)icon.append(el('span','spinner'));else icon.textContent=flow.phase==='confirm'?'✓':flow.phase==='cancelled'?'—':'!';
  const step={checking:0,waiting_login:1,connecting:2,reading:3,confirm:4,close_wechat:3,retry_read:3,needs_restart:1}[flow.phase];
  [...$('flow-steps').children].forEach((node,i)=>{node.classList.toggle('done',step!==undefined&&i<step);node.classList.toggle('current',i===step);});
  const info=$('flow-account');info.replaceChildren();info.hidden=!flow.account?.ready;
  if(flow.account?.ready)info.append(el('strong','',accountName(flow.account)),el('span','',accountSummary(flow.account)));
  $('flow-partial').hidden=!flow.partial;
  const env=accountUI.environment,restart=flow.phase==='needs_restart';
  $('flow-restart-note').hidden=!restart||!env.capture_supported;$('flow-restart').hidden=!restart||!env.capture_supported;
  $('flow-restart').disabled=!$('flow-restart-confirm').checked;$('flow-capture').hidden=!restart||!env.capture_supported;
  $('flow-launch').hidden=!env.installed||!env.windows||flow.phase==='confirm'||flow.phase==='reading';
  $('flow-stop').hidden=!flow.active;$('flow-read').hidden=flow.active||flow.phase==='confirm';
  $('flow-read').textContent=flow.phase==='close_wechat'?'继续读取':'重试读取';
  $('flow-redetect').hidden=flow.active||flow.phase==='confirm';
  $('flow-view').hidden=!flow.account?.ready;$('flow-view').textContent='进入已保存记录';
  $('flow-report').hidden=flow.active||!state.report;
}
function completedAccount(job){
  const context=accountUI.readContext,result=job.result?.account||state.accountFlow?.account;
  if(context?.id){
    const exact=accountUI.items.find(item=>item.id===context.id&&item.ready);
    if(result?.ready&&result.id===context.id)return result;
    if(result?.ready&&result.id!==context.id)return null;
    return exact||null;
  }
  if(result?.ready&&result.id)return result;
  if(context?.kind==='advanced'&&context.path)return accountUI.items.find(item=>item.path===context.path&&item.ready)||null;
  return null;
}
async function finishAccountJob(job){
  if(accountUI.readContext?.jobId&&accountUI.readContext.jobId!==job.id)return;
  const value=await api('/api/state');state.settings=value.settings;state.stats=value.stats;receiveAccountFlow(value);
  if(job.status==='error')state.accountFlow={...(state.accountFlow||{}),active:false,phase:'error',message:job.message};
  if(job.status==='cancelled')state.accountFlow={...(state.accountFlow||{}),active:false,phase:'cancelled',message:'本次读取已停止，已完成的记录会保留。'};
  await refreshAccounts();updateHome();renderAccountFlow();
  $('stats').textContent=`已读取 ${value.stats.conversations} 个联系人／群聊 · ${value.stats.messages.toLocaleString()} 条消息`;
  const item=completedAccount(job),success=!['error','cancelled'].includes(job.status);
  const matchingJob=!accountUI.readContext?.jobId||accountUI.readContext.jobId===job.id;
  if(success&&accountUI.readContext&&matchingJob&&item?.ready&&accountUI.enteredJob!==job.id){
    try{
      await activateAccount(item,accountUI.pendingExportDir||'');accountUI.enteredJob=job.id;
      const partial=state.accountFlow?.partial||item.status==='partial'||job.result?.partial;
      $('account-partial-note').hidden=!partial;
      $('account-partial-note').textContent=partial?'部分记录已读取，仍有项目未完成。可在“查看处理结果”中查看详情。':'';
      if(partial)toast('已进入部分读取的聊天记录；未完成项目保留在处理结果中。');
      return;
    }catch(error){
      state.accountFlow={...(state.accountFlow||{}),active:false,phase:'error',message:error.message};renderAccountFlow();
      if(!$('account-flow-dialog').open)$('account-flow-dialog').showModal();return;
    }
  }
  if(success&&accountUI.readContext&&!item)state.accountFlow={...(state.accountFlow||{}),active:false,phase:'error',
    message:'当前尚未确认所选账号已读取完成。已保留处理结果，请重新检测或重试；不会自动进入其他账号。'};
  renderAccountFlow();if(!$('account-flow-dialog').open)toast(state.accountFlow?.message||job.message);
}
async function retryAccountRead(mode='read'){
  const context=accountUI.readContext;
  if(context?.kind==='advanced')return startAccountRead(context.path,mode);
  const id=context?.id||state.accountFlow?.account?.id;
  if(id)return startAutoAccountRead(id,mode);
  await openAccountPicker();
}
async function launchAndDetect(){
  const result=await api('/api/accounts/launch',{});
  toast(result?.message||'请在电脑版微信窗口完成登录，然后点击重新检测。');
  await redetectAccounts();
}
function initAccounts(){
  $('account-switch').onclick=guard(openAccountPicker);$('home-add-account').onclick=guard(openAccountPicker);
  $('home-prepare').onclick=()=>$('prepare-dialog').showModal();
  $('home-read').onclick=guard(proceedWithAccount);$('home-workspace').onclick=guard(openChatWorkspace);
  $('home-launch').onclick=guard(launchAndDetect);$('home-redetect').onclick=guard(redetectAccounts);
  $('home-advanced').onclick=openAdvancedImport;$('account-advanced').onclick=openAdvancedImport;
  $('account-picker-close').onclick=()=>$('account-dialog').close();
  $('accounts-refresh').onclick=guard(redetectAccounts);$('account-continue').onclick=guard(proceedWithAccount);
  $('account-detail-open').onclick=()=>{const item=chosenAccount();if(item)showAccountDetail(item);};
  $('account-launch').onclick=guard(launchAndDetect);
  $('account-detail-close').onclick=()=>$('account-detail-dialog').close();
  $('account-detail-back').onclick=guard(async()=>{$('account-detail-dialog').close();if(accountUI.detailAdvanced)openAdvancedImport();else await openAccountPicker();});
  $('account-export-browse').onclick=guard(async()=>{const value=await api('/api/pick-folder',{});if(value.path)$('account-export-dir').value=value.path;});
  $('account-next').onclick=guard(async()=>{
    const item=accountUI.detail;if(!item)return;
    if(item.ready)await activateAccount(item,$('account-export-dir').value);
    else if(accountUI.detailAdvanced)await startAccountRead(item.path);
    else await startAutoAccountRead(item.id);
  });
  $('account-refresh-data').onclick=guard(()=>accountUI.detailAdvanced?startAccountRead(accountUI.detail.path):startAutoAccountRead(accountUI.detail.id));
  $('flow-restart-confirm').onchange=renderAccountFlow;
  $('flow-restart').onclick=guard(()=>retryAccountRead('restart'));
  $('flow-capture').onclick=guard(()=>retryAccountRead('capture'));
  $('flow-read').onclick=guard(()=>retryAccountRead());$('flow-launch').onclick=guard(launchAndDetect);
  $('flow-redetect').onclick=guard(async()=>{$('account-flow-dialog').close();await redetectAccounts();await openAccountPicker();});
  $('flow-stop').onclick=guard(async()=>{await api('/api/cancel',{});$('flow-stop').disabled=true;$('flow-stop').textContent='正在停止…';});
  $('flow-close').onclick=()=>{accountUI.flowDismissed=true;$('account-flow-dialog').close();if(state.accountFlow?.active)toast('正在后台读取，完成后会自动进入聊天。');};
  $('flow-report').onclick=()=>{$('account-flow-dialog').close();showReport();};
  $('flow-view').onclick=guard(async()=>{await refreshAccounts();const item=completedAccount(state.report||{});if(item)await activateAccount(item,accountUI.pendingExportDir||'');else toast('所选账号尚未就绪，请稍候或重试。');});
  $('prepare-close').onclick=()=>$('prepare-dialog').close();
  $('prepare-choose').onclick=guard(async()=>{$('prepare-dialog').close();await openAccountPicker();});
  $('prepare-launch').onclick=guard(launchAndDetect);
  $('job-text').setAttribute('role','button');$('job-text').tabIndex=0;
  $('job-text').onclick=()=>{if(state.job?.kind==='account-read'&&state.accountFlow){accountUI.flowDismissed=false;renderAccountFlow();if(!$('account-flow-dialog').open)$('account-flow-dialog').showModal();}};
  $('job-text').onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();$('job-text').click();}};
  renderHomeAccounts();renderAccountPicker();renderAdvancedAccounts();
  setInterval(()=>{void pollAccountDiscovery();},3000);
}
