#include "acoustics_capture.h"

#ifdef USE_ESP32

#include "esphome/components/json/json_util.h"
#include "esphome/components/network/util.h"
#include "esphome/core/application.h"
#include "esphome/core/defines.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/core/version.h"

#include <esp_heap_caps.h>
#include <esp_http_client.h>
#include <esp_log.h>

#include <algorithm>
#include <cctype>
#include <cstring>
#include <vector>

namespace esphome::acoustics_capture {

static const char *const TAG = "acoustics_capture";

#ifdef ESPHOME_PROJECT_VERSION
static const char *const FIRMWARE = ESPHOME_PROJECT_VERSION;
#else
static const char *const FIRMWARE = ESPHOME_VERSION;
#endif

static constexpr uint32_t WORKER_STACK = 8192;
// Below Wi-Fi and lwIP, which also run on core 0; the work is network-bound.
static constexpr UBaseType_t WORKER_PRIORITY = 2;
static constexpr BaseType_t WORKER_CORE = 0;

static constexpr size_t WRITE_CHUNK = 4096;
static constexpr size_t RESPONSE_MAX = 2048;
static constexpr uint32_t REQUEST_TIMEOUT_MS = 10000;
static constexpr uint32_t ACK_TIMEOUT_MS = 3000;
static constexpr int ACK_ATTEMPTS = 3;
static constexpr uint32_t ACK_RETRY_MS = 500;
// The first attempt plus up to three retries.
static constexpr int UPLOAD_ATTEMPTS = 4;
static constexpr uint32_t UPLOAD_TIMEOUT_MS = 30000;
// Extra time the capture may take beyond capture_s before the worker stops waiting for samples.
static constexpr uint32_t CAPTURE_GRACE_MS = 5000;
static constexpr size_t MAX_ID_LEN = 64;
static constexpr size_t MAX_PATH_LEN = 256;

static const char *state_name(CaptureState state) {
  switch (state) {
    case CaptureState::IDLE:
      return "idle";
    case CaptureState::ARMED:
      return "armed";
    case CaptureState::RECORDING:
      return "recording";
    case CaptureState::UPLOADING:
      return "uploading";
    case CaptureState::ERROR:
      return "error";
  }
  return "unknown";
}

static bool safe_id(const std::string &value) {
  if (value.empty() || value.size() > MAX_ID_LEN)
    return false;
  return std::all_of(value.begin(), value.end(),
                     [](char c) { return std::isalnum((unsigned char) c) || c == '-' || c == '_'; });
}

// An absolute path on the engine: no scheme, host, query or characters that need escaping.
static bool safe_path(const std::string &value) {
  if (value.size() < 2 || value.size() > MAX_PATH_LEN || value[0] != '/')
    return false;
  return std::all_of(value.begin(), value.end(), [](char c) {
    return std::isalnum((unsigned char) c) || c == '/' || c == '-' || c == '_' || c == '.';
  });
}

static void task_delay_ms(uint32_t ms) { vTaskDelay(pdMS_TO_TICKS(ms)); }

void AcousticsCaptureComponent::setup() {
  // ESP-IDF's HTTP client writes each request header at debug level, the Authorization header
  // included. Keep it at warnings whatever the IDF log level is, so the token never reaches a log.
  esp_log_level_set("HTTP_CLIENT", ESP_LOG_WARN);

  // Runs on the microphone's task: copy only, no allocation, no locks, no HTTP.
  this->microphone_source_->add_data_callback([this](const std::vector<uint8_t> &data) {
    this->callback_busy_.store(true);
    if (this->recording_.load()) {
      size_t filled = this->filled_.load(std::memory_order_relaxed);
      const size_t n = std::min(this->capacity_ - filled, data.size());
      if (n > 0) {
        std::memcpy(this->buffer_ + filled, data.data(), n);
        filled += n;
        this->filled_.store(filled, std::memory_order_release);
      }
      if (filled >= this->capacity_)
        this->recording_.store(false);
    }
    this->callback_busy_.store(false);
  });
}

void AcousticsCaptureComponent::loop() {
  if (this->worker_handle_ == nullptr && network::is_connected()) {
    if (xTaskCreatePinnedToCore(AcousticsCaptureComponent::worker_task_, "acoustics_capture", WORKER_STACK, this,
                                WORKER_PRIORITY, &this->worker_handle_, WORKER_CORE) != pdPASS) {
      this->worker_handle_ = nullptr;
      ESP_LOGE(TAG, "Could not start the worker task");
      this->mark_failed();
      return;
    }
    ESP_LOGI(TAG, "Network up, worker started");
  }

  const CaptureState state = this->state_.load(std::memory_order_relaxed);
  if (this->state_sensor_ != nullptr && (!this->state_published_ || state != this->published_state_)) {
    this->state_sensor_->publish_state(state_name(state));
    this->published_state_ = state;
    this->state_published_ = true;
  }
}

void AcousticsCaptureComponent::dump_config() {
  ESP_LOGCONFIG(TAG,
                "Acoustics capture:\n"
                "  Engine URL: %s\n"
                "  Node ID: %s\n"
                "  Firmware: %s\n"
                "  Max capture: %.1f s\n"
                "  Poll wait: %u s",
                this->engine_url_.c_str(), this->node_id_.c_str(), FIRMWARE, this->max_capture_s_,
                (unsigned) this->poll_wait_s_);
  LOG_TEXT_SENSOR("  ", "State", this->state_sensor_);
}

void AcousticsCaptureComponent::worker_task_(void *arg) {
  static_cast<AcousticsCaptureComponent *>(arg)->worker_loop_();
}

void AcousticsCaptureComponent::worker_loop_() {
  bool registered = false;
  uint8_t attempt = 0;
  while (true) {
    if (!network::is_connected()) {
      task_delay_ms(1000);
      continue;
    }
    if (!registered) {
      const int status = this->self_report_();
      if (!core::register_then_poll(status)) {
        this->set_state_(CaptureState::ERROR);
        task_delay_ms(core::backoff_ms(attempt));
        if (attempt < UINT8_MAX)
          attempt++;
        continue;
      }
      // The back-off is reset by the next successful poll, so a node the engine keeps answering
      // 404 re-registers at a growing interval.
      registered = true;
      if (status == 200)
        this->set_state_(CaptureState::IDLE);
    }
    Command command;
    const int got = this->poll_(command);
    if (got == POLL_UNKNOWN_NODE) {
      // The engine lost the node, for example after its record was deleted: register again
      // before the next poll.
      ESP_LOGW(TAG, "The engine does not know node %s; registering again", this->node_id_.c_str());
      registered = false;
    }
    if (got < 0) {
      this->set_state_(CaptureState::ERROR);
      task_delay_ms(core::backoff_ms(attempt));
      if (attempt < UINT8_MAX)
        attempt++;
      continue;
    }
    attempt = 0;
    this->set_state_(CaptureState::IDLE);
    if (got > 0) {
      this->handle_command_(command);
      // A lost upload leaves ERROR, which the next successful poll clears.
      if (this->state_.load(std::memory_order_relaxed) != CaptureState::ERROR)
        this->set_state_(CaptureState::IDLE);
    }
  }
}

int AcousticsCaptureComponent::self_report_() {
  const std::string name = App.get_name().str();
  auto body = json::build_json([&name](JsonObject root) {
    root["esphome_name"] = name;
    root["firmware"] = FIRMWARE;
    root["capture"] = true;
  });
  const std::string path = "/api/v1/nodes/" + this->node_id_;
  const int status =
      this->request_("PUT", path, "application/json", reinterpret_cast<const uint8_t *>(body.data()), body.size(),
                     nullptr, 0, REQUEST_TIMEOUT_MS, nullptr);
  if (status == 200) {
    ESP_LOGI(TAG, "Registered as %s (esphome_name %s, firmware %s)", this->node_id_.c_str(), name.c_str(), FIRMWARE);
  } else if (core::register_then_poll(status)) {
    ESP_LOGW(TAG, "Register refused: HTTP %d; polling anyway", status);
  } else {
    ESP_LOGW(TAG, "Register failed: HTTP %d", status);
  }
  return status;
}

int AcousticsCaptureComponent::poll_(Command &command) {
  const std::string path =
      "/api/v1/nodes/" + this->node_id_ + "/commands/next?wait=" + std::to_string(this->poll_wait_s_);
  std::string response;
  // The read timeout is longer than wait, so the engine answers 204 before the client gives up. Each
  // poll closes its connection, so an abandoned poll cannot take a command.
  const int status = this->request_("GET", path, nullptr, nullptr, 0, nullptr, 0, (this->poll_wait_s_ + 5) * 1000,
                                    &response);
  if (status == 204)
    return 0;
  if (status == 404)
    return POLL_UNKNOWN_NODE;
  if (status != 200) {
    ESP_LOGW(TAG, "Poll failed: HTTP %d", status);
    return -1;
  }
  const bool parsed = json::parse_json(response, [&command](JsonObject root) -> bool {
    command.id = root["id"] | "";
    command.kind = root["kind"] | "";
    command.upload_path = root["upload_path"] | "";
    command.capture_s = root["capture_s"] | 0.0f;
    return true;
  });
  if (!parsed || !safe_id(command.id)) {
    ESP_LOGE(TAG, "Poll answered 200 with a command that cannot be read");
    return -1;
  }
  return 1;
}

void AcousticsCaptureComponent::handle_command_(const Command &command) {
  const char *const cid = command.id.c_str();
  ESP_LOGI(TAG, "Command %s: kind %s, capture_s %.2f", cid, command.kind.c_str(), command.capture_s);
  this->set_state_(CaptureState::ARMED);

  if (command.kind != "record") {
    ESP_LOGW(TAG, "Command %s: unsupported kind", cid);
    this->ack_with_retry_(command.id, "failed", "unsupported_kind");
    return;
  }
  if (!safe_path(command.upload_path)) {
    ESP_LOGW(TAG, "Command %s: unusable upload_path", cid);
    this->ack_with_retry_(command.id, "failed", "bad_upload_path");
    return;
  }
  if (!(command.capture_s > 0.0f) || !core::capture_fits(command.capture_s, this->max_capture_s_)) {
    ESP_LOGW(TAG, "Command %s: capture_s %.2f is over max_capture %.1f s", cid, command.capture_s,
             this->max_capture_s_);
    this->ack_with_retry_(command.id, "failed", "no_memory");
    return;
  }
  if (!this->microphone_source_->is_running()) {
    ESP_LOGW(TAG, "Command %s: the microphone is not running", cid);
    this->ack_with_retry_(command.id, "failed", "microphone_not_running");
    return;
  }

  const size_t samples = core::samples_for(command.capture_s, core::SAMPLE_RATE);
  const size_t bytes = samples * sizeof(int16_t);
  auto *buffer = static_cast<uint8_t *>(heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (buffer == nullptr) {
    ESP_LOGW(TAG, "Command %s: no PSRAM for %u bytes", cid, (unsigned) bytes);
    this->ack_with_retry_(command.id, "failed", "no_memory");
    return;
  }

  // Ack before the first sample is stored: the engine plays the sweep once every node records.
  if (!this->ack_with_retry_(command.id, "recording", nullptr)) {
    heap_caps_free(buffer);
    return;
  }

  this->buffer_ = buffer;
  this->capacity_ = bytes;
  this->record_(bytes, command.capture_s);
  const size_t got = this->filled_.load(std::memory_order_acquire) / sizeof(int16_t);
  this->buffer_ = nullptr;
  this->capacity_ = 0;

  if (got == 0) {
    ESP_LOGW(TAG, "Command %s: no audio arrived", cid);
    this->ack_with_retry_(command.id, "failed", "no_audio");
  } else {
    if (got < samples)
      ESP_LOGW(TAG, "Command %s: recorded %u of %u samples", cid, (unsigned) got, (unsigned) samples);
    this->upload_(command, buffer, got);
  }
  heap_caps_free(buffer);
}

void AcousticsCaptureComponent::record_(size_t bytes, float capture_s) {
  this->filled_.store(0);
  this->set_state_(CaptureState::RECORDING);
  this->recording_.store(true);  // after buffer_, capacity_ and filled_ are set

  const uint32_t limit_ms = (uint32_t) (capture_s * 1000.0f) + CAPTURE_GRACE_MS;
  const uint32_t start = millis();
  while (this->filled_.load(std::memory_order_acquire) < bytes && millis() - start < limit_ms)
    task_delay_ms(20);
  this->stop_recording_();
  ESP_LOGI(TAG, "Recorded %u bytes in %u ms", (unsigned) this->filled_.load(), (unsigned) (millis() - start));
}

void AcousticsCaptureComponent::stop_recording_() {
  // With recording_ cleared, a callback that has not yet checked it copies nothing; one that has
  // is still busy, and the worker waits for it before it reads or frees the buffer.
  this->recording_.store(false);
  while (this->callback_busy_.load())
    task_delay_ms(1);
}

int AcousticsCaptureComponent::ack_(const std::string &command_id, const char *state, const char *detail) {
  auto body = json::build_json([state, detail](JsonObject root) {
    root["state"] = state;
    if (detail != nullptr)
      root["detail"] = detail;
  });
  const std::string path = "/api/v1/nodes/" + this->node_id_ + "/commands/" + command_id;
  return this->request_("POST", path, "application/json", reinterpret_cast<const uint8_t *>(body.data()),
                        body.size(), nullptr, 0, ACK_TIMEOUT_MS, nullptr);
}

bool AcousticsCaptureComponent::ack_with_retry_(const std::string &command_id, const char *state,
                                                const char *detail) {
  // The engine expires a taken command that is not acked within 10 s.
  for (int i = 0; i < ACK_ATTEMPTS; i++) {
    const int status = this->ack_(command_id, state, detail);
    ESP_LOGI(TAG, "Command %s: ack %s%s%s: HTTP %d", command_id.c_str(), state, detail != nullptr ? " " : "",
             detail != nullptr ? detail : "", status);
    if (status == 200)
      return true;
    if (status == 409) {
      ESP_LOGW(TAG, "Command %s: closed on the engine, dropped", command_id.c_str());
      return false;
    }
    if (status > 0 && status < 500)
      return false;
    if (i + 1 < ACK_ATTEMPTS)
      task_delay_ms(ACK_RETRY_MS);
  }
  return false;
}

int AcousticsCaptureComponent::upload_(const Command &command, const uint8_t *data, size_t samples) {
  const char *const cid = command.id.c_str();
  uint8_t header[44];
  core::wav_header(header, (uint32_t) samples, core::SAMPLE_RATE);
  const size_t data_bytes = samples * sizeof(int16_t);
  this->set_state_(CaptureState::UPLOADING);

  int status = -1;
  for (int attempt = 0; attempt < UPLOAD_ATTEMPTS; attempt++) {
    if (attempt > 0)
      task_delay_ms(core::backoff_ms((uint8_t) (attempt - 1)));
    std::string response;
    const uint32_t start = millis();
    status = this->request_("PUT", command.upload_path, "audio/wav", header, sizeof(header), data,
                            data_bytes, UPLOAD_TIMEOUT_MS, &response);
    ESP_LOGI(TAG, "Command %s: upload %u bytes, attempt %d: HTTP %d in %u ms", cid,
             (unsigned) (sizeof(header) + data_bytes), attempt + 1, status, (unsigned) (millis() - start));
    if (status == 200) {
      std::string point_state, failure;
      json::parse_json(response, [&point_state, &failure](JsonObject root) -> bool {
        point_state = root["point_state"] | "";
        failure = root["failure"] | "";
        return true;
      });
      ESP_LOGI(TAG, "Command %s: point %s%s%s", cid, point_state.c_str(), failure.empty() ? "" : " ",
               failure.c_str());
      return status;
    }
    if (status == 409) {
      ESP_LOGW(TAG, "Command %s: the session no longer accepts recordings, dropped", cid);
      return status;
    }
    const bool incomplete = status == 400 && response.find("incomplete_body") != std::string::npos;
    if (status > 0 && status < 500 && !incomplete) {
      ESP_LOGW(TAG, "Command %s: upload refused, dropped", cid);
      this->set_state_(CaptureState::ERROR);
      return status;
    }
  }
  ESP_LOGE(TAG, "Command %s: upload failed after %d attempts", cid, UPLOAD_ATTEMPTS);
  this->set_state_(CaptureState::ERROR);
  return status;
}

int AcousticsCaptureComponent::request_(const char *method, const std::string &path, const char *content_type,
                                        const uint8_t *body, size_t body_len, const uint8_t *body2,
                                        size_t body2_len, uint32_t timeout_ms, std::string *response) {
  const std::string url = this->engine_url_ + path;
  esp_http_client_config_t config = {};
  config.url = url.c_str();
  if (std::strcmp(method, "PUT") == 0) {
    config.method = HTTP_METHOD_PUT;
  } else if (std::strcmp(method, "POST") == 0) {
    config.method = HTTP_METHOD_POST;
  } else {
    config.method = HTTP_METHOD_GET;
  }
  config.timeout_ms = (int) timeout_ms;
  config.disable_auto_redirect = true;
  config.keep_alive_enable = false;
  config.buffer_size = 1024;
  config.buffer_size_tx = 1024;

  esp_http_client_handle_t client = esp_http_client_init(&config);
  if (client == nullptr) {
    ESP_LOGE(TAG, "%s %s: could not create the HTTP client", method, path.c_str());
    return -1;
  }
  {
    // The token goes only into this header; it is never logged.
    const std::string auth = "Bearer " + this->token_;
    esp_http_client_set_header(client, "Authorization", auth.c_str());
  }
  if (content_type != nullptr)
    esp_http_client_set_header(client, "Content-Type", content_type);

  const size_t total = body_len + body2_len;
  // write_len makes the client send Content-Length: total.
  esp_err_t err = esp_http_client_open(client, (int) total);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "%s %s: connect failed: %s", method, path.c_str(), esp_err_to_name(err));
    esp_http_client_cleanup(client);
    return -1;
  }

  const uint8_t *parts[2] = {body, body2};
  const size_t lens[2] = {body_len, body2_len};
  for (int p = 0; p < 2; p++) {
    size_t done = 0;
    while (done < lens[p]) {
      const size_t chunk = std::min(WRITE_CHUNK, lens[p] - done);
      const int written = esp_http_client_write(client, reinterpret_cast<const char *>(parts[p] + done), (int) chunk);
      if (written <= 0) {
        ESP_LOGW(TAG, "%s %s: write failed after %u bytes", method, path.c_str(), (unsigned) done);
        esp_http_client_cleanup(client);
        return -1;
      }
      done += (size_t) written;
    }
  }

  const int64_t content_length = esp_http_client_fetch_headers(client);
  if (content_length < 0) {
    ESP_LOGW(TAG, "%s %s: no answer", method, path.c_str());
    esp_http_client_cleanup(client);
    return -1;
  }
  const int status = esp_http_client_get_status_code(client);

  if (response != nullptr) {
    char buf[256];
    while (response->size() < RESPONSE_MAX) {
      const int n = esp_http_client_read(client, buf, (int) std::min(sizeof(buf), RESPONSE_MAX - response->size()));
      if (n <= 0)
        break;
      response->append(buf, (size_t) n);
    }
  }
  esp_http_client_close(client);
  esp_http_client_cleanup(client);
  return status;
}

}  // namespace esphome::acoustics_capture

#endif  // USE_ESP32
