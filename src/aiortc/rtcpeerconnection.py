import asyncio
import logging
import uuid
from typing import Optional

from pyee.asyncio import AsyncIOEventEmitter

from . import clock, sdp
from .exceptions import InvalidStateError
from .rtcconfiguration import RTCConfiguration
from .rtcdatachannel import RTCDataChannel, RTCDataChannelParameters
from .rtcdtlstransport import RTCCertificate, RTCDtlsParameters, RTCDtlsTransport
from .rtcicetransport import (
    RTCIceCandidate,
    RTCIceGatherer,
    RTCIceParameters,
    RTCIceTransport,
)
from .rtcrtpparameters import RTCRtpHeaderExtensionParameters
from .rtcsctptransport import RTCSctpCapabilities, RTCSctpTransport
from .rtcsessiondescription import RTCSessionDescription

DISCARD_HOST = "0.0.0.0"
DISCARD_PORT = 9

logger = logging.getLogger(__name__)


def find_common_header_extensions(
    local_extensions: list[RTCRtpHeaderExtensionParameters],
    remote_extensions: list[RTCRtpHeaderExtensionParameters],
) -> list[RTCRtpHeaderExtensionParameters]:
    common = []
    for rx in remote_extensions:
        for lx in local_extensions:
            if lx.uri == rx.uri:
                common.append(rx)
    return common


def add_transport_description(
    media: sdp.MediaDescription, dtlsTransport: RTCDtlsTransport
) -> None:
    # ice
    iceTransport = dtlsTransport.transport
    iceGatherer = iceTransport.iceGatherer
    media.ice_candidates = iceGatherer.getLocalCandidates()
    media.ice_candidates_complete = iceGatherer.state == "completed"
    media.ice = iceGatherer.getLocalParameters()
    if media.ice_candidates:
        media.host = media.ice_candidates[0].ip
        media.port = media.ice_candidates[0].port
    else:
        media.host = DISCARD_HOST
        media.port = DISCARD_PORT

    # dtls
    if media.dtls is None:
        media.dtls = dtlsTransport.getLocalParameters()
    else:
        media.dtls.fingerprints = dtlsTransport.getLocalParameters().fingerprints


async def add_remote_candidates(
    iceTransport: RTCIceTransport, media: sdp.MediaDescription
) -> None:
    coros = map(iceTransport.addRemoteCandidate, media.ice_candidates)
    await asyncio.gather(*coros)

    if media.ice_candidates_complete:
        await iceTransport.addRemoteCandidate(None)


def allocate_mid(mids: set[str]) -> str:
    """
    Allocate a MID which has not been used yet.
    """
    i = 0
    while True:
        mid = str(i)
        if mid not in mids:
            mids.add(mid)
            return mid
        i += 1


def create_media_description_for_sctp(
    sctp: RTCSctpTransport, legacy: bool, mid: str
) -> sdp.MediaDescription:
    if legacy:
        media = sdp.MediaDescription(
            kind="application", port=DISCARD_PORT, profile="DTLS/SCTP", fmt=[sctp.port]
        )
        media.sctpmap[sctp.port] = f"webrtc-datachannel {sctp._outbound_streams_count}"
    else:
        media = sdp.MediaDescription(
            kind="application",
            port=DISCARD_PORT,
            profile="UDP/DTLS/SCTP",
            fmt=["webrtc-datachannel"],
        )
        media.sctp_port = sctp.port

    media.rtp.muxId = mid
    media.sctpCapabilities = sctp.getCapabilities()
    add_transport_description(media, sctp.transport)

    return media


def and_direction(a: str, b: str) -> str:
    return sdp.DIRECTIONS[sdp.DIRECTIONS.index(a) & sdp.DIRECTIONS.index(b)]


def or_direction(a: str, b: str) -> str:
    return sdp.DIRECTIONS[sdp.DIRECTIONS.index(a) | sdp.DIRECTIONS.index(b)]


def reverse_direction(direction: str) -> str:
    if direction == "sendonly":
        return "recvonly"
    elif direction == "recvonly":
        return "sendonly"
    return direction


def wrap_session_description(
    session_description: Optional[sdp.SessionDescription],
) -> Optional[RTCSessionDescription]:
    if session_description is not None:
        return RTCSessionDescription(
            sdp=str(session_description), type=session_description.type
        )
    return None


class RTCPeerConnection(AsyncIOEventEmitter):
    """
    The :class:`RTCPeerConnection` interface represents a WebRTC connection
    between the local computer and a remote peer.

    :param configuration: An optional :class:`RTCConfiguration`.
    """

    def __init__(self, configuration: Optional[RTCConfiguration] = None) -> None:
        super().__init__()
        self.__certificates = [RTCCertificate.generateCertificate()]
        self.__cname = f"{uuid.uuid4()}"
        self.__configuration = configuration or RTCConfiguration()
        self.__dtlsTransports: set[RTCDtlsTransport] = set()
        self.__iceTransports: set[RTCIceTransport] = set()
        self.__remoteDtls: dict[RTCSctpTransport, RTCDtlsParameters] = {}
        self.__remoteIce: dict[RTCSctpTransport, RTCIceParameters] = {}
        self.__seenMids: set[str] = set()
        self.__sctp: Optional[RTCSctpTransport] = None
        self.__sctp_mline_index: Optional[int] = None
        self._sctpLegacySdp = True
        self.__sctpRemotePort: Optional[int] = None
        self.__sctpRemoteCaps: Optional[RTCSctpCapabilities] = None
        self.__stream_id = str(uuid.uuid4())

        self.__closeTask: Optional[asyncio.Task] = None
        self.__connectionState = "new"
        self.__iceConnectionState = "new"
        self.__iceGatheringState = "new"
        self.__isClosed: Optional[asyncio.Future[bool]] = None
        self.__signalingState = "stable"

        self.__currentLocalDescription: Optional[sdp.SessionDescription] = None
        self.__currentRemoteDescription: Optional[sdp.SessionDescription] = None
        self.__pendingLocalDescription: Optional[sdp.SessionDescription] = None
        self.__pendingRemoteDescription: Optional[sdp.SessionDescription] = None

    @property
    def connectionState(self) -> str:
        """
        The current connection state.

        Possible values: `"connected"`, `"connecting"`, `"closed"`, `"failed"`, `"new`".

        When the state changes, the `"connectionstatechange"` event is fired.
        """
        return self.__connectionState

    @property
    def iceConnectionState(self) -> str:
        """
        The current ICE connection state.

        Possible values: `"checking"`, `"completed"`, `"closed"`, `"failed"`, `"new`".

        When the state changes, the `"iceconnectionstatechange"` event is fired.
        """
        return self.__iceConnectionState

    @property
    def iceGatheringState(self) -> str:
        """
        The current ICE gathering state.

        Possible values: `"complete"`, `"gathering"`, `"new`".

        When the state changes, the `"icegatheringstatechange"` event is fired.
        """
        return self.__iceGatheringState

    @property
    def localDescription(self) -> RTCSessionDescription:
        """
        An :class:`RTCSessionDescription` describing the session for
        the local end of the connection.
        """
        return wrap_session_description(self.__localDescription())

    @property
    def remoteDescription(self) -> RTCSessionDescription:
        """
        An :class:`RTCSessionDescription` describing the session for
        the remote end of the connection.
        """
        return wrap_session_description(self.__remoteDescription())

    @property
    def sctp(self) -> Optional[RTCSctpTransport]:
        """
        An :class:`RTCSctpTransport` describing the SCTP transport being used
        for datachannels or `None`.
        """
        return self.__sctp

    @property
    def signalingState(self) -> str:
        """
        The current signaling state.

        Possible values: `"closed"`, `"have-local-offer"`, `"have-remote-offer`",
        `"stable"`.

        When the state changes, the `"signalingstatechange"` event is fired.
        """
        return self.__signalingState

    async def addIceCandidate(self, candidate: Optional[RTCIceCandidate]) -> None:
        """
        Add a new :class:`RTCIceCandidate` received from the remote peer.

        The specified candidate must have a value for either `sdpMid` or
        `sdpMLineIndex`.

        :param candidate: The new remote candidate or `None` to signal
                            end-of-candidates.
        """
        if (
            candidate is not None
            and candidate.sdpMid is None
            and candidate.sdpMLineIndex is None
        ):
            raise ValueError("Candidate must have either sdpMid or sdpMLineIndex")

        if self.__sctp and (
            candidate is None
            or (
                candidate.sdpMid == self.__sctp.mid
                or candidate.sdpMLineIndex == self.__sctp_mline_index
            )
            and not self.__sctp._bundled
        ):
            iceTransport = self.__sctp.transport.transport
            await iceTransport.addRemoteCandidate(candidate)

        # Update the remote description.
        media = self.__remoteDescription().media
        for sdp_m_line_index in range(0, len(media)):
            if candidate is None:
                media[sdp_m_line_index].ice_candidates_complete = True
            elif (
                candidate.sdpMLineIndex == sdp_m_line_index
                or candidate.sdpMid == media[sdp_m_line_index].rtp.muxId
            ):
                media[sdp_m_line_index].ice_candidates.append(candidate)

    async def close(self) -> None:
        """
        Terminate the ICE agent, ending ICE processing and streams.
        """
        if self.__isClosed:
            await self.__isClosed
            return
        self.__isClosed = asyncio.Future()
        self.__setSignalingState("closed")

        if self.__sctp:
            await self.__sctp.transport.stop()
            await self.__sctp.transport.transport.stop()

        # update states
        self.__updateIceGatheringState()
        self.__updateIceConnectionState()
        self.__updateConnectionState()

        # no more events will be emitted, so remove all event listeners
        # to facilitate garbage collection.
        self.remove_all_listeners()

        self.__isClosed.set_result(True)

    async def createAnswer(self) -> RTCSessionDescription:
        """
        Create an SDP answer to an offer received from a remote peer during
        the offer/answer negotiation of a WebRTC connection.

        :rtype: :class:`RTCSessionDescription`
        """
        # check state is valid
        self.__assertNotClosed()
        if self.signalingState not in ["have-remote-offer", "have-local-pranswer"]:
            raise InvalidStateError(
                f'Cannot create answer in signaling state "{self.signalingState}"'
            )

        # create description
        ntp_seconds = clock.current_ntp_time() >> 32
        description = sdp.SessionDescription()
        description.origin = f"- {ntp_seconds} {ntp_seconds} IN IP4 0.0.0.0"
        description.msid_semantic.append(
            sdp.GroupDescription(semantic="WMS", items=["*"])
        )
        description.type = "answer"

        for remote_m in self.__remoteDescription().media:
            media = create_media_description_for_sctp(
                self.__sctp, legacy=self._sctpLegacySdp, mid=self.__sctp.mid
            )
            dtlsTransport = self.__sctp.transport

            # determine DTLS role, or preserve the currently configured role
            if dtlsTransport._role == "auto":
                media.dtls.role = "client"
            else:
                media.dtls.role = dtlsTransport._role

            description.media.append(media)

        bundle = sdp.GroupDescription(semantic="BUNDLE", items=[])
        for media in description.media:
            bundle.items.append(media.rtp.muxId)
        description.group.append(bundle)

        return wrap_session_description(description)

    def createDataChannel(
        self,
        label: str,
        maxPacketLifeTime: Optional[int] = None,
        maxRetransmits: Optional[int] = None,
        ordered: bool = True,
        protocol: str = "",
        negotiated: bool = False,
        id: Optional[int] = None,
    ) -> RTCDataChannel:
        """
        Create a data channel with the given label.

        :rtype: :class:`RTCDataChannel`
        """
        if maxPacketLifeTime is not None and maxRetransmits is not None:
            raise ValueError("Cannot specify both maxPacketLifeTime and maxRetransmits")

        if not self.__sctp:
            self.__createSctpTransport()

        parameters = RTCDataChannelParameters(
            id=id,
            label=label,
            maxPacketLifeTime=maxPacketLifeTime,
            maxRetransmits=maxRetransmits,
            negotiated=negotiated,
            ordered=ordered,
            protocol=protocol,
        )
        return RTCDataChannel(self.__sctp, parameters)

    async def createOffer(self) -> RTCSessionDescription:
        """
        Create an SDP offer for the purpose of starting a new WebRTC
        connection to a remote peer.

        :rtype: :class:`RTCSessionDescription`
        """
        # check state is valid
        self.__assertNotClosed()

        mids = self.__seenMids.copy()

        # create description
        ntp_seconds = clock.current_ntp_time() >> 32
        description = sdp.SessionDescription()
        description.origin = f"- {ntp_seconds} {ntp_seconds} IN IP4 0.0.0.0"
        description.msid_semantic.append(
            sdp.GroupDescription(semantic="WMS", items=["*"])
        )
        description.type = "offer"

        def get_media(
            description: sdp.SessionDescription,
        ) -> list[sdp.MediaDescription]:
            return description.media if description else []

        def get_media_section(
            media: list[sdp.MediaDescription], i: int
        ) -> Optional[sdp.MediaDescription]:
            return media[i] if i < len(media) else None

        # handle existing sctp
        local_media = get_media(self.__localDescription())
        remote_media = get_media(self.__remoteDescription())
        for i in range(max(len(local_media), len(remote_media))):
            local_m = get_media_section(local_media, i)
            remote_m = get_media_section(remote_media, i)
            media_kind = local_m.kind if local_m else remote_m.kind
            mid = local_m.rtp.muxId if local_m else remote_m.rtp.muxId
            if media_kind == "application":
                self.__sctp_mline_index = i
                description.media.append(
                    create_media_description_for_sctp(
                        self.__sctp, legacy=self._sctpLegacySdp, mid=mid
                    )
                )

        # handle new sctp
        def next_mline_index() -> int:
            return len(description.media)

        if self.__sctp and self.__sctp.mid is None:
            self.__sctp_mline_index = next_mline_index()
            description.media.append(
                create_media_description_for_sctp(
                    self.__sctp, legacy=self._sctpLegacySdp, mid=allocate_mid(mids)
                )
            )

        bundle = sdp.GroupDescription(semantic="BUNDLE", items=[])
        for media in description.media:
            bundle.items.append(media.rtp.muxId)
        description.group.append(bundle)

        return wrap_session_description(description)

    async def setLocalDescription(
        self, sessionDescription: Optional[RTCSessionDescription] = None
    ) -> None:
        """
        Change the local description associated with the connection.

        :param sessionDescription: An :class:`RTCSessionDescription` generated
                                    by :meth:`createOffer` or :meth:`createAnswer()`
                                    or `None` to implicitly create an offer or create
                                    an answer, as needed.
        """
        # check state is valid
        self.__assertNotClosed()

        if sessionDescription is None:
            # https://w3c.github.io/webrtc-pc/#dom-peerconnection-setlocaldescription
            # If left out, then setLocalDescription will implicitly create an offer
            # or create an answer, as needed.
            if self.signalingState == "have-remote-offer":
                sessionDescription = await self.createAnswer()
            else:
                sessionDescription = await self.createOffer()
            self.__log_debug(
                "setLocalDescription(%s, implicit)\n%s",
                sessionDescription.type,
                sessionDescription.sdp,
            )
        else:
            self.__log_debug(
                "setLocalDescription(%s)\n%s",
                sessionDescription.type,
                sessionDescription.sdp,
            )

        # parse and validate description
        description = sdp.SessionDescription.parse(sessionDescription.sdp)
        description.type = sessionDescription.type
        self.__validate_description(description, is_local=True)

        # update signaling state
        if description.type == "offer":
            self.__setSignalingState("have-local-offer")
        elif description.type == "answer":
            self.__setSignalingState("stable")

        # assign MID
        for i, media in enumerate(description.media):
            mid = media.rtp.muxId
            self.__seenMids.add(mid)
            if media.kind == "application":
                self.__sctp.mid = mid

        # set ICE role
        if description.type == "offer":
            for iceTransport in self.__iceTransports:
                if not iceTransport._role_set:
                    iceTransport._connection.ice_controlling = True
                    iceTransport._role_set = True

        # set DTLS role
        if description.type == "answer":
            for i, media in enumerate(description.media):
                if media.kind == "application":
                    self.__sctp.transport._set_role(media.dtls.role)

        # gather candidates
        await self.__gather()
        for i, media in enumerate(description.media):
            if media.kind == "application":
                add_transport_description(media, self.__sctp.transport)

        # connect
        asyncio.ensure_future(self.__connect())

        # replace description
        if description.type == "answer":
            self.__currentLocalDescription = description
            self.__pendingLocalDescription = None
        else:
            self.__pendingLocalDescription = description

    async def setRemoteDescription(
        self, sessionDescription: RTCSessionDescription
    ) -> None:
        """
        Changes the remote description associated with the connection.

        :param sessionDescription: An :class:`RTCSessionDescription` created from
                                    information received over the signaling channel.
        """
        self.__log_debug(
            "setRemoteDescription(%s)\n%s",
            sessionDescription.type,
            sessionDescription.sdp,
        )

        # parse and validate description
        description = sdp.SessionDescription.parse(sessionDescription.sdp)
        description.type = sessionDescription.type
        self.__validate_description(description, is_local=False)

        # apply description
        iceCandidates: dict[RTCIceTransport, sdp.MediaDescription] = {}
        for i, media in enumerate(description.media):
            dtlsTransport: Optional[RTCDtlsTransport] = None
            self.__seenMids.add(media.rtp.muxId)
            if media.kind == "application":
                if not self.__sctp:
                    self.__createSctpTransport()
                if self.__sctp.mid is None:
                    self.__sctp.mid = media.rtp.muxId
                    self.__sctp_mline_index = i

                # configure sctp
                if media.profile == "DTLS/SCTP":
                    self._sctpLegacySdp = True
                    self.__sctpRemotePort = int(media.fmt[0])
                else:
                    self._sctpLegacySdp = False
                    self.__sctpRemotePort = media.sctp_port
                self.__sctpRemoteCaps = media.sctpCapabilities

                # memorise transport parameters
                dtlsTransport = self.__sctp.transport
                self.__remoteDtls[self.__sctp] = media.dtls
                self.__remoteIce[self.__sctp] = media.ice

            if dtlsTransport is not None:
                # add ICE candidates
                iceTransport = dtlsTransport.transport
                iceCandidates[iceTransport] = media

                # set ICE role
                if description.type == "offer" and not iceTransport._role_set:
                    iceTransport._connection.ice_controlling = media.ice.iceLite
                    iceTransport._role_set = True

                # set DTLS role
                if description.type == "offer" and media.dtls.role == "client":
                    dtlsTransport._set_role(role="server")
                if description.type == "answer":
                    dtlsTransport._set_role(
                        role="server" if media.dtls.role == "client" else "client"
                    )

        # remove bundled transports
        bundle = next((x for x in description.group if x.semantic == "BUNDLE"), None)
        if bundle and bundle.items:
            # find main media stream
            primaryMid = bundle.items[0]
            primaryTransport = None
            if self.__sctp and self.__sctp.mid == primaryMid:
                primaryTransport = self.__sctp.transport

            # replace transport for bundled media
            oldTransports = set()
            slaveMids = bundle.items[1:]
            if (
                self.__sctp
                and self.__sctp.mid in slaveMids
                and not self.__sctp._bundled
            ):
                oldTransports.add(self.__sctp.transport)
                self.__sctp.setTransport(primaryTransport)
                self.__sctp._bundled = True

            # stop and discard old ICE transports
            for dtlsTransport in oldTransports:
                await dtlsTransport.stop()
                await dtlsTransport.transport.stop()
                self.__dtlsTransports.discard(dtlsTransport)
                self.__iceTransports.discard(dtlsTransport.transport)
                iceCandidates.pop(dtlsTransport.transport, None)
            self.__updateIceGatheringState()
            self.__updateIceConnectionState()
            self.__updateConnectionState()

        # add remote candidates
        coros = [
            add_remote_candidates(iceTransport, media)
            for iceTransport, media in iceCandidates.items()
        ]
        await asyncio.gather(*coros)

        # connect
        asyncio.ensure_future(self.__connect())

        # update signaling state
        if description.type == "offer":
            self.__setSignalingState("have-remote-offer")
        elif description.type == "answer":
            self.__setSignalingState("stable")

        # replace description
        if description.type == "answer":
            self.__currentRemoteDescription = description
            self.__pendingRemoteDescription = None
        else:
            self.__pendingRemoteDescription = description

    async def __connect(self) -> None:
        if self.__sctp:
            dtlsTransport = self.__sctp.transport
            iceTransport = dtlsTransport.transport
            if (
                iceTransport.iceGatherer.getLocalCandidates()
                and self.__sctp in self.__remoteIce
            ):
                await iceTransport.start(self.__remoteIce[self.__sctp])
                if dtlsTransport.state == "new":
                    await dtlsTransport.start(self.__remoteDtls[self.__sctp])
                if dtlsTransport.state == "connected":
                    await self.__sctp.start(
                        self.__sctpRemoteCaps, self.__sctpRemotePort
                    )

    async def __gather(self) -> None:
        coros = map(lambda t: t.iceGatherer.gather(), self.__iceTransports)
        await asyncio.gather(*coros)

    def __assertNotClosed(self) -> None:
        if self.__isClosed:
            raise InvalidStateError("RTCPeerConnection is closed")

    def __createDtlsTransport(self) -> RTCDtlsTransport:
        # create ICE transport
        if self.__sctp:
            parameters = (
                self.__sctp.transport.transport.iceGatherer.getLocalParameters()
            )
            iceGatherer = RTCIceGatherer(
                iceServers=self.__configuration.iceServers,
                local_username=parameters.usernameFragment,
                local_password=parameters.password,
            )
        else:
            iceGatherer = RTCIceGatherer(iceServers=self.__configuration.iceServers)

        iceGatherer.on("statechange", self.__updateIceGatheringState)
        iceTransport = RTCIceTransport(iceGatherer)
        iceTransport.on("statechange", self.__updateIceConnectionState)
        iceTransport.on("statechange", self.__updateConnectionState)
        self.__iceTransports.add(iceTransport)

        # create DTLS transport
        dtlsTransport = RTCDtlsTransport(iceTransport, self.__certificates)
        dtlsTransport.on("statechange", self.__updateConnectionState)
        self.__dtlsTransports.add(dtlsTransport)

        # update states
        self.__updateIceGatheringState()
        self.__updateIceConnectionState()
        self.__updateConnectionState()

        return dtlsTransport

    def __createSctpTransport(self) -> None:
        dtlsTransport = None
        dtlsTransport = self.__createDtlsTransport()
        self.__sctp = RTCSctpTransport(dtlsTransport)
        self.__sctp._bundled = False
        self.__sctp.mid = None

        @self.__sctp.on("datachannel")
        def on_datachannel(channel: RTCDataChannel) -> None:
            self.emit("datachannel", channel)

    def __localDescription(self) -> Optional[sdp.SessionDescription]:
        return self.__pendingLocalDescription or self.__currentLocalDescription

    def __log_debug(self, msg: str, *args: object) -> None:
        logger.debug(f"RTCPeerConnection() {msg}", *args)

    def __remoteDescription(self) -> Optional[sdp.SessionDescription]:
        return self.__pendingRemoteDescription or self.__currentRemoteDescription

    def __setSignalingState(self, state: str) -> None:
        self.__signalingState = state
        self.emit("signalingstatechange")

    def __updateConnectionState(self) -> None:
        # compute new state
        # NOTE: we do not have a "disconnected" state
        dtlsStates = set(map(lambda x: x.state, self.__dtlsTransports))
        iceStates = set(map(lambda x: x.state, self.__iceTransports))
        if self.__isClosed:
            state = "closed"
        elif "failed" in iceStates or "failed" in dtlsStates:
            state = "failed"
        elif not iceStates.difference(["new", "closed"]) and not dtlsStates.difference(
            ["new", "closed"]
        ):
            state = "new"
        elif "checking" in iceStates or "connecting" in dtlsStates:
            state = "connecting"
        elif "new" in dtlsStates:
            # this avoids a spurious connecting -> connected -> connecting
            # transition after ICE connects but before DTLS starts
            state = "connecting"
        else:
            state = "connected"

        # update state
        if state != self.__connectionState:
            self.__log_debug("connectionState %s -> %s", self.__connectionState, state)
            self.__connectionState = state
            self.emit("connectionstatechange")

        # if all DTLS connections are closed, initiate a shutdown
        if (
            not self.__isClosed
            and self.__closeTask is None
            and dtlsStates == set(["closed"])
        ):
            self.__closeTask = asyncio.ensure_future(self.close())

    def __updateIceConnectionState(self) -> None:
        # compute new state
        # NOTE: we do not have "connected" or "disconnected" states
        states = set(map(lambda x: x.state, self.__iceTransports))
        if self.__isClosed:
            state = "closed"
        elif "failed" in states:
            state = "failed"
        elif states == set(["completed"]):
            state = "completed"
        elif "checking" in states:
            state = "checking"
        else:
            state = "new"

        # update state
        if state != self.__iceConnectionState:
            self.__log_debug(
                "iceConnectionState %s -> %s", self.__iceConnectionState, state
            )
            self.__iceConnectionState = state
            self.emit("iceconnectionstatechange")

    def __updateIceGatheringState(self) -> None:
        # compute new state
        states = set(map(lambda x: x.iceGatherer.state, self.__iceTransports))
        if states == set(["completed"]):
            state = "complete"
        elif "gathering" in states:
            state = "gathering"
        else:
            state = "new"

        # update state
        if state != self.__iceGatheringState:
            self.__log_debug(
                "iceGatheringState %s -> %s", self.__iceGatheringState, state
            )
            self.__iceGatheringState = state
            self.emit("icegatheringstatechange")

    def __validate_description(
        self, description: sdp.SessionDescription, is_local: bool
    ) -> None:
        # check description is compatible with signaling state
        if is_local:
            if description.type == "offer":
                if self.signalingState not in ["stable", "have-local-offer"]:
                    raise InvalidStateError(
                        "Cannot handle offer in signaling state "
                        f'"{self.signalingState}"'
                    )
            elif description.type == "answer":
                if self.signalingState not in [
                    "have-remote-offer",
                    "have-local-pranswer",
                ]:
                    raise InvalidStateError(
                        "Cannot handle answer in signaling state "
                        f'"{self.signalingState}"'
                    )
        else:
            if description.type == "offer":
                if self.signalingState not in ["stable", "have-remote-offer"]:
                    raise InvalidStateError(
                        "Cannot handle offer in signaling state "
                        f'"{self.signalingState}"'
                    )
            elif description.type == "answer":
                if self.signalingState not in [
                    "have-local-offer",
                    "have-remote-pranswer",
                ]:
                    raise InvalidStateError(
                        "Cannot handle answer in signaling state "
                        f'"{self.signalingState}"'
                    )

        for media in description.media:
            # check ICE credentials were provided
            if not media.ice.usernameFragment or not media.ice.password:
                raise ValueError("ICE username fragment or password is missing")

            # check DTLS role is allowed
            if description.type in ["answer", "pranswer"] and media.dtls.role not in [
                "client",
                "server",
            ]:
                raise ValueError(
                    "DTLS setup attribute must be 'active' or 'passive' for an answer"
                )

        # check the number of media section matches
        if description.type in ["answer", "pranswer"]:
            offer = (
                self.__remoteDescription() if is_local else self.__localDescription()
            )
            offer_media = [(media.kind, media.rtp.muxId) for media in offer.media]
            answer_media = [
                (media.kind, media.rtp.muxId) for media in description.media
            ]
            if answer_media != offer_media:
                raise ValueError("Media sections in answer do not match offer")
