#!/usr/bin/env python3
"""
Self-Improving Agent Harness Test using MiniMax 2.7 M2.7 AWQ.
Demonstrates the peer-to-peer worker team topology in Prism for iterative code improvement.
"""

import os
import sys
import json
import asyncio
import httpx

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

DEFAULT_PRISM_URL = "http://10.0.0.16:7777"
MODEL = "cyankiwi/MiniMax-M2.7-AWQ-4bit"
TARGET_FILE = "scratch/flappy_bird.py"

def load_prism_url():
    """Load PRISM_URL from .env file to avoid heavy app stack imports where possible."""
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("PRISM_URL="):
                    return line.split("=", 1)[1].strip('"').strip("'")
    return DEFAULT_PRISM_URL

async def run_harness_via_import():
    """Run the self-improving harness using the codebase's run_prism_agent harness."""
    try:
        from app.tools.prism_agent_harness import run_prism_agent
        from app.services.vllm_client import Priority
        
        print("[*] Successfully imported app.tools.prism_agent_harness. Running via codebase harness...")
        
        system_prompt = (
            "You are a Software Engineering Coordinator Agent. Your task is to coordinate the development of a "
            "self-improving Flappy Bird game using a team of 2 worker agents in a peer-to-peer topology.\n\n"
            "You have access to the `create_team` tool. You MUST call this tool to start the self-improvement loop.\n"
            "Configure the team named 'flappy_improvement' as follows:\n"
            "- topology: 'peer_to_peer'\n"
            "- members:\n"
            "  1. developer_worker: A code developer. Its prompt should instruct it to read scratch/flappy_bird.py, "
            "     find any bugs (like syntax errors, missing imports, or incorrect variables), fix them, and rewrite "
            "     the game to be fully functional, playable, and feature-rich. It must incorporate feedback from the critic_worker.\n"
            "  2. critic_worker: A code validator and critic. Its prompt should instruct it to analyze the updated "
            "     code written in scratch/flappy_bird.py, check for runtime issues, logic errors, or missing elements, "
            "     and provide detailed critiques. If the code is perfect, playable, and complete with no further changes "
            "     needed, the critic MUST output '[DONE]' in its final response to terminate the turn loop.\n\n"
            "Both members MUST use the model 'cyankiwi/MiniMax-M2.7-AWQ-4bit'.\n"
            "After the team finishes, output the final code and summarize the improvements made."
        )

        user_prompt = (
            f"Run the self-improvement loop on {TARGET_FILE}. Spawn exactly 2 workers using peer-to-peer topology "
            "via the `create_team` tool."
        )

        # We construct the custom tool definition for create_team since it's a Prism orchestrator tool
        create_team_schema = {
            "name": "create_team",
            "description": "Spawn worker agents in peer-to-peer topology to write and review code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "topology": {"type": "string", "enum": ["peer_to_peer"]},
                    "members": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "description": {"type": "string"},
                                "prompt": {"type": "string"},
                                "files": {"type": "array", "items": {"type": "string"}},
                                "model": {"type": "string"},
                                "agent": {"type": "string"}
                            },
                            "required": ["description", "prompt"]
                        }
                    }
                },
                "required": ["name", "members"]
            }
        }

        # Include basic file access tools so subagents inherit them
        read_file_schema = {
            "name": "read_file",
            "description": "Read file contents from local workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"]
            }
        }

        write_file_schema = {
            "name": "write_file",
            "description": "Write or overwrite content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        }

        tools_override = [
            {"type": "function", "function": create_team_schema},
            {"type": "function", "function": read_file_schema},
            {"type": "function", "function": write_file_schema}
        ]

        result = await run_prism_agent(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            ticker="FLAPPY",
            agent_name="user_chat_self_improving",
            cycle_id="demo_cycle_123",
            priority=Priority.NORMAL,
            tools_override=tools_override,
            max_tokens=4096,
            temperature=0.2,
            timeout_seconds=600
        )
        
        print("\n" + "=" * 80)
        print("COORDINATOR HARNESS RESPONSE:")
        print("=" * 80)
        print(result.get("final_text", ""))
        print("=" * 80)
        print(f"[*] Routed Via: {result.get('routed_via')}")
        print(f"[*] Tokens Used: {result.get('token_usage')}")
        print(f"[*] Execution Time: {result.get('execution_ms')} ms")
        return True
        
    except Exception as e:
        print(f"[!] Importing app stack failed or threw exception: {e}")
        print("[*] Falling back to direct HTTP request to Prism...")
        return False

async def run_harness_via_http():
    """Run the self-improving harness using a direct HTTP call to Prism endpoint."""
    prism_url = load_prism_url()
    print(f"[*] Direct HTTP invocation targeting Prism at: {prism_url}")
    print(f"[*] Running self-improvement coordinator using {MODEL}...")

    # Coordinator system prompt detailing peer-to-peer self-improvement structure
    system_prompt = (
        "You are a coordinator agent. Your task is to coordinate the development of a self-improving "
        "Flappy Bird game using a team of 2 worker agents in a peer-to-peer topology.\n\n"
        "You have access to the `create_team` tool. You MUST invoke `create_team` to launch the worker team.\n"
        "The team name should be 'flappy_improvement'.\n"
        "Configure the topology to 'peer_to_peer'.\n"
        "The team MUST consist of exactly these 2 members:\n"
        "1. developer_worker: A code writer. Its prompt should instruct it to read scratch/flappy_bird.py, "
        "   find any bugs, missing imports, syntax errors, or logical issues, and rewrite the file with clean, "
        "   functional pygame code. It must address any feedback from the critic_worker.\n"
        "2. critic_worker: A code validator. Its prompt should instruct it to review the updated code in "
        "   scratch/flappy_bird.py, run a syntax or code-correctness review, check if pygame logic is "
        "   sound, and write detailed critiques. If the game is fully correct, playable, and complete with no bugs, "
        "   the critic MUST output '[DONE]' in its final response to terminate the peer-to-peer loop.\n\n"
        "Both members MUST specify model='cyankiwi/MiniMax-M2.7-AWQ-4bit'.\n"
        "Once the team has completed their run, review their discussion and outputs, and present the final flappy_bird.py code "
        "along with a summary of the improvements."
    )

    user_prompt = (
        f"Run the self-improvement loop for the Flappy Bird game located at {TARGET_FILE}. "
        "Ensure you call create_team with 2 workers in a peer_to_peer topology to start the process."
    )

    # Tool schemas
    tools = [
        {
            "type": "function",
            "function": {
                "name": "create_team",
                "description": "Spawn worker agents in peer-to-peer topology to write and review code.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "topology": {"type": "string", "enum": ["peer_to_peer"]},
                        "members": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "description": {"type": "string"},
                                    "prompt": {"type": "string"},
                                    "files": {"type": "array", "items": {"type": "string"}},
                                    "model": {"type": "string"},
                                    "agent": {"type": "string"}
                                },
                                "required": ["description", "prompt"]
                            }
                        }
                    },
                    "required": ["name", "members"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the content of a file from the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"}
                    },
                    "required": ["path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write or overwrite content to a file in the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"}
                    },
                    "required": ["path", "content"]
                }
            }
        }
    ]

    payload = {
        "provider": "vllm",
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "maxTokens": 4096,
        "temperature": 0.2,
        "conversationId": f"self-improvement-test-{int(asyncio.get_event_loop().time())}",
        "project": "vllm-trading-bot",
        "username": "lazy-trader",
        "agent": "CUSTOM_user_chat_self_improving",
        "functionCallingEnabled": True,
        "agenticLoopEnabled": True,
        "autoApprove": True,
        "tools": tools,
        "enabledTools": ["create_team", "read_file", "write_file"],
        "systemPrompt": system_prompt,
        "conversationMeta": {
            "title": "Minimax Self-Improving Harness Demo",
            "systemPrompt": system_prompt,
            "settings": {
                "provider": "vllm",
                "model": MODEL
            }
        }
    }

    headers = {
        "Content-Type": "application/json",
        "x-project": "vllm-trading-bot",
        "x-username": "lazy-trader",
    }

    url = f"{prism_url}/agent?stream=false"
    print(f"[*] Dispatching direct request to coordinator agent at {url}...")
    
    async with httpx.AsyncClient(timeout=600.0) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        result_data = response.json()
        
        print("\n" + "=" * 80)
        print("COORDINATOR HTTP RESPONSE:")
        print("=" * 80)
        response_obj = result_data.get("response", result_data)
        choices = response_obj.get("choices", [])
        if choices:
            print(choices[0].get("message", {}).get("content", ""))
        else:
            print(response_obj.get("text") or response_obj.get("content") or json.dumps(response_obj, indent=2))
        print("=" * 80)
        
        usage = response_obj.get("usage", {})
        print(f"[*] Total Tokens Used: {usage.get('total_tokens', 'N/A') or usage.get('totalTokens', 'N/A')}")

async def main():
    # Attempt imports first
    success = await run_prism_agent_check()
    if not success:
        # Fall back to direct HTTP call
        await run_harness_via_http()

async def run_prism_agent_check():
    # Wrap in a function to isolate importing issues
    return await run_harness_via_import()

if __name__ == "__main__":
    asyncio.run(main())
