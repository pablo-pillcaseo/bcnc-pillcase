// BLTouch controller + lid camera for the XIAO ESP32S3 Sense.
//
// A superset of ~/Arduino/bltouchprobe/bltouchprobe.ino, the firmware the
// machines run today for their (clone) BLTouch. Everything that sketch does is
// unchanged: same pins, same 9600 baud, same single-character commands 1-4,
// same reply lines, same "Touch" line. bCNC talks to this exactly as it talked
// to the old sketch; the camera is an addition on top.
//
// Probing must never wait on the camera, so:
//   * Servo commands are handled in loop() on core 1. The camera is driven by
//     its own task pinned to core 0, so a capture never delays a servo write.
//   * The servo is written BEFORE its reply line is printed, so even if the
//     line has to wait for an image chunk to finish sending, the pin has
//     already moved.
//   * The camera's XCLK runs on LEDC timer 3 / channel 7. The servo is attached
//     first and takes timer 0 / channel 0, so the two PWMs never share a timer.
//   * The camera is only powered up on the first capture request, so a board
//     that is never asked for a picture boots and probes exactly as before.
//   * A capture is refused while the pin is deployed (between '1' and '2').
//
// Commands (single characters, anything else is ignored as before):
//   1  Push pin down          (unchanged)
//   2  Pull pin up            (unchanged)
//   3  Self Test              (unchanged)
//   4  Alarm Release & Touch SW Mode (unchanged)
//   I  Identify: replies "BLTCAM 1 <camera|nocamera>"
//   C  Capture one JPEG of the work area (framed, see below)
//
// Capture framing. Text lines can still arrive in between chunks (a "Touch" or
// a servo reply), and are always whole lines; raw bytes only ever follow a
// "@CHK" header, so a reader that honours the byte counts can never mistake
// image data for a line:
//   @IMG <seq> <length> <width> <height>\n
//   @CHK <offset> <n>\n<n raw bytes>          repeated until <length> is sent
//   @END <seq> <crc32 hex>\n
// or, if no image can be taken:
//   @ERR <seq> <reason>\n

#include <Arduino.h>
#include <ESP32Servo.h> // Use this for ESP32-S3!
#include "esp_camera.h"
#include "esp_rom_crc.h"

Servo myservo;

// Define pins (pick any valid GPIOs for ESP32-S3, these are safe choices)
const uint8_t BLTouchPin     = 2;   // e.g., GPIO2
const uint8_t BLTouchControl = 8;   // e.g., GPIO9 (ensure it's PWM-capable!)

// The Sense board's microSD slot shares GPIO8 (its MISO) with the servo line.
// Holding the card's chip select (GPIO21, also the user LED) high keeps the
// card off that line. This also turns the LED off; it is active-low.
const uint8_t SDCardCS = 21;

volatile bool state = LOW;
volatile bool touched = false; // Communicate touch event to loop()
volatile bool pinDeployed = false;

// ISR for BLTouch touch-output
void IRAM_ATTR touchISR() {
  state = !state;
  touched = true; // Set a flag for the main loop to read
}

// ── Serial access ───────────────────────────────────────────────────────────
// loop() and the camera task both write to Serial. Each line or chunk is
// written under this mutex so the two never interleave mid-line.
SemaphoreHandle_t serialMutex;

void printLine(const char *s) {
  xSemaphoreTake(serialMutex, portMAX_DELAY);
  Serial.println(s);
  xSemaphoreGive(serialMutex);
}

// ── Camera (XIAO ESP32S3 Sense pin map) ─────────────────────────────────────
#define CAM_PIN_PWDN   -1
#define CAM_PIN_RESET  -1
#define CAM_PIN_XCLK   10
#define CAM_PIN_SIOD   40
#define CAM_PIN_SIOC   39
#define CAM_PIN_D7     48
#define CAM_PIN_D6     11
#define CAM_PIN_D5     12
#define CAM_PIN_D4     14
#define CAM_PIN_D3     16
#define CAM_PIN_D2     18
#define CAM_PIN_D1     17
#define CAM_PIN_D0     15
#define CAM_PIN_VSYNC  38
#define CAM_PIN_HREF   47
#define CAM_PIN_PCLK   13

const size_t CHUNK_BYTES = 1024;
// Frames thrown away after the sensor starts, while auto exposure and white
// balance settle. The first frames out of a cold OV2640 are dark and green.
const int WARMUP_FRAMES = 6;

enum CameraState { CAM_OFF, CAM_READY, CAM_FAILED };
CameraState cameraState = CAM_OFF;
TaskHandle_t cameraTask = nullptr;
volatile uint32_t captureSeq = 0;

bool startCamera() {
  camera_config_t config = {};
  config.pin_pwdn = CAM_PIN_PWDN;
  config.pin_reset = CAM_PIN_RESET;
  config.pin_xclk = CAM_PIN_XCLK;
  config.pin_sccb_sda = CAM_PIN_SIOD;
  config.pin_sccb_scl = CAM_PIN_SIOC;
  config.pin_d7 = CAM_PIN_D7;
  config.pin_d6 = CAM_PIN_D6;
  config.pin_d5 = CAM_PIN_D5;
  config.pin_d4 = CAM_PIN_D4;
  config.pin_d3 = CAM_PIN_D3;
  config.pin_d2 = CAM_PIN_D2;
  config.pin_d1 = CAM_PIN_D1;
  config.pin_d0 = CAM_PIN_D0;
  config.pin_vsync = CAM_PIN_VSYNC;
  config.pin_href = CAM_PIN_HREF;
  config.pin_pclk = CAM_PIN_PCLK;
  config.xclk_freq_hz = 20000000;
  // Kept clear of the servo's PWM, which takes timer 0 / channel 0.
  config.ledc_timer = LEDC_TIMER_3;
  config.ledc_channel = LEDC_CHANNEL_7;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_UXGA;   // 1600x1200
  config.jpeg_quality = 10;
  config.fb_count = 1;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  // One buffer, filled only when asked for: no frames are streamed in the
  // background between captures.
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;

  if (esp_camera_init(&config) != ESP_OK) {
    return false;
  }
  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_exposure_ctrl(s, 1);
    s->set_gain_ctrl(s, 1);
  }
  for (int i = 0; i < WARMUP_FRAMES; i++) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb) esp_camera_fb_return(fb);
  }
  return true;
}

void sendCapture(uint32_t seq) {
  char line[64];

  if (pinDeployed) {
    snprintf(line, sizeof(line), "@ERR %lu probe deployed", (unsigned long)seq);
    printLine(line);
    return;
  }
  if (cameraState == CAM_OFF) {
    cameraState = startCamera() ? CAM_READY : CAM_FAILED;
  }
  if (cameraState != CAM_READY) {
    snprintf(line, sizeof(line), "@ERR %lu no camera", (unsigned long)seq);
    printLine(line);
    return;
  }

  // With one buffer filled only on demand, the frame waiting in it may be
  // minutes old. Hand it back and take the next one, which starts now.
  camera_fb_t *fb = esp_camera_fb_get();
  if (fb) esp_camera_fb_return(fb);
  fb = esp_camera_fb_get();
  if (!fb) {
    snprintf(line, sizeof(line), "@ERR %lu capture failed", (unsigned long)seq);
    printLine(line);
    return;
  }

  uint32_t crc = esp_rom_crc32_le(0, fb->buf, fb->len);
  snprintf(line, sizeof(line), "@IMG %lu %u %u %u", (unsigned long)seq,
           (unsigned)fb->len, (unsigned)fb->width, (unsigned)fb->height);
  printLine(line);

  for (size_t off = 0; off < fb->len; off += CHUNK_BYTES) {
    size_t n = min(CHUNK_BYTES, fb->len - off);
    xSemaphoreTake(serialMutex, portMAX_DELAY);
    Serial.printf("@CHK %u %u\n", (unsigned)off, (unsigned)n);
    Serial.write(fb->buf + off, n);
    xSemaphoreGive(serialMutex);
    // Let loop() print anything it has waiting between chunks.
    taskYIELD();
  }

  snprintf(line, sizeof(line), "@END %lu %08lx", (unsigned long)seq, (unsigned long)crc);
  printLine(line);
  esp_camera_fb_return(fb);
}

void cameraTaskMain(void *) {
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    sendCapture(captureSeq);
  }
}

void setup() {
  // A bigger transmit buffer lets an image chunk go out in one write.
  Serial.setTxBufferSize(4096);
  Serial.begin(9600);
  delay(200); // Wait for Serial (no while (!Serial) on ESP32)
  serialMutex = xSemaphoreCreateMutex();

  pinMode(SDCardCS, OUTPUT);
  digitalWrite(SDCardCS, HIGH);

  // Attach servo to control pin
  myservo.attach(BLTouchControl);

  // Configure BLTouch output pin and attach ISR on any CHANGE
  pinMode(BLTouchPin, INPUT_PULLUP);
  attachInterrupt(
    digitalPinToInterrupt(BLTouchPin),
    touchISR,
    CHANGE
  );

  // Set initial “reset” position (60°)
  myservo.write(60);

  // Core 0, below loop()'s priority: the camera only ever gets the CPU time
  // that probing is not using.
  xTaskCreatePinnedToCore(cameraTaskMain, "camera", 8192, nullptr, 1, &cameraTask, 0);
}

void loop() {
  // Print touch event outside the ISR
  if (touched) {
    touched = false;
    printLine("Touch");
  }

  if (Serial.available()) {
    char inByte = (char)Serial.read();
    switch (inByte) {
      case '1':  // Push pin down
        myservo.write(10);
        pinDeployed = true;
        printLine("Push pin down");
        break;
      case '2':  // Pull pin up
        myservo.write(90);
        pinDeployed = false;
        printLine("Pull pin up");
        break;
      case '3':  // Self Test
        myservo.write(120);
        printLine("Self Test");
        break;
      case '4':  // Alarm Release & Touch SW Mode
        myservo.write(60);
        printLine("Alarm Release & Touch SW Mode");
        break;
      case 'I': {  // Identify
        char line[48];
        snprintf(line, sizeof(line), "BLTCAM 1 %s",
                 cameraState == CAM_FAILED ? "nocamera" : "camera");
        printLine(line);
        break;
      }
      case 'C':  // Capture (on the camera task, never here)
        captureSeq++;
        xTaskNotifyGive(cameraTask);
        break;
    }
  }
}
