import argparse
import asyncio
import base64
from fractions import Fraction
import json
import logging
import os
import ssl
import uuid
from aiortc.mediastreams import AUDIO_PTIME
import websockets
import time
import av
import numpy as np
import io
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from datetime import datetime

TIME_SPAN = 0.02
SAMPLE_RATE_OUT = 48000
SAMPLE_RATE_IN = 24000
SAMPLE_COUNT_OUT = int(TIME_SPAN * SAMPLE_RATE_OUT)
SAMPLE_COUNT_IN = int(TIME_SPAN * SAMPLE_RATE_IN)


ROOT = os.path.dirname(__file__)

logger = logging.getLogger("server")
pcs = set()
last_openai_response = None


# Environment variables
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# OpenAI Realtime API configuration
OPENAI_WS_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2025-08-28"
OPENAI_HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "OpenAI-Beta": "realtime=v1"
}

# Instructions for the AI assistant
AI_INSTRUCTIONS = open("preprompt.txt", "r").read() + "\n\nСегодня " + datetime.now().strftime("%d.%m.%Y")

class CustomAudioTrack(MediaStreamTrack):
    """
    Custom audio track that receives frames from a queue (fed by OpenAI responses).
    """
    kind = "audio"
    _count: int = 0
    _start: float = 0

    _last_sent_time: float = 0

    def __init__(self):
        super().__init__()
        self._queue = asyncio.Queue()

    async def recv(self):
        if self._start == 0:
            self._start = time.time()
        frame = await self._queue.get()
        frame.pts = SAMPLE_COUNT_OUT*self._count
        frame.time_base = Fraction(1, SAMPLE_RATE_OUT)
        
        self._count += 1
        
        delta = TIME_SPAN - (time.time() - self._last_sent_time) - 0.001
        if delta > 0:
            await asyncio.sleep(delta)
        self._last_sent_time = time.time()
        return frame



resampler = av.AudioResampler(format='s16', layout='stereo', rate=48000)


async def process_audio_from_openai(queue, out_track: CustomAudioTrack):
    BYTES_PER_SAMPLE = 2
    CHANNELS = 1
    chunk_size = int(SAMPLE_RATE_IN*TIME_SPAN*BYTES_PER_SAMPLE*CHANNELS)
    while True:
        audio_base64 = await queue.get()
        audio = base64.b64decode(audio_base64)
    
        for i in range(0, len(audio), chunk_size):
            audio_delta = audio[i:i + chunk_size]
            
            samples = np.frombuffer(audio_delta, dtype=np.int16)
            
            stereo_audio = np.column_stack((samples, samples))
            interleaved = stereo_audio.ravel()
            packed_audio = interleaved[np.newaxis, :]

            frame = av.AudioFrame.from_ndarray(packed_audio, format='s16', layout='stereo')
            frame.sample_rate = SAMPLE_RATE_IN 
                
            frames = resampler.resample(frame)
            for resampled in frames:
                await out_track._queue.put(resampled)

async def handle_ws_recv_from_openai(ws_to_openai, out_track: CustomAudioTrack):
    """
    Handle incoming messages from OpenAI WebSocket, process audio deltas.
    """
    audio_from_openai_queue = asyncio.Queue()
    
    task =asyncio.create_task(process_audio_from_openai(audio_from_openai_queue, out_track))
    
    while True:
        try:
            message = await ws_to_openai.recv()
            event = json.loads(message)
            event_type = event.get("type")
            
            if event_type == "response.audio.delta":
                #logger.info(f"response.audio.delta")
                await audio_from_openai_queue.put(event["delta"])
            elif event_type == "input_audio_buffer.speech_started":
                while not out_track._queue.empty():
                    try:
                        out_track._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                while not audio_from_openai_queue.empty():
                    try:
                        audio_from_openai_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                continue
            elif event_type == "response.audio.done":
                continue
            elif event_type == "error":
                logger.error(f"OpenAI error: {event}")
            elif event_type == "response.audio_transcript.delta":
                continue
            elif event_type == "response.audio_transcript.done":
                logger.info(f"LLM said: {event.get('transcript')}")
            elif event_type == "conversation.item.input_audio_transcription.delta":
                continue
            elif event_type == "conversation.item.input_audio_transcription.completed":
                logger.info(f"You said: {event.get('transcript')}")
            #else:
            #    logger.info(f"Get event type: {event_type}")

        except websockets.exceptions.ConnectionClosed:
            logger.info("OpenAI connection closed.")
            break
        except Exception as e:
            logger.error(f"Error in WS receive: {e}")
            break
    task.cancel()

@staticmethod
def generate_wav_header(data_size: int, sample_rate: int = 24000, channels: int = 1, bits: int = 16) -> bytes:
    byte_rate = sample_rate * channels * (bits // 8)
    block_align = channels * (bits // 8)
    file_size = data_size + 44 - 8
    header = (
        b'RIFF' +
        file_size.to_bytes(4, 'little') +
        b'WAVEfmt ' +
        (16).to_bytes(4, 'little') +
        (1).to_bytes(2, 'little') +
        channels.to_bytes(2, 'little') +
        sample_rate.to_bytes(4, 'little') +
        byte_rate.to_bytes(4, 'little') +
        block_align.to_bytes(2, 'little') +
        bits.to_bytes(2, 'little') +
        b'data' +
        data_size.to_bytes(4, 'little')
    )
    return header


async def process_audio_from_client(in_track, ws):
    """
    Process incoming WebRTC audio frames, resample, and send to OpenAI.
    """
    # Resampler for WebRTC (48kHz) to OpenAI (24kHz mono s16)
    resampler = av.audio.resampler.AudioResampler(
        format="s16",  # Signed 16-bit
        layout="mono",
        rate=24000
    )
    while True:
        try:
            frame = await in_track.recv() #type: av.AudioFrame
            if not isinstance(frame, av.AudioFrame):
                break
            
            frame.pts = None  # Reset for resampler
            resampled_frames = resampler.resample(frame)

            if resampled_frames:
                    for resampled in resampled_frames:
                        # Convert to bytes (PCM s16 mono)
                        pcm_data = resampled.to_ndarray().tobytes()
                        b64_audio = base64.b64encode(pcm_data).decode("utf-8")

                        
                        # Send to OpenAI
                        append_msg = {
                            "type": "input_audio_buffer.append",
                            "audio": b64_audio
                        }
                        dumped = json.dumps(append_msg)
                        await ws.send(dumped)
        except websockets.exceptions.ConnectionClosed:
            logger.info("OpenAI connection closed.")
            break
        except Exception as e:
            logger.error(f"Error processing audio: {e}")
            break
        

async def index(request):
    content = open(os.path.join(ROOT, "static/index.html"), "r").read()
    return web.Response(content_type="text/html", text=content)


async def javascript(request):
    content = open(os.path.join(ROOT, "static/client.js"), "r").read()
    return web.Response(content_type="application/javascript", text=content)


async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pcs.add(pc)
    

    # Connect to OpenAI WebSocket
    ws_to_openai = await websockets.connect(OPENAI_WS_URL, extra_headers=OPENAI_HEADERS)
    if ws_to_openai is not None:
        response = json.loads(await ws_to_openai.recv())
        if response.get("type") == "error":
            logger.error(f"OpenAI error: {response}")
            return web.Response(content_type="application/json", text="{error: '3rd party service connection error'}")
    
        # Update session configuration
        session_update = {
                            "modalities": ["text", "audio"],
                            "instructions": AI_INSTRUCTIONS,
                            "voice": "marin",
                            "input_audio_noise_reduction": {"type":"near_field"},
                            "input_audio_transcription": {
                                "model": "gpt-4o-transcribe",
                                "language": "ru"
                            },
                            "turn_detection": {
                                "type": "semantic_vad",
                                #"threshold": 0.6,
                                #"prefix_padding_ms": 500,
                                #"silence_duration_ms": 1500,
                                "create_response": True,
                                "interrupt_response": True
                            },
                            "input_audio_format": "pcm16",
                            "output_audio_format": "pcm16",
                            "max_response_output_tokens": 4096
                        }
        event = {
                "type": "session.update",
                "session": session_update
            }
        await ws_to_openai.send(json.dumps(event))
        response = json.loads(await ws_to_openai.recv())
        if response.get("type") == "error":
            logger.error(f"OpenAI error: {response}")
            return web.Response(content_type="application/json", text="{error: '3rd party service connection error'}")


        async def ping_openai(ws):
                while ws is not None and not ws.closed:
                    try:
                        await ws.ping()
                        await asyncio.sleep(15)
                    except Exception as e:
                        logger.error(f"OpenAI ping error: {str(e)}")
                        break
        ping_task = asyncio.create_task(ping_openai(ws_to_openai))
    # Create custom output track for AI audio responses
    out_track = CustomAudioTrack()

    # Start handling OpenAI responses
    asyncio.create_task(handle_ws_recv_from_openai(ws_to_openai, out_track))

    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(message):
            if isinstance(message, str) and message.startswith("ping"):
                channel.send("pong" + message[4:])

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.info("Connection state is %s", pc.connectionState)
        if pc.connectionState == "failed" or pc.connectionState == "closed":
            await pc.close()
            pcs.discard(pc)
            if ping_task is not None:
                ping_task.cancel()
            if ws_to_openai is not None:
                await ws_to_openai.close()
                logger.info("OpenAI connection closed.")

    @pc.on("track")
    def on_track(track):
        logger.info("Track %s received", track.kind)

        if track.kind == "audio":
            asyncio.create_task(process_audio_from_client(track, ws_to_openai))
            # Add output track back to client
            pc.addTrack(out_track)

        @track.on("ended")
        async def on_ended():
            logger.info("Track %s ended", track.kind)
            

    # handle offer
    await pc.setRemoteDescription(offer)

    # send answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
        ),
    )


async def on_shutdown(app):
    # close peer connections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WebRTC audio / video / data-channels demo"
    )
    parser.add_argument("--cert-file", help="SSL certificate file (for HTTPS)")
    parser.add_argument("--key-file", help="SSL key file (for HTTPS)")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port for HTTP server (default: 8080)"
    )
    parser.add_argument("--record-to", help="Write received media to a file.")
    parser.add_argument("--verbose", "-v", action="count")
    args = parser.parse_args()

    if args.verbose or True:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.ERROR)

    if args.cert_file:
        ssl_context = ssl.SSLContext()
        ssl_context.load_cert_chain(args.cert_file, args.key_file)
    else:
        ssl_context = None

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/client.js", javascript)
    app.router.add_post("/offer", offer)
    web.run_app(
        app, access_log=None, host=args.host, port=args.port, ssl_context=ssl_context
    )