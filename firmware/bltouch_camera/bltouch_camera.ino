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
//   * The camera's XCLK runs on LEDC timer 1 / channel 1. The servo is attached
//     first and takes timer 0 / channel 0, so the two PWMs never share a timer.
//     (Timer 3 / channel 7 was tried first: the OV2640 then delivered about one
//     frame in eight. Timer 1 / channel 1 delivers every frame.)
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
//   K/J  Acknowledge a capture piece (echo the token at the end of its header)
//   N    The last @CHK arrived damaged: send it again now
//   S  Status line, for diagnosing the camera: "BLTCAM-STATUS ..."
//
// Capture framing. Text lines can still arrive in between pieces (a "Touch"
// or a servo reply), and are always whole lines; raw bytes only ever follow a
// "@CHK" header, so a reader that honours the byte counts can never mistake
// image data for a line:
//   @IMG <seq> <length> <width> <height> <token>\n
//   @CHK <offset> <n> <crc32 hex> <token>\n<n raw bytes>   until <length> sent
//   @END <seq> <crc32 hex> <token>\n
// or, if no image can be taken:
//   @ERR <seq> <reason>\n
//
// Flow control. The USB serial driver silently drops whole writes when it
// decides the host is not keeping up, which a 200 kB burst trips. So each
// piece is one write, smaller than the transmit buffer, and the next is only
// sent once the host echoes this one's <token> (K and J alternate). A piece
// with no acknowledgement within 250 ms is sent again; 'N' asks for it again
// at once. Measured on the XIAO: ~0.5 s for a 1600x1200 photo.

#include <Arduino.h>
#include <ESP32Servo.h> // Use this for ESP32-S3!
#include "esp_camera.h"
#include "esp_rom_crc.h"
#include "esp_cache.h"

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

const size_t CHUNK_BYTES = 2048;
// Frames thrown away after the sensor starts, while auto exposure and white
// balance settle. The first frames out of a cold OV2640 are dark and green.
const int WARMUP_FRAMES = 6;

enum CameraState { CAM_OFF, CAM_READY, CAM_FAILED };
CameraState cameraState = CAM_OFF;
TaskHandle_t cameraTask = nullptr;
int jpegQuality = 30;              // adapted per frame, see adaptQuality()
esp_err_t cameraInitErr = ESP_OK;
volatile uint32_t framesOk = 0, framesFailed = 0;
volatile uint32_t captureSeq = 0;
SemaphoreHandle_t ackSem;          // given by loop() for each 'K' or 'N'
volatile bool nakReceived = false;  // the last piece arrived damaged: re-send now

// A piece arrives in a few ms. A missing acknowledgement means the piece was
// dropped, so it is re-sent soon rather than waited on.
const TickType_t ACK_WAIT = pdMS_TO_TICKS(250);
const int SEND_TRIES = 12;

// One piece = header line + its raw bytes, assembled here so it goes out as a
// single write. The USB serial driver drops whole writes when it stalls, so a
// piece either arrives complete or not at all, never glued to the next one.
// Pieces are smaller than the 4 kB transmit buffer, and the next is only sent
// once the host has acknowledged this one, so the buffer is empty each time a
// piece is written: the write never has to wait, so the driver never stalls.
static uint8_t piece[64 + CHUNK_BYTES];

// Each piece names the character the host must echo to acknowledge it,
// alternating K and J. A late acknowledgement of a re-sent piece then carries
// the previous piece's character and cannot be taken for this piece's (with a
// single 'K', exactly that let a piece the host never got count as received).
char nextToken = 'K';
volatile char lastAck = 0;

// Send one framed piece, then wait for the host to echo its token. Re-sends
// until acknowledged; false if the host never answers.
bool sendAcked(const char *header, const uint8_t *data, size_t n) {
  char token = nextToken;
  int h = snprintf((char *)piece, 64, "%s %c\n", header, token);
  if (n) memcpy(piece + h, data, n);
  for (int attempt = 0; attempt < SEND_TRIES; attempt++) {
    xSemaphoreTake(serialMutex, portMAX_DELAY);
    Serial.write(piece, h + n);
    xSemaphoreGive(serialMutex);
    TickType_t until = xTaskGetTickCount() + ACK_WAIT;
    for (;;) {
      TickType_t now = xTaskGetTickCount();
      if (now >= until || xSemaphoreTake(ackSem, until - now) != pdTRUE) break;
      if (nakReceived) {          // damaged on the way: send it again at once
        nakReceived = false;
        break;
      }
      if (lastAck == token) {
        nextToken = token == 'K' ? 'J' : 'K';
        return true;
      }
      // a stale acknowledgement of the previous piece: keep waiting
    }
  }
  return false;
}

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
  // Kept clear of the servo's PWM, which takes timer 0 / channel 0. See the
  // header for why not timer 3 / channel 7.
  config.ledc_timer = LEDC_TIMER_1;
  config.ledc_channel = LEDC_CHANNEL_1;
  config.pixel_format = PIXFORMAT_JPEG;
  config.frame_size = FRAMESIZE_UXGA;   // 1600x1200
  config.jpeg_quality = jpegQuality;
  config.fb_count = 1;
  config.fb_location = CAMERA_FB_IN_PSRAM;
  // Quality starts at 30 and adapts (adaptQuality) to keep frames well under
  // the driver's JPEG buffer, width x height / 5 (384 kB here): a frame that
  // overflows it is dropped. At a fixed 12-16 a busy scene reached 300-350 kB.
  // One buffer, filled only when asked for. While a photo is being sent the
  // camera is idle, so its DMA is not writing PSRAM while the CPU reads the
  // photo out of it; with two buffers streaming, that contention corrupted
  // most frames.
  config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;

  cameraInitErr = esp_camera_init(&config);
  if (cameraInitErr != ESP_OK) {
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
    if (fb) { framesOk++; esp_camera_fb_return(fb); } else { framesFailed++; }
  }
  return true;
}

// Hand a frame buffer back to the camera after its bytes were read by the CPU.
//
// The camera writes frames into PSRAM by DMA, behind the data cache. Reading
// a frame to send it pulls its lines into the cache; the buffer is then
// reused for the next frame, and the driver's check for the JPEG start/end
// markers can read the stale cached bytes instead of the new frame and
// discard it. Invalidating the range first makes the next frame visible.
// Without this, most frames after the first photo sent were rejected.
void returnReadFrame(camera_fb_t *fb) {
  // Invalidation needs whole cache lines. The driver allocates frame buffers
  // DMA-aligned and far larger than one frame, so rounding the length up stays
  // inside the buffer; an unaligned start is left alone rather than touching
  // memory that is not ours.
  const uintptr_t LINE = 64;
  uintptr_t start = (uintptr_t)fb->buf;
  if (start % LINE == 0) {
    size_t len = (fb->len + LINE - 1) & ~(LINE - 1);
    esp_cache_msync(fb->buf, len, ESP_CACHE_MSYNC_FLAG_DIR_M2C);
  }
  esp_camera_fb_return(fb);
}

// JPEG size depends on the scene: a dim, noisy picture at a fixed quality can
// grow past the driver's buffer (width x height / 5), and that frame is lost.
// Keep frames between roughly 35% and 70% of the buffer by nudging quality.
const int QUALITY_MIN = 20, QUALITY_MAX = 50;

void adaptQuality(size_t len) {
  sensor_t *s = esp_camera_sensor_get();
  if (!s) return;
  const size_t buffer = 1600 * 1200 / 5;
  int q = jpegQuality;
  if (len > buffer * 70 / 100 && q < QUALITY_MAX) q += 4;
  else if (len < buffer * 35 / 100 && q > QUALITY_MIN) q -= 2;
  if (q != jpegQuality) {
    jpegQuality = q;
    s->set_quality(s, q);
  }
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
  if (fb) { framesOk++; esp_camera_fb_return(fb); } else { framesFailed++; }
  fb = esp_camera_fb_get();
  if (fb) framesOk++; else framesFailed++;
  if (!fb) {
    // Most likely a frame too big for the buffer: compress harder next time.
    adaptQuality(SIZE_MAX);
    snprintf(line, sizeof(line), "@ERR %lu capture failed", (unsigned long)seq);
    printLine(line);
    return;
  }

  uint32_t crc = esp_rom_crc32_le(0, fb->buf, fb->len);
  xSemaphoreTake(ackSem, 0);         // no stale acknowledgement from an earlier capture
  nextToken = 'K';
  lastAck = 0;
  snprintf(line, sizeof(line), "@IMG %lu %u %u %u", (unsigned long)seq,
           (unsigned)fb->len, (unsigned)fb->width, (unsigned)fb->height);
  bool ok = sendAcked(line, nullptr, 0);

  // Between chunks, loop() prints anything it has waiting.
  for (size_t off = 0; ok && off < fb->len; off += CHUNK_BYTES) {
    size_t n = min(CHUNK_BYTES, fb->len - off);
    snprintf(line, sizeof(line), "@CHK %u %u %08lx", (unsigned)off, (unsigned)n,
             (unsigned long)esp_rom_crc32_le(0, fb->buf + off, n));
    ok = sendAcked(line, fb->buf + off, n);
  }
  if (!ok) {
    returnReadFrame(fb);
    snprintf(line, sizeof(line), "@ERR %lu host stopped reading", (unsigned long)seq);
    printLine(line);
    return;
  }

  snprintf(line, sizeof(line), "@END %lu %08lx", (unsigned long)seq, (unsigned long)crc);
  sendAcked(line, nullptr, 0);    // acknowledged too: a dropped @END loses the photo
  adaptQuality(fb->len);
  returnReadFrame(fb);
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
  ackSem = xSemaphoreCreateBinary();

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
      case 'S': {  // Status
        char line[160];
        sensor_t *s = cameraState == CAM_READY ? esp_camera_sensor_get() : nullptr;
        snprintf(line, sizeof(line),
                 "BLTCAM-STATUS psram=%u free_psram=%u heap=%u cam=%d init_err=0x%x pid=0x%x ok=%lu failed=%lu",
                 (unsigned)ESP.getPsramSize(), (unsigned)ESP.getFreePsram(), (unsigned)ESP.getFreeHeap(),
                 (int)cameraState, (unsigned)cameraInitErr, s ? (unsigned)s->id.PID : 0u,
                 (unsigned long)framesOk, (unsigned long)framesFailed);
        printLine(line);
        break;
      }
      case 'K':  // Host has the capture piece whose token is K (or J)
      case 'J':
        lastAck = inByte;
        xSemaphoreGive(ackSem);
        break;
      case 'N':  // Host got the last capture piece damaged
        nakReceived = true;
        xSemaphoreGive(ackSem);
        break;
    }
  }
}
