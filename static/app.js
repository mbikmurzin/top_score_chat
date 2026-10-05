const $=(s,p=document)=>p.querySelector(s),$$=(s,p=document)=>[...p.querySelectorAll(s)];
const state={boot:null,campaigns:null,draft:null,file:null,source:null,campaignDraftRows:null};
const money=n=>n==null?'—':new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB',maximumFractionDigits:0}).format(n), num=n=>n==null?'—':new Intl.NumberFormat('ru-RU',{maximumFractionDigits:1}).format(n), pct=n=>n==null?'—':num(n)+'%';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const month=s=>s?new Date(+s.slice(0,4),+s.slice(5,7)-1).toLocaleDateString('ru-RU',{month:'long',year:'numeric'}):'—';
const calendarDate=s=>s?new Date(`${s}T00:00:00`).toLocaleDateString('ru-RU'):'—';
['sum-as-of','top-as-of'].forEach(id=>{const input=$(`#${id}`);if(input?.tagName==='INPUT'){const select=document.createElement('select');select.id=id;select.innerHTML='<option value="">Актуальные данные</option>';input.replaceWith(select)}});
function mergeReportKeyHeaders(tableSelector,keyColumns){const rows=$$('thead tr',$(tableSelector));if(rows.length<2)return;[...rows[0].children].slice(0,keyColumns).forEach(cell=>cell.rowSpan=2);[...rows[1].children].slice(0,keyColumns).forEach(cell=>cell.remove())}
mergeReportKeyHeaders('#summary-table',3);mergeReportKeyHeaders('#top-table',2);
async function api(url,o={}){const r=await fetch(url,o);if(!r.ok){let m;try{m=(await r.json()).detail}catch{m=await r.text()}throw Error(m||'Ошибка')}return r.json()}
function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)}
let actionPopupTimer;
function showAction(kind,title,message,autoClose=0){
 clearTimeout(actionPopupTimer);
 const popup=$('#action-popup');
 popup.className=`action-popup show ${kind}`;
 $('#action-popup-title').textContent=title;
 $('#action-popup-message').textContent=message;
 $('#action-popup-close').hidden=kind==='loading';
 if(autoClose)actionPopupTimer=setTimeout(hideAction,autoClose);
}
function hideAction(){clearTimeout(actionPopupTimer);$('#action-popup').classList.remove('show')}
const actionStart=(title,message)=>showAction('loading',title,message);
const actionProgress=(title,message)=>showAction('loading',title,message);
const actionSuccess=(title,message)=>showAction('success',title,message,2200);
const actionError=(title,error)=>showAction('error',title,error?.message||String(error));
const actionInfo=(title,message)=>showAction('info',title,message,1800);
$('#action-popup-close').onclick=hideAction;
const titles={uploads:'Загрузка данных',campaigns:'РК и траты',summary:'Сводная по РК',topscore:'TopScore по воронкам'};
async function go(p,feedback=false){
 $$('.page').forEach(x=>x.classList.toggle('active',x.id===`page-${p}`));$$('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.page===p));$('#page-title').textContent=titles[p];location.hash=p;$('.sidebar').classList.remove('open');
 if(feedback)actionStart('Открываем раздел',`Загружаем данные раздела «${titles[p]}».`);
 try{if(p==='campaigns')await loadCampaigns();if(p==='summary')await loadSummary();if(p==='topscore')await loadTopscore();if(feedback)actionSuccess('Раздел открыт',`Данные раздела «${titles[p]}» загружены.`)}catch(e){if(feedback)actionError('Не удалось открыть раздел',e);else throw e}
}
$$('.nav-item').forEach(x=>x.onclick=()=>go(x.dataset.page,true));$('.mobile-menu').onclick=()=>{const opened=$('.sidebar').classList.toggle('open');actionInfo(opened?'Меню открыто':'Меню закрыто',opened?'Выберите нужный раздел платформы.':'Возвращаемся к текущему разделу.')};

const slots=[
 ['subscribers','Подписчики воронок из SaleBot','Таблицы подписчиков по каждой воронке SaleBot','ссылка'],
 ['id_map','MAX ID для подписчиков без ID в SaleBot','Постоянный справочник: client ID SaleBot → ID пользователя MAX','файл'],
 ['retail_funnel','Лиды и клиенты с воронок в RetailCRM','Выгрузка из RetailCRM по лидам и клиентам, пришедшим через воронки','файл'],
 ['retail_channel','Лиды и клиенты с MAX-канала в RetailCRM','Выгрузка из RetailCRM по лидам и клиентам, пришедшим из канала MAX','файл'],
 ['channel_subscribers','Подписчики канала MAX','Выгрузка подписчиков и их статусов из канала MAX','файл']
];
async function init(){actionStart('Запуск платформы','Получаем сохранённые данные и подготавливаем интерфейс.');state.boot=await api('/api/bootstrap');renderSources();await loadCampaigns(false);await go(location.hash.slice(1)||'uploads');actionSuccess('Платформа готова','Данные загружены. Можно начинать работу.')}
function latest(type){return state.boot.history.find(x=>x.source_type===type&&x.active)}
function renderSources(){const visibleSlots=slots.filter(([id])=>id!=='id_map'||!state.boot.backend_sources?.id_map?.ready);$('#source-grid').innerHTML=visibleSlots.map(([id,title,text,kind],i)=>{const h=latest(id),marker=h?'✓':String(i+1).padStart(2,'0');return `<article class="card source-card ${h?'source-card--loaded':''}"><div class="source-no ${h?'loaded':''}" title="${h?'Таблица загружена':'Ожидает загрузки'}">${marker}</div><div class="source-copy"><h3>${title}</h3><p>${text}</p>${h?`<small class="source-success">Таблица загружена · обновлено ${new Date(h.created_at).toLocaleString('ru-RU')} · принято ${num(h.accepted_count)}</small>`:'<small>Данные ещё не загружены</small>'}</div><button class="btn ${i===0?'primary':'ghost'} upload-open" data-source="${id}">${kind==='ссылка'?'Добавить':'Загрузить'}</button></article>`}).join('');$$('.upload-open').forEach(x=>x.onclick=()=>openUpload(x.dataset.source))}
function openUpload(source){
 state.source=source;state.file=null;
 const isSub=source==='subscribers',funnels=state.boot.funnels.filter(f=>f.active);
 const links=isSub?`<div class="funnel-links"><p class="bulk-hint">Ссылки заполнены автоматически. При необходимости их можно изменить.</p>${funnels.map(f=>`<label class="funnel-link-row"><span>${esc(f.name)}</span><input class="funnel-link" data-funnel="${esc(f.id)}" value="${esc(state.boot.salebot_links?.[f.id]||'')}" placeholder="https://salebot.pro/shared/table/…"><small class="link-status"></small></label>`).join('')}</div><button class="btn primary wide" id="import-links" disabled>Загрузить все таблицы SaleBot</button><div class="divider"><span>или загрузите один файл</span></div><label class="field">Воронка для файла<select id="up-funnel">${funnels.map(f=>`<option value="${esc(f.id)}">${esc(f.name)}</option>`).join('')}</select></label>`:'';
 $('#upload-workspace').innerHTML=`<article class="card inline-upload bulk-upload"><div class="panel-head"><div><p class="eyebrow">${esc(state.boot.sources[source])}</p><h2>${isSub?'Добавьте ссылки на все воронки':'Добавьте таблицу'}</h2></div><button class="close-upload">×</button></div>${links}<label class="dropzone"><input type="file" id="up-file" accept=".csv,.xlsx,.xls"><span class="upload-icon">↑</span><b>${isSub?'Выберите XLSX, XLS или CSV':'Выберите XLSX, XLS или CSV'}</b><em id="up-name"></em></label><button class="btn primary" id="read-file" disabled>Прочитать файл</button><div id="mapping"></div></article>`;
 $('.close-upload').onclick=()=>{$('#upload-workspace').innerHTML='';actionInfo('Форма закрыта','Изменения в форме не применялись.')};
 const updateFile=()=>$('#read-file').disabled=!state.file;
 $('#up-file').onchange=e=>{state.file=e.target.files[0];$('#up-name').textContent=state.file?.name||'';updateFile();if(state.file)actionInfo('Файл выбран',`${state.file.name}\nНажмите «Прочитать файл», чтобы проверить его структуру.`)};
 if(isSub){
  const updateLinks=()=>$('#import-links').disabled=!$$('.funnel-link').some(x=>x.value.trim());
  $$('.funnel-link').forEach(x=>x.oninput=updateLinks);
  updateLinks();
  $('#import-links').onclick=importSubscriberLinks;
 }
 $('#read-file').onclick=inspectFile;
 $('#upload-workspace').scrollIntoView({behavior:'smooth'});
 actionInfo('Форма загрузки открыта',isSub?'Ссылки SaleBot уже заполнены. Проверьте их и запустите общую загрузку.':'Выберите файл CSV, XLSX или XLS для загрузки.');
}

async function importSubscriberLinks(){
 const entries=$$('.funnel-link').filter(x=>x.value.trim());
 const button=$('#import-links');let loaded=0,failed=0;
 actionStart('Загрузка таблиц SaleBot',`Подготовлено таблиц: ${entries.length}. Начинаем получение данных.`);
 button.disabled=true;button.textContent=`Загрузка: 0 из ${entries.length}`;
 for(let i=0;i<entries.length;i++){
  const input=entries[i],status=$('.link-status',input.closest('.funnel-link-row'));
  const funnelName=$('span',input.closest('.funnel-link-row')).textContent;
  actionProgress('Загрузка таблиц SaleBot',`Обрабатывается ${i+1} из ${entries.length}: ${funnelName}\nУже загружено: ${loaded}. Ошибок: ${failed}.`);
  input.disabled=true;status.className='link-status loading';status.textContent='Получение данных…';
  try{
   const draft=await api('/api/uploads/inspect-link',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_type:'subscribers',url:input.value.trim()})});
   const missing=draft.required.filter(field=>!draft.mapping[field]);
   if(missing.length)throw Error(`Не найдены столбцы: ${missing.join(', ')}`);
   const result=await api('/api/uploads/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:draft.token,mapping:draft.mapping,funnel_id:input.dataset.funnel})});
   loaded++;status.className='link-status success';status.textContent=result.duplicate_file?'Уже загружено ранее':`Загружено строк: ${num(result.quality.accepted)}`;
  }catch(e){failed++;status.className='link-status error';status.textContent=e.message}
  input.disabled=false;button.textContent=`Загрузка: ${i+1} из ${entries.length}`;
 }
 state.boot=await api('/api/bootstrap');renderSources();
 button.disabled=false;button.textContent='Загрузить все таблицы SaleBot';
 if(failed)actionError('Загрузка завершена с ошибками',`Успешно загружено: ${loaded}. Ошибок: ${failed}. Подробности указаны напротив каждой воронки.`);else actionSuccess('Все таблицы загружены',`Обработано таблиц: ${loaded}. Итоговые показатели пересчитаны.`);
}

async function inspectFile(){actionStart('Чтение файла',`Проверяем структуру файла «${state.file?.name||''}» и определяем столбцы.`);try{const fd=new FormData();fd.append('source_type',state.source);fd.append('file',state.file);state.draft=await api('/api/uploads/inspect',{method:'POST',body:fd});renderMapping();actionSuccess('Файл прочитан',`Найдено строк: ${num(state.draft.rows)}. Проверьте сопоставление столбцов ниже.`)}catch(e){actionError('Не удалось прочитать файл',e)}}
function renderMapping(){const d=state.draft,fields=[...new Set([...d.required,...Object.keys(d.mapping)])];$('#mapping').innerHTML=`<div class="divider"></div><h3>Сопоставление колонок <span class="pill">${num(d.rows)} строк</span></h3><div class="mapping-list">${fields.map(f=>`<label class="mapping-row ${d.required.includes(f)?'required':''}"><span>${f}</span><select data-field="${f}"><option value="">Не выбрано</option>${d.columns.map(c=>`<option ${d.mapping[f]===c?'selected':''}>${esc(c)}</option>`).join('')}</select></label>`).join('')}</div><button class="btn primary wide" id="confirm-up">Подтвердить и пересчитать</button>`;$('#confirm-up').onclick=confirmUpload}
async function confirmUpload(){actionStart('Сохранение данных','Загружаем строки в базу и пересчитываем итоговые показатели.');try{const mapping={};$$('#mapping select').forEach(x=>{if(x.value)mapping[x.dataset.field]=x.value});const r=await api('/api/uploads/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:state.draft.token,mapping,funnel_id:state.source==='subscribers'?$('#up-funnel').value:null})});state.boot=await api('/api/bootstrap');renderSources();$('#upload-workspace').innerHTML='';actionSuccess(r.duplicate_file?'Файл уже был загружен':'Таблица загружена',r.duplicate_file?'Повторная копия не добавлена. Данные остаются учтёнными.':`Принято строк: ${num(r.quality.accepted)}. Итоговые показатели пересчитаны.`)}catch(e){actionError('Не удалось сохранить таблицу',e)}}

function campaignFunnelOptions(selected=''){
 const funnels=state.campaigns?.funnels||[];
 return funnels.length?funnels.map(f=>`<option value="${esc(f.id)}" ${f.id===selected?'selected':''}>${esc(f.name)}</option>`).join(''):'<option value="">Сначала загрузите подписчиков воронок</option>';
}
function syncCampaignPasteControls(){
 const funnel=$('#rk-batch-funnel'),selected=funnel.value;
 funnel.innerHTML=campaignFunnelOptions(selected);
 if(!$('#rk-batch-month').value){const today=new Date();$('#rk-batch-month').value=`${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}`}
}
function parseSpend(value){
 const normalized=String(value??'').replace(/\s/g,'').replace(/[₽р]/gi,'').replace(',','.');
 return normalized!==''&&Number.isFinite(Number(normalized))?Number(normalized):null;
}
const blankCampaignRow=()=>({campaignId:'',rawSpend:''});
function ensureCampaignDraftRows(){
 if(!state.campaignDraftRows)state.campaignDraftRows=Array.from({length:4},blankCampaignRow);
}
function campaignGridData(){
 ensureCampaignDraftRows();
 const seen=new Set(),monthValue=$('#rk-batch-month').value,funnelId=$('#rk-batch-funnel').value;
 const existing=new Set((state.campaigns?.campaigns||[]).filter(c=>c.month===monthValue&&c.funnel_id===funnelId).map(c=>String(c.campaign_id)));
 return state.campaignDraftRows.map((row,index)=>{
  const campaignId=String(row.campaignId||'').trim(),rawSpend=String(row.rawSpend||'').trim(),empty=!campaignId&&!rawSpend,spend=parseSpend(rawSpend);let error='';
  if(!empty&&!campaignId)error='Не указан ID РК';
  else if(!empty&&!rawSpend)error='Не указаны траты';
  else if(!empty&&(spend===null||spend<0))error='Некорректная сумма';
  else if(!empty&&seen.has(campaignId))error='ID повторяется в списке';
  else if(!empty&&existing.has(campaignId))error='Такая РК уже добавлена';
  if(campaignId)seen.add(campaignId);
  return {index,line:index+1,campaignId,rawSpend,spend,error,empty};
 });
}
function updateCampaignGridMeta(){
 const rows=campaignGridData(),filled=rows.filter(row=>!row.empty),errors=filled.filter(row=>row.error),valid=filled.filter(row=>!row.error),total=valid.reduce((sum,row)=>sum+(row.spend||0),0);
 rows.forEach(row=>{const cell=$(`[data-rk-status="${row.index}"]`);if(cell)cell.innerHTML=row.empty?'<span class="campaign-row-empty">Пустая строка</span>':row.error?`<span class="paste-error">${esc(row.error)}</span>`:'<span class="paste-ok">✓ Готово</span>'});
 const summary=$('#rk-paste-summary');summary.textContent=filled.length?`${filled.length} РК · ${money(total)}${errors.length?` · ошибок: ${errors.length}`:' · ошибок нет'}`:'Добавьте рекламные кампании';summary.classList.toggle('has-errors',errors.length>0);
 $('#save-rks').disabled=!valid.length||errors.length>0;$('#save-rks').textContent=valid.length?`Сохранить ${valid.length} РК`:'Сохранить РК';
}
function renderCampaignGrid(){
 ensureCampaignDraftRows();
 $('#rk-editor-rows').innerHTML=state.campaignDraftRows.map((row,index)=>`<tr data-rk-row="${index}"><td class="campaign-row-number">${index+1}</td><td><input class="campaign-cell" data-rk-field="campaignId" data-index="${index}" value="${esc(row.campaignId)}" placeholder="Например, 4839201" autocomplete="off"></td><td><input class="campaign-cell campaign-spend" data-rk-field="rawSpend" data-index="${index}" value="${esc(row.rawSpend)}" placeholder="0" inputmode="decimal" autocomplete="off"></td><td data-rk-status="${index}"></td><td><button class="campaign-remove-row" data-index="${index}" type="button" aria-label="Удалить строку">×</button></td></tr>`).join('');
 $$('.campaign-cell',$('#rk-editor-rows')).forEach(input=>{
  input.oninput=()=>{state.campaignDraftRows[+input.dataset.index][input.dataset.rkField]=input.value;updateCampaignGridMeta()};
  input.onpaste=handleCampaignGridPaste;
  input.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();const next=$(`.campaign-cell[data-index="${Math.min(+input.dataset.index+1,state.campaignDraftRows.length-1)}"][data-rk-field="${input.dataset.rkField}"]`);next?.focus()}};
 });
 $$('.campaign-remove-row',$('#rk-editor-rows')).forEach(button=>button.onclick=()=>{state.campaignDraftRows.splice(+button.dataset.index,1);if(!state.campaignDraftRows.length)state.campaignDraftRows.push(blankCampaignRow());renderCampaignGrid()});
 updateCampaignGridMeta();
}
function handleCampaignGridPaste(event){
 const text=event.clipboardData?.getData('text/plain')||'';
 if(!/[\t\r\n]/.test(text))return;
 event.preventDefault();
 const incoming=text.split(/\r?\n/).filter(line=>line.trim()).map(line=>{const cells=line.split(/\t| {2,}/);return {campaignId:(cells[0]||'').trim(),rawSpend:(cells[1]||'').trim()}}).filter((row,index)=>!(index===0&&/id|айди/i.test(row.campaignId)&&/трат|расход|spend/i.test(row.rawSpend)));
 const start=+event.target.dataset.index;
 incoming.forEach((row,offset)=>{state.campaignDraftRows[start+offset]=row});
 while(state.campaignDraftRows.length<start+incoming.length+2)state.campaignDraftRows.push(blankCampaignRow());
 renderCampaignGrid();
 $(`.campaign-cell[data-index="${start}"][data-rk-field="campaignId"]`)?.focus();
}
async function loadCampaigns(render=true){
 state.campaigns=await api('/api/campaigns');syncCampaignPasteControls();renderCampaignGrid();if(!render)return;
 const names=Object.fromEntries(state.boot.funnels.map(f=>[f.id,f.name]));
 $('#rk-rows').innerHTML=state.campaigns.campaigns.length?state.campaigns.campaigns.map(c=>`<tr><td>${month(c.month)}</td><td>${esc(names[c.funnel_id]||c.funnel_id)}</td><td><b>${esc(c.campaign_id)}</b></td><td>${esc(c.campaign_name||'—')}</td><td class="num">${money(+c.spend_final)}</td><td><button class="btn ghost delete-rk" data-id="${c.id}">Удалить</button></td></tr>`).join(''):'<tr><td colspan="6" class="empty">Кампании ещё не добавлены</td></tr>';
 $$('.delete-rk').forEach(x=>x.onclick=async()=>{actionStart('Удаление кампании','Удаляем строку и пересчитываем показатели.');x.disabled=true;try{await api('/api/campaigns/'+x.dataset.id,{method:'DELETE'});await loadCampaigns();actionSuccess('Кампания удалена','Строка удалена, показатели пересчитаны.')}catch(e){x.disabled=false;actionError('Не удалось удалить кампанию',e)}});
}
$('#rk-batch-month').onchange=updateCampaignGridMeta;$('#rk-batch-funnel').onchange=updateCampaignGridMeta;
$('#add-rk-row').onclick=()=>{state.campaignDraftRows.push(blankCampaignRow());renderCampaignGrid();$(`.campaign-cell[data-index="${state.campaignDraftRows.length-1}"][data-rk-field="campaignId"]`)?.focus()};
$('#clear-rks').onclick=()=>{state.campaignDraftRows=Array.from({length:4},blankCampaignRow);renderCampaignGrid();$('.campaign-cell')?.focus();actionInfo('Таблица очищена','Можно вставить новый список из Google Sheets.')};
$('#save-rks').onclick=async()=>{
 const parsed=campaignGridData().filter(row=>!row.empty),invalid=parsed.find(row=>row.error),monthValue=$('#rk-batch-month').value,funnelId=$('#rk-batch-funnel').value;
 if(invalid){actionError('Исправьте вставленные данные',`Строка ${invalid.line}: ${invalid.error}.`);return}
 if(!parsed.length){actionError('Нет данных для сохранения','Вставьте два столбца из Google Sheets или заполните строки вручную.');return}
 if(!monthValue||!funnelId){actionError('Не выбраны общие параметры','Укажите месяц и воронку для вставленных рекламных кампаний.');return}
 const campaigns=parsed.map(row=>({month:monthValue,funnel_id:funnelId,campaign_id:row.campaignId,campaign_name:'',spend:row.spend}));
 actionStart('Сохранение рекламных кампаний',`Сохраняем строк: ${campaigns.length}. После загрузки показатели будут пересчитаны один раз.`);$('#save-rks').disabled=true;
 try{const result=await api('/api/campaigns/batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({campaigns})});state.campaignDraftRows=Array.from({length:4},blankCampaignRow);await loadCampaigns();actionSuccess('Рекламные кампании добавлены',`Сохранено строк: ${result.created}. Все данные включены в расчёты.`)}catch(e){actionError('Не удалось сохранить кампании',e)}finally{updateCampaignGridMeta()}
};

function setOpts(el,items,label,mapper=x=>({v:x,l:x})){const old=el.value;el.innerHTML=`<option value="">${label}</option>`+items.map((x,index)=>{const m=mapper(x,index);return `<option value="${esc(m.v)}">${esc(m.l)}</option>`}).join('');el.value=old}
function empty(cols,text='Нет данных по выбранным фильтрам'){return `<tr><td colspan="${cols}" class="empty">${text}</td></tr>`}
const summaryBaseFields=['spend','subscribers','channel_subscribers','funnel_leads','funnel_paid','funnel_ltv','channel_leads','channel_paid','channel_ltv','total_leads','total_paid','total_ltv'];
function aggregateSummary(rows,label={}){
 const result={...label};summaryBaseFields.forEach(field=>result[field]=rows.reduce((sum,row)=>sum+(+row[field]||0),0));
 result.cpf=ratio(result.spend,result.subscribers);
 result.channel_subscription_cr=ratio(result.channel_subscribers,result.subscribers,100);
 result.funnel_lead_cr=ratio(result.funnel_leads,result.subscribers,100);
 result.funnel_cpl=ratio(result.spend,result.funnel_leads);
 result.funnel_paid_cr_lead=ratio(result.funnel_paid,result.funnel_leads,100);
 result.funnel_paid_cr_subscriber=ratio(result.funnel_paid,result.subscribers,100);
 result.funnel_average_check=ratio(result.funnel_ltv,result.funnel_paid);
 result.total_lead_cr=ratio(result.total_leads,result.subscribers,100);
 result.cpl=ratio(result.spend,result.total_leads);
 result.cmc=ratio(result.spend,result.total_paid);
 result.total_drr=ratio(result.spend,result.total_ltv,100);
 return result;
}
function summaryRow(r,total=false){return `<tr${total?' class="total-row"':''}><td>${total?'TOTAL':month(r.month)}</td><td><b>${esc(r.funnel||'Все воронки')}</b></td><td><b>${total?'TOTAL':esc(r.campaign_id)}</b>${!total&&r.campaign_name?`<br><small>${esc(r.campaign_name)}</small>`:''}</td><td class="num">${money(r.spend)}</td><td class="num">${num(r.subscribers)}</td><td class="num">${money(r.cpf)}</td><td class="num">${pct(r.channel_subscription_cr)}</td><td class="num">${num(r.channel_subscribers)}</td><td class="num">${pct(r.funnel_lead_cr)}</td><td class="num">${num(r.funnel_leads)}</td><td class="num">${money(r.funnel_cpl)}</td><td class="num">${pct(r.funnel_paid_cr_lead)}</td><td class="num">${pct(r.funnel_paid_cr_subscriber)}</td><td class="num">${num(r.funnel_paid)}</td><td class="num">${money(r.funnel_ltv)}</td><td class="num">${money(r.funnel_average_check)}</td><td class="num">${num(r.channel_leads)}</td><td class="num">${num(r.channel_paid)}</td><td class="num">${money(r.channel_ltv)}</td><td class="num">${pct(r.total_lead_cr)}</td><td class="num">${num(r.total_leads)}</td><td class="num">${money(r.cpl)}</td><td class="num">${num(r.total_paid)}</td><td class="num">${money(r.cmc)}</td><td class="num">${money(r.total_ltv)}</td><td class="num">${pct(r.total_drr)}</td></tr>`}
async function loadSummary(){
 const selectedMonth=$('#sum-month').value,selectedFunnel=$('#sum-funnel').value,selectedCampaign=$('#sum-rk').value.trim(),selectedAsOf=$('#sum-as-of').value,p=new URLSearchParams();
 if(selectedMonth)p.set('month',selectedMonth);if(selectedFunnel)p.set('funnel_id',selectedFunnel);if(selectedCampaign)p.set('campaign_id',selectedCampaign);if(selectedAsOf)p.set('as_of',selectedAsOf);
 const d=await api('/api/reports/campaigns?'+p);
 setOpts($('#sum-month'),d.filters.months,'Все месяцы',x=>({v:x,l:month(x)}));setOpts($('#sum-funnel'),d.filters.funnels,'Все воронки',x=>({v:x.id,l:x.name}));
 $('#sum-rk-options').innerHTML=d.filters.campaigns.map(id=>`<option value="${esc(id)}"></option>`).join('');
 setOpts($('#sum-as-of'),d.fact_dates||[],'Актуальные данные',(date,index)=>({v:date,l:calendarDate(date)+(index===0?' · последнее обновление':'')}));
 const rows=[...d.rows].sort((a,b)=>(a.month||'').localeCompare(b.month||'')||(a.funnel||'').localeCompare(b.funnel||'','ru')||String(a.campaign_id).localeCompare(String(b.campaign_id)));
 // Keep the structural columns visible. Hiding rowspan cells with display:none
 // shifts the second header row relative to body cells in wide tables.
 const table=$('#summary-table');table.classList.remove('hide-summary-month','hide-summary-funnel');
 $('#summary-table-title').textContent=selectedCampaign?`Рекламная кампания ${selectedCampaign}`:selectedFunnel?($('#sum-funnel').selectedOptions[0]?.textContent||'Результаты кампаний'):selectedMonth?`Кампании за ${month(selectedMonth)}`:'Результаты кампаний';
 const summaryCaption=selectedMonth&&selectedFunnel?'Кампании выбранной воронки за выбранный месяц':selectedMonth?'Сравнение кампаний всех воронок за выбранный месяц':selectedFunnel?'Кампании выбранной воронки по месяцам':'Все рекламные кампании по месяцам и воронкам';
 $('#summary-view-caption').textContent=summaryCaption+(selectedAsOf?` · факт на ${calendarDate(selectedAsOf)}`:' · актуальный факт');
 $('#summary-rows').innerHTML=rows.length?rows.map(row=>summaryRow(row)).join(''):empty(26);
 const total=rows.length?aggregateSummary(rows,{month:'',funnel:selectedFunnel?($('#sum-funnel').selectedOptions[0]?.textContent||''):'Все воронки',campaign_id:'TOTAL'}):null;
 $('#summary-total').innerHTML=total?summaryRow(total,true):'';
}

const topBaseFields=['budget','subscribers','month_leads','month_paid','month_revenue','month_ltv','year_leads','year_clients','year_revenue','year_ltv'];
const ratio=(a,b,m=1)=>b?a/b*m:null;
function aggregateTopscore(rows,label={}){
 const result={...label};topBaseFields.forEach(field=>result[field]=rows.reduce((sum,row)=>sum+(+row[field]||0),0));
 result.cpf=ratio(result.budget,result.subscribers);
 result.month_cr_subscriber=ratio(result.month_leads,result.subscribers,100);
 result.month_cpl=ratio(result.budget,result.month_leads);
 result.month_cr_lead=ratio(result.month_paid,result.month_leads,100);
 result.month_cr10=ratio(result.month_paid,result.subscribers,100);
 result.month_average_check=ratio(result.month_revenue,result.month_paid);
 result.month_drr_ltv=ratio(result.budget,result.month_ltv,100);
 result.month_cmc=ratio(result.budget,result.month_paid);
 result.year_cr_subscriber=ratio(result.year_leads,result.subscribers,100);
 result.year_cpl=ratio(result.budget,result.year_leads);
 result.year_cr_lead=ratio(result.year_clients,result.year_leads,100);
 result.year_cr1=ratio(result.year_clients,result.subscribers,100);
 result.year_cr1_cr10=ratio(result.year_cr1,result.month_cr10);
 result.year_average_check=ratio(result.year_revenue,result.year_clients);
 result.year_drr_ltv=ratio(result.budget,result.year_ltv,100);
 result.month_year_drr=ratio(result.month_drr_ltv,result.year_drr_ltv);
 result.year_cmc=ratio(result.budget,result.year_clients);
 return result;
}
function topRow(r,total=false){return `<tr${total?' class="total-row"':''}><td>${total?'TOTAL':month(r.month)}</td><td><b>${esc(r.funnel||'Все воронки')}</b></td><td class="num">${money(r.budget)}</td><td class="num">${money(r.cpf)}</td><td class="num">${num(r.subscribers)}</td><td class="num">${pct(r.month_cr_subscriber)}</td><td class="num">${num(r.month_leads)}</td><td class="num">${money(r.month_cpl)}</td><td class="num">${pct(r.month_cr_lead)}</td><td class="num">${pct(r.month_cr10)}</td><td class="num">${num(r.month_paid)}</td><td class="num">${money(r.month_revenue)}</td><td class="num">${money(r.month_average_check)}</td><td class="num">${money(r.month_ltv)}</td><td class="num">${pct(r.month_drr_ltv)}</td><td class="num">${money(r.month_cmc)}</td><td class="num">${pct(r.year_cr_subscriber)}</td><td class="num">${num(r.year_leads)}</td><td class="num">${money(r.year_cpl)}</td><td class="num">${pct(r.year_cr_lead)}</td><td class="num">${pct(r.year_cr1)}</td><td class="num">${r.year_cr1_cr10==null?'—':num(r.year_cr1_cr10)+'×'}</td><td class="num">${num(r.year_clients)}</td><td class="num">${money(r.year_revenue)}</td><td class="num">${money(r.year_average_check)}</td><td class="num">${money(r.year_ltv)}</td><td class="num">${pct(r.year_drr_ltv)}</td><td class="num">${r.month_year_drr==null?'—':num(r.month_year_drr)+'×'}</td><td class="num">${money(r.year_cmc)}</td></tr>`}
async function loadTopscore(){
 const p=new URLSearchParams();
 const selectedMonth=$('#top-month').value,selectedFunnel=$('#top-funnel').value,selectedAsOf=$('#top-as-of').value;
 if(selectedMonth)p.set('month',selectedMonth);
 if(selectedFunnel)p.set('funnel_id',selectedFunnel);
 if(selectedAsOf)p.set('as_of',selectedAsOf);
 const d=await api('/api/reports/topscore?'+p);
 setOpts($('#top-month'),d.months,'Все месяцы',x=>({v:x,l:month(x)}));
 setOpts($('#top-funnel'),d.funnels,'Все воронки',x=>({v:x.id,l:x.name}));
 setOpts($('#top-as-of'),d.fact_dates||[],'Актуальные данные',(date,index)=>({v:date,l:calendarDate(date)+(index===0?' · последнее обновление':'')}));
 let rows=[...d.rows];
 if(!selectedMonth){
  const grouped=Object.groupBy?Object.groupBy(rows,row=>row.month):rows.reduce((acc,row)=>((acc[row.month]??=[]).push(row),acc),{});
  rows=Object.entries(grouped).map(([cohort,items])=>aggregateTopscore(items,{month:cohort,funnel:selectedFunnel?(items[0]?.funnel||''):'Все воронки'}));
 }
 rows.sort((a,b)=>selectedMonth?(a.funnel||'').localeCompare(b.funnel||'','ru'):(a.month||'').localeCompare(b.month||''));
 // Rowspan headers must keep all structural columns visible or the metric
 // labels shift horizontally relative to their values.
 $('#top-table').classList.remove('hide-funnel-column');
 $('#top-table-title').textContent=!selectedMonth?(selectedFunnel?($('#top-funnel').selectedOptions[0]?.textContent||'Воронка'):'Итоги по месяцам'):'Эффективность воронок';
 const topCaption=!selectedMonth?(selectedFunnel?'Динамика выбранной воронки по месяцам':'Общая статистика всех воронок по месяцам'):(selectedFunnel?'Показатели выбранной когорты':'Сравнение всех воронок за выбранный месяц');
 $('#top-view-caption').textContent=topCaption+(selectedAsOf?` · факт на ${calendarDate(selectedAsOf)}`:' · актуальный факт');
 $('#top-rows').innerHTML=rows.length?rows.map(row=>topRow(row)).join(''):empty(29,'Загрузите данные запусков, чтобы построить TopScore');
 const total=rows.length?aggregateTopscore(rows,{month:'',funnel:selectedFunnel?($('#top-funnel').selectedOptions[0]?.textContent||''):'Все воронки'}):null;
 $('#top-total').innerHTML=total?topRow(total,true):'';
}
async function refreshReport(loader,title){actionStart('Обновление отчёта',`Применяем ${title} и пересчитываем таблицу.`);try{await loader();actionSuccess('Отчёт обновлён','Новые параметры применены.')}catch(e){actionError('Не удалось обновить отчёт',e)}}
['#sum-month','#sum-funnel','#sum-as-of'].forEach(s=>$(s).onchange=()=>refreshReport(loadSummary,'выбранные фильтры'));['#top-month','#top-funnel','#top-as-of'].forEach(s=>$(s).onchange=()=>refreshReport(loadTopscore,'выбранные фильтры'));
let summaryCampaignFilterTimer;
const applySummaryCampaignFilter=()=>loadSummary().catch(e=>actionError('Не удалось применить ID рекламной кампании',e));
$('#sum-rk').oninput=()=>{clearTimeout(summaryCampaignFilterTimer);summaryCampaignFilterTimer=setTimeout(applySummaryCampaignFilter,350)};
$('#sum-rk').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();clearTimeout(summaryCampaignFilterTimer);applySummaryCampaignFilter()}};
$('#sum-reset').onclick=async()=>{$$('#page-summary select, #page-summary input').forEach(x=>x.value='');await refreshReport(loadSummary,'сброс фильтров')};
$('#top-reset').onclick=async()=>{$$('#page-topscore select, #page-topscore input').forEach(x=>x.value='');await refreshReport(loadTopscore,'сброс фильтров')};
$$('[data-export]').forEach(button=>button.onclick=()=>{
 actionStart('Подготовка CSV','Формируем файл из таблицы на экране.');
 try{
  const table=button.closest('.table-card').querySelector('table'),headerRows=$$('thead tr',table),lastHeader=headerRows.at(-1);
  const visible=cell=>getComputedStyle(cell).display!=='none';
  const carried=headerRows.length>1?$$('th[rowspan]',headerRows[0]).filter(visible).map(cell=>cell.innerText):[];
  const header=[...carried,...$$('th',lastHeader).filter(visible).map(cell=>cell.innerText)];
  const rows=[header,...$$('tbody tr, tfoot tr',table).map(row=>$$('td',row).filter(cell=>getComputedStyle(cell).display!=='none').map(cell=>cell.innerText))];
  const lines=rows.map(row=>row.map(value=>'"'+value.replaceAll('"','""')+'"').join(';'));
  const blob=new Blob(['\ufeff'+lines.join('\n')],{type:'text/csv;charset=utf-8'}),a=document.createElement('a');
  a.href=URL.createObjectURL(blob);a.download=button.dataset.export+'.csv';a.click();URL.revokeObjectURL(a.href);
  actionSuccess('CSV подготовлен',`Файл «${button.dataset.export}.csv» передан браузеру для скачивания.`)
 }catch(e){actionError('Не удалось экспортировать CSV',e)}
});
init().catch(e=>actionError('Не удалось запустить платформу',e));
