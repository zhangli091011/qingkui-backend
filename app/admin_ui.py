from fastapi.responses import HTMLResponse


ADMIN_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>青葵管理后台</title>
<style>
:root{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#17202a;background:#f5f7fa}body{margin:0}header{background:#155eef;color:#fff;padding:18px 24px;display:flex;justify-content:space-between;align-items:center}main{max-width:1180px;margin:20px auto;padding:0 16px;display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}.panel{background:#fff;border:1px solid #dfe3eb;border-radius:8px;padding:16px;box-shadow:0 1px 2px #0000000b}h2{font-size:18px;margin:0 0 12px}label{display:block;font-size:13px;color:#59636e;margin:8px 0 4px}input,select,textarea{width:100%;box-sizing:border-box;border:1px solid #cbd2dc;border-radius:5px;padding:8px;font:inherit}textarea{min-height:72px;resize:vertical}button{border:0;border-radius:5px;background:#155eef;color:#fff;padding:8px 12px;cursor:pointer;margin-top:10px}button.secondary{background:#687386}button.danger{background:#d92d20}.row{display:flex;gap:8px}.row>*{flex:1}pre{background:#101828;color:#d0d5dd;padding:10px;border-radius:5px;max-height:250px;overflow:auto;font-size:12px;white-space:pre-wrap}.wide{grid-column:1/-1}.muted{color:#667085;font-size:13px}
</style></head><body><header><strong>青葵计划 · 管理后台</strong><span id="status">未连接</span></header>
<main>
<section class="panel"><h2>管理员令牌</h2><label>Access Token</label><input id="token" type="password" placeholder="粘贴登录后获得的 JWT"><div class="row"><button onclick="saveToken()">保存令牌</button><button class="secondary" onclick="loadLogs()">审计日志</button></div><p class="muted">令牌仅保存在当前浏览器会话。</p></section>
<section class="panel"><h2>知识节点状态</h2><label>节点 ID</label><input id="nodeId" placeholder="例如 quadratic_function"><div class="row"><button onclick="nodeAction('publish')">发布</button><button class="danger" onclick="nodeAction('withdraw')">下线</button></div><label>恢复版本号</label><div class="row"><input id="version" type="number" min="1" value="1"><button class="secondary" onclick="restoreNode()">恢复为草稿</button></div><button class="secondary" onclick="loadVersions()">查看版本历史</button><div class="row"><button class="secondary" onclick="loadNodes()">节点列表</button><button class="secondary" onclick="loadChapters()">章节列表</button></div></section>
<section class="panel"><h2>知识关系</h2><label>源节点 / 目标节点</label><div class="row"><input id="source" placeholder="源节点"><input id="target" placeholder="目标节点"></div><label>关系类型</label><select id="edgeType"><option value="prerequisite">前置</option><option value="confused_with">易混</option><option value="question_type">题型</option><option value="related">相关</option><option value="extension">拓展</option></select><label>说明</label><input id="edgeExplanation" value="管理员维护的知识关系"><button onclick="createEdge()">创建关系</button><button class="secondary" onclick="loadEdges()">刷新关系列表</button></section>
<section class="panel"><h2>反馈审核</h2><label>反馈 ID</label><input id="feedbackId"><label>审核状态</label><select id="feedbackStatus"><option>accepted</option><option>resolved</option><option>rejected</option><option>pending</option></select><label>审核备注</label><input id="feedbackNote"><button onclick="reviewFeedback()">提交审核</button><button class="secondary" onclick="loadFeedback()">待审核列表</button></section>
<section class="panel"><h2>用户额度</h2><label>用户 ID</label><input id="userId"><label>调整数量（可为负）</label><input id="amount" type="number" value="100"><label>原因</label><input id="reason" value="后台调整"><button onclick="adjustCredits()">调整额度</button></section>
<section class="panel"><h2>模型成本</h2><button onclick="loadCosts()">查询模型成本</button></section>
<section class="panel wide"><h2>结果</h2><pre id="output">准备就绪</pre></section>
</main><script>
const $=id=>document.getElementById(id);let token=sessionStorage.getItem('adminToken')||''; $('token').value=token;
function saveToken(){token=$('token').value.trim();sessionStorage.setItem('adminToken',token);$('status').textContent=token?'已设置令牌':'未连接';}
async function api(path,options={}){if(!token)saveToken();const headers=Object.assign({'Content-Type':'application/json','Authorization':'Bearer '+token},options.headers||{});const r=await fetch('/api'+path,{...options,headers});const text=await r.text();let data;try{data=JSON.parse(text)}catch{data=text}if(!r.ok)throw new Error((data&&data.detail)||r.status+' '+r.statusText);return data}
function show(data){$('output').textContent=JSON.stringify(data,null,2)}
async function nodeAction(action){try{show(await api('/admin/knowledge/nodes/'+encodeURIComponent($('nodeId').value)+'/'+action,{method:'POST',body:JSON.stringify({change_note:'管理后台操作'})}))}catch(e){show({error:e.message})}}
async function restoreNode(){try{show(await api('/admin/knowledge/nodes/'+encodeURIComponent($('nodeId').value)+'/restore',{method:'POST',body:JSON.stringify({version:Number($('version').value),change_note:'管理后台恢复'})}))}catch(e){show({error:e.message})}}
async function loadVersions(){try{show(await api('/admin/knowledge/nodes/'+encodeURIComponent($('nodeId').value)+'/versions'))}catch(e){show({error:e.message})}}
async function loadNodes(){try{show(await api('/admin/knowledge/nodes'))}catch(e){show({error:e.message})}}
async function loadChapters(){try{show(await api('/admin/knowledge/chapters'))}catch(e){show({error:e.message})}}
async function createEdge(){try{show(await api('/admin/knowledge/edges',{method:'POST',body:JSON.stringify({source_node_id:$('source').value,target_node_id:$('target').value,edge_type:$('edgeType').value,explanation:$('edgeExplanation').value})}))}catch(e){show({error:e.message})}}
async function loadEdges(){try{show(await api('/admin/knowledge/edges'))}catch(e){show({error:e.message})}}
async function reviewFeedback(){try{show(await api('/admin/feedback/'+encodeURIComponent($('feedbackId').value),{method:'PATCH',body:JSON.stringify({status:$('feedbackStatus').value,review_note:$('feedbackNote').value})}))}catch(e){show({error:e.message})}}
async function loadFeedback(){try{show(await api('/admin/feedback?status=pending'))}catch(e){show({error:e.message})}}
async function adjustCredits(){try{show(await api('/admin/credits/'+encodeURIComponent($('userId').value)+'/adjust',{method:'POST',body:JSON.stringify({amount:Number($('amount').value),reason:$('reason').value})}))}catch(e){show({error:e.message})}}
async function loadCosts(){try{show(await api('/admin/model-costs'))}catch(e){show({error:e.message})}}
if(token)$('status').textContent='已设置令牌';
</script></body></html>'''


def admin_page() -> HTMLResponse:
    return HTMLResponse(ADMIN_HTML)
