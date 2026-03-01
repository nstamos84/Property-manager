from flask import Flask, request, jsonify, render_template_string
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import json
import os
import sqlite3
from datetime import datetime

app = Flask(__name__)

# --- CONFIG ---
TWILIO_SID = os.environ.get("TWILIO_SID", "AC4dee8295213fcc043eb39b3120c6d138")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "08f2bc5cbd20e9a2c90bc79026aac6df")
TWILIO_PHONE = os.environ.get("TWILIO_PHONE", "+18556389238")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_KEY", "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")

twilio_client = Client(TWILIO_SID, TWILIO_TOKEN)
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

DB = "landlord.db"

# --- DATABASE ---
def init_db():
    con = sqlite3.connect(DB)
    c = con.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_phone TEXT, to_phone TEXT, body TEXT,
        direction TEXT, timestamp TEXT,
        ai_draft TEXT, status TEXT DEFAULT 'pending',
        is_repair INTEGER DEFAULT 0, media_urls TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS tenants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, phone TEXT UNIQUE,
        unit TEXT, property_address TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS vendors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, phone TEXT, specialty TEXT, notes TEXT
    )""")
    con.commit(); con.close()

def get_db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

init_db()

# --- AI HELPERS ---
def classify_and_draft(message_body, tenant_name, property_address, unit):
    prompt = f"""You are a professional property manager assistant.

Tenant: {tenant_name}
Property: {property_address}, Unit {unit}
Tenant's message: "{message_body}"

1. Is this a repair/maintenance request? Reply with YES or NO on the first line.
2. If YES, what type of repair? (plumbing/electrical/hvac/appliance/structural/pest/other) on second line.
3. Draft a professional, friendly reply to the tenant on the third line onwards.

Format:
REPAIR: YES/NO
TYPE: <type or none>
REPLY: <your drafted reply>"""

    resp = ai_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text
    lines = text.strip().split('\n')
    is_repair = False
    repair_type = "none"
    reply_lines = []
    for line in lines:
        if line.startswith("REPAIR:"):
            is_repair = "YES" in line.upper()
        elif line.startswith("TYPE:"):
            repair_type = line.replace("TYPE:", "").strip().lower()
        elif line.startswith("REPLY:"):
            reply_lines.append(line.replace("REPLY:", "").strip())
        elif reply_lines:
            reply_lines.append(line)
    return is_repair, repair_type, "\n".join(reply_lines)

def match_vendor(repair_type):
    con = get_db()
    vendors = con.execute("SELECT * FROM vendors").fetchall()
    con.close()
    if not vendors:
        return None
    # Simple keyword match
    type_keywords = {
        "plumbing": ["plumb", "pipe", "leak", "drain", "toilet", "water"],
        "electrical": ["electric", "wire", "outlet", "breaker", "light", "power"],
        "hvac": ["hvac", "heat", "ac", "air", "furnace", "cool"],
        "appliance": ["appliance", "fridge", "stove", "washer", "dryer", "dishwasher"],
        "pest": ["pest", "bug", "roach", "mouse", "rat", "insect"],
    }
    for vendor in vendors:
        specialty = (vendor["specialty"] or "").lower()
        for key, words in type_keywords.items():
            if key in repair_type or repair_type in key:
                if any(w in specialty for w in words) or key in specialty:
                    return dict(vendor)
    return dict(vendors[0])  # fallback to first vendor

def generate_vendor_message(tenant_name, property_address, unit, issue, media_urls):
    photos_note = f"\nPhotos attached: {', '.join(json.loads(media_urls))}" if media_urls and json.loads(media_urls) else ""
    return (f"Hi! This is your property manager. New repair request:\n"
            f"Tenant: {tenant_name}\n"
            f"Address: {property_address}, Unit {unit}\n"
            f"Issue: {issue}{photos_note}\n"
            f"Please confirm availability. Thanks!")

# --- ROUTES ---

@app.route("/sms", methods=["POST"])
def sms_webhook():
    """Twilio calls this when a tenant texts in."""
    from_phone = request.form.get("From", "")
    body = request.form.get("Body", "")
    num_media = int(request.form.get("NumMedia", 0))
    media_urls = [request.form.get(f"MediaUrl{i}") for i in range(num_media)]

    con = get_db()
    tenant = con.execute("SELECT * FROM tenants WHERE phone=?", (from_phone,)).fetchone()
    con.close()

    tenant_name = tenant["name"] if tenant else "Tenant"
    property_address = tenant["property_address"] if tenant else "Unknown Property"
    unit = tenant["unit"] if tenant else "Unknown Unit"

    is_repair, repair_type, ai_draft = classify_and_draft(body, tenant_name, property_address, unit)

    con = get_db()
    con.execute("""INSERT INTO messages (from_phone, to_phone, body, direction, timestamp, ai_draft, status, is_repair, media_urls)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (from_phone, TWILIO_PHONE, body, "inbound", datetime.now().isoformat(),
                 ai_draft, "pending", 1 if is_repair else 0, json.dumps(media_urls)))
    con.commit(); con.close()

    # Auto-reply mode off by default — just acknowledge
    resp = MessagingResponse()
    # Uncomment below for auto-reply mode:
    # resp.message(ai_draft)
    return str(resp)

@app.route("/api/messages")
def get_messages():
    con = get_db()
    msgs = con.execute("""
        SELECT m.*, t.name as tenant_name, t.property_address, t.unit
        FROM messages m LEFT JOIN tenants t ON m.from_phone = t.phone
        ORDER BY m.timestamp DESC LIMIT 100
    """).fetchall()
    con.close()
    return jsonify([dict(m) for m in msgs])

@app.route("/api/send", methods=["POST"])
def send_message():
    data = request.json
    msg_id = data["message_id"]
    reply_text = data["reply"]

    con = get_db()
    msg = con.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    if not msg:
        return jsonify({"error": "Message not found"}), 404

    twilio_client.messages.create(body=reply_text, from_=TWILIO_PHONE, to=msg["from_phone"])
    con.execute("UPDATE messages SET status='sent' WHERE id=?", (msg_id,))
    # Log the sent message
    con.execute("""INSERT INTO messages (from_phone, to_phone, body, direction, timestamp, status)
                   VALUES (?,?,?,?,?,?)""",
                (TWILIO_PHONE, msg["from_phone"], reply_text, "outbound", datetime.now().isoformat(), "sent"))
    con.commit(); con.close()
    return jsonify({"success": True})

@app.route("/api/dispatch_vendor", methods=["POST"])
def dispatch_vendor():
    data = request.json
    msg_id = data["message_id"]
    vendor_id = data.get("vendor_id")

    con = get_db()
    msg = con.execute("""SELECT m.*, t.name as tenant_name, t.property_address, t.unit
                         FROM messages m LEFT JOIN tenants t ON m.from_phone=t.phone
                         WHERE m.id=?""", (msg_id,)).fetchone()
    if vendor_id:
        vendor = con.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
    else:
        vendor = None
    con.close()

    if not msg or not vendor:
        return jsonify({"error": "Not found"}), 404

    vendor_msg = generate_vendor_message(
        msg["tenant_name"] or "Tenant",
        msg["property_address"] or "Unknown",
        msg["unit"] or "?",
        msg["body"],
        msg["media_urls"]
    )

    twilio_client.messages.create(body=vendor_msg, from_=TWILIO_PHONE, to=vendor["phone"])
    return jsonify({"success": True, "message_sent": vendor_msg})

@app.route("/api/suggest_vendor/<int:msg_id>")
def suggest_vendor(msg_id):
    con = get_db()
    msg = con.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
    con.close()
    if not msg:
        return jsonify({}), 404
    # Re-classify to get repair type
    _, repair_type, _ = classify_and_draft(msg["body"], "", "", "")
    vendor = match_vendor(repair_type)
    return jsonify({"vendor": vendor, "repair_type": repair_type})

# CRUD for tenants
@app.route("/api/tenants", methods=["GET"])
def get_tenants():
    con = get_db()
    rows = con.execute("SELECT * FROM tenants").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/tenants", methods=["POST"])
def add_tenant():
    d = request.json
    con = get_db()
    con.execute("INSERT OR REPLACE INTO tenants (name,phone,unit,property_address) VALUES (?,?,?,?)",
                (d["name"], d["phone"], d["unit"], d["property_address"]))
    con.commit(); con.close()
    return jsonify({"success": True})

@app.route("/api/tenants/<int:tid>", methods=["DELETE"])
def delete_tenant(tid):
    con = get_db()
    con.execute("DELETE FROM tenants WHERE id=?", (tid,))
    con.commit(); con.close()
    return jsonify({"success": True})

# CRUD for vendors
@app.route("/api/vendors", methods=["GET"])
def get_vendors():
    con = get_db()
    rows = con.execute("SELECT * FROM vendors").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/vendors", methods=["POST"])
def add_vendor():
    d = request.json
    con = get_db()
    con.execute("INSERT INTO vendors (name,phone,specialty,notes) VALUES (?,?,?,?)",
                (d["name"], d["phone"], d["specialty"], d.get("notes","")))
    con.commit(); con.close()
    return jsonify({"success": True})

@app.route("/api/vendors/<int:vid>", methods=["DELETE"])
def delete_vendor(vid):
    con = get_db()
    con.execute("DELETE FROM vendors WHERE id=?", (vid,))
    con.commit(); con.close()
    return jsonify({"success": True})

@app.route("/")
def index():
    return render_template_string(HTML)

# --- FRONTEND ---
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Property Manager</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0;}
:root{
  --bg:#0d1117;--surface:#161b22;--surface2:#21262d;
  --border:#30363d;--accent:#2f81f7;--green:#3fb950;
  --red:#f85149;--orange:#d29922;--text:#e6edf3;--muted:#8b949e;
}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;height:100vh;display:flex;flex-direction:column;}
/* NAV */
nav{display:flex;background:var(--surface);border-bottom:1px solid var(--border);padding:0;}
nav button{flex:1;background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);cursor:pointer;font-family:'Inter',sans-serif;font-size:0.78rem;font-weight:500;padding:12px 4px;transition:all 0.2s;letter-spacing:0.03em;}
nav button.active{color:var(--accent);border-bottom-color:var(--accent);}
/* PAGES */
.page{display:none;flex:1;overflow-y:auto;padding:16px;}
.page.active{display:block;}
/* INBOX */
.msg-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:12px;overflow:hidden;}
.msg-card.repair{border-left:3px solid var(--orange);}
.msg-card.sent-card{border-left:3px solid var(--green);}
.msg-header{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:var(--surface2);}
.tenant-name{font-weight:600;font-size:0.9rem;}
.msg-time{font-size:0.72rem;color:var(--muted);}
.repair-badge{background:var(--orange);color:#000;font-size:0.65rem;font-weight:700;padding:2px 7px;border-radius:20px;letter-spacing:0.05em;}
.msg-body{padding:12px 14px;font-size:0.88rem;color:var(--muted);line-height:1.5;}
.msg-photos{padding:0 14px 10px;display:flex;gap:8px;flex-wrap:wrap;}
.msg-photos img{width:70px;height:70px;object-fit:cover;border-radius:6px;border:1px solid var(--border);}
.draft-section{padding:10px 14px;border-top:1px solid var(--border);}
.draft-label{font-size:0.7rem;font-weight:600;color:var(--muted);letter-spacing:0.08em;text-transform:uppercase;margin-bottom:6px;}
textarea.draft{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-family:'Inter',sans-serif;font-size:0.85rem;line-height:1.5;padding:10px;resize:vertical;min-height:80px;outline:none;}
textarea.draft:focus{border-color:var(--accent);}
.btn-row{display:flex;gap:8px;margin-top:8px;}
.btn{border:none;border-radius:8px;cursor:pointer;font-family:'Inter',sans-serif;font-size:0.82rem;font-weight:600;padding:9px 16px;transition:all 0.2s;}
.btn-primary{background:var(--accent);color:#fff;}
.btn-orange{background:var(--orange);color:#000;}
.btn-green{background:var(--green);color:#000;}
.btn-ghost{background:var(--surface2);color:var(--text);border:1px solid var(--border);}
.btn:active{opacity:0.8;transform:scale(0.97);}
.status-sent{display:flex;align-items:center;gap:6px;color:var(--green);font-size:0.8rem;padding:8px 14px;border-top:1px solid var(--border);}
/* VENDOR DISPATCH MODAL */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:100;align-items:flex-end;}
.modal-overlay.open{display:flex;}
.modal{background:var(--surface);border-radius:16px 16px 0 0;padding:20px;width:100%;max-height:80vh;overflow-y:auto;}
.modal h3{font-size:1rem;margin-bottom:4px;}
.modal p{font-size:0.82rem;color:var(--muted);margin-bottom:16px;}
.vendor-option{display:flex;align-items:center;justify-content:space-between;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px;margin-bottom:8px;}
.vendor-info .vname{font-weight:600;font-size:0.9rem;}
.vendor-info .vdetail{font-size:0.75rem;color:var(--muted);margin-top:2px;}
.vendor-msg-preview{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:0.82rem;line-height:1.5;color:var(--muted);margin:12px 0;white-space:pre-wrap;}
/* SECTIONS (tenants/vendors) */
.section-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;}
.section-header h2{font-size:1rem;font-weight:600;}
.list-item{background:var(--surface);border:1px solid var(--border);border-radius:10px;display:flex;justify-content:space-between;align-items:center;padding:12px 14px;margin-bottom:8px;}
.list-info .lname{font-weight:600;font-size:0.9rem;}
.list-info .ldetail{font-size:0.75rem;color:var(--muted);margin-top:2px;}
.btn-del{background:none;border:none;color:var(--red);cursor:pointer;font-size:1.2rem;padding:4px 8px;}
/* FORM */
.form-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px;}
.form-card h3{font-size:0.9rem;font-weight:600;margin-bottom:12px;color:var(--muted);}
input,select{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--text);font-family:'Inter',sans-serif;font-size:0.88rem;padding:10px 12px;margin-bottom:8px;outline:none;-webkit-appearance:none;}
input:focus,select:focus{border-color:var(--accent);}
select option{background:#161b22;}
.empty{text-align:center;color:var(--muted);font-size:0.88rem;padding:40px 20px;}
.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--green);color:#000;font-weight:600;font-size:0.85rem;padding:10px 20px;border-radius:20px;display:none;z-index:200;}
.toast.show{display:block;animation:fadeInOut 2.5s forwards;}
@keyframes fadeInOut{0%{opacity:0;transform:translateX(-50%) translateY(10px);}15%{opacity:1;transform:translateX(-50%) translateY(0);}75%{opacity:1;}100%{opacity:0;}}
</style>
</head>
<body>
<nav>
  <button class="active" onclick="showPage('inbox',this)">📨 Inbox</button>
  <button onclick="showPage('tenants',this)">🏠 Tenants</button>
  <button onclick="showPage('vendors',this)">👷 Vendors</button>
</nav>

<!-- INBOX -->
<div id="inbox" class="page active">
  <div id="msg-list"><div class="empty">Loading messages...</div></div>
</div>

<!-- TENANTS -->
<div id="tenants" class="page">
  <div class="form-card">
    <h3>ADD TENANT</h3>
    <input id="t-name" placeholder="Tenant name">
    <input id="t-phone" placeholder="Phone (e.g. +12035551234)">
    <input id="t-unit" placeholder="Unit # (e.g. 2B)">
    <input id="t-address" placeholder="Property address">
    <button class="btn btn-primary" onclick="addTenant()" style="width:100%">Add Tenant</button>
  </div>
  <div class="section-header"><h2>Tenants</h2></div>
  <div id="tenant-list"><div class="empty">No tenants yet.</div></div>
</div>

<!-- VENDORS -->
<div id="vendors" class="page">
  <div class="form-card">
    <h3>ADD VENDOR</h3>
    <input id="v-name" placeholder="Vendor name">
    <input id="v-phone" placeholder="Phone (e.g. +12035551234)">
    <select id="v-specialty">
      <option value="">Select specialty...</option>
      <option>Plumbing</option><option>Electrical</option><option>HVAC</option>
      <option>Appliance Repair</option><option>Pest Control</option>
      <option>Structural/Carpentry</option><option>General Handyman</option>
    </select>
    <input id="v-notes" placeholder="Notes (e.g. reliable, fast)">
    <button class="btn btn-primary" onclick="addVendor()" style="width:100%">Add Vendor</button>
  </div>
  <div class="section-header"><h2>Vendors</h2></div>
  <div id="vendor-list"><div class="empty">No vendors yet.</div></div>
</div>

<!-- VENDOR DISPATCH MODAL -->
<div class="modal-overlay" id="dispatch-modal">
  <div class="modal">
    <h3>🔧 Dispatch Vendor</h3>
    <p id="dispatch-issue"></p>
    <div id="vendor-options"></div>
    <div class="vendor-msg-preview" id="vendor-msg-preview"></div>
    <div class="btn-row">
      <button class="btn btn-ghost" onclick="closeModal()" style="flex:1">Cancel</button>
      <button class="btn btn-orange" onclick="confirmDispatch()" style="flex:2">Send to Vendor ✓</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let currentDispatch = {msgId: null, vendorId: null};
let allVendors = [];

function showPage(id, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  btn.classList.add('active');
  if(id==='tenants') loadTenants();
  if(id==='vendors') loadVendors();
  if(id==='inbox') loadMessages();
}

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.remove('show');
  void t.offsetWidth; t.classList.add('show');
}

async function loadMessages() {
  const res = await fetch('/api/messages');
  const msgs = await res.json();
  // Group by conversation (from_phone)
  const convos = {};
  msgs.forEach(m => {
    const key = m.direction === 'inbound' ? m.from_phone : m.to_phone;
    if(!convos[key]) convos[key] = [];
    convos[key].push(m);
  });

  const inbound = msgs.filter(m => m.direction === 'inbound');
  if(!inbound.length) {
    document.getElementById('msg-list').innerHTML = '<div class="empty">No messages yet.<br>Texts from tenants will appear here.</div>';
    return;
  }

  document.getElementById('msg-list').innerHTML = inbound.map(m => {
    const name = m.tenant_name || m.from_phone;
    const time = new Date(m.timestamp).toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
    const photos = m.media_urls ? JSON.parse(m.media_urls) : [];
    const isSent = m.status === 'sent';
    const isRepair = m.is_repair;

    return `<div class="msg-card ${isRepair?'repair':''} ${isSent?'sent-card':''}">
      <div class="msg-header">
        <div style="display:flex;align-items:center;gap:8px;">
          <span class="tenant-name">${name}</span>
          ${isRepair?'<span class="repair-badge">REPAIR</span>':''}
        </div>
        <span class="msg-time">${time}</span>
      </div>
      <div class="msg-body">${m.body}</div>
      ${photos.length?`<div class="msg-photos">${photos.map(u=>`<img src="${u}" onerror="this.style.display='none'">`).join('')}</div>`:''}
      ${isSent ? `<div class="status-sent">✓ Reply sent</div>` : `
      <div class="draft-section">
        <div class="draft-label">AI Draft Reply</div>
        <textarea class="draft" id="draft-${m.id}">${m.ai_draft||''}</textarea>
        <div class="btn-row">
          <button class="btn btn-primary" onclick="sendReply(${m.id})" style="flex:2">Send Reply</button>
          ${isRepair?`<button class="btn btn-orange" onclick="openDispatch(${m.id},'${(m.body||'').replace(/'/g,"\\'")}','${m.property_address||''}','${m.unit||''}','${m.tenant_name||''}')">Dispatch Vendor</button>`:''}
        </div>
      </div>`}
    </div>`;
  }).join('');
}

async function sendReply(msgId) {
  const reply = document.getElementById(`draft-${msgId}`).value.trim();
  if(!reply) return alert('Reply cannot be empty');
  const res = await fetch('/api/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({message_id: msgId, reply})
  });
  if((await res.json()).success) { toast('✓ Reply sent!'); loadMessages(); }
}

async function openDispatch(msgId, issue, address, unit, tenantName) {
  currentDispatch = {msgId, vendorId: null};
  document.getElementById('dispatch-issue').textContent = `${tenantName} @ ${address} Unit ${unit}: "${issue}"`;

  // Load vendors and suggest
  const vRes = await fetch('/api/vendors');
  allVendors = await vRes.json();
  const sugRes = await fetch(`/api/suggest_vendor/${msgId}`);
  const {vendor: suggested} = await sugRes.json();

  document.getElementById('vendor-options').innerHTML = allVendors.map(v => `
    <div class="vendor-option" id="voption-${v.id}" onclick="selectVendor(${v.id},'${(issue||'').replace(/'/g,"\\'")}','${address}','${unit}','${tenantName}')">
      <div class="vendor-info">
        <div class="vname">${v.name} ${suggested && suggested.id===v.id?'⭐':''}</div>
        <div class="vdetail">${v.specialty} • ${v.notes||''}</div>
      </div>
      <div style="color:var(--muted);font-size:0.8rem">${v.phone}</div>
    </div>`).join('') || '<div class="empty">No vendors added yet. Go to Vendors tab.</div>';

  // Auto-select suggested
  if(suggested) selectVendor(suggested.id, issue, address, unit, tenantName);

  document.getElementById('dispatch-modal').classList.add('open');
}

function selectVendor(vid, issue, address, unit, tenantName) {
  currentDispatch.vendorId = vid;
  document.querySelectorAll('.vendor-option').forEach(el => el.style.borderColor = 'var(--border)');
  const el = document.getElementById(`voption-${vid}`);
  if(el) el.style.borderColor = 'var(--orange)';
  const vendor = allVendors.find(v=>v.id===vid);
  document.getElementById('vendor-msg-preview').textContent =
    `Hi! This is your property manager. New repair request:\nTenant: ${tenantName}\nAddress: ${address}, Unit ${unit}\nIssue: ${issue}\nPlease confirm availability. Thanks!`;
}

function closeModal() {
  document.getElementById('dispatch-modal').classList.remove('open');
}

async function confirmDispatch() {
  if(!currentDispatch.vendorId) return alert('Please select a vendor');
  const res = await fetch('/api/dispatch_vendor', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({message_id: currentDispatch.msgId, vendor_id: currentDispatch.vendorId})
  });
  if((await res.json()).success) { closeModal(); toast('✓ Vendor dispatched!'); }
}

// TENANTS
async function loadTenants() {
  const res = await fetch('/api/tenants');
  const tenants = await res.json();
  document.getElementById('tenant-list').innerHTML = tenants.length ?
    tenants.map(t=>`<div class="list-item">
      <div class="list-info">
        <div class="lname">${t.name}</div>
        <div class="ldetail">${t.property_address} · Unit ${t.unit} · ${t.phone}</div>
      </div>
      <button class="btn-del" onclick="deleteTenant(${t.id})">✕</button>
    </div>`).join('') : '<div class="empty">No tenants yet.</div>';
}

async function addTenant() {
  const name=document.getElementById('t-name').value.trim();
  const phone=document.getElementById('t-phone').value.trim();
  const unit=document.getElementById('t-unit').value.trim();
  const property_address=document.getElementById('t-address').value.trim();
  if(!name||!phone||!unit||!property_address) return alert('Please fill all fields');
  await fetch('/api/tenants',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,phone,unit,property_address})});
  ['t-name','t-phone','t-unit','t-address'].forEach(id=>document.getElementById(id).value='');
  toast('✓ Tenant added!'); loadTenants();
}

async function deleteTenant(id) {
  if(!confirm('Remove this tenant?')) return;
  await fetch(`/api/tenants/${id}`,{method:'DELETE'});
  loadTenants();
}

// VENDORS
async function loadVendors() {
  const res = await fetch('/api/vendors');
  const vendors = await res.json();
  allVendors = vendors;
  document.getElementById('vendor-list').innerHTML = vendors.length ?
    vendors.map(v=>`<div class="list-item">
      <div class="list-info">
        <div class="lname">${v.name}</div>
        <div class="ldetail">${v.specialty} · ${v.phone}${v.notes?' · '+v.notes:''}</div>
      </div>
      <button class="btn-del" onclick="deleteVendor(${v.id})">✕</button>
    </div>`).join('') : '<div class="empty">No vendors yet.</div>';
}

async function addVendor() {
  const name=document.getElementById('v-name').value.trim();
  const phone=document.getElementById('v-phone').value.trim();
  const specialty=document.getElementById('v-specialty').value;
  const notes=document.getElementById('v-notes').value.trim();
  if(!name||!phone||!specialty) return alert('Please fill name, phone, and specialty');
  await fetch('/api/vendors',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,phone,specialty,notes})});
  ['v-name','v-phone','v-notes'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('v-specialty').value='';
  toast('✓ Vendor added!'); loadVendors();
}

async function deleteVendor(id) {
  if(!confirm('Remove this vendor?')) return;
  await fetch(`/api/vendors/${id}`,{method:'DELETE'});
  loadVendors();
}

loadMessages();
setInterval(loadMessages, 15000); // auto-refresh every 15s
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
