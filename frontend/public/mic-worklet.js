/**
 * Microphone capture, off the main thread.
 *
 * An AudioWorklet rather than the deprecated ScriptProcessorNode: capture runs
 * on the audio thread, so a React render cannot stall it. A dropped block here
 * is a gap in the middle of what the caller said.
 *
 * Emits 16-bit signed PCM, which is what the STT provider expects. The
 * AudioContext is created at 16 kHz, so the browser has already resampled the
 * device's native rate (usually 48 kHz) before anything reaches this file --
 * cheaper and better than resampling by hand, and there is no interpolation
 * code here to get subtly wrong.
 */

// 1024 samples at 16 kHz = 64 ms per message. Small enough that end-of-speech
// detection is not visibly late, large enough not to post 125 messages a
// second at the main thread.
const FRAMES_PER_MESSAGE = 1024;

class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Int16Array(FRAMES_PER_MESSAGE);
    this.offset = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) {
      // No input block this quantum (the node is disconnected, or the track
      // ended). Returning true keeps the processor alive for when it resumes.
      return true;
    }
    for (let i = 0; i < channel.length; i += 1) {
      // Clamp before scaling: values outside [-1, 1] are legal in Web Audio
      // and wrap into loud noise if they are allowed to overflow the integer.
      const sample = Math.max(-1, Math.min(1, channel[i]));
      this.buffer[this.offset] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      this.offset += 1;
      if (this.offset === FRAMES_PER_MESSAGE) {
        // Transferred, not copied: the buffer is handed over and replaced.
        const out = this.buffer;
        this.port.postMessage(out.buffer, [out.buffer]);
        this.buffer = new Int16Array(FRAMES_PER_MESSAGE);
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor("mic-processor", MicProcessor);
