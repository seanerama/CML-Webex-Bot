# CML-Webex-Bot

A Webex bot that deploys Cisco CML (Cisco Modeling Labs) network topologies from natural language descriptions or whiteboard photos. Powered by Claude AI and CML's MCP (Model Context Protocol) tools.

## How It Works

```
User (Webex) → Webhook → FastAPI → Claude Agent → CML MCP Tools → CML Lab
                                        ↓
                                  Webex Reply
```

1. **Describe** a network topology in Webex — plain text or a photo of a whiteboard drawing
2. The bot checks CML system resources and presents a **topology summary** for review
3. Reply **yes** to confirm — the bot deploys the lab to CML
4. The bot **verifies** connectivity (OSPF neighbors, pings) and reports back with device details and SSH credentials

## Features

- **Natural language topology creation** — "3 routers in a triangle with OSPF area 0"
- **Whiteboard photo interpretation** — snap a photo of a hand-drawn network diagram
- **Resource-aware** — checks CML CPU, memory, and licensing before deploying
- **Confirmation before deploy** — presents a summary and waits for approval
- **Full verification** — checks OSPF adjacencies and loopback reachability after boot
- **Lab management** — tear down, list labs, run CLI commands on devices
- **47 CML MCP tools** — full programmatic access to CML via [cml-mcp](https://github.com/xorrkaz/cml-mcp)

## Architecture

The bot is 4 Python files with no framework overhead:

| File | Purpose |
|------|---------|
| `main.py` | FastAPI server, ngrok tunnel, Webex webhook handling |
| `agent.py` | Claude conversation loop with MCP tool routing |
| `mcp_bridge.py` | Manages cml-mcp subprocess, bridges tools to Anthropic API |
| `webex.py` | Webex API helpers (notifier) |

### Why MCP?

Instead of a custom CML REST client, the bot uses [cml-mcp](https://github.com/xorrkaz/cml-mcp) — a Model Context Protocol server that exposes 47 CML operations as tools. Claude calls these tools directly during the conversation, meaning the AI decides which API calls to make based on the user's request. No hardcoded deployment pipelines.

## Prerequisites

- **Cisco CML** 2.9+ with IOL-XE images installed
- **Python** 3.12+
- **Anthropic API key** — [console.anthropic.com](https://console.anthropic.com)
- **Webex Bot** — create one at [developer.webex.com/my-apps/new/bot](https://developer.webex.com/my-apps/new/bot)
- **ngrok account** — [ngrok.com](https://ngrok.com) (free tier works)

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/seanerama/CML-Webex-Bot.git
cd CML-Webex-Bot
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# CML server
CML_URL=https://your-cml-server
CML_USERNAME=admin
CML_PASSWORD=your-password
CML_VERIFY_SSL=false

# Anthropic (Claude)
ANTHROPIC_API_KEY=sk-ant-...

# Webex Bot (from developer.webex.com)
WEBEX_BOT_TOKEN=your-bot-token

# ngrok (from dashboard.ngrok.com)
NGROK_AUTHTOKEN=your-ngrok-token
```

> **Note:** `WEBEX_ROOM_ID` and `WEBEX_BOT_ID` are not needed — the bot auto-detects its identity and responds to any room it's messaged in.

### 3. Run

```bash
python main.py
```

The bot will:
1. Connect to CML via MCP (47 tools)
2. Start an ngrok tunnel
3. Register a Webex webhook automatically
4. Begin listening for messages

### 4. Message the bot

Open Webex and message your bot directly or add it to a space.

## Usage Examples

### Create a topology from text
```
Create a lab with 3 routers in a triangle, R1 R2 R3, OSPF area 0.
Each has a loopback (1.1.1.1, 2.2.2.2, 3.3.3.3).
Add R4 connected to R3 only via static route, loopback 4.4.4.4.
```

### Create from a whiteboard photo
Drop a photo of a hand-drawn network diagram into the chat. The bot uses Claude's vision to interpret the topology.

### Check CML status
```
What's the current CML resource usage?
```

### Run CLI commands
```
Run "show ip ospf neighbor" on R1
```

### Tear down a lab
```
Delete the lab
```

## CML Topology Details

The bot generates labs with:

- **Router type**: IOL-XE (`iol-xe` with `iol-xe-17-16-01a` image) — lightweight IOS-XE, boots in ~30 seconds
- **Management network**: Each router gets Ethernet0/0 on DHCP via an unmanaged switch + external connector (NAT mode)
- **Data interfaces**: Ethernet0/1+ for inter-router links
- **SSH access**: Two user accounts are pre-configured:
  - `hacker` / `BreakMe123` — general access
  - `herbie` / `H3rb13!Ops` — operations access

## Deployment

The bot should run on a host with direct network access to CML for best results. This enables:
- `send_cli_command` via pyATS (console access to devices)
- Direct SSH to router management IPs
- Low-latency MCP tool calls

### Running as a service (systemd)

```ini
# /etc/systemd/system/cml-bot.service
[Unit]
Description=CML Webex Bot
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/CML-Webex-Bot
Environment=PATH=/home/your-user/.local/bin:/usr/bin
EnvironmentFile=/path/to/CML-Webex-Bot/.env
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## How the Agent Works

The Claude agent maintains a conversation with the user. When it needs to interact with CML, it calls MCP tools through the bridge:

```
User: "Create a 3-router OSPF triangle"
  ↓
Claude: calls get_cml_statistics → checks resources
Claude: calls get_cml_licensing_details → checks limits
Claude: responds with topology summary, asks for confirmation
  ↓
User: "yes"
  ↓
Claude: calls create_full_lab_topology → creates lab
Claude: calls start_cml_lab → boots nodes
Claude: calls get_nodes_for_cml_lab → checks states
Claude: calls send_cli_command → verifies OSPF neighbors
Claude: responds with deployment report
```

The agent is stateful — it remembers the conversation context, so you can ask follow-up questions about the lab it just created.

## License

MIT

## Acknowledgments

- [cml-mcp](https://github.com/xorrkaz/cml-mcp) by Joe Clarke — CML MCP server
- [Anthropic Claude](https://www.anthropic.com) — AI backbone
- [Cisco CML](https://developer.cisco.com/modeling-labs/) — Network simulation platform
