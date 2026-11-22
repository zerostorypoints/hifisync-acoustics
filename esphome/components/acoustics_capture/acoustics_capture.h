#pragma once

#ifdef USE_ESP32

#include "capture_core.h"

#include "esphome/components/microphone/microphone_source.h"
#include "esphome/components/text_sensor/text_sensor.h"
#include "esphome/core/component.h"

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string>

namespace esphome::acoustics_capture {

namespace core = ::acoustics_capture_core;

enum class CaptureState : uint8_t { IDLE, ARMED, RECORDING, UPLOADING, ERROR };

// One measurement node of the hifisync-acoustics engine (docs/api.md, "Node protocol"). A worker
// task on core 0 does all HTTP: it self-reports, long-polls for a record command, acks it, records
// from a passive microphone source into PSRAM and uploads the WAV. The microphone callback only
// copies into the buffer. loop() publishes the state text sensor, so entities are touched only on
// the main loop.
class AcousticsCaptureComponent : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  void set_microphone_source(microphone::MicrophoneSource *source) { this->microphone_source_ = source; }
  void set_engine_url(const std::string &url) { this->engine_url_ = url; }
  void set_node_id(const std::string &node_id) { this->node_id_ = node_id; }
  void set_token(const std::string &token) { this->token_ = token; }
  void set_max_capture_s(float seconds) { this->max_capture_s_ = seconds; }
  void set_poll_wait_s(uint32_t seconds) { this->poll_wait_s_ = seconds; }
  void set_state_sensor(text_sensor::TextSensor *sensor) { this->state_sensor_ = sensor; }

 protected:
  struct Command {
    std::string id;
    std::string kind;
    std::string upload_path;
    float capture_s{0.0f};
  };

  static void worker_task_(void *arg);
  void worker_loop_();
  // Registers the node; returns the HTTP status, or -1 for a network error.
  int self_report_();
  // Returns 1 for a command, 0 for no command (204), POLL_UNKNOWN_NODE for 404 (the caller registers
  // again) and -1 for another error (the caller backs off).
  static constexpr int POLL_UNKNOWN_NODE = -2;
  int poll_(Command &command);
  void handle_command_(const Command &command);
  // Returns the HTTP status, or -1 for a network error.
  int ack_(const std::string &command_id, const char *state, const char *detail);
  bool ack_with_retry_(const std::string &command_id, const char *state, const char *detail);
  void record_(size_t bytes, float capture_s);
  void stop_recording_();
  // Returns the HTTP status of the last attempt, or -1 for a network error.
  int upload_(const Command &command, const uint8_t *data, size_t samples);

  // One request on a fresh connection, closed before returning. The body is body followed by
  // body2 (either may be null), written in 4 KiB pieces with Content-Length set to their sum. The
  // response body is read into response (at most 2 KiB) when it is not null. Returns the HTTP
  // status, or -1 for a network error.
  int request_(const char *method, const std::string &path, const char *content_type, const uint8_t *body,
               size_t body_len, const uint8_t *body2, size_t body2_len, uint32_t timeout_ms, std::string *response);

  void set_state_(CaptureState state) { this->state_.store(state, std::memory_order_relaxed); }

  microphone::MicrophoneSource *microphone_source_{nullptr};
  text_sensor::TextSensor *state_sensor_{nullptr};
  std::string engine_url_;
  std::string node_id_;
  std::string token_;
  float max_capture_s_{60.0f};
  uint32_t poll_wait_s_{25};

  TaskHandle_t worker_handle_{nullptr};

  // Written by the worker, read by loop() for the text sensor.
  std::atomic<CaptureState> state_{CaptureState::IDLE};
  CaptureState published_state_{CaptureState::IDLE};
  bool state_published_{false};

  // Capture buffer shared with the microphone callback. The worker sets buffer_ and capacity_,
  // resets filled_ and then sets recording_; the callback copies only while recording_ is set.
  // callback_busy_ lets the worker wait out a copy in flight before it reads or frees the buffer.
  uint8_t *buffer_{nullptr};
  size_t capacity_{0};
  std::atomic<size_t> filled_{0};
  std::atomic<bool> recording_{false};
  std::atomic<bool> callback_busy_{false};
};

}  // namespace esphome::acoustics_capture

#endif  // USE_ESP32
