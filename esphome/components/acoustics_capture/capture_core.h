#pragma once

// Host-testable core of the acoustics_capture component: buffer sizing, WAV header and
// reconnect back-off, for 1 or 2 channels of 16-bit PCM. No ESPHome or ESP-IDF includes, so
// tests build it with a plain C++ compiler.

#include <cmath>
#include <cstddef>
#include <cstdint>

namespace acoustics_capture_core {

constexpr uint32_t SAMPLE_RATE = 48000;
constexpr uint32_t BACKOFF_BASE_MS = 1000;
constexpr uint32_t BACKOFF_MAX_MS = 30000;

constexpr uint16_t BYTES_PER_SAMPLE = 2;  // 16-bit PCM
constexpr uint16_t MAX_CHANNELS = 2;

// Number of frames (one sample per channel) for capture_s seconds at rate Hz, rounded up.
inline size_t samples_for(float capture_s, uint32_t rate) {
  if (!(capture_s > 0.0f)) return 0;
  // Computed in double so e.g. 20.0 s * 48000 does not pick up float error before the ceiling.
  return (size_t) std::ceil((double) capture_s * (double) rate - 1e-6);
}

// Bytes of one frame of interleaved 16-bit PCM.
inline size_t frame_bytes(uint16_t channels) { return (size_t) channels * BYTES_PER_SAMPLE; }

// Capture buffer size in bytes for capture_s seconds of 16-bit PCM with `channels` channels.
inline size_t bytes_for(float capture_s, uint32_t rate, uint16_t channels) {
  return samples_for(capture_s, rate) * frame_bytes(channels);
}

// Whole seconds of capture that fit in `bytes` (for example the largest free PSRAM block).
inline float seconds_in(size_t bytes, uint32_t rate, uint16_t channels) {
  if (rate == 0 || channels == 0) return 0.0f;
  return (float) (bytes / frame_bytes(channels) / rate);
}

namespace detail {
inline void put_le16(uint8_t *p, uint16_t v) {
  p[0] = (uint8_t)(v & 0xff);
  p[1] = (uint8_t)(v >> 8);
}
inline void put_le32(uint8_t *p, uint32_t v) {
  p[0] = (uint8_t)(v & 0xff);
  p[1] = (uint8_t)((v >> 8) & 0xff);
  p[2] = (uint8_t)((v >> 16) & 0xff);
  p[3] = (uint8_t)((v >> 24) & 0xff);
}
inline void put_tag(uint8_t *p, const char *tag) {
  for (int i = 0; i < 4; i++) p[i] = (uint8_t) tag[i];
}
}  // namespace detail

// 44-byte canonical WAV header: PCM, 16-bit, little-endian, `channels` interleaved channels.
// num_frames counts frames (one sample per channel), so data is num_frames * channels * 2 bytes.
inline void wav_header(uint8_t out[44], uint32_t num_frames, uint32_t rate, uint16_t channels = 1) {
  const uint16_t bits = 16;
  const uint16_t block_align = channels * bits / 8;
  const uint32_t data_bytes = num_frames * block_align;
  detail::put_tag(out + 0, "RIFF");
  detail::put_le32(out + 4, 36 + data_bytes);
  detail::put_tag(out + 8, "WAVE");
  detail::put_tag(out + 12, "fmt ");
  detail::put_le32(out + 16, 16);
  detail::put_le16(out + 20, 1);
  detail::put_le16(out + 22, channels);
  detail::put_le32(out + 24, rate);
  detail::put_le32(out + 28, rate * block_align);
  detail::put_le16(out + 32, block_align);
  detail::put_le16(out + 34, bits);
  detail::put_tag(out + 36, "data");
  detail::put_le32(out + 40, data_bytes);
}

inline bool capture_fits(float capture_s, float max_capture_s) { return capture_s <= max_capture_s; }

// Reconnect delay after `attempt` failures (0-based): 1 s, 2 s, 4 s, ... capped at 30 s.
inline uint32_t backoff_ms(uint8_t attempt) {
  if (attempt >= 5) return BACKOFF_MAX_MS;
  uint32_t ms = BACKOFF_BASE_MS << attempt;
  return ms > BACKOFF_MAX_MS ? BACKOFF_MAX_MS : ms;
}

// Whether the worker goes on to polling after a registration answer (HTTP status, or <= 0 for a
// network error). 2xx goes on. 401 and 403 mean the token is wrong: back off and register again.
// Another 4xx is logged and polling goes on, so a refused registration (for example a name that
// Home Assistant owns on an older engine) does not stop the node. A network error, 3xx or 5xx
// backs off and registers again.
inline bool register_then_poll(int status) {
  if (status >= 200 && status < 300) return true;
  if (status == 401 || status == 403) return false;
  return status >= 400 && status < 500;
}

}  // namespace acoustics_capture_core
