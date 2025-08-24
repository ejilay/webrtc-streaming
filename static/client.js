// get DOM elements
var dataChannelLog = document.getElementById('data-channel'),
    iceConnectionLog = document.getElementById('ice-connection-state'),
    iceGatheringLog = document.getElementById('ice-gathering-state'),
    signalingLog = document.getElementById('signaling-state');

// peer connection
var pc = null;

// data channel
var dc = null, dcInterval = null;

// audio monitoring
var audioContext = null;
var analyser = null;
var microphoneStream = null;
var audioLevelInterval = null;

function createPeerConnection() {
    var config = {
        sdpSemantics: 'unified-plan'
    };

    if (document.getElementById('use-stun').checked) {
        config.iceServers = [{ urls: ['stun:stun.l.google.com:19302'] }];
    }

    pc = new RTCPeerConnection(config);

    // register some listeners to help debugging
    pc.addEventListener('icegatheringstatechange', () => {
        iceGatheringLog.textContent += ' -> ' + pc.iceGatheringState;
    }, false);
    iceGatheringLog.textContent = pc.iceGatheringState;

    pc.addEventListener('iceconnectionstatechange', () => {
        iceConnectionLog.textContent += ' -> ' + pc.iceConnectionState;
        
        // Update main UI status based on connection state
        if (pc.iceConnectionState === 'connected') {
            updateStatus('Подключен');
            updateAudioVisualization(true);
        } else if (pc.iceConnectionState === 'disconnected' || pc.iceConnectionState === 'failed') {
            updateStatus('Соединение потеряно');
            updateAudioVisualization(false);
        } else if (pc.iceConnectionState === 'connecting') {
            updateStatus('Подключение...');
        }
    }, false);
    iceConnectionLog.textContent = pc.iceConnectionState;

    pc.addEventListener('signalingstatechange', () => {
        signalingLog.textContent += ' -> ' + pc.signalingState;
    }, false);
    signalingLog.textContent = pc.signalingState;

    // connect audio / video
    pc.addEventListener('track', (evt) => {
        if (evt.track.kind == 'video')
            document.getElementById('video').srcObject = evt.streams[0];
        else
            document.getElementById('audio').srcObject = evt.streams[0];
    });

    return pc;
}

function enumerateInputDevices() {
    const populateSelect = (select, devices) => {
        let counter = 1;
        devices.forEach((device) => {
            const option = document.createElement('option');
            option.value = device.deviceId;
            option.text = device.label || ('Device #' + counter);
            select.appendChild(option);
            counter += 1;
        });
    };

    navigator.mediaDevices.enumerateDevices().then((devices) => {
        populateSelect(
            document.getElementById('audio-input'),
            devices.filter((device) => device.kind == 'audioinput')
        );
        populateSelect(
            document.getElementById('video-input'),
            devices.filter((device) => device.kind == 'videoinput')
        );
    }).catch((e) => {
        alert(e);
    });
}

function negotiate() {
    return pc.createOffer().then((offer) => {
        return pc.setLocalDescription(offer);
    }).then(() => {
        // wait for ICE gathering to complete
        return new Promise((resolve) => {
            if (pc.iceGatheringState === 'complete') {
                resolve();
            } else {
                function checkState() {
                    if (pc.iceGatheringState === 'complete') {
                        pc.removeEventListener('icegatheringstatechange', checkState);
                        resolve();
                    }
                }
                pc.addEventListener('icegatheringstatechange', checkState);
            }
        });
    }).then(() => {
        var offer = pc.localDescription;
        var codec;

        codec = document.getElementById('audio-codec').value;
        if (codec !== 'default') {
            offer.sdp = sdpFilterCodec('audio', codec, offer.sdp);
        }

        codec = document.getElementById('video-codec').value;
        if (codec !== 'default') {
            offer.sdp = sdpFilterCodec('video', codec, offer.sdp);
        }

        document.getElementById('offer-sdp').textContent = offer.sdp;
        return fetch('/offer', {
            body: JSON.stringify({
                sdp: offer.sdp,
                type: offer.type,
                video_transform: document.getElementById('video-transform').value
            }),
            headers: {
                'Content-Type': 'application/json'
            },
            method: 'POST'
        });
    }).then((response) => {
        return response.json();
    }).then((answer) => {
        document.getElementById('answer-sdp').textContent = answer.sdp;
        return pc.setRemoteDescription(answer);
    }).catch((e) => {
        alert(e);
    });
}

function start() {
    document.getElementById('start').style.display = 'none';
    updateStatus('Connecting...');

    pc = createPeerConnection();

    var time_start = null;

    const current_stamp = () => {
        if (time_start === null) {
            time_start = new Date().getTime();
            return 0;
        } else {
            return new Date().getTime() - time_start;
        }
    };

    if (document.getElementById('use-datachannel').checked) {
        var parameters = JSON.parse(document.getElementById('datachannel-parameters').value);

        dc = pc.createDataChannel('chat', parameters);
        dc.addEventListener('close', () => {
            clearInterval(dcInterval);
            dataChannelLog.textContent += '- close\n';
        });
        dc.addEventListener('open', () => {
            dataChannelLog.textContent += '- open\n';
            dcInterval = setInterval(() => {
                var message = 'ping ' + current_stamp();
                dataChannelLog.textContent = '> ' + message + '\n';
                dc.send(message);
            }, 1000);
        });
        dc.addEventListener('message', (evt) => {
            dataChannelLog.textContent += '< ' + evt.data + '\n';

            if (evt.data.substring(0, 4) === 'pong') {
                var elapsed_ms = current_stamp() - parseInt(evt.data.substring(5), 10);
                dataChannelLog.textContent += ' RTT ' + elapsed_ms + ' ms\n';
            }
        });
    }

    // Build media constraints.

    const constraints = {
        audio: false,
        video: false
    };

    if (document.getElementById('use-audio').checked) {
        const audioConstraints = {
            echoCancellation: document.getElementById('echo-cancellation').checked,
            noiseSuppression: document.getElementById('noise-suppression').checked,
            autoGainControl: document.getElementById('auto-gain-control').checked,
            latency: 0.02,  // 20ms latency for real-time processing
            channelCount: 1, // Mono for better processing
            sampleRate: 48000,
            sampleSize: 16
        };

        const device = document.getElementById('audio-input').value;
        if (device) {
            audioConstraints.deviceId = { exact: device };
        }

        constraints.audio = audioConstraints;
    }

    if (document.getElementById('use-video').checked) {
        const videoConstraints = {};

        const device = document.getElementById('video-input').value;
        if (device) {
            videoConstraints.deviceId = { exact: device };
        }

        const resolution = document.getElementById('video-resolution').value;
        if (resolution) {
            const dimensions = resolution.split('x');
            videoConstraints.width = parseInt(dimensions[0], 0);
            videoConstraints.height = parseInt(dimensions[1], 0);
        }

        constraints.video = Object.keys(videoConstraints).length ? videoConstraints : true;
    }

    // Acquire media and start negociation.

    if (constraints.audio || constraints.video) {
        if (constraints.video) {
            document.getElementById('media').style.display = 'block';
        }
        navigator.mediaDevices.getUserMedia(constraints).then((stream) => {
            microphoneStream = stream;
            stream.getTracks().forEach((track) => {
                pc.addTrack(track, stream);
            });
            
            // Initialize audio monitoring if audio is enabled
            if (constraints.audio) {
                initAudioMonitoring(stream);
                updateAudioVisualization(true);
                updateStatus('Acquiring media...');
            }
            
            return negotiate();
        }, (err) => {
            alert('Could not acquire media: ' + err);
        });
    } else {
        negotiate();
    }

    document.getElementById('stop').style.display = 'inline-block';
}

function stop() {
    document.getElementById('stop').style.display = 'none';
    document.getElementById('start').style.display = 'block';
    
    updateStatus('Disconnected');
    updateAudioVisualization(false);

    // Stop audio monitoring
    stopAudioMonitoring();

    // close data channel
    if (dc) {
        dc.close();
    }

    // close transceivers
    if (pc.getTransceivers) {
        pc.getTransceivers().forEach((transceiver) => {
            if (transceiver.stop) {
                transceiver.stop();
            }
        });
    }

    // close local audio / video
    pc.getSenders().forEach((sender) => {
        sender.track.stop();
    });

    // Stop microphone stream tracks
    if (microphoneStream) {
        microphoneStream.getTracks().forEach(track => track.stop());
        microphoneStream = null;
    }

    // close peer connection
    setTimeout(() => {
        pc.close();
    }, 500);
}

function sdpFilterCodec(kind, codec, realSdp) {
    var allowed = []
    var rtxRegex = new RegExp('a=fmtp:(\\d+) apt=(\\d+)\r$');
    var codecRegex = new RegExp('a=rtpmap:([0-9]+) ' + escapeRegExp(codec))
    var videoRegex = new RegExp('(m=' + kind + ' .*?)( ([0-9]+))*\\s*$')

    var lines = realSdp.split('\n');

    var isKind = false;
    for (var i = 0; i < lines.length; i++) {
        if (lines[i].startsWith('m=' + kind + ' ')) {
            isKind = true;
        } else if (lines[i].startsWith('m=')) {
            isKind = false;
        }

        if (isKind) {
            var match = lines[i].match(codecRegex);
            if (match) {
                allowed.push(parseInt(match[1]));
            }

            match = lines[i].match(rtxRegex);
            if (match && allowed.includes(parseInt(match[2]))) {
                allowed.push(parseInt(match[1]));
            }
        }
    }

    var skipRegex = 'a=(fmtp|rtcp-fb|rtpmap):([0-9]+)';
    var sdp = '';

    isKind = false;
    for (var i = 0; i < lines.length; i++) {
        if (lines[i].startsWith('m=' + kind + ' ')) {
            isKind = true;
        } else if (lines[i].startsWith('m=')) {
            isKind = false;
        }

        if (isKind) {
            var skipMatch = lines[i].match(skipRegex);
            if (skipMatch && !allowed.includes(parseInt(skipMatch[2]))) {
                continue;
            } else if (lines[i].match(videoRegex)) {
                sdp += lines[i].replace(videoRegex, '$1 ' + allowed.join(' ')) + '\n';
            } else {
                sdp += lines[i] + '\n';
            }
        } else {
            sdp += lines[i] + '\n';
        }
    }

    return sdp;
}

function escapeRegExp(string) {
    return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); // $& means the whole matched string
}

function initAudioMonitoring(stream) {
    if (!audioContext) {
        audioContext = new (window.AudioContext || window.webkitAudioContext)();
    }
    
    const source = audioContext.createMediaStreamSource(stream);
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.8;
    
    source.connect(analyser);
    
    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);
    let feedbackCounter = 0;
    let noiseFloor = 0;
    let calibrationFrames = 0;
    
    function updateAudioLevel() {
        analyser.getByteFrequencyData(dataArray);
        
        // Calculate average volume and frequency distribution
        let sum = 0;
        let highFreqSum = 0;
        let lowFreqSum = 0;
        
        for (let i = 0; i < bufferLength; i++) {
            sum += dataArray[i];
            if (i < bufferLength / 3) {
                lowFreqSum += dataArray[i];
            } else if (i > 2 * bufferLength / 3) {
                highFreqSum += dataArray[i];
            }
        }
        
        const average = sum / bufferLength;
        const highFreqAvg = highFreqSum / (bufferLength / 3);
        const lowFreqAvg = lowFreqSum / (bufferLength / 3);
        
        // Calibrate noise floor during first few seconds
        if (calibrationFrames < 50) {
            noiseFloor = Math.max(noiseFloor, average);
            calibrationFrames++;
        }
        
        // Update audio level indicator (for debug panel)
        const audioLevelEl = document.getElementById('audio-level');
        if (audioLevelEl) {
            const percentage = Math.round((average / 255) * 100);
            audioLevelEl.style.width = percentage + '%';
            audioLevelEl.textContent = percentage + '%';
            
            // Enhanced feedback detection
            const feedbackRisk = detectFeedbackRisk(average, highFreqAvg, lowFreqAvg, noiseFloor);
            
            if (feedbackRisk === 'high') {
                feedbackCounter++;
                audioLevelEl.style.backgroundColor = '#ff4444';
                document.getElementById('feedback-warning').style.display = 'block';
                
                // Auto-adjust audio constraints if feedback detected for 5+ frames
                if (feedbackCounter > 5) {
                    autoAdjustAudioSettings();
                    feedbackCounter = 0;
                }
            } else if (feedbackRisk === 'medium') {
                audioLevelEl.style.backgroundColor = '#ffaa00';
                document.getElementById('feedback-warning').style.display = 'none';
                feedbackCounter = Math.max(0, feedbackCounter - 1);
            } else {
                audioLevelEl.style.backgroundColor = '#00aa00';
                document.getElementById('feedback-warning').style.display = 'none';
                feedbackCounter = Math.max(0, feedbackCounter - 1);
            }
        }
        
        // Update main visualizer bars
        updateVisualizerBars(dataArray, bufferLength);
    }
    
    audioLevelInterval = setInterval(updateAudioLevel, 100);
}

function updateVisualizerBars(dataArray, bufferLength) {
    const bars = document.querySelectorAll('.bar');
    if (bars.length === 0) return;
    
    // Group frequency data into 8 segments for the 8 bars
    const segmentSize = Math.floor(bufferLength / bars.length);
    
    bars.forEach((bar, index) => {
        let sum = 0;
        const start = index * segmentSize;
        const end = start + segmentSize;
        
        // Average the frequency data for this segment
        for (let i = start; i < end && i < bufferLength; i++) {
            sum += dataArray[i];
        }
        
        const average = sum / segmentSize;
        const intensity = Math.min(average / 255, 1);
        
        // Scale the bar height based on frequency intensity
        const scale = 0.2 + (intensity * 1.8); // Scale from 0.2 to 2.0
        bar.style.transform = `scaleY(${scale})`;
        
        // Change color based on intensity
        if (intensity > 0.7) {
            bar.style.backgroundColor = 'rgba(76, 175, 80, 1)'; // Green
        } else if (intensity > 0.3) {
            bar.style.backgroundColor = 'rgba(255, 193, 7, 1)'; // Yellow
        } else {
            bar.style.backgroundColor = 'rgba(255, 255, 255, 0.6)'; // White
        }
    });
}

function detectFeedbackRisk(average, highFreq, lowFreq, noiseFloor) {
    // High average level with disproportionate high frequencies suggests feedback
    if (average > 200 && highFreq > lowFreq * 1.5) {
        return 'high';
    }
    
    // Sustained levels significantly above noise floor
    if (average > noiseFloor * 3 && average > 150) {
        return 'medium';
    }
    
    return 'low';
}

function autoAdjustAudioSettings() {
    console.log('Auto-adjusting audio settings due to feedback detection');
    
    // Force enable all audio enhancements
    document.getElementById('echo-cancellation').checked = true;
    document.getElementById('noise-suppression').checked = true;
    document.getElementById('auto-gain-control').checked = true;
    
    // Show notification to user
    const notification = document.createElement('div');
    notification.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        background-color: #ff9800;
        color: white;
        padding: 15px;
        border-radius: 5px;
        z-index: 1000;
        max-width: 300px;
    `;
    notification.textContent = 'Audio feedback detected! Auto-enabled all audio enhancements.';
    document.body.appendChild(notification);
    
    setTimeout(() => {
        document.body.removeChild(notification);
    }, 5000);
}

function stopAudioMonitoring() {
    if (audioLevelInterval) {
        clearInterval(audioLevelInterval);
        audioLevelInterval = null;
    }
    
    if (audioContext && audioContext.state !== 'closed') {
        audioContext.close();
        audioContext = null;
    }
    
    analyser = null;
    microphoneStream = null;
    
    // Reset audio level indicator
    const audioLevelEl = document.getElementById('audio-level');
    if (audioLevelEl) {
        audioLevelEl.style.width = '0%';
        audioLevelEl.textContent = '0%';
        audioLevelEl.style.backgroundColor = '#00aa00';
    }
    
    const feedbackWarning = document.getElementById('feedback-warning');
    if (feedbackWarning) {
        feedbackWarning.style.display = 'none';
    }
}

// Status and visualization helper functions (these are also available from the HTML)
function updateStatus(message) {
    const statusEl = document.getElementById('status-message');
    if (statusEl) {
        statusEl.textContent = message;
    }
}

function updateAudioVisualization(isActive) {
    const visualizer = document.getElementById('audio-visualizer');
    if (visualizer) {
        if (isActive) {
            visualizer.classList.add('active');
        } else {
            visualizer.classList.remove('active');
        }
    }
}

// Initialize the interface
document.addEventListener('DOMContentLoaded', function() {
    updateStatus('Готов к подключению');
});

enumerateInputDevices();