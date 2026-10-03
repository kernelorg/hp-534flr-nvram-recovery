// Minimal flashrom serprog (protocol v1, SPI only) for ESP32 / ESP8266.
// Usage: flashrom -p serprog:dev=/dev/ttyUSB0:921600 -c M45PE16 ...
#include <Arduino.h>
#include <SPI.h>

#define BAUD 921600

#if defined(ESP32)
static const int PIN_SCK = 18, PIN_MISO = 19, PIN_MOSI = 23, PIN_CS = 5;
#else  // ESP8266 (NodeMCU: D5=SCK, D6=MISO, D7=MOSI, D1=CS)
static const int PIN_SCK = 14, PIN_MISO = 12, PIN_MOSI = 13, PIN_CS = 5;
#endif

enum : uint8_t {
  ACK = 0x06, NAK = 0x15,
  S_CMD_NOP = 0x00, S_CMD_Q_IFACE = 0x01, S_CMD_Q_CMDMAP = 0x02,
  S_CMD_Q_PGMNAME = 0x03, S_CMD_Q_BUSTYPE = 0x05, S_CMD_SYNCNOP = 0x10,
  S_CMD_S_BUSTYPE = 0x12, S_CMD_O_SPIOP = 0x13, S_CMD_S_SPI_FREQ = 0x14,
  S_CMD_S_PIN_STATE = 0x15,
};
static const uint8_t BUS_SPI = 0x08;

static uint32_t spi_freq = 4000000;
static uint8_t buf[512];

static uint8_t rd() {
  while (!Serial.available()) yield();
  return Serial.read();
}
static uint32_t rd24() { uint32_t v = rd(); v |= (uint32_t)rd() << 8; v |= (uint32_t)rd() << 16; return v; }
static uint32_t rd32() { uint32_t v = rd24(); v |= (uint32_t)rd() << 24; return v; }

static void pins_on() {
  pinMode(PIN_CS, OUTPUT);
  digitalWrite(PIN_CS, HIGH);
#if defined(ESP32)
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, -1);
  // weak pull-up on MISO: an absent/silent chip then reads 0xFF instead of floating
  gpio_pullup_en((gpio_num_t)PIN_MISO);
#else
  SPI.begin();  // ESP8266 HSPI pins are fixed: SCK=14, MISO=12, MOSI=13
#endif
}
static void pins_off() {  // release the bus (hi-Z) so the chip can be swapped safely
  SPI.end();
  pinMode(PIN_SCK, INPUT); pinMode(PIN_MOSI, INPUT); pinMode(PIN_MISO, INPUT);
  pinMode(PIN_CS, INPUT_PULLUP);
}

static void spiop() {
  uint32_t slen = rd24(), rlen = rd24();
  SPI.beginTransaction(SPISettings(spi_freq, MSBFIRST, SPI_MODE0));
  digitalWrite(PIN_CS, LOW);
  while (slen) {  // stream command/data bytes straight to the chip
    size_t n = slen > sizeof(buf) ? sizeof(buf) : slen;
    for (size_t i = 0; i < n; i++) buf[i] = rd();
    SPI.transferBytes(buf, buf, n);
    slen -= n;
  }
  Serial.write(ACK);
  while (rlen) {
    size_t n = rlen > sizeof(buf) ? sizeof(buf) : rlen;
    memset(buf, 0xFF, n);
    SPI.transferBytes(buf, buf, n);
    Serial.write(buf, n);
    rlen -= n;
  }
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
}

void setup() {
  Serial.setRxBufferSize(4096);
  Serial.begin(BAUD);
  pins_on();
}

void loop() {
  uint8_t cmd = rd();
  switch (cmd) {
    case S_CMD_NOP: Serial.write(ACK); break;
    case S_CMD_Q_IFACE: Serial.write(ACK); Serial.write(0x01); Serial.write(0x00); break;
    case S_CMD_Q_CMDMAP: {
      uint8_t map[32] = {0};
      const uint8_t cmds[] = {S_CMD_NOP, S_CMD_Q_IFACE, S_CMD_Q_CMDMAP, S_CMD_Q_PGMNAME,
                              S_CMD_Q_BUSTYPE, S_CMD_SYNCNOP, S_CMD_S_BUSTYPE,
                              S_CMD_O_SPIOP, S_CMD_S_SPI_FREQ, S_CMD_S_PIN_STATE};
      for (uint8_t c : cmds) map[c / 8] |= 1 << (c % 8);
      Serial.write(ACK); Serial.write(map, sizeof(map));
      break;
    }
    case S_CMD_Q_PGMNAME: {
      char name[16] = "esp-serprog";
      Serial.write(ACK); Serial.write((uint8_t *)name, 16);
      break;
    }
    case S_CMD_Q_BUSTYPE: Serial.write(ACK); Serial.write(BUS_SPI); break;
    case S_CMD_SYNCNOP: Serial.write(NAK); Serial.write(ACK); break;
    case S_CMD_S_BUSTYPE: Serial.write((rd() & BUS_SPI) ? ACK : NAK); break;
    case S_CMD_O_SPIOP: spiop(); break;
    case S_CMD_S_SPI_FREQ: {
      uint32_t f = rd32();
      if (f == 0) { Serial.write(NAK); break; }
      spi_freq = f > 8000000 ? 8000000 : f;  // jumper wires: keep it modest
      Serial.write(ACK);
      for (int i = 0; i < 4; i++) Serial.write((uint8_t)(spi_freq >> (8 * i)));
      break;
    }
    case S_CMD_S_PIN_STATE: rd() ? pins_on() : pins_off(); Serial.write(ACK); break;
    default: Serial.write(NAK); break;
  }
}
