import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Dict

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from websockets.legacy.client import WebSocketClientProtocol

from config import OPENAI_API_KEY, BASE_URL, OPENAI_MODEL, BOOKING_PROMPT, TWILIO_FROM_NUMBER
from tools import TOOLS, TOOLS_CONFIG
from hybrid_audio import HybridAudioService

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

active_calls: Dict[str, dict] = {}
ui_connections: Dict[str, WebSocket] = {}


async def broadcast(call_id: str, data: dict):
    ws = ui_connections.get(call_id)
    if ws:
        try:
            await ws.send_json(data)
        except Exception:
            pass


@app.post("/incoming-call")
async def incoming_call(request: Request):
    form = dict(await request.form())
    call_id = str(uuid.uuid4())
    active_calls[call_id] = {
        "call_sid": form.get("CallSid"),
        "from": form.get("From", ""),
        "to": form.get("To", ""),
        "started_at": datetime.utcnow().isoformat()
    }
    base = BASE_URL.replace("https://", "").replace("http://", "").rstrip("/")
    from twilio.twiml.voice_response import VoiceResponse, Connect
    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=f"wss://{base}/media-stream/{call_id}")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.post("/test-call")
async def test_call():
    call_id = str(uuid.uuid4())
    active_calls[call_id] = {
        "call_sid": f"TEST-{call_id[:8]}",
        "from": "+923001234567",
        "to": TWILIO_FROM_NUMBER,
        "started_at": datetime.utcnow().isoformat()
    }
    base = BASE_URL.replace("https://", "").replace("http://", "").rstrip("/")
    return {
        "call_id": call_id,
        "ws_url": f"wss://{base}/media-stream/{call_id}",
        "ui_ws_url": f"wss://{base}/ui-stream/{call_id}"
    }


@app.websocket("/ui-stream/{call_id}")
async def ui_stream(websocket: WebSocket, call_id: str):
    await websocket.accept()
    ui_connections[call_id] = websocket
    try:
        while True:
            await asyncio.sleep(1)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        ui_connections.pop(call_id, None)


@app.websocket("/media-stream/{call_id}")
async def media_stream(websocket: WebSocket, call_id: str):
    await websocket.accept()

    if call_id not in active_calls:
        await websocket.close()
        return

    hybrid_audio = HybridAudioService(call_id=call_id, output_format="ulaw_8000")
    await hybrid_audio.initialize()

    # Connect to OpenAI Realtime
    openai_ws: WebSocketClientProtocol = await websockets.connect(
        f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}",
        additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        close_timeout=10,
        open_timeout=20,
    )

    # Initialize OpenAI session
    system_message = BOOKING_PROMPT.replace("{call_id}", call_id)
    await openai_ws.send(json.dumps({
        "type": "session.update",
        "session": {
            "instructions": system_message,
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.88,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 700,
                "create_response": True,
            },
            "tools": TOOLS_CONFIG,
            "tool_choice": "auto",
            "voice": "alloy",
        }
    }))

    # Trigger greeting
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text",
                         "text": "[System: The customer has just called. Begin with your greeting now.]"}]
        }
    }))
    await openai_ws.send(json.dumps({"type": "response.create"}))

    stream_sid = None
    agent_is_speaking = False
    assistant_transcript = ""

    async def receive_from_client():
        nonlocal stream_sid, agent_is_speaking
        try:
            async for message in websocket.iter_text():
                data = json.loads(message)
                event = data.get("event")
                if event == "media":
                    if not agent_is_speaking:
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"],
                        }))
                elif event == "start":
                    stream_sid = data["start"]["streamSid"]
                elif event == "stop":
                    break
        except Exception as e:
            print(f"[receive_from_client] {e}")
        finally:
            if not openai_ws.closed:
                await openai_ws.close()

    async def stream_elevenlabs_to_client():
        nonlocal agent_is_speaking
        while True:
            try:
                await asyncio.wait_for(hybrid_audio.response_ready.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if not (hybrid_audio.is_active and hybrid_audio.elevenlabs_service and stream_sid):
                continue

            async for audio_chunk in hybrid_audio.get_audio_stream():
                try:
                    await websocket.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": audio_chunk},
                    })
                except Exception:
                    break

            agent_is_speaking = False

    async def process_openai():
        nonlocal agent_is_speaking, assistant_transcript, stream_sid

        try:
            async for message in openai_ws:
                response = json.loads(message)
                event_type = response.get("type", "")

                if event_type == "response.content_part.added":
                    agent_is_speaking = True
                    await hybrid_audio.start_new_response()
                    await broadcast(call_id, {"type": "agent_speaking", "speaking": True})

                elif event_type == "response.output_audio_transcript.delta":
                    delta = response.get("delta", "")
                    assistant_transcript += delta
                    await hybrid_audio.process_text_delta(delta)

                elif event_type == "response.output_audio_transcript.done":
                    if assistant_transcript.strip():
                        await hybrid_audio.process_text_complete(assistant_transcript.strip())
                        await broadcast(call_id, {
                            "type": "transcript",
                            "speaker": "agent",
                            "text": assistant_transcript.strip(),
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })
                    assistant_transcript = ""

                elif event_type == "conversation.item.input_audio_transcription.completed":
                    transcript = response.get("transcript", "").strip()
                    if transcript:
                        await broadcast(call_id, {
                            "type": "transcript",
                            "speaker": "user",
                            "text": transcript,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })

                elif event_type == "input_audio_buffer.speech_started":
                    agent_is_speaking = False
                    if stream_sid:
                        try:
                            await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                        except Exception:
                            pass
                    if hybrid_audio.use_elevenlabs:
                        asyncio.create_task(hybrid_audio._prewarm_connection())
                    await broadcast(call_id, {"type": "user_speaking"})

                elif event_type == "response.output_item.added":
                    item = response.get("item", {})
                    if item.get("type") == "function_call":
                        pass

                elif event_type == "response.function_call_arguments.done":
                    tool_name = response.get("name")
                    args_str = response.get("arguments", "{}")
                    try:
                        params = json.loads(args_str)
                    except Exception:
                        params = {}

                    if "call_id" not in params:
                        params["call_id"] = call_id

                    tool_fn = TOOLS.get(tool_name)
                    if tool_fn:
                        result = await tool_fn(**params)
                        await broadcast(call_id, {
                            "type": "tool_call",
                            "tool": tool_name,
                            "params": params,
                            "result": result
                        })

                        if tool_name == "end_call":
                            await broadcast(call_id, {"type": "status", "status": "ended"})
                            await asyncio.sleep(7.0)
                            try:
                                await websocket.close()
                            except Exception:
                                pass
                            return

                        tool_call_id = response.get("call_id", str(uuid.uuid4()))
                        await openai_ws.send(json.dumps({
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output",
                                "call_id": tool_call_id,
                                "output": json.dumps(result)
                            }
                        }))
                        await openai_ws.send(json.dumps({"type": "response.create"}))

        except Exception as e:
            print(f"[process_openai] {e}")

    tasks = [
        asyncio.create_task(receive_from_client()),
        asyncio.create_task(process_openai()),
        asyncio.create_task(stream_elevenlabs_to_client()),
    ]

    await asyncio.gather(*tasks)

    await hybrid_audio.close()
    active_calls.pop(call_id, None)
    ui_connections.pop(call_id, None)
    print(f"[media_stream] Call {call_id} ended and cleaned up")