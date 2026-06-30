import { useState, useEffect, useRef } from "react"
import axios from "axios"

const API = "http://localhost:9000"
const WS_BASE = "ws://localhost:9000"

export default function App() {
  const [callId, setCallId] = useState(null)
  const [status, setStatus] = useState("idle")
  const [transcript, setTranscript] = useState([])
  const uiWsRef = useRef(null)
  const mediaWsRef = useRef(null)
  const audioCtxRef = useRef(null)
  const transcriptEndRef = useRef(null)

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [transcript])

  async function startCall() {
    setTranscript([])
    setStatus("connecting")

    const res = await axios.post(`${API}/test-call`)
    const { call_id, ws_url, ui_ws_url } = res.data
    setCallId(call_id)

    // Connect UI WebSocket for transcript
    const uiWs = new WebSocket(ui_ws_url)
    uiWsRef.current = uiWs

    uiWs.onmessage = (e) => {
      const msg = JSON.parse(e.data)
      if (msg.type === "transcript") {
        setTranscript(prev => [...prev, { speaker: msg.speaker, text: msg.text, time: msg.timestamp }])
      }
      if (msg.type === "status" && msg.status === "ended") {
        setStatus("ended")
        stopCall()
      }
    }

    // Setup audio context for µ-law conversion
    audioCtxRef.current = new AudioContext({ sampleRate: 48000 })

    // Connect media WebSocket
    const mediaWs = new WebSocket(ws_url)
    mediaWsRef.current = mediaWs

    // Send Twilio-style handshake
    const streamSid = "MZ" + Math.random().toString(36).slice(2, 34)
    const callSid = "CA" + Math.random().toString(36).slice(2, 34)

    mediaWs.onopen = () => {
      setStatus("active")
      mediaWs.send(JSON.stringify({ event: "connected", protocol: "Call", version: "1.0.0" }))
      mediaWs.send(JSON.stringify({
        event: "start",
        sequenceNumber: "1",
        start: {
          streamSid,
          callSid,
          tracks: ["inbound"],
          mediaFormat: { encoding: "audio/x-mulaw", sampleRate: 8000, channels: 1 }
        },
        streamSid
      }))

      // Start mic capture
      startMic(mediaWs, streamSid)
    }

    mediaWs.onmessage = async (e) => {
      const msg = JSON.parse(e.data)
      if (msg.event === "media") {
        const ulaw = Uint8Array.from(atob(msg.media.payload), c => c.charCodeAt(0))
        playUlaw(ulaw)
      }
      if (msg.event === "clear") {
        // Stop current playback
      }
    }

    mediaWs.onclose = () => {
      if (status !== "ended") setStatus("ended")
    }
  }

  async function startMic(mediaWs, streamSid) {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    const ctx = audioCtxRef.current
    const source = ctx.createMediaStreamSource(stream)
    const processor = ctx.createScriptProcessor(1024, 1, 1)

    let seq = 0
    let ts = 0

    processor.onaudioprocess = (e) => {
      if (mediaWs.readyState !== WebSocket.OPEN) return
      const input = e.inputBuffer.getChannelData(0)
      // Downsample 48k -> 8k
      const downsampled = downsample(input, 48000, 8000)
      const ulaw = pcmToUlaw(downsampled)
      const b64 = btoa(String.fromCharCode(...ulaw))
      mediaWs.send(JSON.stringify({
        event: "media",
        sequenceNumber: String(seq++),
        media: { track: "inbound", chunk: String(seq), timestamp: String(ts), payload: b64 },
        streamSid
      }))
      ts += 20
    }

    source.connect(processor)
    processor.connect(ctx.destination)
  }

  function downsample(buffer, fromRate, toRate) {
    const ratio = fromRate / toRate
    const result = new Float32Array(Math.floor(buffer.length / ratio))
    for (let i = 0; i < result.length; i++) {
      result[i] = buffer[Math.floor(i * ratio)]
    }
    return result
  }

  function pcmToUlaw(samples) {
    const ulaw = new Uint8Array(samples.length)
    for (let i = 0; i < samples.length; i++) {
      let s = Math.max(-1, Math.min(1, samples[i]))
      let pcm = s < 0 ? s * 0x8000 : s * 0x7FFF
      pcm = Math.abs(pcm)
      pcm = Math.min(pcm, 32767)
      const BIAS = 132
      pcm += BIAS
      let exp = 7
      for (let expMask = 0x4000; (pcm & expMask) === 0 && exp > 0; exp--, expMask >>= 1) {}
      const mantissa = (pcm >> (exp + 3)) & 0x0F
      ulaw[i] = ~((s < 0 ? 0 : 0x80) | (exp << 4) | mantissa) & 0xFF
    }
    return ulaw
  }

  function playUlaw(ulaw) {
    const ctx = audioCtxRef.current
    if (!ctx) return
    // Convert µ-law to PCM float
    const pcm = new Float32Array(ulaw.length * 6) // upsample 8k->48k
    for (let i = 0; i < ulaw.length; i++) {
      let u = ~ulaw[i]
      const sign = u & 0x80
      const exp = (u >> 4) & 0x07
      const mantissa = u & 0x0F
      let sample = ((mantissa << 3) + 132) << exp
      sample = sign ? -(sample - 132) : (sample - 132)
      const f = sample / 32768.0
      for (let j = 0; j < 6; j++) pcm[i * 6 + j] = f
    }
    const buffer = ctx.createBuffer(1, pcm.length, 48000)
    buffer.copyToChannel(pcm, 0)
    const source = ctx.createBufferSource()
    source.buffer = buffer
    source.connect(ctx.destination)
    source.start()
  }

  function stopCall() {
    if (mediaWsRef.current) {
      mediaWsRef.current.send(JSON.stringify({ event: "stop", sequenceNumber: "999" }))
      mediaWsRef.current.close()
    }
    if (uiWsRef.current) uiWsRef.current.close()
    if (audioCtxRef.current) audioCtxRef.current.close()
    mediaWsRef.current = null
    uiWsRef.current = null
    audioCtxRef.current = null
  }

  const statusColor = { idle: "#888", connecting: "#f59e0b", active: "#22c55e", ended: "#ef4444" }

  return (
    <div style={{ maxWidth: 700, margin: "40px auto", fontFamily: "sans-serif", padding: "0 20px" }}>
      <h1 style={{ fontSize: 24, fontWeight: 600, marginBottom: 8 }}>HBAG Home Services</h1>
      <p style={{ color: "#666", marginBottom: 24 }}>Booking Agent — Live Test</p>

      <div style={{ display: "flex", alignItems: "center", gap: 16, marginBottom: 32 }}>
        <button
          onClick={status === "active" ? stopCall : startCall}
          disabled={status === "connecting"}
          style={{
            padding: "10px 28px",
            borderRadius: 8,
            border: "none",
            background: status === "active" ? "#ef4444" : "#2563eb",
            color: "#fff",
            fontSize: 15,
            fontWeight: 500,
            cursor: status === "connecting" ? "not-allowed" : "pointer"
          }}
        >
          {status === "active" ? "End Call" : status === "connecting" ? "Connecting..." : "Start Test Call"}
        </button>

        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <div style={{ width: 10, height: 10, borderRadius: "50%", background: statusColor[status] }} />
          <span style={{ color: "#666", fontSize: 14, textTransform: "capitalize" }}>{status}</span>
        </div>
      </div>

      <div style={{
        background: "#f8f8f8",
        borderRadius: 12,
        padding: 20,
        minHeight: 400,
        maxHeight: 500,
        overflowY: "auto",
        border: "1px solid #e5e5e5"
      }}>
        {transcript.length === 0 && (
          <p style={{ color: "#aaa", textAlign: "center", marginTop: 160 }}>
            Start a call to see the conversation here
          </p>
        )}
        {transcript.map((msg, i) => (
          <div key={i} style={{
            marginBottom: 16,
            display: "flex",
            flexDirection: "column",
            alignItems: msg.speaker === "agent" ? "flex-start" : "flex-end"
          }}>
            <span style={{ fontSize: 11, color: "#999", marginBottom: 4 }}>
              {msg.speaker === "agent" ? "Agent" : "You"} · {new Date(msg.time).toLocaleTimeString()}
            </span>
            <div style={{
              background: msg.speaker === "agent" ? "#fff" : "#2563eb",
              color: msg.speaker === "agent" ? "#1a1a1a" : "#fff",
              padding: "10px 14px",
              borderRadius: msg.speaker === "agent" ? "4px 12px 12px 12px" : "12px 4px 12px 12px",
              maxWidth: "80%",
              fontSize: 14,
              lineHeight: 1.5,
              boxShadow: "0 1px 3px rgba(0,0,0,0.08)"
            }}>
              {msg.text}
            </div>
          </div>
        ))}
        <div ref={transcriptEndRef} />
      </div>
    </div>
  )
}