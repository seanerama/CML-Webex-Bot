"""cml-manager bot — deploys CML labs via Webex webhooks."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request

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
