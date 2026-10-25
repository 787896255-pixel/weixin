'use strict';
const mediaUI={source:'',offset:0};
function mediaURL(item){return '/api/media-file?'+new URLSearchParams({id:item.id,session:token});}
function appendMedia(item,parent){
  const wrap=el('div','media-preview');
  if(item.status==='ready'){
    if(item.kind==='image'){
      const image=el('img');image.src=mediaURL(item);image.alt=item.name;image.loading='lazy';wrap.append(image);
    }else if(item.kind==='audio'||item.kind==='video'){
      const player=el(item.kind);player.controls=true;player.preload='none';player.src=mediaURL(item);wrap.append(player);
    }
    const link=el('a','media-download',item.kind==='file'?'下载 '+item.name:'保存文件');
    const ext={'image/jpeg':'.jpg','image/png':'.png','image/gif':'.gif','image/webp':'.webp','audio/wav':'.wav','video/mp4':'.mp4'}[item.mime];
    link.href=mediaURL(item);link.download=ext?item.name.replace(/\.[^.]+$/,'')+ext:item.name;wrap.append(link);
  }else{
    if(item.error)wrap.append(el('p','warning',item.error));
    const load=el('button','button',item.status==='queued'?'加载预览':'重试读取');
    load.onclick=guard(async()=>{
      load.disabled=true;load.textContent='正在准备预览…';
      try{
        await api('/api/media/prepare',{ids:[item.id]});
        for(let attempt=0;attempt<240&&wrap.isConnected;attempt++){
          await new Promise(resolve=>setTimeout(resolve,1000));
          const r=await api('/api/media-status?'+new URLSearchParams({ids:item.id}));
          const next=r.items[0];
          if(next?.status==='ready'){wrap.remove();appendMedia(next,parent);return;}
          if(next?.status==='unread')throw Error(next.error||'媒体未能解码');
        }
        if(wrap.isConnected)throw Error('该媒体仍在处理，稍后可以重新查看');
      }finally{load.disabled=false;load.textContent='加载预览';}
    });
    wrap.append(load);
  }
  parent.append(wrap);
}
async function openMedia(source){
  mediaUI.source=source??accountUI.active?.id??'';mediaUI.offset=0;
  $('media-items').replaceChildren();
  $('media-source-path').value=accountUI.active?.path||$('source-path').value||state.settings.source_paths?.[0]||'';
  await loadMedia();if(!$('media-dialog').open)$('media-dialog').showModal();
}
async function loadMedia(){
  const result=await api('/api/media-list?'+new URLSearchParams({source:mediaUI.source,offset:mediaUI.offset}));
  $('media-total').textContent=`已登记 ${result.total} 个本地媒体文件；点击预览时解码，导出时只处理所需文件。`;
  for(const item of result.items){
    const card=el('div','media-card');card.append(el('strong','',item.name),
      el('small','',item.linked_messages?`已关联 ${item.linked_messages} 条聊天`:'尚未关联聊天'));
    appendMedia(item,card);$('media-items').append(card);
  }
  mediaUI.offset+=result.items.length;$('media-more').hidden=!result.has_more;
}
function initMedia(){
  api('/api/media-list?offset=0').then(r=>{$('media-open').textContent=r.total?`媒体文件（${r.total}）`:'媒体文件';}).catch(()=>{});
  $('media-open').onclick=guard(()=>openMedia());
  $('media-close').onclick=()=>$('media-dialog').close();
  $('media-more').onclick=guard(loadMedia);
  $('media-browse').onclick=guard(async()=>{const r=await api('/api/pick-folder',{});if(r.path)$('media-source-path').value=r.path;});
  $('media-read').onclick=guard(async()=>{const path=$('media-source-path').value.trim();if(!path)throw Error('先选择聊天记录目录');
    $('media-dialog').close();await updateJob(await api('/api/media/read',{path,backup_password:$('backup-password').value}));});
  $('media-export').onclick=guard(async()=>{const r=await api('/api/pick-folder',{});if(!r.path)return;
    $('media-dialog').close();await updateJob(await api('/api/media/export',{folder:r.path,source:mediaUI.source}));});
}
