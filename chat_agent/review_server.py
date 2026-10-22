"""Loopback-only REVIEW panel for local draft inspection.

This is deliberately a review API, not an automatic gateway: the optional
send endpoint is exposed only when an explicit manual coordinator is injected,
there is no AUTO path, no remote bind option, and the stored context reference
is never exposed to the browser.
"""

from datetime import datetime, timezone
from dataclasses import asdict
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock, Thread
from urllib.parse import unquote, urlsplit

from .drafts import (
    DraftConflictError,
    DraftError,
    DraftExpiredError,
    SensitiveConfirmationRequired,
)
from .contacts import ConsentRequiredError, PairingExpiredError
from .send_coordinator import (
    ContextUnavailableError,
    SendConfirmationRequired,
    SendNotAllowedError,
)


MAX_BODY_BYTES = 64 * 1024


def _contact_parts(contact_key):
    parts = contact_key.split("\x1f") if isinstance(contact_key, str) else ()
    return parts if len(parts) == 3 and all(parts) else ()


def _row_to_dict(row, contacts=None):
    if not row:
        return None
    payload = {
        "draft_id": row["draft_id"],
        "account_scope": row["account_scope"],
        "contact_key": row["contact_key"],
        "batch_id": row["batch_id"],
        "source_message_ids": json.loads(row["source_message_ids"]),
        "latest_message_id": row["latest_message_id"],
        "draft_text": row["draft_text"],
        "final_text": row["final_text"],
        "status": row["status"],
        "needs_user_input": bool(row["needs_user_input"]),
        "risk_flags": json.loads(row["risk_flags"] or "[]"),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "version": row["version"],
        "approved_at": row["approved_at"],
        "sent_at": row["sent_at"],
        "send_state": row["send_state"],
    }
    payload["contact_alias"] = None
    if contacts is not None:
        account_scope, sender_fingerprint, bot_fingerprint = _contact_parts(row["contact_key"])
        if account_scope:
            contact = contacts.get(account_scope, sender_fingerprint, bot_fingerprint)
            if contact:
                payload["contact_alias"] = contact.alias
    return payload


def _candidate_to_dict(candidate):
    return {
        "account_scope": candidate.account_scope,
        "sender_fingerprint": candidate.sender_fingerprint,
        "bot_fingerprint": candidate.bot_fingerprint,
        "message_id": candidate.message_id,
        "preview_text": candidate.preview_text,
        "observed_at": candidate.observed_at,
        "expires_at": candidate.expires_at,
    }


PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chat Agent REVIEW</title>
<style>
body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem}
article{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}
pre{white-space:pre-wrap;background:#f6f6f6;padding:.75rem;border-radius:6px}
textarea{display:block;width:100%;min-height:5rem;box-sizing:border-box;margin:.75rem 0;padding:.6rem;font:inherit}
button{margin-right:.5rem;padding:.45rem .8rem}
.warn{color:#9a3412}.meta{color:#666;font-size:.9rem}
</style>
<main><h1>Chat Agent REVIEW</h1><p class="meta">仅本机审核；本页面不会自动发送消息。</p>
<section id="pairing" hidden><h2>待确认配对</h2><p class="meta">只确认你明确识别的联系人；AUTO 配对被关闭。</p><div id="candidates"></div></section>
<section id="drafts">加载中…</section></main>
<script>
const root=document.querySelector('#drafts');
const sendEnabled=__SEND_ENABLED__;
const pairingEnabled=__PAIRING_ENABLED__;
const pairingRoot=document.querySelector('#pairing');
function text(value){return value==null?'':String(value)}
async function action(id,kind,body={}){
  const r=await fetch('/api/drafts/'+encodeURIComponent(id)+'/'+kind,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok){const e=await r.json().catch(()=>({}));throw new Error(e.error||('HTTP '+r.status));}
  await load();
}
async function load(){
  await loadPairing();
  const r=await fetch('/api/drafts',{cache:'no-store'}); const data=await r.json(); root.textContent='';
  if(!data.drafts.length){root.textContent='暂无待审核草稿。';return;}
  for(const d of data.drafts){
    const card=document.createElement('article');
    const title=document.createElement('h2'); title.textContent=text(d.contact_alias)||'未命名联系人'; card.append(title);
    const meta=document.createElement('p'); meta.className='meta'; meta.textContent='状态：'+text(d.status)+' · 批次：'+text(d.batch_id); card.append(meta);
    if(d.risk_flags.length){const w=document.createElement('p');w.className='warn';w.textContent='需要注意：'+d.risk_flags.join(', ');card.append(w);}
    const pre=document.createElement('pre');pre.textContent=text(d.draft_text)||'模型未生成可发送文本，请补充必要信息。';card.append(pre);
    if(['PENDING','NEEDS_USER_INPUT'].includes(d.status)){
      const editor=document.createElement('textarea');
      editor.placeholder='可在这里修改回复；需要补充信息的草稿必须填写后才能批准。';
      editor.value=text(d.draft_text); card.append(editor);
      const approve=document.createElement('button');approve.textContent=d.status==='PENDING'?'批准并保存':'填写后批准';
      approve.onclick=()=>{
        const highRisk=d.risk_flags.includes('RISK_HIGH');
        if(highRisk && !confirm('这条草稿包含高风险或提示注入标记，确认你已复核并仍要批准？'))return;
        const body={edited_text:editor.value};
        if(highRisk){body.sensitive=true;body.second_confirmation=true;}
        action(d.draft_id,'approve',body).catch(alert);
      };card.append(approve);
    }
    if(d.status==='APPROVED' && sendEnabled){
      const send=document.createElement('button');send.textContent='发送';send.onclick=()=>{if(confirm('确认发送这条已批准的消息？')) action(d.draft_id,'send',{confirm:true}).catch(alert)};card.append(send);
    }
    if(['PENDING','NEEDS_USER_INPUT','APPROVED'].includes(d.status)){
      const reject=document.createElement('button');reject.textContent='驳回';reject.onclick=()=>action(d.draft_id,'reject').catch(alert);card.append(reject);
    }
    if(d.status==='APPROVED' && !sendEnabled){const hint=document.createElement('span');hint.className='meta';hint.textContent='发送通道未显式接线';card.append(hint);}
    root.append(card);
  }
}
async function loadPairing(){
  if(!pairingEnabled)return;
  pairingRoot.hidden=false;
  const box=document.querySelector('#candidates'); box.textContent='';
  const r=await fetch('/api/pairing/candidates',{cache:'no-store'}); const data=await r.json();
  if(!data.candidates.length){box.textContent='暂无待确认联系人。';return;}
  for(const c of data.candidates){
    const card=document.createElement('article');
    const p=document.createElement('p');p.textContent='消息：'+text(c.preview_text)+' · sender='+text(c.sender_fingerprint);card.append(p);
    const button=document.createElement('button');button.textContent='确认 REVIEW 配对';button.onclick=async()=>{
      const alias=prompt('请输入本地联系人别名'); if(!alias)return;
      const body={account_scope:c.account_scope,sender_fingerprint:c.sender_fingerprint,bot_fingerprint:c.bot_fingerprint,alias:alias,mode:'review'};
      const response=await fetch('/api/pairing/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(error.error||('HTTP '+response.status));}
      await load();
    };card.append(button);box.append(card);
  }
}
load().catch(e=>{root.textContent='加载失败：'+e.message});
</script>
"""


def _panel_html(send_enabled, pairing_enabled):
    return PANEL_HTML.replace("__SEND_ENABLED__", "true" if send_enabled else "false").replace(
        "__PAIRING_ENABLED__", "true" if pairing_enabled else "false"
    )


class _ReviewHandler(BaseHTTPRequestHandler):
    server_version = "ChatAgentReview/1.0"

    def log_message(self, *_args):
        # Do not print message text or local identifiers into a shared console.
        return

    @property
    def store(self):
        return self.server.review_store

    @property
    def store_lock(self):
        return self.server.review_store_lock

    @property
    def sender(self):
        return self.server.review_sender

    @property
    def contacts(self):
        return self.server.review_contacts

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid content length")
        if length < 0:
            raise ValueError("invalid content length")
        if length > MAX_BODY_BYTES:
            raise OverflowError("request body is too large")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, _panel_html(bool(self.sender), bool(self.contacts)), "text/html; charset=utf-8")
            return
        if path == "/api/drafts":
            with self.store_lock:
                rows = self.store.list_drafts()
                payload = {"drafts": [_row_to_dict(row, self.contacts) for row in rows]}
            self._send(200, payload)
            return
        if path == "/api/pairing/candidates":
            if self.contacts is None:
                self._send(409, {"error": "PAIRING_NOT_CONFIGURED"})
                return
            with self.store_lock:
                candidates = self.contacts.list_candidates()
            self._send(200, {"candidates": [_candidate_to_dict(item) for item in candidates]})
            return
        self._send(404, {"error": "NOT_FOUND"})

    def do_POST(self):
        if urlsplit(self.path).path == "/api/pairing/confirm":
            if self.contacts is None:
                self._send(409, {"error": "PAIRING_NOT_CONFIGURED"})
                return
            try:
                payload = self._read_json()
            except OverflowError:
                self._send(413, {"error": "REQUEST_TOO_LARGE"})
                return
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"error": "INVALID_JSON"})
                return
            if payload.get("mode", "review") == "auto":
                self._send(409, {"error": "AUTO_DISABLED"})
                return
            required = ("account_scope", "sender_fingerprint", "bot_fingerprint", "alias")
            if not all(isinstance(payload.get(key), str) and payload[key].strip() for key in required):
                self._send(400, {"error": "INVALID_PAIRING"})
                return
            try:
                with self.store_lock:
                    contact = self.contacts.confirm_candidate(
                        payload["account_scope"],
                        payload["sender_fingerprint"],
                        payload["bot_fingerprint"],
                        alias=payload["alias"],
                        mode="review",
                    )
                self._send(200, {"contact": asdict(contact)})
            except PairingExpiredError:
                self._send(409, {"error": "PAIRING_EXPIRED"})
            except ConsentRequiredError:
                self._send(409, {"error": "CONSENT_REQUIRED"})
            except ValueError:
                self._send(400, {"error": "INVALID_PAIRING"})
            return
        parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
        if len(parts) != 4 or parts[0:2] != ["api", "drafts"]:
            self._send(404, {"error": "NOT_FOUND"})
            return
        draft_id, action = parts[2], parts[3]
        try:
            payload = self._read_json()
        except OverflowError:
            self._send(413, {"error": "REQUEST_TOO_LARGE"})
            return
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "INVALID_JSON"})
            return
        try:
            with self.store_lock:
                if action == "approve":
                    edited_text = payload.get("edited_text")
                    if edited_text is not None and not isinstance(edited_text, str):
                        self._send(400, {"error": "INVALID_EDITED_TEXT"})
                        return
                    sensitive = payload.get("sensitive", False)
                    second_confirmation = payload.get("second_confirmation", False)
                    if not isinstance(sensitive, bool) or not isinstance(second_confirmation, bool):
                        self._send(400, {"error": "INVALID_CONFIRMATION"})
                        return
                    row = self.store.approve(
                        draft_id,
                        edited_text=edited_text,
                        sensitive=sensitive,
                        second_confirmation=second_confirmation,
                    )
                elif action == "reject":
                    row = self.store.reject(draft_id)
                elif action == "send":
                    if self.sender is None:
                        self._send(409, {"error": "SEND_NOT_CONFIGURED"})
                        return
                    if payload.get("confirm") is not True:
                        self._send(409, {"error": "SEND_CONFIRMATION_REQUIRED"})
                        return
                    outcome = self.sender.send_approved(draft_id, confirmed=True)
                    row = self.store.get(draft_id)
                    self._send(200, {"draft": _row_to_dict(row, self.contacts), "send": asdict(outcome)})
                    return
                else:
                    self._send(404, {"error": "NOT_FOUND"})
                    return
            self._send(200, {"draft": _row_to_dict(row, self.contacts)})
        except KeyError:
            self._send(404, {"error": "DRAFT_NOT_FOUND"})
        except SensitiveConfirmationRequired:
            self._send(409, {"error": "SECOND_CONFIRMATION_REQUIRED"})
        except DraftExpiredError:
            self._send(409, {"error": "DRAFT_EXPIRED"})
        except DraftConflictError:
            self._send(409, {"error": "DRAFT_CONFLICT"})
        except SendConfirmationRequired:
            self._send(409, {"error": "SEND_CONFIRMATION_REQUIRED"})
        except ContextUnavailableError:
            self._send(409, {"error": "CONTEXT_UNAVAILABLE"})
        except SendNotAllowedError:
            self._send(409, {"error": "SEND_NOT_ALLOWED"})
        except DraftError:
            self._send(409, {"error": "DRAFT_NOT_REVIEWABLE"})


class LocalReviewServer:
    """Serve the review panel on loopback and nowhere else."""

    def __init__(self, draft_store, *, host="127.0.0.1", port=0, send_coordinator=None, contact_store=None):
        if host != "127.0.0.1":
            raise ValueError("review server must bind to 127.0.0.1")
        self.draft_store = draft_store
        self.host = host
        self.port = port
        self.send_coordinator = send_coordinator
        self.contact_store = contact_store
        self._server = None
        self._thread = None
        self._lock = RLock()

    @property
    def address(self):
        if not self._server:
            return None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self):
        if self._server:
            return self
        server = ThreadingHTTPServer((self.host, self.port), _ReviewHandler)
        server.daemon_threads = True
        server.review_store = self.draft_store
        server.review_store_lock = self._lock
        server.review_sender = self.send_coordinator
        server.review_contacts = self.contact_store
        self._server = server
        self._thread = Thread(target=server.serve_forever, name="chat-agent-review", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=2)
