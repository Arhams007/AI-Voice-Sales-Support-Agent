import asyncio
import json
import re
import websockets
from typing import Optional
from config import ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL

class ElevenLabsService:

    _SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[.!?])$")

    def __init__(self, voice_id=None, model_id=None, output_format="ulaw_8000"):
        self.api_key = ELEVENLABS_API_KEY
        self.voice_id = voice_id or ELEVENLABS_VOICE_ID
        self.model = model_id or ELEVENLABS_MODEL

        format_map = {
            "pcm16": "pcm_24000",
            "ulaw_8000": "ulaw_8000",
            "pcm_16000": "pcm_16000",
            "pcm_24000": "pcm_24000",
        }
        self.output_format = format_map.get(output_format, "ulaw_8000")
        self.ws = None
        self.text_buffer = ""
        self.sentence_delimiters = re.compile(r'[.!?]\s+|[.!?]$')
        self._connected = False
        self._audio_queue = asyncio.Queue()
        self._receiver_task = None

    async def connect(self):
        uri = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}"
            f"/stream-input?model_id={self.model}&output_format={self.output_format}"
        )
        print(f"[ElevenLabs] Connecting to: {uri}")
        self.ws = await websockets.connect(uri)
        await self.ws.send(json.dumps({
            "text": " ",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.8, "use_speaker_boost": True},
            "xi_api_key": self.api_key,
        }))
        self._connected = True
        self._receiver_task = asyncio.create_task(self._receive_loop())
        print("[ElevenLabs] Connected and initialized successfully")
        return self.ws

    async def send_text_chunk(self, text_chunk: str, flush=False):
        if not self.ws:
            return
        self.text_buffer += text_chunk
        sentences = self.sentence_delimiters.split(self.text_buffer)
        if len(sentences) > 1 or flush:
            for sentence in sentences[:-1]:
                if sentence.strip():
                    await self.ws.send(json.dumps({
                        "text": sentence.strip() + ". ",
                        "try_trigger_generation": True,
                    }))
            if flush and sentences[-1].strip():
                await self.ws.send(json.dumps({
                    "text": sentences[-1].strip(),
                    "try_trigger_generation": True,
                }))
                self.text_buffer = ""
            else:
                self.text_buffer = sentences[-1]

    async def end_stream(self):
        if self.ws:
            if self.text_buffer.strip():
                await self.ws.send(json.dumps({
                    "text": self.text_buffer,
                    "try_trigger_generation": True,
                }))
                self.text_buffer = ""
            try:
                await self.ws.send(json.dumps({"text": ""}))
                print("[ElevenLabs] EOS sent")
            except Exception as e:
                print(f"[ElevenLabs] EOS failed: {e}")

    async def _receive_loop(self):
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                audio = data.get("audio")
                if audio:
                    await self._audio_queue.put(audio)
                if data.get("isFinal"):
                    await self._audio_queue.put(None)
        except websockets.exceptions.ConnectionClosed:
            print("[ElevenLabs] WebSocket closed in receiver loop")
        except Exception as e:
            print(f"[ElevenLabs] Receiver error: {e}")
        finally:
            await self._audio_queue.put(None)

    async def get_audio_chunks(self):
        while True:
            chunk = await self._audio_queue.get()
            if chunk is None:
                break
            yield chunk

    async def close(self):
        self._connected = False
        if self._receiver_task and not self._receiver_task.done():
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            try:
                await self.end_stream()
                await self.ws.close()
            except Exception:
                pass
        self.ws = None
        self.text_buffer = ""
        print("[ElevenLabs] Service closed")