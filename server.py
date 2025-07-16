import argparse
import asyncio
import base64
import json
import logging
import os
import ssl
import uuid
import websockets
import time
import av
import numpy as np
import io
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay


ROOT = os.path.dirname(__file__)

logger = logging.getLogger("pc")
pcs = set()
last_openai_response = None


# Environment variables
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# OpenAI Realtime API configuration
OPENAI_WS_URL = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2025-06-03"
OPENAI_HEADERS = {
    "Authorization": f"Bearer {OPENAI_API_KEY}",
    "OpenAI-Beta": "realtime=v1"
}

# Instructions for the AI assistant
AI_INSTRUCTIONS = "You are a helpful AI assistant. Respond conversationally to user input."

class CustomAudioTrack(MediaStreamTrack):
    """
    Custom audio track that receives frames from a queue (fed by OpenAI responses).
    """
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._queue = asyncio.Queue()

    async def recv(self):
        frame = await self._queue.get()
        return frame

import av
import numpy as np


async def handle_ws_recv_from_openai(ws, out_track):
    """
    Handle incoming messages from OpenAI WebSocket, process audio deltas.
    """
    # Resampler for OpenAI (24kHz stereo) to WebRTC (typically 48kHz stereo)
    resampler = av.AudioResampler(format='s16', layout='stereo', rate=48000)
    pts = 0
    time_delta = int(0.02*24000)
    while True:
        try:
            message = await ws.recv()
            event = json.loads(message)
            event_type = event.get("type")
            
            if event_type == "response.audio.delta":
                # Decode base64 audio delta (PCM 16-bit 24kHz mono)
                audio_delta = base64.b64decode(event["delta"])
                samples = np.frombuffer(audio_delta, dtype=np.int16)
                
                stereo_audio = np.column_stack((samples, samples))
                interleaved = stereo_audio.ravel()
                packed_audio = interleaved[np.newaxis, :]

                frame = av.AudioFrame.from_ndarray(packed_audio, format='s16', layout='stereo')
                frame.sample_rate = 24000 
                    
                frames = resampler.resample(frame)
                for resampled in frames:
                    resampled.pts = pts
                    pts += time_delta
                    await out_track._queue.put(resampled)


            elif event_type == "response.audio.done":
                logger.info("Audio response complete.")
                # Optionally flush or handle end of response

            elif event_type == "error":
                logger.error(f"OpenAI error: {event}")

        except websockets.exceptions.ConnectionClosed:
            break
        except Exception as e:
            logger.error(f"Error in WS receive: {e}")
            break

@staticmethod
def generate_wav_header(data_size: int) -> bytes:
    sample_rate = 24000
    channels = 1
    bits = 16
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
    #session_pcm_buffer = io.BytesIO()
    while True:
        try:
            frame = await in_track.recv() #type: av.AudioFrame
            if not isinstance(frame, av.AudioFrame):
                continue
            
            frame.pts = None  # Reset for resampler
            resampled_frames = resampler.resample(frame)

            if resampled_frames:
                    for resampled in resampled_frames:
                        # Convert to bytes (PCM s16 mono)
                        pcm_data = resampled.to_ndarray().tobytes()
                        #session_pcm_buffer.write(pcm_data)
                        b64_audio = base64.b64encode(pcm_data).decode("utf-8")

                        
                        # Send to OpenAI
                        append_msg = {
                            "type": "input_audio_buffer.append",
                            "audio": b64_audio
                        }
                        dumped = json.dumps(append_msg)
                        await ws.send(dumped)

        except Exception as e:
            logger.error(f"Error processing audio: {e}")
            break
        #finally:
        #   logger.info("Audio processing loop ended unexpectedly")
    #data = session_pcm_buffer.getvalue()
    #header = generate_wav_header(len(data))
    #with open(f"input_record_last.wav", "wb") as f:
    #    f.write(header + data)

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
    pc_id = "PeerConnection(%s)" % uuid.uuid4()
    pcs.add(pc)

    def log_info(msg, *args):
        logger.info(pc_id + " " + msg, *args)

    log_info("Created for %s", request.remote)

    # Connect to OpenAI WebSocket
    ws = await websockets.connect(OPENAI_WS_URL, extra_headers=OPENAI_HEADERS)
    logger.info(f"received OpenAI session created: {await ws.recv()}")
    # Update session configuration
    session_update = {
                        "modalities": ["text", "audio"],
                        "instructions": AI_INSTRUCTIONS,
                        "voice": "alloy",
                        "input_audio_noise_reduction": {"type":"near_field"},
                        "input_audio_transcription": {
                            "model": "gpt-4o-transcribe",
                            "language": "ru"
                        },
                        "turn_detection": {
                            "type": "server_vad",
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
    await ws.send(json.dumps(event))
    logger.info(f"received OpenAI session update: {await ws.recv()}")


    async def check_openai_response_timeout(ws):
            while ws is not None and not ws.closed:
                try:
                    await ws.ping()
                    await asyncio.sleep(15)
                except Exception as e:
                    logger.error(f"OpenAI ping error: {str(e)}")
                    break
    #ping_task = asyncio.create_task(check_openai_response_timeout(ws))
    # Create custom output track for AI audio responses
    out_track = CustomAudioTrack()

    # Start handling OpenAI responses
    asyncio.create_task(handle_ws_recv_from_openai(ws, out_track))

    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(message):
            if isinstance(message, str) and message.startswith("ping"):
                channel.send("pong" + message[4:])

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        log_info("Connection state is %s", pc.connectionState)
        if pc.connectionState == "failed" or pc.connectionState == "closed":
            await pc.close()
            pcs.discard(pc)
            ping_task.cancel()
            ws.close()

    @pc.on("track")
    def on_track(track):
        log_info("Track %s received", track.kind)

        if track.kind == "audio":
            asyncio.create_task(process_audio_from_client(track, ws))
            # Add output track back to client
            pc.addTrack(out_track)

        @track.on("ended")
        async def on_ended():
            log_info("Track %s ended", track.kind)
            

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

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

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