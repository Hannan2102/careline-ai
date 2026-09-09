# ADR 007: A plain WebSocket for browser audio, not WebRTC

**Status:** Accepted (Phase 13)

## Context

The browser needs to send microphone audio to the agent and play the agent's replies,
with barge-in — the caller can interrupt mid-sentence and the agent stops.

The obvious choice is WebRTC, usually via a hosted SFU such as LiveKit. It is what
production voice agents use, and the Phase 15 telephony path (Twilio → SIP) genuinely
wants one.

## The reasoning that nearly went wrong

The first argument for LiveKit written down in this project was that a WebSocket "loses
echo cancellation." That is false, and it is worth recording because it is the kind of
mistake that silently decides an architecture.

Barge-in requires acoustic echo cancellation. Without it the microphone hears the
speakers, the recogniser transcribes the agent's own voice, and the agent interrupts
itself in a loop. But AEC is applied by the **browser**, in its audio pipeline, via
`getUserMedia({audio: {echoCancellation: true}})`. It has nothing to do with what
carries the bytes afterwards. Chrome supplies it to a WebSocket page exactly as it
supplies it to a WebRTC one; headphones remove the echo path altogether.

What WebRTC actually adds is **network** robustness: jitter buffering, packet-loss
concealment, adaptive bitrate, NAT traversal, and the SIP interworking telephony needs.

## Decision

Use a plain WebSocket (`/ws/voice`) for the browser path. Revisit for Phase 15, where
SIP makes WebRTC load-bearing for reasons that have nothing to do with this decision.

For a demo where the browser and the server are on the same machine, none of WebRTC's
advantages apply and one of its costs does: a hosted SFU routes audio through the
vendor's edge, adding a round trip to a budget measured in hundreds of milliseconds.

It also removes a vendor, a set of credentials, and a client SDK from a project whose
entire budget is $20.

## Consequences

The transport is ~200 lines and has no dependencies beyond FastAPI's WebSocket support.

**The client must be told to discard its playback queue.** This is the one real cost, and
it is not obvious. Cancelling synthesis server-side stops us *producing* audio, which is
all a unit test can observe — but everything already sent is sitting in scheduled Web
Audio nodes with start times in the future. Without an explicit signal the agent keeps
talking for a second or more after being interrupted, which is precisely what barge-in
exists to prevent, and no test that lacks a client holding a buffer would catch it.

So the turn manager takes an `on_interrupt` callback, fired **before** cancellation, and
the wire protocol carries an `interrupt` event. WebRTC would not have made this
unnecessary — the same queue exists there — but it makes it explicit here.

Audio is unreliable over a lossy network: a WebSocket over TCP head-of-line blocks rather
than concealing loss. Acceptable on localhost and on a LAN; not acceptable over the open
internet, which is a deployment this project does not have.

## Alternatives

**LiveKit Cloud.** Correct for production and for Phase 15. Adds a vendor, a round trip,
and an SDK to solve a problem this deployment does not have.

**Self-hosted LiveKit.** All of the complexity, none of the hosted convenience, plus a
container to run.

**MediaRecorder posting chunks over HTTP.** Simplest of all, and fatal: it produces
encoded chunks at fixed intervals, so barge-in latency is bounded below by the chunk
length, and streaming recognition never starts early.
