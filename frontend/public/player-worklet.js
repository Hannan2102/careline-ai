/**
 * Continuous playback from a ring buffer.
 *
 * The obvious approach — one `AudioBufferSourceNode` per chunk, scheduled back
 * to back — does not survive contact with a real stream. Groq sends ~88 chunks
 * for a single sentence, at irregular sizes and irregular times, so that design
 * offers 88 chances for a seam: any chunk that arrives after its scheduled
 * start time leaves a gap, and a gap is an audible click.
 *
 * A ring buffer instead. The audio thread pulls whatever is available at a
 * steady rate and plays silence when it runs dry, so a late chunk delays the
 * audio rather than punching a hole in it. Barge-in becomes a pointer reset,
 * which is both instant and impossible to get half-right.
 */

// Thirty seconds at 24 kHz (~2.9 MB).
//
// It was ten, which was not enough and failed in the worst way: the server
// sends at roughly the rate audio is heard now, but a jitter spike or a long
// utterance must never reach the point where the writer laps the reader —
// overrun drops samples, and dropped samples are the crackle this buffer
// exists to prevent. Sized for the longest utterance the agent can produce
// rather than for the steady state.
const CAPACITY = 24000 * 30;

class PlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ring = new Float32Array(CAPACITY);
    this.read = 0;
    this.write = 0;
    this.draining = false;
    this.reportedOverrun = false;
    this.port.onmessage = (event) => {
      const message = event.data;
      if (message.type === "samples") {
        this.enqueue(message.samples);
      } else if (message.type === "flush") {
        // Barge-in. Everything buffered is now stale.
        this.read = this.write;
        this.draining = false;
        this.reportedOverrun = false;
      }
    };
  }

  enqueue(samples) {
    for (let i = 0; i < samples.length; i += 1) {
      this.ring[this.write] = samples[i];
      this.write = (this.write + 1) % CAPACITY;
      if (this.write === this.read) {
        // Overrun. Dropping the oldest sample keeps the newest speech audible,
        // but the result is audibly damaged either way, so it is reported
        // rather than silently absorbed — this failing quietly is what made
        // the original crackling so hard to place.
        this.read = (this.read + 1) % CAPACITY;
        if (!this.reportedOverrun) {
          this.reportedOverrun = true;
          this.port.postMessage({ type: "overrun" });
        }
      }
    }
    this.draining = true;
  }

  process(_inputs, outputs) {
    const channel = outputs[0]?.[0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i += 1) {
      if (this.read === this.write) {
        // Underrun. Silence, not a stop: more audio is probably in flight, and
        // ending the node here would make the rest of the sentence unplayable.
        channel[i] = 0;
        continue;
      }
      channel[i] = this.ring[this.read];
      this.read = (this.read + 1) % CAPACITY;
    }

    if (this.draining && this.read === this.write) {
      // Ran dry having had something to play: the utterance is over. The main
      // thread uses this to put the UI back into "listening".
      this.draining = false;
      this.port.postMessage({ type: "empty" });
    }
    return true;
  }
}

registerProcessor("player-processor", PlayerProcessor);
