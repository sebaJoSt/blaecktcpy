/*
  FakeMasterWithSlaves.ino

  Single-board Arduino sketch that emulates a BlaeckSerial master with
  two I2C slaves — no real slave hardware needed. Speaks the raw
  BlaeckSerial protocol (B0, B3, D1) so blaecktcpy hubs and Loggbok
  can't tell the difference.

  Device tree as seen by Loggbok / blaecktcpy:
    Fake Master  (master, SlaveID 0)   — MasterVoltage, Uptime
    ├── TempSensor    (slave, SlaveID 8)  — Temperature, Humidity
    └── PressureSensor (slave, SlaveID 42) — Pressure

  Upload to any Arduino and connect via Serial or blaecktcpy:
    hub.add_serial("COM3", 115200)

  Requires: CRC32 library (by Rob Tillaart, Arduino Library Manager)
*/

#include <Arduino.h>
#include <CRC32.h>

// ── Protocol constants ──────────────────────────────────────────────
static const byte MSG_KEY_B0 = 0xB0;  // Symbol list
static const byte MSG_KEY_B3 = 0xB3;  // Device info
static const byte MSG_KEY_D1 = 0xD1;  // Data frame
static const byte MSG_KEY_C0 = 0xC0;  // Restarted

// ── Fake signal storage ─────────────────────────────────────────────
// Master signals (SlaveID 0)
float masterVoltage = 3.3;
unsigned long uptimeSeconds = 0;
// Slave 8 signals
float temperature = 22.0;
float humidity = 55.0;
// Slave 42 signals
float pressure = 1013.25;

// ── Timed data ──────────────────────────────────────────────────────
bool timedActive = false;
unsigned long timedInterval = 1000;
unsigned long timedNextSend = 0;
bool firstTimedSend = true;

// ── Restart flag ────────────────────────────────────────────────────
bool sendRestartFlag = true;

// ── Command parser ──────────────────────────────────────────────────
static const int MAX_CMD = 64;
char cmdBuf[MAX_CMD];
int cmdIdx = 0;
bool cmdInProgress = false;

// ── CRC helper ──────────────────────────────────────────────────────
CRC32 crc;

void crcReset()
{
  crc.setPolynome(0x04C11DB7);
  crc.setInitial(0xFFFFFFFF);
  crc.setXorOut(0xFFFFFFFF);
  crc.setReverseIn(true);
  crc.setReverseOut(true);
  crc.restart();
}

void serialWriteAndCRC(const byte *data, size_t len)
{
  Serial.write(data, len);
  crc.add(data, len);
}

void serialWriteAndCRC(byte b)
{
  Serial.write(b);
  crc.add(b);
}

// ── Frame header / footer ───────────────────────────────────────────
void writeHeader(byte msgKey, unsigned long msgId)
{
  Serial.write("<BLAECK:");
  Serial.write(msgKey);
  Serial.write(":");
  byte idBytes[4];
  idBytes[0] = msgId & 0xFF;
  idBytes[1] = (msgId >> 8) & 0xFF;
  idBytes[2] = (msgId >> 16) & 0xFF;
  idBytes[3] = (msgId >> 24) & 0xFF;
  Serial.write(idBytes, 4);
  Serial.write(":");
}

void writeFooter()
{
  Serial.write("/BLAECK>");
  Serial.write("\r\n");
  Serial.flush();
}

// ── Null-terminated string write ────────────────────────────────────
void writeString0(const char *s)
{
  Serial.print(s);
  Serial.print('\0');
}

// ── B0: Write Symbols ───────────────────────────────────────────────
void writeSymbols(unsigned long msgId)
{
  writeHeader(MSG_KEY_B0, msgId);

  // Master (MSC=1, SlaveID=0): MasterVoltage (float=0x08), Uptime (ulong=0x07)
  Serial.write((byte)1); Serial.write((byte)0);
  writeString0("MasterVoltage"); Serial.write((byte)0x08);
  Serial.write((byte)1); Serial.write((byte)0);
  writeString0("Uptime"); Serial.write((byte)0x07);

  // Slave 8 (MSC=2, SlaveID=8): Temperature (float), Humidity (float)
  Serial.write((byte)2); Serial.write((byte)8);
  writeString0("Temperature"); Serial.write((byte)0x08);
  Serial.write((byte)2); Serial.write((byte)8);
  writeString0("Humidity"); Serial.write((byte)0x08);

  // Slave 42 (MSC=2, SlaveID=42): Pressure (float)
  Serial.write((byte)2); Serial.write((byte)42);
  writeString0("Pressure"); Serial.write((byte)0x08);

  writeFooter();
}

// ── B3: Write Devices ───────────────────────────────────────────────
void writeDevice(byte msc, byte slaveId, const char *name,
                 const char *hw, const char *fw)
{
  Serial.write(msc);
  Serial.write(slaveId);
  writeString0(name);
  writeString0(hw);
  writeString0(fw);
  writeString0("5.0.1");         // Library version
  writeString0("BlaeckSerial");  // Library name
}

void writeDevices(unsigned long msgId)
{
  writeHeader(MSG_KEY_B3, msgId);
  writeDevice(1, 0,  "Fake Master",     "Arduino Mega 2560", "1.0");
  writeDevice(2, 8,  "TempSensor",      "Arduino Nano",      "1.0");
  writeDevice(2, 42, "PressureSensor",  "Arduino Nano",      "1.0");
  writeFooter();
}

// ── C0: Write Restarted ─────────────────────────────────────────────
void writeRestarted(unsigned long msgId)
{
  writeHeader(MSG_KEY_C0, msgId);
  // Only master device
  writeDevice(1, 0, "Fake Master", "Arduino Mega 2560", "1.0");
  writeFooter();
}

// ── D1: Write Data ──────────────────────────────────────────────────
void writeFloatData(uint16_t idx, float val)
{
  byte idxBytes[2] = { (byte)(idx & 0xFF), (byte)((idx >> 8) & 0xFF) };
  serialWriteAndCRC(idxBytes, 2);
  byte *fb = (byte *)&val;
  serialWriteAndCRC(fb, 4);
}

void writeULongData(uint16_t idx, unsigned long val)
{
  byte idxBytes[2] = { (byte)(idx & 0xFF), (byte)((idx >> 8) & 0xFF) };
  serialWriteAndCRC(idxBytes, 2);
  byte vb[4];
  vb[0] = val & 0xFF;
  vb[1] = (val >> 8) & 0xFF;
  vb[2] = (val >> 16) & 0xFF;
  vb[3] = (val >> 24) & 0xFF;
  serialWriteAndCRC(vb, 4);
}

void writeData(unsigned long msgId)
{
  Serial.write("<BLAECK:");

  crcReset();

  // Message key
  serialWriteAndCRC(MSG_KEY_D1);

  // ":"
  serialWriteAndCRC(':');

  // Message ID (4 bytes)
  byte idBytes[4];
  idBytes[0] = msgId & 0xFF;
  idBytes[1] = (msgId >> 8) & 0xFF;
  idBytes[2] = (msgId >> 16) & 0xFF;
  idBytes[3] = (msgId >> 24) & 0xFF;
  serialWriteAndCRC(idBytes, 4);

  // ":"
  serialWriteAndCRC(':');

  // Restart flag
  byte rf = sendRestartFlag ? 1 : 0;
  serialWriteAndCRC(rf);
  sendRestartFlag = false;

  // ":"
  serialWriteAndCRC(':');

  // Timestamp mode = 0 (no timestamp)
  serialWriteAndCRC((byte)0);

  // ":"
  serialWriteAndCRC(':');

  // Signal data: idx 0..4
  writeFloatData(0, masterVoltage);
  writeULongData(1, uptimeSeconds);
  writeFloatData(2, temperature);
  writeFloatData(3, humidity);
  writeFloatData(4, pressure);

  // Status byte = 0 (normal)
  Serial.write((byte)0);

  // CRC32
  uint32_t crcVal = crc.calc();
  Serial.write((byte *)&crcVal, 4);

  writeFooter();
}

// ── Command parsing ─────────────────────────────────────────────────
unsigned long parseMsgId(const char *params)
{
  // Parse up to 4 comma-separated bytes from params string
  int p[4] = {0, 0, 0, 0};
  int n = 0;
  char tmp[MAX_CMD];
  strncpy(tmp, params, MAX_CMD - 1);
  tmp[MAX_CMD - 1] = '\0';
  char *tok = strtok(tmp, ",");
  while (tok && n < 4)
  {
    p[n++] = atoi(tok);
    tok = strtok(NULL, ",");
  }
  return ((unsigned long)p[3] << 24) | ((unsigned long)p[2] << 16) |
         ((unsigned long)p[1] << 8) | (unsigned long)p[0];
}

void handleCommand(const char *cmd)
{
  // Echo back
  Serial.print("<");
  Serial.print(cmd);
  Serial.println(">");

  // Find first comma to split command from params
  const char *comma = strchr(cmd, ',');
  char command[MAX_CMD];
  char params[MAX_CMD] = "";

  if (comma)
  {
    int cmdLen = comma - cmd;
    strncpy(command, cmd, cmdLen);
    command[cmdLen] = '\0';
    strncpy(params, comma + 1, MAX_CMD - 1);
    params[MAX_CMD - 1] = '\0';
  }
  else
  {
    strncpy(command, cmd, MAX_CMD - 1);
    command[MAX_CMD - 1] = '\0';
  }

  // Trim leading space from command
  char *c = command;
  while (*c == ' ') c++;

  if (strcmp(c, "BLAECK.WRITE_SYMBOLS") == 0)
  {
    writeSymbols(parseMsgId(params));
  }
  else if (strcmp(c, "BLAECK.WRITE_DATA") == 0)
  {
    writeData(parseMsgId(params));
  }
  else if (strcmp(c, "BLAECK.GET_DEVICES") == 0)
  {
    writeDevices(parseMsgId(params));
  }
  else if (strcmp(c, "BLAECK.ACTIVATE") == 0)
  {
    unsigned long interval = parseMsgId(params);  // same 4-byte parse
    timedActive = true;
    timedInterval = interval;
    firstTimedSend = true;
  }
  else if (strcmp(c, "BLAECK.DEACTIVATE") == 0)
  {
    timedActive = false;
  }
}

// ── Main ────────────────────────────────────────────────────────────
void setup()
{
  Serial.begin(115200);
}

void loop()
{
  // Update fake sensor data
  float t = millis() / 1000.0;
  masterVoltage = 3.3 + 0.1 * sin(t * 0.2);
  uptimeSeconds = millis() / 1000;
  temperature = 22.0 + 3.0 * sin(t * 0.3);
  humidity = 55.0 + 10.0 * sin(t * 0.15);
  pressure = 1013.25 + 5.0 * sin(t * 0.1);

  // Read serial commands
  while (Serial.available() > 0)
  {
    char rc = Serial.read();
    if (rc == '<')
    {
      cmdInProgress = true;
      cmdIdx = 0;
    }
    else if (rc == '>' && cmdInProgress)
    {
      cmdBuf[cmdIdx] = '\0';
      cmdInProgress = false;
      handleCommand(cmdBuf);
    }
    else if (cmdInProgress)
    {
      if (cmdIdx < MAX_CMD - 1)
        cmdBuf[cmdIdx++] = rc;
    }
  }

  // Send C0 (restarted) once
  static bool restartedSent = false;
  if (!restartedSent)
  {
    restartedSent = true;
    writeRestarted(1);
  }

  // Timed data
  if (timedActive)
  {
    unsigned long now = millis();
    if (firstTimedSend || now >= timedNextSend)
    {
      if (firstTimedSend)
        timedNextSend = now + timedInterval;
      else
        timedNextSend += timedInterval;
      firstTimedSend = false;

      writeData(185273099);  // default msg_id used by BlaeckSerial
    }
  }
}
