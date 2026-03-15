"""Claude agent — deploys CML labs via MCP tools through conversation."""
from __future__ import annotations

import base64
import json
import logging
from typing import Callable, Optional

from anthropic import AsyncAnthropic

from mcp_bridge import MCPBridge

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are cml-manager, a Cisco CML lab deployment bot. You create and manage CML labs from user descriptions or whiteboard photos.

## What you do

When a user describes a network topology (text or whiteboard photo), follow this TWO-PHASE process:

### Phase 1 — Plan (DO NOT deploy yet)
1. Check CML system resources with get_cml_statistics (CPU, memory, running nodes)
2. Check licensing limits with get_cml_licensing_details
3. Interpret the user's request and design the topology
4. Present a clear summary in markdown:
   - **Topology name**
   - **Devices**: list each router with loopback IP and role
   - **Links**: list each connection with subnet
   - **Routing**: protocol and areas
   - **Resource check**: current CML usage vs what this lab will need
5. Ask: "Ready to deploy? Reply **yes** to confirm."
6. **STOP and wait for the user to confirm.** Do NOT call create_full_lab_topology until the user says yes/confirm/deploy/go.

### Phase 2 — Deploy (only after user confirms)
1. Create the lab with create_full_lab_topology
2. Start with start_cml_lab (wait_for_convergence=true)
3. Get node states with get_nodes_for_cml_lab
4. Verify connectivity using send_cli_command (show ip ospf neighbor, ping between loopbacks)
5. Report the final state: device hostnames, management IPs, SSH credentials

### Other commands
When asked to tear down: use delete_cml_lab (it auto-stops and wipes).

When asked to run commands on routers: use send_cli_command with the node label.

When asked about existing labs: use get_cml_labs and get_nodes_for_cml_lab.

When asked about CML status/resources: use get_cml_statistics and get_cml_status.

## CML topology rules

- Use `iol-xe` node_definition with `iol-xe-17-16-01a` image_definition for all routers
- Reserve Ethernet0/0 on each router for management (configure with `ip address dhcp` + `no shutdown`)
- Use Ethernet0/1, Ethernet0/2, etc. for data links between routers
- Always include management infrastructure:
  - An `unmanaged_switch` node labeled "mgmt-sw" with enough ports for all routers + 1
  - An `external_connector` node labeled "ext-conn" with configuration "NAT"
  - Link each router's Ethernet0/0 to a port on mgmt-sw
  - Link ext-conn to the last port on mgmt-sw
- Each router config should include:
  - Loopback0 with the assigned IP
  - Data interfaces with assigned IPs
  - OSPF (or static routes as requested)
  - `username hacker privilege 15 secret 0 BreakMe123`
  - `username herbie privilege 15 secret 0 H3rb13!Ops`
  - `line vty 0 4` / `login local` / `transport input ssh telnet`
  - `ip ssh version 2` / `crypto key generate rsa modulus 2048`
  - `interface Ethernet0/0` / `ip address dhcp` / `no shutdown`

## Topology JSON format for create_full_lab_topology

The `lab` parameter must be a dict with keys: title, version ("0.3.0"), description.
The `nodes` parameter must be a list of node dicts. Each router node needs: id, label, node_definition, image_definition, x, y, configuration, interfaces (list of {id, label, slot, type}).
The `links` parameter must be a list of link dicts: {id, src_node (node id), src_interface (interface id), dst_node, dst_interface}.

Interface IDs must be globally unique (i0, i1, i2...). Node IDs must be unique (n0, n1, n2...).

## Output format

After successful deployment, always report:
- Lab title and ID
- List of routers with their management IPs (from DHCP on Ethernet0/0)
- SSH credentials: `hacker` / `BreakMe123` (audience access), `herbie` / `H3rb13!Ops` (Herbie access)
- OSPF neighbor verification results

## Style

- Be concise but informative
- Use markdown formatting for Webex messages
- Report progress at each step (creating, starting, verifying)
"""


class CMLAgent:
    def __init__(
        self,
        api_key: str,
        mcp_bridge: MCPBridge,
        notify: Optional[Callable] = None,
    ) -> None:
        self.client = AsyncAnthropic(api_key=api_key)
        self.mcp = mcp_bridge
        self.notify = notify
        self.conversation: list[dict] = []
        self.max_history = 20
        self._busy = False

    async def _notify(self, text: str) -> None:
        if self.notify:
            try:
                await self.notify(text)
            except Exception as e:
                logger.warning(f"Notify error: {e}")

    async def handle_message(self, text: str = None, image_bytes: bytes = None) -> str:
        """Process a user message. Returns the agent's text response."""
        content = []
        if image_bytes:
            media_type = "image/png" if image_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(image_bytes).decode(),
                },
            })
        if text:
            content.append({"type": "text", "text": text})

        if not content:
            return "Send me a topology description or a whiteboard photo!"

        if self._busy:
            return "I'm currently working on something. Please wait."

        self._busy = True
        self.conversation.append({"role": "user", "content": content})
        self._trim_history()

        tools = self.mcp.get_anthropic_tools()
        response_text = ""

        try:
            for _ in range(15):  # max tool-use iterations
                response = await self.client.messages.create(
                    model="claude-sonnet-4-5-20250929",
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=tools,
                    messages=self.conversation,
                )

                assistant_content = []
                tool_calls = []
                for block in response.content:
                    if block.type == "text":
                        response_text += block.text
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        tool_calls.append(block)
                        assistant_content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })

                self.conversation.append({"role": "assistant", "content": assistant_content})

                if response.stop_reason == "end_turn" or not tool_calls:
                    break

                # Execute tool calls and send progress
                tool_results = []
                for tc in tool_calls:
                    await self._send_progress(tc.name, tc.input)
                    result = await self._execute_tool(tc.name, tc.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result,
                    })

                self.conversation.append({"role": "user", "content": tool_results})
        finally:
            self._busy = False

        return response_text

    async def _execute_tool(self, name: str, arguments: dict) -> str:
        if self.mcp.has_tool(name):
            return await self.mcp.call_tool(name, arguments)
        return f"Unknown tool: {name}"

    async def _send_progress(self, tool_name: str, tool_args: dict) -> None:
        """Send Webex progress updates for key operations."""
        messages = {
            "create_full_lab_topology": "Creating lab topology...",
            "start_cml_lab": "Starting lab — waiting for nodes to boot...",
            "get_nodes_for_cml_lab": "Checking node states...",
            "send_cli_command": f"Running CLI on **{tool_args.get('label', '?')}**...",
            "delete_cml_lab": "Tearing down lab...",
        }
        msg = messages.get(tool_name)
        if msg:
            await self._notify(msg)

    def _trim_history(self) -> None:
        if len(self.conversation) > self.max_history * 2:
            self.conversation = self.conversation[-self.max_history * 2:]

    def reset(self) -> None:
        self.conversation = []
