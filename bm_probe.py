#!/usr/bin/env python3
"""
Verbose probe for the BrandMeister Last Heard stream — with the join step.

The server now requires the client to emit a "join" after connecting before it
sends any events. This probe joins "everything" so you can confirm data flows.

Run ~20 seconds, then Ctrl-C, and share the output.
"""

import logging
import socketio

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")

sio = socketio.Client()


@sio.event
def connect():
    print(">>> CONNECTED, sending join...")
    sio.emit("join", "everything")


@sio.event
def disconnect():
    print(">>> DISCONNECTED")


@sio.on("mqtt")
def on_mqtt(data):
    print(">>> MQTT EVENT:", str(data)[:300])


sio.connect(url="https://api.brandmeister.network",
            socketio_path="/lh/socket.io",
            transports="websocket")
sio.wait()
