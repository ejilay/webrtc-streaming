import asyncio
import json
import uuid
import av
from aiortc import RTCConfiguration, RTCPeerConnection, VideoStreamTrack, AudioStreamTrack
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Dict, Optional

app = FastAPI()

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Dictionary to store active sessions
sessions: Dict[str, RTCPeerConnection] = {}

# Pydantic models for request validation
class SDPPayload(BaseModel):
    sdp: str
    type: str

class ICECandidatePayload(BaseModel):
    candidate: str
    sdpMid: Optional[str]
    sdpMLineIndex: Optional[int]
    usernameFragment: Optional[str]

# Generate unique session IDs
def generate_session_id() -> str:
    return str(uuid.uuid4())

# Video track that loops a video file
class LoopingVideoStreamTrack(VideoStreamTrack):
    def __init__(self, container):
        super().__init__()
        self.container = container
        self.stream = container.streams.video[0]
        self.frame_iter = container.decode(self.stream)
        self.loop_counter = 0
        self.duration = self.stream.duration * self.stream.time_base

    async def recv(self):
        try:
            frame = next(self.frame_iter)
        except StopIteration:
            self.loop_counter += 1
            self.container.seek(0)
            self.frame_iter = self.container.decode(self.stream)
            frame = next(self.frame_iter)
        frame.pts = frame.pts + self.loop_counter * self.duration
        return frame

# Audio track that loops audio from the same video file
class LoopingAudioStreamTrack(AudioStreamTrack):
    def __init__(self, container):
        super().__init__()
        self.container = container
        self.stream = container.streams.audio[0]
        self.frame_iter = container.decode(self.stream)
        self.loop_counter = 0
        self.duration = self.stream.duration * self.stream.time_base

    async def recv(self):
        try:
            frame = next(self.frame_iter)
        except StopIteration:
            self.loop_counter += 1
            self.container.seek(0)
            self.frame_iter = self.container.decode(self.stream)
            frame = next(self.frame_iter)
        frame.pts = frame.pts + self.loop_counter * self.duration
        return frame

# Video track that blends two video files
class BlendedVideoStreamTrack(VideoStreamTrack):
    def __init__(self, file1: str, file2: str):
        super().__init__()
        self.container1 = av.open(file1)
        self.container2 = av.open(file2)
        self.stream1 = self.container1.streams.video[0]
        self.stream2 = self.container2.streams.video[0]
        self.frame_iter1 = self.container1.decode(self.stream1)
        self.frame_iter2 = self.container2.decode(self.stream2)

    async def recv(self):
        try:
            frame1 = next(self.frame_iter1)
            frame2 = next(self.frame_iter2)
        except StopIteration:
            # Loop both videos
            self.container1.seek(0)
            self.container2.seek(0)
            self.frame_iter1 = self.container1.decode(self.stream1)
            self.frame_iter2 = self.container2.decode(self.stream2)
            frame1 = next(self.frame_iter1)
            frame2 = next(self.frame_iter2)
        # Blend frames by averaging pixel values
        blended_array = (frame1.to_ndarray() + frame2.to_ndarray()) / 2
        return av.VideoFrame.from_ndarray(blended_array.astype('uint8'), format='rgb24')

# Endpoint to create a new session for single video streaming
@app.post("/create_session")
async def create_session():
    session_id = generate_session_id()
    pc = RTCPeerConnection()
    sessions[session_id] = pc
    container = av.open('video.mp4')
    video_track = LoopingVideoStreamTrack(container)
    audio_track = LoopingAudioStreamTrack(container)
    pc.addTrack(video_track)
    pc.addTrack(audio_track)

    # Store ICE candidates for this session
    ice_candidates = []

    @pc.on("icecandidate")
    def on_icecandidate(event):
        if event.candidate:
            ice_candidates.append({
                "candidate": event.candidate.candidate,
                "sdpMid": event.candidate.sdpMid,
                "sdpMLineIndex": event.candidate.sdpMLineIndex,
                "usernameFragment": event.candidate.usernameFragment
            })

    return {"session_id": session_id}

# Endpoint to create a new session for blended video streaming
@app.post("/create_blended_session")
async def create_blended_session():
    session_id = generate_session_id()
    pc = RTCPeerConnection()
    sessions[session_id] = pc
    blended_track = BlendedVideoStreamTrack('video1.mp4', 'video2.mp4')
    pc.addTrack(blended_track)

    # Store ICE candidates for this session
    ice_candidates = []

    @pc.on("icecandidate")
    def on_icecandidate(event):
        if event.candidate:
            ice_candidates.append({
                "candidate": event.candidate.candidate,
                "sdpMid": event.candidate.sdpMid,
                "sdpMLineIndex": event.candidate.sdpMLineIndex,
                "usernameFragment": event.candidate.usernameFragment
            })

    return {"session_id": session_id}

# Endpoint to handle client's SDP offer
@app.post("/offer/{session_id}")
async def handle_offer(session_id: str, payload: SDPPayload):
    pc = sessions.get(session_id)
    if not pc:
        raise HTTPException(status_code=404, detail="Session not found")
    from aiortc import RTCSessionDescription
    await pc.setRemoteDescription(RTCSessionDescription(sdp=payload.sdp, type=payload.type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

# Endpoint to handle client's ICE candidates
@app.post("/ice_candidate/{session_id}")
async def handle_ice_candidate(session_id: str, payload: ICECandidatePayload):
    pc = sessions.get(session_id)
    if not pc:
        raise HTTPException(status_code=404, detail="Session not found")

    from aiortc import RTCIceCandidate
    await pc.addIceCandidate(RTCIceCandidate({
        "candidate": payload.candidate,
        "sdpMid": payload.sdpMid,
        "sdpMLineIndex": payload.sdpMLineIndex,
        "usernameFragment": payload.usernameFragment
    })
    return {"status": "ICE candidate added"}

# Endpoint to retrieve server's ICE candidates
@app.get("/ice_candidates/{session_id}")
async def get_ice_candidates(session_id: str):
    pc = sessions.get(session_id)
    if not pc:
        raise HTTPException(status_code=404, detail="Session not found")

    # This is a simplified approach; in practice, store candidates in a list
    # Since aiortc doesn't expose candidates directly, we rely on the on_icecandidate handler
    # Clients should poll this endpoint after sending offer
    return {"candidates": []}  # Update based on stored candidates if needed

# Endpoint to clean up session
@app.delete("/session/{session_id}")
async def delete_session(session_id: str):
    pc = sessions.pop(session_id, None)
    if pc:
        await pc.close()
        return {"status": "Session closed"}
    raise HTTPException(status_code=404, detail="Session not found")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)