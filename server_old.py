import argparse
import asyncio
from fractions import Fraction
import json
import logging
import os
from re import S
import ssl
import uuid
import time
import av
import numpy as np
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription


ROOT = os.path.dirname(__file__)

logger = logging.getLogger("pc")
pcs = set()
last_openai_response = None

TIME_SPAN = 0.02
SAMPLE_RATE_OUT = 48000
SAMPLE_RATE_IN = 24000
SAMPLE_COUNT_OUT = int(TIME_SPAN * SAMPLE_RATE_OUT)
SAMPLE_COUNT_IN = int(TIME_SPAN * SAMPLE_RATE_IN)


class CustomAudioTrack(MediaStreamTrack):
    """
    Custom audio track that receives frames from a queue (fed by OpenAI responses).
    """
    kind = "audio"
    _count: int = 0
    _start: float = 0
    _prev_time: float = 0

    def __init__(self):
        super().__init__()
        self._queue = asyncio.Queue()

    async def recv(self):
        if self._start == 0:
            self._start = time.time()
        if self._prev_time == 0:
            self._prev_time = time.time()
        
        frame = await self._queue.get()
        frame.pts = SAMPLE_COUNT_OUT*self._count
        frame.time_base = Fraction(1, SAMPLE_RATE_OUT)
        
        self._count += 1
        
        wait = self._start + (self._count+1) * TIME_SPAN - time.time()
        if wait>0:
            await asyncio.sleep(wait)
        
        return frame



async def send_audio(out_track: CustomAudioTrack):
    chunk_size = SAMPLE_COUNT_IN * 2 #two bytes per sample

    with open("audio.pcm", "rb") as f:
        audio = f.read()
    resampler = av.AudioResampler(format='s16', layout='stereo', rate=SAMPLE_RATE_OUT)
    for i in range(0, len(audio), chunk_size):
        audio_delta = audio[i:i + chunk_size]
        
        samples = np.frombuffer(audio_delta, dtype=np.int16)
        
        #if len(samples) < chunk_size:
        #    samples = np.pad(samples, (0, chunk_size - len(samples)), mode='constant')
        
        stereo_audio = np.column_stack((samples, samples))
        interleaved = stereo_audio.ravel()
        packed_audio = interleaved[np.newaxis, :]

        frame = av.AudioFrame.from_ndarray(packed_audio, format='s16', layout='stereo')
        frame.sample_rate = 24000 
        
        logger.info(f"frame before resampler: {frame.samples}")
        frames = resampler.resample(frame)
        logger.info(f"frame after resampler: {frames[0].samples}")
        for resampled in frames:
            logger.info(f"resampled: {resampled.pts} {resampled.time_base}")
            await out_track._queue.put(resampled)
 

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

    
    out_track = CustomAudioTrack()

    
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
            

    @pc.on("track")
    def on_track(track):
        log_info("Track %s received", track.kind)

        if track.kind == "audio":
            # Add output track back to client
            pc.addTrack(out_track)
            time.sleep(1)
            asyncio.create_task(send_audio(out_track))


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