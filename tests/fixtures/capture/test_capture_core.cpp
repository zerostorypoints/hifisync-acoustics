// Host test of capture_core.h. With no arguments it runs the assertions and prints "ok" lines.
// With `<num_frames> <rate> [channels]` it prints the 44-byte WAV header as hex, for comparison in
// pytest. channels defaults to 1.
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "capture_core.h"

using namespace acoustics_capture_core;

static int failures = 0;
#define CHECK(cond)                                      \
  do {                                                   \
    if (cond) {                                          \
      std::printf("ok %s\n", #cond);                     \
    } else {                                             \
      std::printf("FAIL %s (line %d)\n", #cond, __LINE__); \
      failures++;                                        \
    }                                                    \
  } while (0)

static uint32_t le32(const uint8_t *p) { return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t) p[3] << 24); }
static uint16_t le16(const uint8_t *p) { return p[0] | (p[1] << 8); }

int main(int argc, char **argv) {
  if (argc == 3 || argc == 4) {
    uint8_t h[44];
    const uint16_t channels = argc == 4 ? (uint16_t) std::strtoul(argv[3], nullptr, 10) : 1;
    wav_header(h, (uint32_t) std::strtoul(argv[1], nullptr, 10), (uint32_t) std::strtoul(argv[2], nullptr, 10),
               channels);
    for (int i = 0; i < 44; i++) std::printf("%02x", h[i]);
    std::printf("\n");
    return 0;
  }

  CHECK(SAMPLE_RATE == 48000);
  CHECK(samples_for(20.0f, 48000) == 960000);
  CHECK(samples_for(0.00001f, 48000) == 1);  // rounded up
  CHECK(samples_for(1.5f, 1000) == 1500);

  uint8_t h[44];
  wav_header(h, 960000, 48000);
  CHECK(std::memcmp(h, "RIFF", 4) == 0);
  CHECK(le32(h + 4) == 36 + 1920000);
  CHECK(std::memcmp(h + 8, "WAVE", 4) == 0);
  CHECK(std::memcmp(h + 12, "fmt ", 4) == 0);
  CHECK(le32(h + 16) == 16);
  CHECK(le16(h + 20) == 1);
  CHECK(le16(h + 22) == 1);
  CHECK(le32(h + 24) == 48000);
  CHECK(le32(h + 28) == 96000);
  CHECK(le16(h + 32) == 2);
  CHECK(le16(h + 34) == 16);
  CHECK(std::memcmp(h + 36, "data", 4) == 0);
  CHECK(le32(h + 40) == 1920000);

  // Stereo: 20 s of 48 kHz, 2 channels = 960000 frames, 3840000 bytes.
  wav_header(h, 960000, 48000, 2);
  CHECK(le32(h + 4) == 36 + 3840000);
  CHECK(le16(h + 22) == 2);
  CHECK(le32(h + 24) == 48000);
  CHECK(le32(h + 28) == 192000);
  CHECK(le16(h + 32) == 4);
  CHECK(le16(h + 34) == 16);
  CHECK(le32(h + 40) == 3840000);

  CHECK(frame_bytes(1) == 2);
  CHECK(frame_bytes(2) == 4);
  CHECK(bytes_for(20.0f, 48000, 1) == 1920000);
  CHECK(bytes_for(20.0f, 48000, 2) == 3840000);
  CHECK(bytes_for(60.0f, 48000, 1) == 5760000);
  CHECK(bytes_for(40.0f, 48000, 2) == 7680000);
  CHECK(bytes_for(0.0f, 48000, 2) == 0);
  CHECK(seconds_in(7680000, 48000, 2) == 40.0f);
  CHECK(seconds_in(7679999, 48000, 2) == 39.0f);
  CHECK(seconds_in(8388608, 48000, 2) == 43.0f);  // 8 MiB, before anything else uses PSRAM
  CHECK(seconds_in(5760000, 48000, 1) == 60.0f);
  CHECK(seconds_in(1000, 0, 2) == 0.0f);

  CHECK(capture_fits(60.0f, 60.0f));
  CHECK(!capture_fits(60.1f, 60.0f));

  const uint32_t expect[] = {1000, 2000, 4000, 8000, 16000, 30000};
  for (uint8_t i = 0; i < 6; i++) CHECK(backoff_ms(i) == expect[i]);
  CHECK(backoff_ms(7) == 30000);

  CHECK(register_then_poll(200));
  CHECK(register_then_poll(204));
  CHECK(register_then_poll(409));
  CHECK(register_then_poll(400));
  CHECK(register_then_poll(404));
  CHECK(!register_then_poll(401));
  CHECK(!register_then_poll(403));
  CHECK(!register_then_poll(500));
  CHECK(!register_then_poll(503));
  CHECK(!register_then_poll(302));
  CHECK(!register_then_poll(-1));
  CHECK(!register_then_poll(0));
  CHECK(backoff_ms(255) == 30000);

  return failures == 0 ? 0 : 1;
}
