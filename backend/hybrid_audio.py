import asyncio
from typing import Optional, AsyncGenerator
from elevenlabs_service import ElevenLabsService
from config import ELEVENLABS_VOICE_ID, USE_ELEVENLABS

def _ws_is_alive(ws) -> bool:
    try:
        from websockets.legacy.client import WebSocketClientProtocol
        return ws is not None and not ws.closed
    except Exception:
        return ws is not None

class HybridAudioService:

    def __init__(self, call_id: str, output_format: str = "ulaw_8000", voice_id: Optional[str] = None):
        self.call_id = call_id
        self.use_elevenlabs = USE_ELEVENLABS
        self.output_format = output_format
        self.voice_id = voice_id or ELEVENLABS_VOICE_ID
        self.elevenlabs_service: Optional[ElevenLabsService] = None
        self.is_active = False
        self._stream_lock = asyncio.Lock()
        self._warm_service: Optional[ElevenLabsService] = None
        self._prewarm_ready = asyncio.Event()
        self.response_ready = asyncio.Event()
        self._response_buffer: str = ""
        print(f"[HybridAudio-{call_id}] Initialized (USE_ELEVENLABS={self.use_elevenlabs})")

    async def initialize(self):
        if self.use_elevenlabs:
            print(f"[HybridAudio-{self.call_id}] ElevenLabs enabled")

    async def _prewarm_connection(self):
        if not self.use_elevenlabs:
            return
        try:
            self._prewarm_ready.clear()
            if self._warm_service:
                try:
                    await self._warm_service.close()
                except Exception:
                    pass
                self._warm_service = None
            print(f"[HybridAudio-{self.call_id}] PRE-WARM: Opening fresh ElevenLabs connection")
            warm = ElevenLabsService(voice_id=self.voice_id, output_format=self.output_format)
            await warm.connect()
            self._warm_service = warm
            self._prewarm_ready.set()
            print(f"[HybridAudio-{self.call_id}] PRE-WARM: Ready")
        except Exception as e:
            print(f"[HybridAudio-{self.call_id}] PRE-WARM failed: {e}")
            self._warm_service = None
            self._prewarm_ready.set()

    async def start_new_response(self):
        if not self.use_elevenlabs:
            return
        async with self._stream_lock:
            try:
                if self.elevenlabs_service:
                    try:
                        await self.elevenlabs_service.close()
                    except Exception:
                        pass
                    self.elevenlabs_service = None
                    self.is_active = False

                self._response_buffer = ""

                if not self._prewarm_ready.is_set():
                    print(f"[HybridAudio-{self.call_id}] Waiting for pre-warm (up to 400ms)...")
                    try:
                        await asyncio.wait_for(self._prewarm_ready.wait(), timeout=0.4)
                    except asyncio.TimeoutError:
                        print(f"[HybridAudio-{self.call_id}] Pre-warm timeout — opening fresh connection")

                if self._warm_service and self._warm_service._connected and _ws_is_alive(self._warm_service.ws):
                    self.elevenlabs_service = self._warm_service
                    self._warm_service = None
                    self.is_active = True
                    self.response_ready.set()
                    print(f"[HybridAudio-{self.call_id}] Using pre-warmed connection (zero latency)")
                    return
                else:
                    if self._warm_service:
                        try:
                            await self._warm_service.close()
                        except Exception:
                            pass
                        self._warm_service = None

                self.elevenlabs_service = ElevenLabsService(
                    voice_id=self.voice_id,
                    output_format=self.output_format
                )
                await self.elevenlabs_service.connect()
                self.is_active = True
                self.response_ready.set()
                print(f"[HybridAudio-{self.call_id}] Fresh ElevenLabs connection ready")

            except Exception as e:
                print(f"[HybridAudio-{self.call_id}] start_new_response failed: {e}")
                self.is_active = False

    async def process_text_delta(self, text_delta: str):
        if not (self.use_elevenlabs and self.elevenlabs_service and self.is_active):
            return
        self._response_buffer += text_delta

    async def process_text_complete(self, complete_text: str):
        if not (self.use_elevenlabs and self.elevenlabs_service and self.is_active):
            return
        try:
            text_to_speak = self._response_buffer or complete_text
            self._response_buffer = ""
            print(f"[Hybrid-{self.call_id}] COMPLETE: '{complete_text[:80]}'")
            await self.elevenlabs_service.send_text_chunk(text_to_speak)
            await self.elevenlabs_service.send_text_chunk("", flush=True)
            await self.elevenlabs_service.end_stream()
            print(f"[Hybrid-{self.call_id}] EOS sent — waiting for audio drain")
        except Exception as e:
            print(f"[Hybrid-{self.call_id}] Completion failed: {e}")

    async def get_audio_stream(self) -> AsyncGenerator[str, None]:
        if not (self.use_elevenlabs and self.elevenlabs_service and self.is_active):
            return
        if not _ws_is_alive(self.elevenlabs_service.ws):
            return
        chunk_count = 0
        try:
            print(f"[Hybrid-{self.call_id}] STREAM: Starting audio stream")
            async for audio_chunk in self.elevenlabs_service.get_audio_chunks():
                chunk_count += 1
                yield audio_chunk
            print(f"[Hybrid-{self.call_id}] COMPLETE: Stream finished - {chunk_count} chunks")
        except Exception as e:
            print(f"[Hybrid-{self.call_id}] Stream failed: {e}")
        finally:
            await self.close_current_response()

    async def close_current_response(self):
        if self.elevenlabs_service:
            try:
                await self.elevenlabs_service.close()
                print(f"[Hybrid-{self.call_id}] Response complete, connection closed")
            except Exception as e:
                print(f"[Hybrid-{self.call_id}] Error closing: {e}")
            finally:
                self.elevenlabs_service = None
                self.is_active = False
                self.response_ready.clear()

    async def close(self):
        print(f"[Hybrid-{self.call_id}] Closing HybridAudioService")
        if self._warm_service:
            try:
                await self._warm_service.close()
            except Exception:
                pass
            self._warm_service = None
        self._prewarm_ready.set()
        self.response_ready.set()
        await self.close_current_response()

    def should_suppress_openai_audio(self) -> bool:
        return self.use_elevenlabs and self.is_active