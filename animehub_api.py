import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from data_manager import DataManager
from anime_slayer_provider import AnimeSlayerProvider
from job_manager import JobManager
from library_organizer import build_library_plan
from upload18_provider import Upload18Provider
from bunny_stream_provider import BunnyStreamProvider

SERVICE_NAME = "AnimeHub.Bot"
ADMIN_PASSWORD = os.getenv("ANIMEHUB_ADMIN_PASSWORD", "").strip()
ADMIN_SESSION_SECRET = os.getenv("ANIMEHUB_ADMIN_SESSION_SECRET", "").strip()
DATA = DataManager()
SLAYER = AnimeSlayerProvider()
JOBS = JobManager()
UPLOAD18 = Upload18Provider()
BUNNY = BunnyStreamProvider()
STORAGE_JOBS = {}
# Searching/resolution can fan out, while the expensive media transfer stage is
# bounded separately. Remote Bunny fetch normally avoids Render disk usage.
# Every submitted work gets its own async task. The Bunny submission stage is
# separately bounded, so a long list starts immediately without batch pauses.
MAX_WORK_CONCURRENCY = max(2, int(os.getenv("ANIMEHUB_MAX_WORK_CONCURRENCY", "1000000")))
MEDIA_TRANSFER_CONCURRENCY = max(1, min(int(os.getenv("ANIMEHUB_MEDIA_TRANSFER_CONCURRENCY", "8")), 8))
MEDIA_TRANSFER_SEMAPHORE = asyncio.Semaphore(MEDIA_TRANSFER_CONCURRENCY)


def _admin_token():
    if not ADMIN_PASSWORD or not ADMIN_SESSION_SECRET:
        return None
    issued = str(int(time.time()))
    payload = base64.urlsafe_b64encode(issued.encode()).decode().rstrip("=")
    signature = hmac.new(ADMIN_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _admin_authenticated(request):
    token = request.cookies.get("animehub_admin")
    if not token or "." not in token or not ADMIN_SESSION_SECRET:
        return False
    payload, signature = token.rsplit(".", 1)
    expected = hmac.new(ADMIN_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        issued = int(base64.urlsafe_b64decode(payload + "==").decode())
        return time.time() - issued < 86400
    except Exception:
        return False


ADMIN_PAGE = r'''<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AnimeHub.Bot · مدير التنزيل</title>
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#e8eef7;background:#0b1220}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 90% 0,#173b52 0,#0b1220 42%);min-height:100vh}.shell{max-width:1180px;margin:auto;padding:28px 18px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:22px}.brand{font-size:25px;font-weight:800}.muted{color:#9aadc2}.grid{display:grid;grid-template-columns:1.05fr .95fr;gap:18px}.card{background:rgba(19,31,49,.9);border:1px solid #263c55;border-radius:18px;padding:20px;box-shadow:0 16px 40px #0003}.card h2{margin:0 0 14px;font-size:18px}label{display:block;color:#b8c8d9;font-size:13px;margin:12px 0 6px}input,select,textarea{width:100%;border:1px solid #304963;background:#0d1a2b;color:#eef6ff;border-radius:10px;padding:11px;font:inherit}textarea{min-height:280px;resize:vertical;line-height:1.6}button{border:0;border-radius:10px;padding:11px 15px;background:#29b6a8;color:#04131b;font-weight:800;cursor:pointer}button.secondary{background:#20364e;color:#d7e7f6}button:disabled{opacity:.5;cursor:wait}.row{display:flex;gap:10px;align-items:end}.row>*{flex:1}.actions{display:flex;gap:10px;align-items:center;margin-top:14px}.hint{font-size:12px;color:#8ea4ba;line-height:1.6}.status{margin-top:14px;padding:12px;border-radius:10px;background:#102238;color:#bcd0e3}.job{padding:14px 0;border-bottom:1px solid #294057}.job:last-child{border-bottom:0}.jobtitle{font-weight:800}.pill{display:inline-block;border-radius:999px;padding:3px 9px;font-size:12px;margin-inline-start:6px;background:#24435b}.ok{background:#145244;color:#9cf0cf}.bad{background:#5a2630;color:#ffc2c8}.warn{background:#5a4721;color:#ffdc8a}.meter{height:7px;border-radius:99px;background:#20364b;overflow:hidden;margin:9px 0}.meter i{display:block;height:100%;background:#29b6a8}.item{font-size:13px;padding:7px 0;border-top:1px solid #23394e}.error{color:#ffb6bd;font-size:12px;margin-top:3px}.hide{display:none}@media(max-width:800px){.grid{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}}
</style></head><body><main class="shell"><header class="top"><div><div class="brand">AnimeHub<span style="color:#29b6a8">.Bot</span></div><div class="muted">مدير التنزيل المنظم · التنفيذ بالترتيب</div></div><button class="secondary" onclick="logout()">تسجيل الخروج</button></header>
<section class="grid"><div class="card"><h2>إضافة قائمة جديدة</h2><label>القائمة</label><textarea id="list" placeholder="سطر لكل عمل، مثال:\nYour Name | فيلم\nNaruto | مسلسل | 720p | 1-12\nOne Piece | مسلسل | 720p | كل الحلقات"></textarea><div class="hint">الصيغة: الاسم | فيلم/مسلسل | الجودة | كل الحلقات أو كل 12 أو 12 أو 1-12. يمكن خلط الأفلام والمسلسلات ولصق JSON من نوع items.</div><div class="row"><div><label>الجودة الافتراضية</label><select id="quality"><option>720p</option><option>1080p</option><option>480p</option><option>source</option></select></div><div><label>مكتبة Bunny</label><select id="library"><option value="">اختر المكتبة لهذه القائمة</option></select></div></div><div class="status">وضع التنزيل مفعّل دائماً. تبدأ كل عناصر القائمة كمهام متوازية فوراً، وترسل إلى المكتبة التي تختارها. يضع كل عمل داخل Collection باسمه.</div><div class="actions"><button id="submit" onclick="submitList()">بدء التنزيل</button><span id="formStatus" class="hint"></span></div></div>
<div class="card"><h2>قواعد التنفيذ</h2><div class="hint" style="font-size:14px;line-height:2">✓ يبدأ من أول عمل إلى آخر عمل<br>✓ يمنع تكرار نفس العمل داخل القائمة<br>✓ لا ينتقل للعمل التالي قبل إنهاء الحالي<br>✓ كل حلقة تسجل مكتملة أو فاشلة مع السبب<br>✓ التنزيل الخادمي يفحص الفيديو قبل رفعه<br>✓ ينظم كل عمل داخل Collection مستقلة في Bunny</div><div id="selected" class="status">لم تبدأ قائمة بعد.</div></div></section>
<section class="card" style="margin-top:18px"><div class="top" style="margin-bottom:8px"><h2 style="margin:0">المهام والتقارير</h2><button class="secondary" onclick="refreshJobs()">تحديث</button></div><div id="jobs"><div class="muted">لا توجد مهام ظاهرة بعد.</div></div></section></main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
function parseList(text,quality,library){let t=text.trim();if(!t)return[];if(t.startsWith('[')){let x=JSON.parse(t);return x.map(i=>({...i,quality:i.quality||quality,mode:'download',library_id:i.library_id||library}));}return t.split(/\r?\n/).map(x=>x.trim()).filter(Boolean).map(line=>{let p=line.split('|').map(x=>x.trim()),title=p[0],mal=(title.match(/^\[?MAL\s*:\s*(\d+)\]?\s*/i)||[])[1];title=title.replace(/^\[?MAL\s*:\s*\d+\]?\s*/i,'').trim();let type=/(film|movie|فيلم|فلم)/i.test(p[1]||'')?'film':'series',second=p[2]||'',q=/^\d{3,4}p?$/i.test(second)?second:quality,range=/^\d{3,4}p?$/i.test(second)?(p[3]||''):second,o={title,type,quality:q,mode:'download',library_id:library};if(mal)o.mal_id=mal;if(type==='series'&&range){let cleaned=range.replace(/^(كل|all|episodes?|الحلقات?|حلقة)\s*/i,'').trim();if(/^(كل|all)(\s+الحلقات?)?$/i.test(range)||/^(كل|all)\s+الحلقات?$/i.test(range))o.all_episodes=true;else if(/^\d+\s*-\s*\d+$/.test(cleaned)){let [a,b]=cleaned.split('-').map(Number);o.start_episode=a;o.end_episode=b;}else if(/^\d+$/.test(cleaned)){o.episode_count=Number(cleaned);}}else if(type==='film')o.all_episodes=false;return o;});}
async function submitList(){let b=document.getElementById('submit'),msg=document.getElementById('formStatus');try{let items=parseList(document.getElementById('list').value,document.getElementById('quality').value,document.getElementById('library').value);if(!items.length)throw Error('أدخل قائمة غير فارغة');b.disabled=true;msg.textContent='جاري التحقق من القائمة...';let r=await fetch('/v1/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items,concurrency:items.length,ordered:false,download_only:true})});let d=await r.json();if(!r.ok)throw Error(d.detail||'تعذر إنشاء المهمة');msg.textContent='تم إنشاء المهمة: '+d.job_id+' · المكرر المحذوف: '+(d.deduplicated||0);document.getElementById('selected').textContent='بدأ التنزيل. يعمل النظام تلقائياً بتوازٍ آمن. رقم المهمة: '+d.job_id;document.getElementById('list').value='';refreshJobs();}catch(e){msg.textContent=e.message}finally{b.disabled=false}}
async function refreshJobs(){let r=await fetch('/v1/jobs');if(r.status===401){location.reload();return}let d=await r.json(),root=document.getElementById('jobs');if(!d.jobs?.length){root.innerHTML='<div class="muted">لا توجد مهام ظاهرة بعد.</div>';return}root.innerHTML=d.jobs.slice().reverse().map(j=>{let total=j.total||1,done=j.completed||0, cls=j.status==='completed'?'ok':j.status.includes('error')?'warn':j.status==='failed'?'bad':'pill';let items=(j.items||[]).map(i=>{let r=i.result||{},failed=(r.failed_count||0)+(i.status==='failed'?1:0),episodeErrors=(r.episodes||[]).filter(e=>e.status==='failed').map(e=>'<div class=\"error\">الحلقة '+esc(e.requested_episode||e.episode||'?')+': '+esc((e.error||{}).message||'فشل غير محدد')+'</div>').join('');return '<div class=\"item\"><b>'+esc(i.title)+'</b> <span class=\"pill '+(i.status==='completed'?'ok':failed?'bad':'')+'\">'+esc(i.status)+'</span>'+ (r.completed_count!=null?'<span class=\"muted\"> · '+r.completed_count+' مكتملة · '+(r.failed_count||0)+' فاشلة</span>':'')+(i.error?'<div class=\"error\">'+esc(i.error.code)+': '+esc(i.error.message)+'</div>':'')+episodeErrors+'</div>';}).join('');return '<div class="job"><div class="jobtitle">'+esc(j.id)+' <span class="pill '+cls+'">'+esc(j.status)+'</span> <button class="secondary" onclick="togglePause(\''+esc(j.id)+'\')">'+(j.status==='paused'?'استئناف':'إيقاف مؤقت')+'</button> <button class="secondary" onclick="cancelJob(\''+esc(j.id)+'\')">إلغاء كامل</button></div><div class="meter"><i style="width:'+Math.min(100,done/total*100)+'%"></i></div><div class="muted">الأعمال: '+done+'/'+total+' · فشل: '+(j.failed||0)+'</div>'+items+'</div>'}).join('')}
async function togglePause(id){let j=await (await fetch('/v1/jobs/'+encodeURIComponent(id))).json();let action=j.status==='paused'?'resume':'pause';await fetch('/v1/jobs/'+encodeURIComponent(id)+'/'+action,{method:'POST'});refreshJobs()}
async function cancelJob(id){if(!confirm('إلغاء المهمة بالكامل؟'))return;await fetch('/v1/jobs/'+encodeURIComponent(id)+'/cancel',{method:'POST'});refreshJobs()}
async function loadLibraries(){let r=await fetch('/v1/storage/libraries');if(!r.ok)return;let d=await r.json(),s=document.getElementById('library');s.innerHTML='<option value=\"\">اختر المكتبة لهذه القائمة</option>'+d.libraries.map(x=>'<option value=\"'+esc(x.id)+'\">اختيار يدوي: '+esc(x.name)+' ('+esc(x.id)+')</option>').join('')}
async function logout(){await fetch('/admin/logout',{method:'POST'});location.reload()}loadLibraries();refreshJobs();setInterval(refreshJobs,2500);
</script></body></html>'''


LOGIN_PAGE = r'''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AnimeHub.Bot · دخول</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b1220;color:#edf5ff;font-family:system-ui}.box{width:min(420px,calc(100% - 32px));background:#132138;border:1px solid #2a4561;border-radius:18px;padding:28px;box-shadow:0 20px 60px #0005}h1{margin-top:0}input,button{width:100%;padding:13px;border-radius:10px;border:1px solid #35516d;background:#0d1a2b;color:#fff;font:inherit;box-sizing:border-box}button{margin-top:12px;background:#29b6a8;color:#03151a;font-weight:800;border:0;cursor:pointer}.err{color:#ffabb4;margin-top:12px}</style><div class="box"><h1>AnimeHub<span style="color:#29b6a8">.Bot</span></h1><p>لوحة إدارة التنزيلات والقوائم</p><form method="post" action="/admin/login"><input name="password" type="password" autocomplete="current-password" placeholder="كلمة المرور" autofocus><button>دخول آمن</button></form>{ERROR}</div></html>'''


def direct_downloads(records):
    return [r for r in records if r.get("link_type") == "direct_download" and r.get("download_url")]


def select_links(records, quality=None, language=None):
    links = direct_downloads(records)
    if language:
        needle = language.lower()
        links = [r for r in links if needle in str(r.get("language", "")).lower()]
    if quality and links:
        requested = str(quality).lower().replace(" ", "")
        # Keep non-numeric/source filters exact. Numeric qualities use the
        # requested resolution first, then the nearest higher resolution, and
        # finally the nearest lower resolution when no higher one exists.
        if requested not in {"source", "auto", "best"}:
            def resolution(value):
                match = re.search(r"(\d{3,4})", str(value or ""))
                return int(match.group(1)) if match else None
            wanted = resolution(requested)
            exact = [r for r in links if str(r.get("quality", "")).lower().replace(" ", "") == requested]
            if exact:
                links = exact
            elif wanted is not None:
                available = sorted({resolution(r.get("quality")) for r in links if resolution(r.get("quality"))})
                higher = [value for value in available if value >= wanted]
                chosen = min(higher) if higher else (max(available) if available else None)
                if chosen is not None:
                    links = [r for r in links if resolution(r.get("quality")) == chosen]
                else:
                    links = []
            else:
                links = [r for r in links if str(r.get("quality", "")).lower().replace(" ", "") == requested]
    return links


def public_link(record):
    return {
        "provider": record.get("name"),
        "source": record.get("source"),
        "quality": record.get("quality"),
        "language": record.get("language"),
        "url": record.get("download_url"),
        "watch_url": record.get("view_url"),
        "download_url": record.get("download_url"),
        "type": "direct_download",
        "verified": bool(record.get("verified")),
        "is_pixeldrain": bool(record.get("is_pd")),
    }


def resolve_work_episode(title, episode_number, mal_id=None):
    results = DATA.search_anime(title)
    if not results:
        raise LookupError("Work not found")
    if mal_id is not None and str(mal_id).strip():
        wanted_mal = str(mal_id).strip()
        matches = [r for r in results if str(r.get("mal_id", "")).strip() == wanted_mal]
        if not matches:
            raise LookupError(f"No search result matched MyAnimeList ID {wanted_mal}")
        target = matches[0]
    else:
        target = results[0]
    episodes = DATA.get_episodes(target["doc_ref"])
    selected = next((e for e in episodes if e.get("order") == episode_number), None)
    # Firestore marks a standalone movie as order 999; expose it to clients as
    # the single requested item (episode 1) without changing the source data.
    if selected is None and episode_number == 1 and len(episodes) == 1:
        selected = dict(episodes[0])
        selected["order"] = 1
    if not selected:
        raise LookupError("Episode not found")
    # Both providers are attempted in the same request. The second provider
    # returns [] until an authorized JSON endpoint is configured.
    with ThreadPoolExecutor(max_workers=2) as executor:
        witcher_future = executor.submit(DATA.get_servers, target["doc_ref"], selected["id"])
        slayer_future = executor.submit(SLAYER.fetch, target.get("name"), selected.get("order", episode_number))
        witcher = witcher_future.result()
        slayer = slayer_future.result()
    records = select_links(witcher + slayer)
    unique = {}
    for record in records:
        unique[(record.get("download_url"), record.get("quality"), record.get("name"))] = record
    return target, selected, list(unique.values()), len(episodes)


def resolve_target_episode(target, selected):
    with ThreadPoolExecutor(max_workers=2) as executor:
        witcher_future = executor.submit(DATA.get_servers, target["doc_ref"], selected["id"])
        slayer_future = executor.submit(SLAYER.fetch, target.get("name"), selected.get("order", 1))
        records = select_links(witcher_future.result() + slayer_future.result())
    unique = {(r.get("download_url"), r.get("quality"), r.get("name")): r for r in records}
    return list(unique.values())


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(lambda: DATA.algolia_app_id)
    yield


app = FastAPI(title=SERVICE_NAME, version="1.1.0", lifespan=lifespan)


@app.get("/")
async def root():
    return {"service": SERVICE_NAME, "status": "online", "api_version": "v1", "direct_only": True,
            "downloadable_only": True, "docs": "/docs",
            "endpoints": ["/v1/search", "/v1/links", "/v1/download", "/v1/batch-links", "/v1/sources"]}


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not ADMIN_PASSWORD or not ADMIN_SESSION_SECRET:
        return HTMLResponse("Admin page is not configured. Set ANIMEHUB_ADMIN_PASSWORD and ANIMEHUB_ADMIN_SESSION_SECRET.", status_code=503)
    if _admin_authenticated(request):
        return HTMLResponse(ADMIN_PAGE)
    return HTMLResponse(LOGIN_PAGE.replace("{ERROR}", ""))


@app.post("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request, password: str = Form(...)):
    if not ADMIN_PASSWORD or not ADMIN_SESSION_SECRET:
        return HTMLResponse("Admin page is not configured.", status_code=503)
    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        return HTMLResponse(LOGIN_PAGE.replace("{ERROR}", "<div class='err'>كلمة المرور غير صحيحة.</div>"), status_code=401)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie("animehub_admin", _admin_token(), max_age=86400, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax")
    return response


@app.post("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie("animehub_admin")
    return response


@app.get("/health")
async def health():
    return {"service": SERVICE_NAME, "status": "healthy", "direct_only": True}


@app.get("/v1/sources")
async def sources():
    return {"service": SERVICE_NAME, "direct_only": True,
            "enabled": {"anime_witcher": True, "anime_slayer": SLAYER.enabled and bool(SLAYER.endpoint)},
            "sources": DATA.get_source_catalog()}


@app.get("/v1/jobs")
async def list_jobs(request: Request):
    if not _admin_authenticated(request):
        raise HTTPException(401, "Admin authentication required")
    return {"jobs": list(JOBS.jobs.values())}


@app.get("/v1/storage/status")
async def storage_status():
    return {"provider": "Bunny Stream", "configured": BUNNY.configured,
            "library_id": BUNNY.library_id, "cdn_configured": bool(BUNNY.cdn_hostname),
            "secret_exposed": False}


@app.get("/v1/storage/libraries")
async def storage_libraries():
    if not BUNNY.configured:
        raise HTTPException(503, "Bunny Stream integration is not configured")
    return {"provider": "Bunny Stream", "libraries": BUNNY.library_catalog}


@app.post("/v1/storage/upload-url")
async def storage_upload_url(payload: dict):
    video_url = str(payload.get("video_url", "")).strip()
    if not video_url.startswith("https://"):
        raise HTTPException(422, "video_url must be an HTTPS direct media URL")
    if not UPLOAD18.configured:
        raise HTTPException(503, "Upload18 integration is not configured")
    try:
        result = await asyncio.to_thread(UPLOAD18.upload_url, video_url, payload.get("title"), payload.get("folder_id"))
        return {"provider": "Upload18", "status": "submitted", "result": result}
    except Exception as exc:
        raise HTTPException(502, f"Upload18 upload failed: {type(exc).__name__}")


@app.post("/v1/storage/download-and-upload")
async def storage_download_and_upload(payload: dict):
    """Download an authorized direct file on the server, validate it, then upload it."""
    direct_url = str(payload.get("direct_url", "")).strip()
    if not direct_url.startswith("https://"):
        raise HTTPException(422, "direct_url must be an HTTPS direct media URL")
    if not BUNNY.configured:
        raise HTTPException(503, "Bunny Stream integration is not configured")
    job_id = uuid.uuid4().hex
    STORAGE_JOBS[job_id] = {"id": job_id, "provider": "Bunny Stream", "status": "queued",
                             "title": payload.get("title"), "created_at": time.time(),
                             "server_temp_deleted": False, "result": None, "error": None}

    async def run_job():
        job = STORAGE_JOBS[job_id]
        job["status"] = "running"
        try:
            result = await asyncio.to_thread(
                BUNNY.server_download_and_upload,
                direct_url,
                payload.get("title") or "Untitled",
                payload.get("quality"),
                payload.get("collection_name") or payload.get("title") or "AnimeHub Library",
            )
            job.update({"status": "completed", "server_temp_deleted": True, "result": result})
        except Exception as exc:
            job.update({"status": "failed", "server_temp_deleted": True,
                        "error": {"code": type(exc).__name__, "message": str(exc)}})
        finally:
            job["finished_at"] = time.time()

    asyncio.create_task(run_job())
    return {"provider": "Bunny Stream", "status": "queued", "job_id": job_id,
            "status_url": f"/v1/storage/jobs/{job_id}"}


@app.get("/v1/storage/jobs/{job_id}")
async def storage_job_status(job_id: str):
    job = STORAGE_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Storage job not found")
    return job


@app.get("/v1/search")
async def search(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(20, ge=1, le=50)):
    results = await asyncio.to_thread(DATA.search_anime, q)
    return {"service": SERVICE_NAME, "query": q, "count": min(len(results), limit), "results": results[:limit]}


@app.get("/v1/work")
async def work(doc_ref: str = Query(..., description="Document reference returned by /v1/search")):
    episodes = await asyncio.to_thread(DATA.get_episodes, doc_ref)
    return {"service": SERVICE_NAME, "doc_ref": doc_ref, "episodes": episodes, "count": len(episodes)}


@app.get("/v1/links")
async def links(q: str = Query(..., min_length=1, max_length=200), episode: Optional[int] = Query(None, ge=1),
                quality: Optional[str] = Query(None), language: Optional[str] = Query(None)):
    name, parsed_episode = DATA.parse_smart_query(q)
    episode_number = episode or parsed_episode or 1
    try:
        target, selected, records, episode_count = await asyncio.to_thread(resolve_work_episode, name, episode_number)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    records = select_links(records, quality, language)
    return {"service": SERVICE_NAME, "query": q,
            "work": {"name": target.get("name"), "doc_ref": target.get("doc_ref"), "sources": target.get("sources", [])},
            "episode": {"number": selected.get("order"), "id": selected.get("id"), "name": selected.get("name")},
            "episode_count": episode_count, "direct_only": True, "downloadable_only": True,
            "filters": {"quality": quality, "selected_quality": records[0].get("quality") if records else None,
                        "language": language}, "count": len(records),
            "links": [public_link(r) for r in records]}


@app.get("/v1/download")
async def download(q: str = Query(..., min_length=1, max_length=200), episode: Optional[int] = Query(None, ge=1),
                   quality: Optional[str] = Query(None), language: Optional[str] = Query(None)):
    name, parsed_episode = DATA.parse_smart_query(q)
    episode_number = episode or parsed_episode or 1
    try:
        _, _, records, _ = await asyncio.to_thread(resolve_work_episode, name, episode_number)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    records = select_links(records, quality, language)
    if not records:
        raise HTTPException(404, "No direct downloadable link matches the requested filters")
    return RedirectResponse(records[0]["download_url"], status_code=307)


@app.get("/v1/batch-links")
async def batch_links(q: str = Query(..., min_length=1, max_length=200), start: int = Query(1, ge=1),
                      limit: int = Query(20, ge=1, le=100), quality: Optional[str] = Query(None),
                      language: Optional[str] = Query(None)):
    results = await asyncio.to_thread(DATA.search_anime, q)
    if not results:
        raise HTTPException(404, "Work not found")
    target = results[0]
    episodes = await asyncio.to_thread(DATA.get_episodes, target["doc_ref"])
    selected = [e for e in episodes if start <= int(e.get("order", 0)) < start + limit]
    if not selected and start == 1 and len(episodes) == 1:
        selected = [dict(episodes[0], order=1)]

    async def one(ep):
        try:
            records = await asyncio.to_thread(resolve_target_episode, target, ep)
            return {"episode": ep, "links": [public_link(r) for r in select_links(records, quality, language)]}
        except LookupError:
            return {"episode": ep, "links": []}

    pages = await asyncio.gather(*(one(ep) for ep in selected))
    return {"service": SERVICE_NAME, "work": target.get("name"), "direct_only": True,
            "downloadable_only": True, "start": start, "limit": limit, "total_episodes": len(episodes),
            "filters": {"quality": quality, "language": language}, "episodes": pages}


def expand_plan_item(item):
    kind = str(item.get("type") or "auto").strip().lower()
    is_film = kind in {"film", "movie", "فيلم", "فلم"} or "فيلم" in kind or "فلم" in kind
    if is_film:
        return [1]
    if isinstance(item.get("episodes"), list) and item["episodes"]:
        return [int(x) for x in item["episodes"]]
    episode_count = item.get("episode_count") or item.get("episodes_count") or item.get("count")
    if episode_count is not None and str(episode_count).strip().isdigit():
        count = int(episode_count)
        if count < 1 or count > 100:
            raise ValueError("episode_count must be between 1 and 100")
        return list(range(1, count + 1))
    raw_start = item.get("start_episode")
    raw_end = item.get("end_episode")
    start = int(raw_start if raw_start is not None and str(raw_start).strip() else 1)
    end = int(raw_end if raw_end is not None and str(raw_end).strip() else start)
    if end < start or end - start > 100:
        raise ValueError("Each series item must contain a range of at most 100 episodes")
    return list(range(start, end + 1))


async def resolve_plan_item(item):
    title = str(item.get("title", "")).strip()
    if not title:
        raise ValueError("title is required")
    quality = item.get("quality")
    language = item.get("language")
    if item.get("all_episodes"):
        found = await asyncio.to_thread(DATA.search_anime, title)
        if not found:
            raise LookupError("Work not found")
        all_episodes = await asyncio.to_thread(DATA.get_episodes, found[0]["doc_ref"])
        episode_numbers = [int(ep.get("order")) for ep in all_episodes if str(ep.get("order", "")).isdigit()]
        if not episode_numbers:
            raise LookupError("No episodes found for this work")
    else:
        episode_numbers = expand_plan_item(item)
    # Resolve episodes concurrently as well as work items. Keep the fan-out
    # bounded so a large series cannot exhaust the source APIs or the server.
    episode_limit = max(1, min(int(item.get("episode_concurrency", 6)), 8))
    semaphore = asyncio.Semaphore(episode_limit)

    async def resolve_one(number):
        try:
            async with semaphore:
                target, selected, records, _ = await asyncio.to_thread(
                    resolve_work_episode, title, number,
                    item.get("mal_id") or item.get("myanimelist_id"),
                )
                records = select_links(records, quality, language)
                if not records:
                    raise LookupError("No direct downloadable link matched the requested filters")
                selected_quality = records[0].get("quality") or quality or "source"
                result = {"status": "completed", "work": target.get("name"), "episode": selected.get("order"),
                          "episode_name": selected.get("name"), "requested_quality": quality or "720p",
                          "selected_quality": selected_quality, "links": [public_link(r) for r in records]}
                if str(item.get("mode", "links")).lower() == "download":
                    if not BUNNY.configured:
                        raise RuntimeError("Bunny Stream integration is not configured")
                    canonical_name = target.get("name") or title
                    # Keep the user's label in storage identity. Two different
                    # cuts/editions may resolve to one canonical database title,
                    # and must remain distinguishable instead of being merged.
                    work_name = title or canonical_name
                    is_film = str(item.get("type") or "").strip().lower() in {"film", "movie", "فيلم", "فلم"}
                    episode_title = (f"{work_name} - {selected_quality}" if is_film else
                                     f"{work_name} - Episode {selected.get('order', number):02d} - {selected_quality}")
                    provider = BUNNY.for_library(item.get("library_id"))
                    attempts = []
                    stored = None
                    # A source can be online in the catalog but unavailable from
                    # Render or return an invalid media response. Try every
                    # direct source matching the requested quality before marking
                    # the episode failed.
                    for record in records:
                        try:
                            async with MEDIA_TRANSFER_SEMAPHORE:
                                stored = await asyncio.to_thread(
                                    provider.server_download_and_upload,
                                    record["download_url"], episode_title, quality, work_name,
                                )
                            result["canonical_work"] = canonical_name
                            result["requested_title"] = title
                            result["selected_source"] = record.get("name")
                            result["storage"] = stored
                            result["accepted_by_bunny"] = bool(stored.get("accepted", True))
                            result["encoding_pending"] = bool(stored.get("encoding_pending", False))
                            result["verified"] = result["accepted_by_bunny"]
                            break
                        except Exception as source_exc:
                            attempts.append({
                                "provider": record.get("name"),
                                "url": record.get("download_url"),
                                "code": type(source_exc).__name__,
                                "message": str(source_exc),
                            })
                    if stored is None:
                        raise RuntimeError(json.dumps({
                            "message": "All matching direct sources failed",
                            "attempts": attempts,
                        }, ensure_ascii=False))
                return result
        except Exception as exc:
            return {"status": "failed", "requested_episode": number,
                    "error": {"code": type(exc).__name__, "message": str(exc)}, "links": []}

    # gather preserves the requested episode order while doing the I/O in parallel.
    results = await asyncio.gather(*(resolve_one(number) for number in episode_numbers))
    failed_count = sum(1 for result in results if result.get("status") == "failed")
    return {"mode": item.get("mode", "links"), "requested_title": title, "episodes": results,
            "completed_count": len(results) - failed_count, "failed_count": failed_count,
            "direct_only": True, "downloadable_only": True}


@app.post("/v1/jobs")
async def create_job(payload: dict):
    plan = payload.get("items") or payload.get("list")
    if not isinstance(plan, list) or not plan:
        raise HTTPException(422, "items must be a non-empty list")
    # Preserve the first occurrence and drop repeated works before scheduling.
    # This keeps the user-facing order deterministic and prevents duplicate uploads.
    unique_plan, seen = [], set()
    for item in plan:
        if not isinstance(item, dict):
            raise HTTPException(422, "each item must be an object")
        # Same-name works can be different MAL entries, years, cuts, or
        # seasons. Only collapse an actually identical request.
        identity_fields = ("mal_id", "myanimelist_id", "year", "season", "version", "edition", "quality", "language")
        key = (str(item.get("title", "")).strip().casefold(),
               str(item.get("type", "auto")).strip().casefold(),
               tuple(str(item.get(field, "")).strip().casefold() for field in identity_fields))
        if key in seen:
            continue
        seen.add(key)
        unique_plan.append(item)
    deduplicated = len(plan) - len(unique_plan)
    plan = unique_plan
    # A submitted list belongs to one selected library. If the caller omits a
    # library, use the first configured library as a safe default; never split
    # one list across libraries automatically.
    movie_libraries = [entry["id"] for entry in BUNNY.library_catalog[:4]]
    if movie_libraries:
        for item in plan:
            if not item.get("library_id"):
                item["library_id"] = movie_libraries[0]
    try:
        for item in plan:
            expand_plan_item(item)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    # The browser workflow is download-only. Keep a conservative fixed fan-out
    # to protect Render CPU/memory while still processing several works together.
    for item in plan:
        item["mode"] = "download"
    ordered = bool(payload.get("ordered", False))
    concurrency = 1 if ordered else max(2, len(plan))
    job = await JOBS.create(plan, resolve_plan_item, concurrency)
    return {"job_id": job["id"], "status": job["status"], "total": job["total"],
            "concurrency": max(1, min(concurrency, MAX_WORK_CONCURRENCY)),
            "ordered": ordered, "deduplicated": deduplicated,
            "mode": "server_download",
            "message": "Use GET /v1/jobs/{job_id} to follow progress"}


@app.get("/v1/jobs/{job_id}")
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.post("/v1/library/plan")
async def library_plan(payload: dict):
    """Preview organized storage paths and detect duplicate/missing episodes."""
    items = payload.get("items") or payload.get("files")
    if not isinstance(items, list) or not items:
        raise HTTPException(422, "items must be a non-empty list")
    return build_library_plan(items)


@app.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    if not JOBS.get(job_id):
        raise HTTPException(404, "Job not found")
    cancelled = await JOBS.cancel(job_id)
    return {"job_id": job_id, "cancelled": cancelled}


@app.post("/v1/jobs/{job_id}/pause")
async def pause_job(job_id: str):
    if not JOBS.get(job_id):
        raise HTTPException(404, "Job not found")
    paused = await JOBS.pause(job_id)
    return {"job_id": job_id, "paused": paused, "status": JOBS.get(job_id)["status"]}


@app.post("/v1/jobs/{job_id}/resume")
async def resume_job(job_id: str):
    if not JOBS.get(job_id):
        raise HTTPException(404, "Job not found")
    resumed = await JOBS.resume(job_id)
    return {"job_id": job_id, "resumed": resumed, "status": JOBS.get(job_id)["status"]}
