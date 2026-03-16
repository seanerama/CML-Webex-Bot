"""cml-manager bot — deploys CML labs via Webex webhooks."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from agent import CMLAgent
from mcp_bridge import MCPBridge
from webex import WebexNotifier

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Globals initialized in lifespan
mcp: MCPBridge = None
agent: CMLAgent = None
notifier: WebexNotifier = None
BOT_ID: str = ""
NGROK_URL: str = ""

# Cached lab data — refreshed in background
_lab_cache: dict = {"labs": [], "error": None, "last_updated": None}
_cache_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mcp, agent, notifier, BOT_ID, NGROK_URL

    # Start MCP bridge
    mcp = MCPBridge()
    await mcp.connect()

    # Notifier
    notifier = WebexNotifier(
        bot_token=os.getenv("WEBEX_BOT_TOKEN", ""),
        room_id="",  # will send to specific rooms from webhook data
    )

    # Agent
    agent = CMLAgent(
        api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        mcp_bridge=mcp,
    )

    # Get bot identity
    bot_token = os.getenv("WEBEX_BOT_TOKEN", "")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://webexapis.com/v1/people/me",
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        bot_info = resp.json()
        BOT_ID = bot_info.get("id", "")
        logger.info(f"Bot: {bot_info.get('displayName')} ({bot_info.get('emails', ['?'])[0]})")

    # Start ngrok tunnel
    ngrok_token = os.getenv("NGROK_AUTHTOKEN", "")
    if ngrok_token:
        import ngrok
        listener = await ngrok.forward(8000, authtoken=ngrok_token)
        NGROK_URL = listener.url()
        logger.info(f"ngrok tunnel: {NGROK_URL}")

        # Register webhook with Webex
        await _setup_webhook(bot_token, f"{NGROK_URL}/webhook")
    else:
        logger.warning("No NGROK_AUTHTOKEN — webhook won't be reachable externally")

    logger.info("cml-manager ready")

    # Start background lab cache refresh
    asyncio.create_task(_refresh_lab_cache_loop())

    yield

    # Cleanup
    await mcp.disconnect()


async def _setup_webhook(bot_token: str, target_url: str) -> None:
    """Register or update the Webex webhook."""
    headers = {"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        # List existing webhooks
        resp = await client.get("https://webexapis.com/v1/webhooks", headers=headers)
        webhooks = resp.json().get("items", [])

        # Delete old webhooks with our name
        for wh in webhooks:
            if wh.get("name") == "cml-manager":
                await client.delete(f"https://webexapis.com/v1/webhooks/{wh['id']}", headers=headers)
                logger.info(f"Deleted old webhook {wh['id']}")

        # Create new webhook
        resp = await client.post(
            "https://webexapis.com/v1/webhooks",
            headers=headers,
            json={
                "name": "cml-manager",
                "targetUrl": target_url,
                "resource": "messages",
                "event": "created",
            },
        )
        if resp.status_code == 200:
            wh = resp.json()
            logger.info(f"Webhook registered: {wh['id']} -> {target_url}")
        else:
            logger.error(f"Webhook registration failed: {resp.status_code} {resp.text}")


# Create app
app = FastAPI(title="cml-manager", lifespan=lifespan)


@app.get("/")
async def root():
    return {"name": "cml-manager", "status": "running"}


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "mcp_tools": len(mcp.get_anthropic_tools()) if mcp else 0,
        "ngrok_url": NGROK_URL,
    }


async def _refresh_lab_cache() -> None:
    """Fetch lab data and update cache."""
    import json as _json
    global _lab_cache

    lab_data = {"labs": [], "error": None, "last_updated": None}
    try:
        result = await mcp.call_tool("get_cml_labs", {})
        labs = _json.loads(result) if result.startswith("[") else []

        for lab in labs:
            if not isinstance(lab, dict):
                continue
            lid = lab.get("id", "")
            lab_info = {
                "id": lid,
                "title": lab.get("lab_title", "?"),
                "state": lab.get("state", "?"),
                "nodes": [],
            }
            if lab.get("state") == "STARTED":
                nodes_result = await mcp.call_tool("get_nodes_for_cml_lab", {"lid": lid})
                nodes = _json.loads(nodes_result) if nodes_result.startswith("[") else []
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    nd = node.get("node_definition", "")
                    if nd in ("unmanaged_switch", "external_connector"):
                        continue
                    mgmt_ip = ""
                    try:
                        ip_result = await mcp.call_tool("send_cli_command", {
                            "lid": lid, "label": node.get("label", ""),
                            "commands": "show ip interface brief | include Ethernet0/0"
                        })
                        for line in ip_result.split("\n"):
                            if "Ethernet0/0" in line:
                                parts = line.split()
                                if len(parts) >= 2 and parts[1][0].isdigit():
                                    mgmt_ip = parts[1]
                    except Exception:
                        pass
                    lab_info["nodes"].append({
                        "label": node.get("label", "?"),
                        "state": node.get("state", "?"),
                        "mgmt_ip": mgmt_ip,
                        "node_definition": nd,
                    })
            lab_data["labs"].append(lab_info)
    except Exception as e:
        lab_data["error"] = str(e)

    from datetime import datetime
    lab_data["last_updated"] = datetime.now().strftime("%H:%M:%S")
    async with _cache_lock:
        _lab_cache.update(lab_data)
    logger.info(f"Lab cache refreshed: {len(lab_data['labs'])} labs")


async def _refresh_lab_cache_loop() -> None:
    """Background loop to refresh lab cache every 30 seconds."""
    await asyncio.sleep(2)  # wait for MCP to be ready
    while True:
        try:
            await _refresh_lab_cache()
        except Exception as e:
            logger.warning(f"Lab cache refresh error: {e}")
        await asyncio.sleep(30)


@app.get("/lab", response_class=HTMLResponse)
async def lab_page():
    """Show current lab state with management IPs (served from cache)."""
    async with _cache_lock:
        lab_data = dict(_lab_cache)

    # Render HTML
    html = """<!DOCTYPE html>
<html><head>
<title>CML Lab Status</title>
<meta http-equiv="refresh" content="30">
<style>
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0a0e17; color: #e2e8f0; max-width: 900px; margin: 40px auto; padding: 0 20px; }
  h1 { color: #00ff88; font-size: 1.5em; }
  h2 { color: #64bbe3; font-size: 1.2em; margin-top: 2em; }
  .lab { background: #1a1f2e; border-radius: 8px; padding: 16px 20px; margin: 12px 0; border-left: 3px solid #00ff88; }
  .lab.stopped { border-left-color: #64748b; }
  .lab-title { font-weight: bold; font-size: 1.1em; }
  .lab-state { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; margin-left: 8px; }
  .lab-state.STARTED { background: #00ff8833; color: #00ff88; }
  .lab-state.STOPPED { background: #64748b33; color: #64748b; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; }
  th { text-align: left; padding: 8px; border-bottom: 1px solid #334155; color: #94a3b8; font-size: 0.85em; }
  td { padding: 8px; border-bottom: 1px solid #1e293b; }
  .ip { font-family: monospace; color: #00ff88; font-weight: bold; cursor: pointer; }
  .ip:hover { text-decoration: underline; }
  .creds { background: #1a1f2e; border-radius: 8px; padding: 16px 20px; margin: 12px 0; font-family: monospace; }
  .creds span { color: #ffaa00; }
  .copy-btn { background: #334155; border: none; color: #e2e8f0; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 0.8em; margin-left: 8px; }
  .copy-btn:hover { background: #475569; }
  .empty { color: #64748b; font-style: italic; }
  #copied { display: none; color: #00ff88; font-size: 0.8em; margin-left: 8px; }
</style>
<script>
function copyIPs() {
  const ips = [...document.querySelectorAll('.ip')].map(el => el.textContent).filter(Boolean);
  navigator.clipboard.writeText(ips.join(','));
  document.getElementById('copied').style.display = 'inline';
  setTimeout(() => document.getElementById('copied').style.display = 'none', 2000);
}
function copyTable() {
  const rows = [...document.querySelectorAll('tbody tr')].map(tr => {
    const cells = [...tr.querySelectorAll('td')];
    return cells.map(c => c.textContent.trim()).join('\\t');
  });
  navigator.clipboard.writeText(rows.join('\\n'));
}
</script>
</head><body>
<h1>CML Lab Status</h1>"""

    if lab_data["error"]:
        html += f'<p style="color:#ff3355">Error: {lab_data["error"]}</p>'

    if not lab_data["labs"]:
        html += '<p class="empty">No labs found.</p>'

    for lab in lab_data["labs"]:
        state_class = lab["state"]
        html += f'''<div class="lab {state_class.lower()}">
<span class="lab-title">{lab["title"]}</span>
<span class="lab-state {state_class}">{state_class}</span>
<br><small style="color:#64748b">ID: {lab["id"]}</small>'''

        if lab["nodes"]:
            ips = [n["mgmt_ip"] for n in lab["nodes"] if n["mgmt_ip"]]
            html += f'''
<div style="margin-top:8px">
  <button class="copy-btn" onclick="copyIPs()">Copy IPs</button>
  <span id="copied">Copied!</span>
</div>
<table><thead><tr><th>Device</th><th>State</th><th>Management IP</th><th>Type</th></tr></thead><tbody>'''
            for node in lab["nodes"]:
                ip_display = f'<span class="ip">{node["mgmt_ip"]}</span>' if node["mgmt_ip"] else '<span class="empty">pending</span>'
                html += f'<tr><td>{node["label"]}</td><td>{node["state"]}</td><td>{ip_display}</td><td>{node["node_definition"]}</td></tr>'
            html += '</tbody></table>'
        else:
            html += '<p class="empty">No router nodes (lab may not be started)</p>'

        html += '</div>'

    html += '''
<h2>SSH Credentials</h2>
<div class="creds">
  <span>hacker</span> / BreakMe123 &nbsp;(audience access)<br>
  <span>herbie</span> / H3rb13!Ops &nbsp;(operations access)
</div>
<p style="color:#64748b;font-size:0.8em">Last updated: {lab_data.get("last_updated", "loading...")} — auto-refreshes every 30 seconds</p>
</body></html>'''

    return HTMLResponse(content=html)


@app.get("/lab/json")
async def lab_json():
    """API endpoint for lab data — returns cached JSON with management IPs."""
    async with _cache_lock:
        data = dict(_lab_cache)
    # Reshape for API consumers (like Herbie)
    labs = []
    for lab in data.get("labs", []):
        if lab.get("state") != "STARTED":
            continue
        labs.append({
            "id": lab["id"],
            "title": lab["title"],
            "devices": [{"hostname": n["label"], "mgmt_ip": n["mgmt_ip"], "state": n["state"]} for n in lab.get("nodes", [])],
            "ssh_users": {"hacker": "BreakMe123", "herbie": "H3rb13!Ops"},
        })
    return {"labs": labs, "last_updated": data.get("last_updated")}


@app.post("/lab/refresh")
async def lab_refresh():
    """Force refresh the lab cache."""
    asyncio.create_task(_refresh_lab_cache())
    return {"status": "refreshing"}


@app.post("/webhook")
async def webhook(request: Request):
    """Handle incoming Webex webhook."""
    payload = await request.json()

    resource = payload.get("resource")
    event = payload.get("event")
    data = payload.get("data", {})

    # Only handle message:created
    if resource != "messages" or event != "created":
        return {"status": "ignored"}

    # Ignore messages from self
    if data.get("personId") == BOT_ID:
        return {"status": "ignored", "reason": "from_self"}

    message_id = data.get("id")
    room_id = data.get("roomId")
    person_email = data.get("personEmail", "someone")

    if not message_id:
        return {"status": "error", "reason": "no_message_id"}

    # Fetch full message (webhook only sends metadata)
    bot_token = os.getenv("WEBEX_BOT_TOKEN", "")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://webexapis.com/v1/messages/{message_id}",
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        if resp.status_code != 200:
            logger.error(f"Failed to fetch message: {resp.status_code}")
            return {"status": "error"}
        message = resp.json()

    text = (message.get("text") or "").strip()
    files = message.get("files") or []
    logger.info(f"Message from {person_email}: {text[:80]} (files: {len(files)})")

    # Download image if attached
    image_bytes = None
    if files:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                files[0],
                headers={"Authorization": f"Bearer {bot_token}"},
            )
            if resp.status_code == 200:
                image_bytes = resp.content

    # Process message via agent (run in background so webhook returns fast)
    asyncio.create_task(_process_message(room_id, text, image_bytes))

    return {"status": "processing"}


async def _process_message(room_id: str, text: str, image_bytes: bytes = None) -> None:
    """Process message and send reply."""
    bot_token = os.getenv("WEBEX_BOT_TOKEN", "")

    async def send_to_room(markdown: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                "https://webexapis.com/v1/messages",
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                json={"roomId": room_id, "markdown": markdown},
            )

    # Set agent's notify to send to this room
    agent.notify = send_to_room

    try:
        response = await agent.handle_message(text=text, image_bytes=image_bytes)
        if response:
            await send_to_room(response)
    except Exception as e:
        logger.error(f"Agent error: {e}", exc_info=True)
        await send_to_room(f"Error: {e}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
