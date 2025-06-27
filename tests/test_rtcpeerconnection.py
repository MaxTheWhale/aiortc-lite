import asyncio
import re
from collections.abc import Callable
from typing import Optional, Union
from unittest import TestCase

import aioice.stun
from aiortc import (
    RTCDataChannel,
    RTCIceCandidate,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.exceptions import InvalidStateError

from .utils import asynctest

LONG_DATA = b"\xff" * 2000
STRIP_CANDIDATES_RE = re.compile("^a=(candidate:.*|end-of-candidates)\r\n", re.M)


def mids(pc: RTCPeerConnection) -> list[Optional[str]]:
    if pc.sctp:
        return [pc.sctp.mid]
    return []


def strip_ice_candidates(description: RTCSessionDescription) -> RTCSessionDescription:
    return RTCSessionDescription(
        sdp=STRIP_CANDIDATES_RE.sub("", description.sdp), type=description.type
    )


def track_states(pc: RTCPeerConnection) -> dict[str, list[str]]:
    states = {
        "connectionState": [pc.connectionState],
        "iceConnectionState": [pc.iceConnectionState],
        "iceGatheringState": [pc.iceGatheringState],
        "signalingState": [pc.signalingState],
    }

    @pc.on("connectionstatechange")
    def connectionstatechange() -> None:
        states["connectionState"].append(pc.connectionState)

    @pc.on("iceconnectionstatechange")
    def iceconnectionstatechange() -> None:
        states["iceConnectionState"].append(pc.iceConnectionState)

    @pc.on("icegatheringstatechange")
    def icegatheringstatechange() -> None:
        states["iceGatheringState"].append(pc.iceGatheringState)

    @pc.on("signalingstatechange")
    def signalingstatechange() -> None:
        states["signalingState"].append(pc.signalingState)

    return states


class RTCPeerConnectionTest(TestCase):
    def assertClosed(self, pc: RTCPeerConnection) -> None:
        self.assertEqual(pc.connectionState, "closed")
        self.assertEqual(pc.iceConnectionState, "closed")
        self.assertEqual(pc.signalingState, "closed")

    async def assertDataChannelOpen(self, dc: RTCDataChannel) -> None:
        await self.sleepWhile(lambda: dc.readyState == "connecting")
        self.assertEqual(dc.readyState, "open")

    async def assertIceChecking(self, pc: RTCPeerConnection) -> None:
        await self.sleepWhile(lambda: pc.iceConnectionState == "new")
        self.assertEqual(pc.iceConnectionState, "checking")
        self.assertEqual(pc.iceGatheringState, "complete")

    async def assertIceCompleted(
        self, pc1: RTCPeerConnection, pc2: RTCPeerConnection
    ) -> None:
        await self.sleepWhile(
            lambda: pc1.iceConnectionState == "checking"
            or pc2.iceConnectionState == "checking"
        )
        self.assertEqual(pc1.iceConnectionState, "completed")
        self.assertEqual(pc2.iceConnectionState, "completed")

    def assertHasIceCandidates(self, description: RTCSessionDescription) -> None:
        self.assertTrue("a=candidate:" in description.sdp)
        self.assertTrue("a=end-of-candidates" in description.sdp)

    def assertHasDtls(self, description: RTCSessionDescription, setup: str) -> None:
        self.assertTrue("a=fingerprint:sha-256" in description.sdp)
        self.assertEqual(
            set(re.findall("a=setup:(.*)\r$", description.sdp)), set([setup])
        )

    async def closeDataChannel(self, dc: RTCDataChannel) -> None:
        dc.close()
        await self.sleepWhile(lambda: dc.readyState == "closing")
        self.assertEqual(dc.readyState, "closed")

    async def sleepWhile(self, f: Callable[[], bool], max_sleep: float = 1.0) -> None:
        sleep = 0.1
        total = 0.0
        while f() and total < max_sleep:
            await asyncio.sleep(sleep)
            total += sleep

    def setUp(self) -> None:
        # save timers
        self.retry_max = aioice.stun.RETRY_MAX
        self.retry_rto = aioice.stun.RETRY_RTO

        # shorten timers to run tests faster
        aioice.stun.RETRY_MAX = 1
        aioice.stun.RETRY_RTO = 0.1

    def tearDown(self) -> None:
        # restore timers
        aioice.stun.RETRY_MAX = self.retry_max
        aioice.stun.RETRY_RTO = self.retry_rto

    @asynctest
    async def test_addIceCandidate(self) -> None:
        pc = RTCPeerConnection()
        pc.createDataChannel("test")
        offer = await pc.createOffer()
        await pc.setRemoteDescription(offer)
        self.assertFalse("a=candidate:" in pc.remoteDescription.sdp)
        candidate_with_index = RTCIceCandidate(
            component=1,
            foundation="0",
            ip="192.168.99.7",
            port=33543,
            priority=2122252543,
            protocol="UDP",
            type="host",
            sdpMLineIndex=0,
        )
        await pc.addIceCandidate(candidate_with_index)
        self.assertTrue("a=candidate:" in pc.remoteDescription.sdp)

        candidate_with_mid = RTCIceCandidate(
            component=1,
            foundation="0",
            ip="192.168.99.7",
            port=33544,
            priority=2122252543,
            protocol="UDP",
            type="host",
            sdpMid=pc.sctp.mid,
        )
        await pc.addIceCandidate(candidate_with_mid)
        self.assertEqual(pc.remoteDescription.sdp.count("a=candidate:"), 2)

    @asynctest
    async def test_addIceCandidate_no_sdpMid_or_sdpMLineIndex(self) -> None:
        pc = RTCPeerConnection()
        with self.assertRaises(ValueError) as cm:
            await pc.addIceCandidate(
                RTCIceCandidate(
                    component=1,
                    foundation="0",
                    ip="192.168.99.7",
                    port=33543,
                    priority=2122252543,
                    protocol="UDP",
                    type="host",
                )
            )
        self.assertEqual(
            str(cm.exception), "Candidate must have either sdpMid or sdpMLineIndex"
        )

    @asynctest
    async def test_addIceCandidate_null(self) -> None:
        pc = RTCPeerConnection()
        pc.createDataChannel("test")
        offer = await pc.createOffer()
        await pc.setRemoteDescription(offer)
        self.assertFalse("a=end-of-candidates" in pc.remoteDescription.sdp)
        await pc.addIceCandidate(None)
        self.assertTrue("a=end-of-candidates" in pc.remoteDescription.sdp)

    @asynctest
    async def test_close(self) -> None:
        pc = RTCPeerConnection()
        pc_states = track_states(pc)

        # close once
        await pc.close()

        # close twice
        await pc.close()

        self.assertEqual(pc_states["signalingState"], ["stable", "closed"])

    @asynctest
    async def test_connect_datachannel_and_close_immediately(self) -> None:
        pc1 = RTCPeerConnection()
        pc2 = RTCPeerConnection()

        # create two data channels
        dc1 = pc1.createDataChannel("chat1")
        self.assertEqual(dc1.readyState, "connecting")
        dc2 = pc1.createDataChannel("chat2")
        self.assertEqual(dc2.readyState, "connecting")

        # close one data channel
        dc1.close()
        self.assertEqual(dc1.readyState, "closed")
        self.assertEqual(dc2.readyState, "connecting")

        # perform SDP exchange
        await pc1.setLocalDescription(await pc1.createOffer())
        await pc2.setRemoteDescription(pc1.localDescription)
        await pc2.setLocalDescription(await pc2.createAnswer())
        await pc1.setRemoteDescription(pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)
        self.assertEqual(dc1.readyState, "closed")
        await self.assertDataChannelOpen(dc2)

        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

    @asynctest
    async def test_connect_datachannel_negotiated_and_close_immediately(self) -> None:
        pc1 = RTCPeerConnection()
        pc2 = RTCPeerConnection()

        # create two negotiated data channels
        dc1 = pc1.createDataChannel("chat1", negotiated=True, id=100)
        self.assertEqual(dc1.readyState, "connecting")
        dc2 = pc1.createDataChannel("chat2", negotiated=True, id=102)
        self.assertEqual(dc2.readyState, "connecting")

        # close one data channel
        dc1.close()
        self.assertEqual(dc1.readyState, "closed")
        self.assertEqual(dc2.readyState, "connecting")

        # perform SDP exchange
        await pc1.setLocalDescription(await pc1.createOffer())
        await pc2.setRemoteDescription(pc1.localDescription)
        await pc2.setLocalDescription(await pc2.createAnswer())
        await pc1.setRemoteDescription(pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)
        self.assertEqual(dc1.readyState, "closed")
        await self.assertDataChannelOpen(dc2)

        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

    @asynctest
    async def test_connect_datachannel_legacy_sdp(self) -> None:
        pc1 = RTCPeerConnection()
        pc1._sctpLegacySdp = True
        pc1_data_messages = []
        pc1_states = track_states(pc1)

        pc2 = RTCPeerConnection()
        pc2_data_channels = []
        pc2_data_messages = []
        pc2_states = track_states(pc2)

        @pc2.on("datachannel")
        def on_datachannel(channel: RTCDataChannel) -> None:
            self.assertEqual(channel.readyState, "open")
            pc2_data_channels.append(channel)

            @channel.on("message")
            def on_message(message: Union[bytes, str]) -> None:
                pc2_data_messages.append(message)
                if isinstance(message, str):
                    channel.send("string-echo: " + message)
                else:
                    channel.send(b"binary-echo: " + message)

        # create data channel
        dc = pc1.createDataChannel("chat", protocol="bob")
        self.assertEqual(dc.label, "chat")
        self.assertEqual(dc.maxPacketLifeTime, None)
        self.assertEqual(dc.maxRetransmits, None)
        self.assertEqual(dc.ordered, True)
        self.assertEqual(dc.protocol, "bob")
        self.assertEqual(dc.readyState, "connecting")

        # send messages
        @dc.on("open")
        def on_open() -> None:
            dc.send("hello")
            dc.send("")
            dc.send(b"\x00\x01\x02\x03")
            dc.send(b"")
            dc.send(LONG_DATA)
            with self.assertRaises(ValueError) as cm:
                dc.send(1234)  # type: ignore
            self.assertEqual(
                str(cm.exception), "Cannot send unsupported data type: <class 'int'>"
            )
            self.assertEqual(dc.bufferedAmount, 2011)

        @dc.on("message")
        def on_message(message: Union[bytes, str]) -> None:
            pc1_data_messages.append(message)

        # create offer
        offer = await pc1.createOffer()
        self.assertEqual(offer.type, "offer")
        self.assertTrue("m=application " in offer.sdp)
        self.assertFalse("a=candidate:" in offer.sdp)
        self.assertFalse("a=end-of-candidates" in offer.sdp)

        await pc1.setLocalDescription(offer)
        self.assertEqual(pc1.iceConnectionState, "new")
        self.assertEqual(pc1.iceGatheringState, "complete")
        self.assertEqual(mids(pc1), ["0"])
        self.assertTrue("m=application " in pc1.localDescription.sdp)
        self.assertTrue(
            "a=sctpmap:5000 webrtc-datachannel 65535" in pc1.localDescription.sdp
        )
        self.assertHasIceCandidates(pc1.localDescription)
        self.assertHasDtls(pc1.localDescription, "actpass")

        # handle offer
        await pc2.setRemoteDescription(pc1.localDescription)
        self.assertEqual(pc2.remoteDescription, pc1.localDescription)
        self.assertEqual(mids(pc2), ["0"])

        # create answer
        answer = await pc2.createAnswer()
        self.assertEqual(answer.type, "answer")
        self.assertTrue("m=application " in answer.sdp)
        self.assertFalse("a=candidate:" in answer.sdp)
        self.assertFalse("a=end-of-candidates" in answer.sdp)

        await pc2.setLocalDescription(answer)
        await self.assertIceChecking(pc2)
        self.assertTrue("m=application " in pc2.localDescription.sdp)
        self.assertTrue(
            "a=sctpmap:5000 webrtc-datachannel 65535" in pc2.localDescription.sdp
        )
        self.assertHasIceCandidates(pc2.localDescription)
        self.assertHasDtls(pc2.localDescription, "active")

        # handle answer
        await pc1.setRemoteDescription(pc2.localDescription)
        self.assertEqual(pc1.remoteDescription, pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)
        await self.assertDataChannelOpen(dc)
        self.assertEqual(dc.bufferedAmount, 0)

        # check pc2 got a datachannel
        self.assertEqual(len(pc2_data_channels), 1)
        self.assertEqual(pc2_data_channels[0].label, "chat")
        self.assertEqual(pc2_data_channels[0].maxPacketLifeTime, None)
        self.assertEqual(pc2_data_channels[0].maxRetransmits, None)
        self.assertEqual(pc2_data_channels[0].ordered, True)
        self.assertEqual(pc2_data_channels[0].protocol, "bob")

        # check pc2 got messages
        await asyncio.sleep(0.1)
        self.assertEqual(
            pc2_data_messages, ["hello", "", b"\x00\x01\x02\x03", b"", LONG_DATA]
        )

        # check pc1 got replies
        self.assertEqual(
            pc1_data_messages,
            [
                "string-echo: hello",
                "string-echo: ",
                b"binary-echo: \x00\x01\x02\x03",
                b"binary-echo: ",
                b"binary-echo: " + LONG_DATA,
            ],
        )

        # close data channel
        await self.closeDataChannel(dc)

        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

        # check state changes
        self.assertEqual(
            pc1_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc1_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc1_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc1_states["signalingState"],
            ["stable", "have-local-offer", "stable", "closed"],
        )

        self.assertEqual(
            pc2_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc2_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc2_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc2_states["signalingState"],
            ["stable", "have-remote-offer", "stable", "closed"],
        )

    @asynctest
    async def test_connect_datachannel_modern_sdp(self) -> None:
        pc1 = RTCPeerConnection()
        pc1._sctpLegacySdp = False
        pc1_data_messages = []
        pc1_states = track_states(pc1)

        pc2 = RTCPeerConnection()
        pc2_data_channels = []
        pc2_data_messages = []
        pc2_states = track_states(pc2)

        @pc2.on("datachannel")
        def on_datachannel(channel: RTCDataChannel) -> None:
            self.assertEqual(channel.readyState, "open")
            pc2_data_channels.append(channel)

            @channel.on("message")
            def on_message(message: Union[bytes, str]) -> None:
                pc2_data_messages.append(message)
                if isinstance(message, str):
                    channel.send("string-echo: " + message)
                else:
                    channel.send(b"binary-echo: " + message)

        # create data channel
        dc = pc1.createDataChannel("chat", protocol="bob")
        self.assertEqual(dc.label, "chat")
        self.assertEqual(dc.maxPacketLifeTime, None)
        self.assertEqual(dc.maxRetransmits, None)
        self.assertEqual(dc.ordered, True)
        self.assertEqual(dc.protocol, "bob")
        self.assertEqual(dc.readyState, "connecting")

        # send messages
        @dc.on("open")
        def on_open() -> None:
            dc.send("hello")
            dc.send("")
            dc.send(b"\x00\x01\x02\x03")
            dc.send(b"")
            dc.send(LONG_DATA)
            with self.assertRaises(ValueError) as cm:
                dc.send(1234)  # type: ignore
            self.assertEqual(
                str(cm.exception), "Cannot send unsupported data type: <class 'int'>"
            )

        @dc.on("message")
        def on_message(message: Union[bytes, str]) -> None:
            pc1_data_messages.append(message)

        # create offer
        offer = await pc1.createOffer()
        self.assertEqual(offer.type, "offer")
        self.assertTrue("m=application " in offer.sdp)
        self.assertFalse("a=candidate:" in offer.sdp)
        self.assertFalse("a=end-of-candidates" in offer.sdp)

        await pc1.setLocalDescription(offer)
        self.assertEqual(pc1.iceConnectionState, "new")
        self.assertEqual(pc1.iceGatheringState, "complete")
        self.assertEqual(mids(pc1), ["0"])
        self.assertTrue("m=application " in pc1.localDescription.sdp)
        self.assertTrue("a=sctp-port:5000" in pc1.localDescription.sdp)
        self.assertHasIceCandidates(pc1.localDescription)
        self.assertHasDtls(pc1.localDescription, "actpass")

        # handle offer
        await pc2.setRemoteDescription(pc1.localDescription)
        self.assertEqual(pc2.remoteDescription, pc1.localDescription)
        self.assertEqual(mids(pc2), ["0"])

        # create answer
        answer = await pc2.createAnswer()
        self.assertEqual(answer.type, "answer")
        self.assertTrue("m=application " in answer.sdp)
        self.assertFalse("a=candidate:" in answer.sdp)
        self.assertFalse("a=end-of-candidates" in answer.sdp)

        await pc2.setLocalDescription(answer)
        await self.assertIceChecking(pc2)
        self.assertTrue("m=application " in pc2.localDescription.sdp)
        self.assertTrue("a=sctp-port:5000" in pc2.localDescription.sdp)
        self.assertHasIceCandidates(pc2.localDescription)
        self.assertHasDtls(pc2.localDescription, "active")

        # handle answer
        await pc1.setRemoteDescription(pc2.localDescription)
        self.assertEqual(pc1.remoteDescription, pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)
        await self.assertDataChannelOpen(dc)

        # check pc2 got a datachannel
        self.assertEqual(len(pc2_data_channels), 1)
        self.assertEqual(pc2_data_channels[0].label, "chat")
        self.assertEqual(pc2_data_channels[0].maxPacketLifeTime, None)
        self.assertEqual(pc2_data_channels[0].maxRetransmits, None)
        self.assertEqual(pc2_data_channels[0].ordered, True)
        self.assertEqual(pc2_data_channels[0].protocol, "bob")

        # check pc2 got messages
        await asyncio.sleep(0.1)
        self.assertEqual(
            pc2_data_messages, ["hello", "", b"\x00\x01\x02\x03", b"", LONG_DATA]
        )

        # check pc1 got replies
        self.assertEqual(
            pc1_data_messages,
            [
                "string-echo: hello",
                "string-echo: ",
                b"binary-echo: \x00\x01\x02\x03",
                b"binary-echo: ",
                b"binary-echo: " + LONG_DATA,
            ],
        )

        # close data channel
        await self.closeDataChannel(dc)

        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

        # check state changes
        self.assertEqual(
            pc1_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc1_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc1_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc1_states["signalingState"],
            ["stable", "have-local-offer", "stable", "closed"],
        )

        self.assertEqual(
            pc2_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc2_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc2_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc2_states["signalingState"],
            ["stable", "have-remote-offer", "stable", "closed"],
        )

    @asynctest
    async def test_connect_datachannel_modern_sdp_negotiated(self) -> None:
        pc1 = RTCPeerConnection()
        pc1._sctpLegacySdp = False
        pc1_data_messages = []
        pc1_states = track_states(pc1)

        pc2 = RTCPeerConnection()
        pc2_data_messages = []
        pc2_states = track_states(pc2)

        # create data channels
        dc1 = pc1.createDataChannel("chat", protocol="bob", negotiated=True, id=100)
        self.assertEqual(dc1.id, 100)
        self.assertEqual(dc1.label, "chat")
        self.assertEqual(dc1.maxPacketLifeTime, None)
        self.assertEqual(dc1.maxRetransmits, None)
        self.assertEqual(dc1.ordered, True)
        self.assertEqual(dc1.protocol, "bob")
        self.assertEqual(dc1.readyState, "connecting")

        dc2 = pc2.createDataChannel("chat", protocol="bob", negotiated=True, id=100)
        self.assertEqual(dc2.id, 100)
        self.assertEqual(dc2.label, "chat")
        self.assertEqual(dc2.maxPacketLifeTime, None)
        self.assertEqual(dc2.maxRetransmits, None)
        self.assertEqual(dc2.ordered, True)
        self.assertEqual(dc2.protocol, "bob")
        self.assertEqual(dc2.readyState, "connecting")

        @dc1.on("message")
        def on_message1(message: Union[bytes, str]) -> None:
            pc1_data_messages.append(message)

        @dc2.on("message")
        def on_message2(message: Union[bytes, str]) -> None:
            pc2_data_messages.append(message)
            if isinstance(message, str):
                dc2.send("string-echo: " + message)
            else:
                dc2.send(b"binary-echo: " + message)

        # create offer
        offer = await pc1.createOffer()
        self.assertEqual(offer.type, "offer")
        self.assertTrue("m=application " in offer.sdp)
        self.assertFalse("a=candidate:" in offer.sdp)
        self.assertFalse("a=end-of-candidates" in offer.sdp)

        await pc1.setLocalDescription(offer)
        self.assertEqual(pc1.iceConnectionState, "new")
        self.assertEqual(pc1.iceGatheringState, "complete")
        self.assertEqual(mids(pc1), ["0"])
        self.assertTrue("m=application " in pc1.localDescription.sdp)
        self.assertTrue("a=sctp-port:5000" in pc1.localDescription.sdp)
        self.assertHasIceCandidates(pc1.localDescription)
        self.assertHasDtls(pc1.localDescription, "actpass")

        # handle offer
        await pc2.setRemoteDescription(pc1.localDescription)
        self.assertEqual(pc2.remoteDescription, pc1.localDescription)
        self.assertEqual(mids(pc2), ["0"])

        # create answer
        answer = await pc2.createAnswer()
        self.assertEqual(answer.type, "answer")
        self.assertTrue("m=application " in answer.sdp)
        self.assertFalse("a=candidate:" in answer.sdp)
        self.assertFalse("a=end-of-candidates" in answer.sdp)

        await pc2.setLocalDescription(answer)
        await self.assertIceChecking(pc2)
        self.assertTrue("m=application " in pc2.localDescription.sdp)
        self.assertTrue("a=sctp-port:5000" in pc2.localDescription.sdp)
        self.assertHasIceCandidates(pc2.localDescription)
        self.assertHasDtls(pc2.localDescription, "active")

        # handle answer
        await pc1.setRemoteDescription(pc2.localDescription)
        self.assertEqual(pc1.remoteDescription, pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)
        await self.assertDataChannelOpen(dc1)
        await self.assertDataChannelOpen(dc2)

        # send message
        dc1.send("hello")
        dc1.send("")
        dc1.send(b"\x00\x01\x02\x03")
        dc1.send(b"")
        dc1.send(LONG_DATA)
        with self.assertRaises(ValueError) as cm:
            dc1.send(1234)  # type: ignore
        self.assertEqual(
            str(cm.exception), "Cannot send unsupported data type: <class 'int'>"
        )

        # check pc2 got messages
        await asyncio.sleep(0.1)
        self.assertEqual(
            pc2_data_messages, ["hello", "", b"\x00\x01\x02\x03", b"", LONG_DATA]
        )

        # check pc1 got replies
        self.assertEqual(
            pc1_data_messages,
            [
                "string-echo: hello",
                "string-echo: ",
                b"binary-echo: \x00\x01\x02\x03",
                b"binary-echo: ",
                b"binary-echo: " + LONG_DATA,
            ],
        )

        # close data channels
        await self.closeDataChannel(dc1)

        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

        # check state changes
        self.assertEqual(
            pc1_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc1_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc1_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc1_states["signalingState"],
            ["stable", "have-local-offer", "stable", "closed"],
        )

        self.assertEqual(
            pc2_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc2_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc2_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc2_states["signalingState"],
            ["stable", "have-remote-offer", "stable", "closed"],
        )

    @asynctest
    async def test_connect_datachannel_recycle_stream_id(self) -> None:
        pc1 = RTCPeerConnection()
        pc2 = RTCPeerConnection()

        # create three data channels
        dc1 = pc1.createDataChannel("chat1")
        self.assertEqual(dc1.readyState, "connecting")
        dc2 = pc1.createDataChannel("chat2")
        self.assertEqual(dc2.readyState, "connecting")
        dc3 = pc1.createDataChannel("chat3")
        self.assertEqual(dc3.readyState, "connecting")

        # perform SDP exchange
        await pc1.setLocalDescription(await pc1.createOffer())
        await pc2.setRemoteDescription(pc1.localDescription)
        await pc2.setLocalDescription(await pc2.createAnswer())
        await pc1.setRemoteDescription(pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)
        await self.assertDataChannelOpen(dc1)
        self.assertEqual(dc1.id, 1)
        await self.assertDataChannelOpen(dc2)
        self.assertEqual(dc2.id, 3)
        await self.assertDataChannelOpen(dc3)
        self.assertEqual(dc3.id, 5)

        # close one data channel
        await self.closeDataChannel(dc2)

        # create a new data channel
        dc4 = pc1.createDataChannel("chat4")
        await self.assertDataChannelOpen(dc4)
        self.assertEqual(dc4.id, 3)

        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

    def test_create_datachannel_with_maxpacketlifetime_and_maxretransmits(self) -> None:
        pc = RTCPeerConnection()
        with self.assertRaises(ValueError) as cm:
            pc.createDataChannel("chat", maxPacketLifeTime=500, maxRetransmits=0)
        self.assertEqual(
            str(cm.exception),
            "Cannot specify both maxPacketLifeTime and maxRetransmits",
        )

    @asynctest
    async def test_datachannel_bufferedamountlowthreshold(self) -> None:
        pc = RTCPeerConnection()
        dc = pc.createDataChannel("chat")
        self.assertEqual(dc.bufferedAmountLowThreshold, 0)

        dc.bufferedAmountLowThreshold = 4294967295
        self.assertEqual(dc.bufferedAmountLowThreshold, 4294967295)

        dc.bufferedAmountLowThreshold = 16384
        self.assertEqual(dc.bufferedAmountLowThreshold, 16384)

        dc.bufferedAmountLowThreshold = 0
        self.assertEqual(dc.bufferedAmountLowThreshold, 0)

        with self.assertRaises(ValueError):
            dc.bufferedAmountLowThreshold = -1
            self.assertEqual(dc.bufferedAmountLowThreshold, 0)

        with self.assertRaises(ValueError):
            dc.bufferedAmountLowThreshold = 4294967296
            self.assertEqual(dc.bufferedAmountLowThreshold, 0)

    @asynctest
    async def test_datachannel_send_invalid_state(self) -> None:
        pc = RTCPeerConnection()
        dc = pc.createDataChannel("chat")
        with self.assertRaises(InvalidStateError):
            dc.send("hello")

    async def _test_connect_datachannel_trickle(self, with_mid: bool) -> None:
        pc1 = RTCPeerConnection()
        pc1_data_messages = []
        pc1_states = track_states(pc1)

        pc2 = RTCPeerConnection()
        pc2_data_channels = []
        pc2_data_messages = []
        pc2_states = track_states(pc2)

        @pc2.on("datachannel")
        def on_datachannel(channel: RTCDataChannel) -> None:
            self.assertEqual(channel.readyState, "open")
            pc2_data_channels.append(channel)

            @channel.on("message")
            def on_message(message: Union[bytes, str]) -> None:
                pc2_data_messages.append(message)
                if isinstance(message, str):
                    channel.send("string-echo: " + message)
                else:
                    channel.send(b"binary-echo: " + message)

        # create data channel
        dc = pc1.createDataChannel("chat", protocol="bob")
        self.assertEqual(dc.label, "chat")
        self.assertEqual(dc.maxPacketLifeTime, None)
        self.assertEqual(dc.maxRetransmits, None)
        self.assertEqual(dc.ordered, True)
        self.assertEqual(dc.protocol, "bob")
        self.assertEqual(dc.readyState, "connecting")

        # send messages
        @dc.on("open")
        def on_open() -> None:
            dc.send("hello")
            dc.send("")
            dc.send(b"\x00\x01\x02\x03")
            dc.send(b"")
            dc.send(LONG_DATA)
            with self.assertRaises(ValueError) as cm:
                dc.send(1234)  # type: ignore
            self.assertEqual(
                str(cm.exception), "Cannot send unsupported data type: <class 'int'>"
            )

        @dc.on("message")
        def on_message(message: Union[bytes, str]) -> None:
            pc1_data_messages.append(message)

        # create offer
        offer = await pc1.createOffer()
        self.assertEqual(offer.type, "offer")
        self.assertTrue("m=application " in offer.sdp)
        self.assertFalse("a=candidate:" in offer.sdp)
        self.assertFalse("a=end-of-candidates" in offer.sdp)

        await pc1.setLocalDescription(offer)
        self.assertEqual(pc1.iceConnectionState, "new")
        self.assertEqual(pc1.iceGatheringState, "complete")
        self.assertEqual(mids(pc1), ["0"])
        self.assertTrue("m=application " in pc1.localDescription.sdp)
        self.assertHasIceCandidates(pc1.localDescription)
        self.assertHasDtls(pc1.localDescription, "actpass")

        # strip out candidates
        desc1 = strip_ice_candidates(pc1.localDescription)

        # handle offer
        await pc2.setRemoteDescription(desc1)
        self.assertEqual(pc2.remoteDescription, desc1)
        self.assertEqual(mids(pc2), ["0"])

        # create answer
        answer = await pc2.createAnswer()
        self.assertEqual(answer.type, "answer")
        self.assertTrue("m=application " in answer.sdp)
        self.assertFalse("a=candidate:" in answer.sdp)
        self.assertFalse("a=end-of-candidates" in answer.sdp)

        await pc2.setLocalDescription(answer)
        await self.assertIceChecking(pc2)
        self.assertTrue("m=application " in pc2.localDescription.sdp)
        self.assertHasIceCandidates(pc2.localDescription)
        self.assertHasDtls(pc2.localDescription, "active")

        # strip out candidates
        desc2 = strip_ice_candidates(pc2.localDescription)

        # handle answer
        await pc1.setRemoteDescription(desc2)
        self.assertEqual(pc1.remoteDescription, desc2)

        # trickle candidates
        for candidate in pc2.sctp.transport.transport.iceGatherer.getLocalCandidates():
            if with_mid:
                candidate.sdpMid = pc2.sctp.mid
            else:
                candidate.sdpMLineIndex = 0
            await pc1.addIceCandidate(candidate)
        for candidate in pc1.sctp.transport.transport.iceGatherer.getLocalCandidates():
            if with_mid:
                candidate.sdpMid = pc1.sctp.mid
            else:
                candidate.sdpMLineIndex = 0
            await pc2.addIceCandidate(candidate)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)
        await self.assertDataChannelOpen(dc)

        # check pc2 got a datachannel
        self.assertEqual(len(pc2_data_channels), 1)
        self.assertEqual(pc2_data_channels[0].label, "chat")
        self.assertEqual(pc2_data_channels[0].maxPacketLifeTime, None)
        self.assertEqual(pc2_data_channels[0].maxRetransmits, None)
        self.assertEqual(pc2_data_channels[0].ordered, True)
        self.assertEqual(pc2_data_channels[0].protocol, "bob")

        # check pc2 got messages
        await asyncio.sleep(0.1)
        self.assertEqual(
            pc2_data_messages, ["hello", "", b"\x00\x01\x02\x03", b"", LONG_DATA]
        )

        # check pc1 got replies
        self.assertEqual(
            pc1_data_messages,
            [
                "string-echo: hello",
                "string-echo: ",
                b"binary-echo: \x00\x01\x02\x03",
                b"binary-echo: ",
                b"binary-echo: " + LONG_DATA,
            ],
        )

        # close data channel
        await self.closeDataChannel(dc)

        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

        # check state changes
        self.assertEqual(
            pc1_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc1_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc1_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc1_states["signalingState"],
            ["stable", "have-local-offer", "stable", "closed"],
        )

        self.assertEqual(
            pc2_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc2_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc2_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc2_states["signalingState"],
            ["stable", "have-remote-offer", "stable", "closed"],
        )

    @asynctest
    async def test_connect_datachannel_trickle_with_mid(self) -> None:
        await self._test_connect_datachannel_trickle(with_mid=True)

    @asynctest
    async def test_connect_datachannel_trickle_with_mline_index(self) -> None:
        await self._test_connect_datachannel_trickle(with_mid=False)

    @asynctest
    async def test_connect_datachannel_max_packet_lifetime(self) -> None:
        pc1 = RTCPeerConnection()
        pc1_data_messages = []
        pc1_states = track_states(pc1)

        pc2 = RTCPeerConnection()
        pc2_data_channels = []
        pc2_data_messages = []
        pc2_states = track_states(pc2)

        @pc2.on("datachannel")
        def on_datachannel(channel: RTCDataChannel) -> None:
            self.assertEqual(channel.readyState, "open")
            pc2_data_channels.append(channel)

            @channel.on("message")
            def on_message(message: Union[bytes, str]) -> None:
                assert isinstance(message, str)
                pc2_data_messages.append(message)
                channel.send("string-echo: " + message)

        # create data channel
        dc = pc1.createDataChannel("chat", maxPacketLifeTime=500, protocol="bob")
        self.assertEqual(dc.label, "chat")
        self.assertEqual(dc.maxPacketLifeTime, 500)
        self.assertEqual(dc.maxRetransmits, None)
        self.assertEqual(dc.ordered, True)
        self.assertEqual(dc.protocol, "bob")
        self.assertEqual(dc.readyState, "connecting")

        # send message
        @dc.on("open")
        def on_open() -> None:
            dc.send("hello")

        @dc.on("message")
        def on_message(message: Union[bytes, str]) -> None:
            pc1_data_messages.append(message)

        # create offer
        offer = await pc1.createOffer()
        await pc1.setLocalDescription(offer)
        await pc2.setRemoteDescription(pc1.localDescription)

        # create answer
        answer = await pc2.createAnswer()
        await pc2.setLocalDescription(answer)
        await pc1.setRemoteDescription(pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)
        await self.assertDataChannelOpen(dc)

        # check pc2 got a datachannel
        self.assertEqual(len(pc2_data_channels), 1)
        self.assertEqual(pc2_data_channels[0].label, "chat")
        self.assertEqual(pc2_data_channels[0].maxPacketLifeTime, 500)
        self.assertEqual(pc2_data_channels[0].maxRetransmits, None)
        self.assertEqual(pc2_data_channels[0].ordered, True)
        self.assertEqual(pc2_data_channels[0].protocol, "bob")

        # check pc2 got message
        await asyncio.sleep(0.1)
        self.assertEqual(pc2_data_messages, ["hello"])

        # check pc1 got replies
        self.assertEqual(pc1_data_messages, ["string-echo: hello"])

        # close data channel
        await self.closeDataChannel(dc)

        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

        # check state changes
        self.assertEqual(
            pc1_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc1_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc1_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc1_states["signalingState"],
            ["stable", "have-local-offer", "stable", "closed"],
        )

        self.assertEqual(
            pc2_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc2_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc2_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc2_states["signalingState"],
            ["stable", "have-remote-offer", "stable", "closed"],
        )

    @asynctest
    async def test_connect_datachannel_max_retransmits(self) -> None:
        pc1 = RTCPeerConnection()
        pc1_data_messages = []
        pc1_states = track_states(pc1)

        pc2 = RTCPeerConnection()
        pc2_data_channels = []
        pc2_data_messages = []
        pc2_states = track_states(pc2)

        @pc2.on("datachannel")
        def on_datachannel(channel: RTCDataChannel) -> None:
            self.assertEqual(channel.readyState, "open")
            pc2_data_channels.append(channel)

            @channel.on("message")
            def on_message(message: Union[bytes, str]) -> None:
                assert isinstance(message, str)
                pc2_data_messages.append(message)
                channel.send("string-echo: " + message)

        # create data channel
        dc = pc1.createDataChannel("chat", maxRetransmits=0, protocol="bob")
        self.assertEqual(dc.label, "chat")
        self.assertEqual(dc.maxPacketLifeTime, None)
        self.assertEqual(dc.maxRetransmits, 0)
        self.assertEqual(dc.ordered, True)
        self.assertEqual(dc.protocol, "bob")
        self.assertEqual(dc.readyState, "connecting")

        # send message
        @dc.on("open")
        def on_open() -> None:
            dc.send("hello")

        @dc.on("message")
        def on_message(message: Union[bytes, str]) -> None:
            pc1_data_messages.append(message)

        # create offer
        offer = await pc1.createOffer()
        await pc1.setLocalDescription(offer)
        await pc2.setRemoteDescription(pc1.localDescription)

        # create answer
        answer = await pc2.createAnswer()
        await pc2.setLocalDescription(answer)
        await pc1.setRemoteDescription(pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)
        await self.assertDataChannelOpen(dc)

        # check pc2 got a datachannel
        self.assertEqual(len(pc2_data_channels), 1)
        self.assertEqual(pc2_data_channels[0].label, "chat")
        self.assertEqual(pc2_data_channels[0].maxPacketLifeTime, None)
        self.assertEqual(pc2_data_channels[0].maxRetransmits, 0)
        self.assertEqual(pc2_data_channels[0].ordered, True)
        self.assertEqual(pc2_data_channels[0].protocol, "bob")

        # check pc2 got message
        await asyncio.sleep(0.1)
        self.assertEqual(pc2_data_messages, ["hello"])

        # check pc1 got replies
        self.assertEqual(pc1_data_messages, ["string-echo: hello"])

        # close data channel
        await self.closeDataChannel(dc)

        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

        # check state changes
        self.assertEqual(
            pc1_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc1_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc1_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc1_states["signalingState"],
            ["stable", "have-local-offer", "stable", "closed"],
        )

        self.assertEqual(
            pc2_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc2_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc2_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc2_states["signalingState"],
            ["stable", "have-remote-offer", "stable", "closed"],
        )

    @asynctest
    async def test_connect_datachannel_unordered(self) -> None:
        pc1 = RTCPeerConnection()
        pc1_data_messages = []
        pc1_states = track_states(pc1)

        pc2 = RTCPeerConnection()
        pc2_data_channels = []
        pc2_data_messages = []
        pc2_states = track_states(pc2)

        @pc2.on("datachannel")
        def on_datachannel(channel: RTCDataChannel) -> None:
            self.assertEqual(channel.readyState, "open")
            pc2_data_channels.append(channel)

            @channel.on("message")
            def on_message(message: Union[bytes, str]) -> None:
                assert isinstance(message, str)
                pc2_data_messages.append(message)
                channel.send("string-echo: " + message)

        # create data channel
        dc = pc1.createDataChannel("chat", ordered=False, protocol="bob")
        self.assertEqual(dc.label, "chat")
        self.assertEqual(dc.maxPacketLifeTime, None)
        self.assertEqual(dc.maxRetransmits, None)
        self.assertEqual(dc.ordered, False)
        self.assertEqual(dc.protocol, "bob")
        self.assertEqual(dc.readyState, "connecting")

        # send message
        @dc.on("open")
        def on_open() -> None:
            dc.send("hello")

        @dc.on("message")
        def on_message(message: Union[bytes, str]) -> None:
            pc1_data_messages.append(message)

        # create offer
        offer = await pc1.createOffer()
        self.assertEqual(offer.type, "offer")
        self.assertTrue("m=application " in offer.sdp)
        self.assertFalse("a=candidate:" in offer.sdp)
        self.assertFalse("a=end-of-candidates" in offer.sdp)

        await pc1.setLocalDescription(offer)
        self.assertEqual(pc1.iceConnectionState, "new")
        self.assertEqual(pc1.iceGatheringState, "complete")
        self.assertEqual(mids(pc1), ["0"])
        self.assertTrue("m=application " in pc1.localDescription.sdp)
        self.assertHasIceCandidates(pc1.localDescription)
        self.assertHasDtls(pc1.localDescription, "actpass")

        # handle offer
        await pc2.setRemoteDescription(pc1.localDescription)
        self.assertEqual(pc2.remoteDescription, pc1.localDescription)
        self.assertEqual(mids(pc2), ["0"])

        # create answer
        answer = await pc2.createAnswer()
        self.assertEqual(answer.type, "answer")
        self.assertTrue("m=application " in answer.sdp)
        self.assertFalse("a=candidate:" in answer.sdp)
        self.assertFalse("a=end-of-candidates" in answer.sdp)

        await pc2.setLocalDescription(answer)
        await self.assertIceChecking(pc2)
        self.assertTrue("m=application " in pc2.localDescription.sdp)
        self.assertHasIceCandidates(pc2.localDescription)
        self.assertHasDtls(pc2.localDescription, "active")

        # handle answer
        await pc1.setRemoteDescription(pc2.localDescription)
        self.assertEqual(pc1.remoteDescription, pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)
        await self.assertDataChannelOpen(dc)

        # check pc2 got a datachannel
        self.assertEqual(len(pc2_data_channels), 1)
        self.assertEqual(pc2_data_channels[0].label, "chat")
        self.assertEqual(pc2_data_channels[0].maxPacketLifeTime, None)
        self.assertEqual(pc2_data_channels[0].maxRetransmits, None)
        self.assertEqual(pc2_data_channels[0].ordered, False)
        self.assertEqual(pc2_data_channels[0].protocol, "bob")

        # check pc2 got message
        await asyncio.sleep(0.1)
        self.assertEqual(pc2_data_messages, ["hello"])

        # check pc1 got replies
        self.assertEqual(pc1_data_messages, ["string-echo: hello"])

        # close data channel
        await self.closeDataChannel(dc)

        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

        # check state changes
        self.assertEqual(
            pc1_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc1_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc1_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc1_states["signalingState"],
            ["stable", "have-local-offer", "stable", "closed"],
        )

        self.assertEqual(
            pc2_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc2_states["iceConnectionState"], ["new", "checking", "completed", "closed"]
        )
        self.assertEqual(
            pc2_states["iceGatheringState"], ["new", "gathering", "complete"]
        )
        self.assertEqual(
            pc2_states["signalingState"],
            ["stable", "have-remote-offer", "stable", "closed"],
        )

    @asynctest
    async def test_createAnswer_closed(self) -> None:
        pc = RTCPeerConnection()
        await pc.close()
        with self.assertRaises(InvalidStateError) as cm:
            await pc.createAnswer()
        self.assertEqual(str(cm.exception), "RTCPeerConnection is closed")

    @asynctest
    async def test_createAnswer_without_offer(self) -> None:
        pc = RTCPeerConnection()
        with self.assertRaises(InvalidStateError) as cm:
            await pc.createAnswer()
        self.assertEqual(
            str(cm.exception), 'Cannot create answer in signaling state "stable"'
        )

    @asynctest
    async def test_createOffer_closed(self) -> None:
        pc = RTCPeerConnection()
        await pc.close()
        with self.assertRaises(InvalidStateError) as cm:
            await pc.createOffer()
        self.assertEqual(str(cm.exception), "RTCPeerConnection is closed")

    @asynctest
    async def test_createOffer_without_media(self) -> None:
        pc1 = RTCPeerConnection()
        pc2 = RTCPeerConnection()

        offer = await pc1.createOffer()
        await pc1.setLocalDescription(offer)
        await pc2.setRemoteDescription(offer)

        answer = await pc2.createAnswer()
        await pc2.setLocalDescription(answer)
        await pc1.setRemoteDescription(answer)

        await pc1.close()
        await pc2.close()

    @asynctest
    async def test_setRemoteDescription_unexpected_answer(self) -> None:
        pc = RTCPeerConnection()
        with self.assertRaises(InvalidStateError) as cm:
            await pc.setRemoteDescription(RTCSessionDescription(sdp="", type="answer"))
        self.assertEqual(
            str(cm.exception), 'Cannot handle answer in signaling state "stable"'
        )

        # close
        await pc.close()

    @asynctest
    async def test_dtls_role_offer_actpass(self) -> None:
        pc1 = RTCPeerConnection()
        pc2 = RTCPeerConnection()

        pc1_states = track_states(pc1)
        pc2_states = track_states(pc2)

        self.assertEqual(pc1.iceConnectionState, "new")
        self.assertEqual(pc1.iceGatheringState, "new")
        self.assertIsNone(pc1.localDescription)
        self.assertIsNone(pc1.remoteDescription)

        self.assertEqual(pc2.iceConnectionState, "new")
        self.assertEqual(pc2.iceGatheringState, "new")
        self.assertIsNone(pc2.localDescription)
        self.assertIsNone(pc2.remoteDescription)

        # create offer
        pc1.createDataChannel("chat", protocol="")
        offer = await pc1.createOffer()
        self.assertEqual(offer.type, "offer")

        await pc1.setLocalDescription(offer)
        self.assertEqual(pc1.iceConnectionState, "new")
        self.assertEqual(pc1.iceGatheringState, "complete")

        # set remote description
        await pc2.setRemoteDescription(pc1.localDescription)

        # create answer
        answer = await pc2.createAnswer()
        self.assertHasDtls(answer, "active")

        await pc2.setLocalDescription(answer)
        await self.assertIceChecking(pc2)

        # handle answer
        await pc1.setRemoteDescription(pc2.localDescription)
        self.assertEqual(pc1.remoteDescription, pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)

        self.assertEqual(pc1.sctp.transport._role, "server")
        self.assertEqual(pc2.sctp.transport._role, "client")
        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

        # check state changes
        self.assertEqual(
            pc1_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc2_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )

    @asynctest
    async def test_dtls_role_offer_passive(self) -> None:
        pc1 = RTCPeerConnection()
        pc2 = RTCPeerConnection()

        pc1_states = track_states(pc1)
        pc2_states = track_states(pc2)

        self.assertEqual(pc1.iceConnectionState, "new")
        self.assertEqual(pc1.iceGatheringState, "new")
        self.assertIsNone(pc1.localDescription)
        self.assertIsNone(pc1.remoteDescription)

        self.assertEqual(pc2.iceConnectionState, "new")
        self.assertEqual(pc2.iceGatheringState, "new")
        self.assertIsNone(pc2.localDescription)
        self.assertIsNone(pc2.remoteDescription)

        # create offer
        pc1.createDataChannel("chat", protocol="")
        offer = await pc1.createOffer()
        self.assertEqual(offer.type, "offer")

        await pc1.setLocalDescription(offer)
        self.assertEqual(pc1.iceConnectionState, "new")
        self.assertEqual(pc1.iceGatheringState, "complete")

        # handle offer with replaced DTLS role
        await pc2.setRemoteDescription(
            RTCSessionDescription(
                type="offer", sdp=pc1.localDescription.sdp.replace("actpass", "passive")
            )
        )

        # create answer
        answer = await pc2.createAnswer()
        self.assertHasDtls(answer, "active")

        await pc2.setLocalDescription(answer)
        await self.assertIceChecking(pc2)

        # handle answer
        await pc1.setRemoteDescription(pc2.localDescription)
        self.assertEqual(pc1.remoteDescription, pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)

        # pc1 is explicity passive so server.
        self.assertEqual(pc1.sctp.transport._role, "server")
        self.assertEqual(pc2.sctp.transport._role, "client")
        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

        # check state changes
        self.assertEqual(
            pc1_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc2_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )

    @asynctest
    async def test_dtls_role_offer_active(self) -> None:
        pc1 = RTCPeerConnection()
        pc2 = RTCPeerConnection()

        pc1_states = track_states(pc1)
        pc2_states = track_states(pc2)

        self.assertEqual(pc1.iceConnectionState, "new")
        self.assertEqual(pc1.iceGatheringState, "new")
        self.assertIsNone(pc1.localDescription)
        self.assertIsNone(pc1.remoteDescription)

        self.assertEqual(pc2.iceConnectionState, "new")
        self.assertEqual(pc2.iceGatheringState, "new")
        self.assertIsNone(pc2.localDescription)
        self.assertIsNone(pc2.remoteDescription)

        # create offer
        pc1.createDataChannel("chat", protocol="")
        offer = await pc1.createOffer()
        self.assertEqual(offer.type, "offer")

        await pc1.setLocalDescription(offer)
        self.assertEqual(pc1.iceConnectionState, "new")
        self.assertEqual(pc1.iceGatheringState, "complete")

        # handle offer with replaced DTLS role
        await pc2.setRemoteDescription(
            RTCSessionDescription(
                type="offer", sdp=pc1.localDescription.sdp.replace("actpass", "active")
            )
        )

        # create answer
        answer = await pc2.createAnswer()
        self.assertHasDtls(answer, "passive")

        await pc2.setLocalDescription(answer)
        await self.assertIceChecking(pc2)

        # handle answer
        await pc1.setRemoteDescription(pc2.localDescription)
        self.assertEqual(pc1.remoteDescription, pc2.localDescription)

        # check outcome
        await self.assertIceCompleted(pc1, pc2)

        # pc1 is explicity active so client.
        self.assertEqual(pc1.sctp.transport._role, "client")
        self.assertEqual(pc2.sctp.transport._role, "server")
        # close
        await pc1.close()
        await pc2.close()
        self.assertClosed(pc1)
        self.assertClosed(pc2)

        # check state changes
        self.assertEqual(
            pc1_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
        self.assertEqual(
            pc2_states["connectionState"], ["new", "connecting", "connected", "closed"]
        )
