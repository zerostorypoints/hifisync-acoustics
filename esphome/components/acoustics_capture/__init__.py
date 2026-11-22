"""acoustics_capture: a measurement node for the hifisync-acoustics engine.

The node long-polls the engine for a record command, records 48 kHz/16-bit mono from a passive
microphone source into PSRAM, uploads the WAV and acks. docs/node-firmware.md describes the YAML,
docs/api.md ("Node protocol") the HTTP exchange.
"""

import re

import esphome.codegen as cg
from esphome.components import esp32, microphone, text_sensor
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_STATE, PLATFORM_ESP32
from esphome.core import CORE

CODEOWNERS = ["@zerostorypoints"]
# psram: the capture buffer (up to 80 s, 7.7 MB) only fits in PSRAM.
DEPENDENCIES = ["microphone", "network", "psram"]
AUTO_LOAD = ["json", "text_sensor"]

CONF_ENGINE_URL = "engine_url"
CONF_NODE_ID = "node_id"
CONF_TOKEN = "token"
CONF_MAX_CAPTURE = "max_capture"
CONF_POLL_WAIT = "poll_wait"

# The engine's sweeps are generated at this rate; capture_core.h has the same constant.
SAMPLE_RATE = 48000

acoustics_capture_ns = cg.esphome_ns.namespace("acoustics_capture")
AcousticsCaptureComponent = acoustics_capture_ns.class_(
    "AcousticsCaptureComponent", cg.Component
)

_NODE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _engine_url(value):
    value = cv.string_strict(value).strip()
    if not value.startswith("http://"):
        # The worker uses plain esp_http_client without TLS; see docs/node-firmware.md.
        raise cv.Invalid("engine_url must start with http://")
    if "?" in value or "#" in value:
        raise cv.Invalid("engine_url must not carry a query or fragment")
    return value.rstrip("/")


def _node_id(value):
    value = cv.string_strict(value)
    if not _NODE_ID.fullmatch(value):
        raise cv.Invalid(
            "node_id may use only letters, digits, '-' and '_' (1 to 64 characters)"
        )
    return value


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(AcousticsCaptureComponent),
            cv.Required(CONF_MICROPHONE): microphone.microphone_source_schema(
                min_bits_per_sample=16,
                max_bits_per_sample=16,
                min_channels=1,
                max_channels=1,
            ),
            cv.Required(CONF_ENGINE_URL): _engine_url,
            cv.Optional(CONF_NODE_ID): _node_id,
            cv.Required(CONF_TOKEN): cv.string_strict,
            cv.Optional(CONF_MAX_CAPTURE, default="60s"): cv.All(
                cv.positive_time_period_milliseconds,
                cv.Range(min=cv.TimePeriod(seconds=1), max=cv.TimePeriod(seconds=80)),
            ),
            cv.Optional(CONF_POLL_WAIT, default="25s"): cv.All(
                cv.positive_time_period_seconds,
                cv.Range(min=cv.TimePeriod(seconds=1), max=cv.TimePeriod(seconds=30)),
            ),
            cv.Optional(CONF_STATE): text_sensor.text_sensor_schema(),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on([PLATFORM_ESP32]),
)

FINAL_VALIDATE_SCHEMA = cv.Schema(
    {
        cv.Required(
            CONF_MICROPHONE
        ): microphone.final_validate_microphone_source_schema(
            "acoustics_capture", sample_rate=SAMPLE_RATE
        ),
    },
    extra=cv.ALLOW_EXTRA,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    # Passive: another component (sound_level with passive: false) keeps the microphone running;
    # this source never starts or stops it.
    mic_source = await microphone.microphone_source_to_code(
        config[CONF_MICROPHONE], passive=True
    )
    cg.add(var.set_microphone_source(mic_source))

    cg.add(var.set_engine_url(config[CONF_ENGINE_URL]))
    cg.add(var.set_node_id(config.get(CONF_NODE_ID, CORE.name)))
    cg.add(var.set_token(config[CONF_TOKEN]))
    cg.add(var.set_max_capture_s(config[CONF_MAX_CAPTURE].total_milliseconds / 1000.0))
    cg.add(var.set_poll_wait_s(config[CONF_POLL_WAIT].total_seconds))

    if state_config := config.get(CONF_STATE):
        sens = await text_sensor.new_text_sensor(state_config)
        cg.add(var.set_state_sensor(sens))

    # ESPHome leaves ESP-IDF's HTTP client out of the build unless a component asks for it.
    esp32.include_builtin_idf_component("esp_http_client")
