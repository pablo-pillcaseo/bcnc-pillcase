# BLTouch + lid camera firmware (XIAO ESP32S3 Sense)

`bltouch_camera.ino` replaces `~/Arduino/bltouchprobe/bltouchprobe.ino` on the
probe's XIAO ESP32S3. Probe behaviour is unchanged: same pins (GPIO2 touch,
GPIO8 servo), 9600 baud, commands `1`–`4`, the same reply lines and the same
`Touch` line. It adds `I` (identify) and `C` (capture a JPEG), and from
version 2 `E` (fixed exposure, gain and white balance, sent as letters so a
garbled command can never move the pin) and `A` (automatic again). See the
header of the sketch for the framing, the commands and how the camera is kept
away from probing.

The fixed exposure comes from Calibrate Exposure in `tools/lidcam_viewer.py`
(bright lid, then dark lid in the same spot); bCNC and the tools send it
whenever they connect. Without a calibration the camera stays automatic.

bCNC works with either firmware. With the old one, the lid camera reports
"no camera firmware" once and scanning carries on as before.

## Build

Arduino IDE: board **XIAO_ESP32S3**, with

| Option | Value |
|---|---|
| PSRAM | **OPI PSRAM** (the camera's frame buffer lives there) |
| USB Mode | Hardware CDC and JTAG (what the current firmware uses) |
| Partition Scheme | Default with spiffs (3MB APP/1.5MB SPIFFS) |

or from the command line (the CLI is inside the IDE's AppImage at
`resources/app/lib/backend/resources/arduino-cli`):

    arduino-cli --config-file ~/.arduinoIDE/arduino-cli.yaml compile \
      -b esp32:esp32:XIAO_ESP32S3:PSRAM=opi,USBMode=hwcdc,PartitionScheme=default_8MB \
      --output-dir build firmware/bltouch_camera

Needs ESP32Servo (installed: 3.0.6) and the esp32 core (installed: 3.2.1,
which includes the camera driver).

## Flash

Close bCNC first, or at least close the BLTouch port, so nothing else has it
open.

    arduino-cli upload -p /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_8C:BF:EA:8E:F0:BC-if00 \
      -b esp32:esp32:XIAO_ESP32S3:PSRAM=opi,USBMode=hwcdc,PartitionScheme=default_8MB \
      --input-dir build

Then, in a serial monitor at 9600: `I` should answer `BLTCAM 2 camera`, and
`1` / `2` should still deploy / stow the pin.

## Restore the original firmware

A full 8 MB image of the board as it was (bltouchprobe.ino) was saved on
2026-10-15 before anything was changed:

    ~/xiao_backups/xiao_8CBFEA8EF0BC_bltouchprobe_full_2026-10-15.bin

Write it back with the esptool that ships with the esp32 core:

    ~/.arduino15/packages/esp32/tools/esptool_py/5.0.dev1/esptool \
      --port /dev/ttyACM1 write-flash 0 ~/xiao_backups/xiao_8CBFEA8EF0BC_bltouchprobe_full_2026-10-15.bin

## Power

The camera draws roughly 100–150 mA while capturing, on top of the BLTouch.
If the board browns out (resets) when a photo is taken, the USB port or hub
is not supplying enough current. Use a powered hub.
