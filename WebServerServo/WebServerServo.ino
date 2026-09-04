#include <ESP32Servo.h>
#include <WebServer.h>
#include <WiFi.h>

const char *ssid = "TP-Link_Extender";
const char *password = "VILLAROSA1225!";

WebServer server(80);
Servo servos[6];

// DONT REMOVE FOR FUTURE USE {4, 13, 16, 17, 18, 19, 21, 22, 25, 26}
// const int servoPins[] = {15, 16, 17, 18, 8, 3};
const int servoPins[] = {4, 13, 16, 17, 18, 19};

const int servoCount = sizeof(servoPins) / sizeof(servoPins[0]);
const int idleAngle = 180;

void handleActivate() {
  if (!server.hasArg("servo") || !server.hasArg("angle")) {
    server.send(400, "text/plain", "Missing parameters");
    return;
  }

  int servoNum = server.arg("servo").toInt();
  int targetAngle = server.arg("angle").toInt();

  if (servoNum < 1 || servoNum > servoCount) {
    server.send(400, "text/plain", "Servo out of range");
    return;
  }

  targetAngle = constrain(targetAngle, 1, 180);
  targetAngle = 180 - targetAngle;

  int index = servoNum - 1;
  servos[index].write(targetAngle);
  delay(1000);
  servos[index].write(idleAngle);

  server.send(200, "text/plain", "OK");
}

void handleServoCount() {
  String json = "{\"count\":" + String(servoCount) + "}";
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.send(200, "application/json", json);
}

void setup() {
  Serial.begin(115200);

  for (int i = 0; i < servoCount; i++) {
    servos[i].attach(servoPins[i]);
    servos[i].write(idleAngle);
  }

  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
  }
  Serial.println(WiFi.localIP());

  server.on("/activate", HTTP_GET, handleActivate);
  server.on("/servo_count", HTTP_GET, handleServoCount);
  server.begin();
}

void loop() { server.handleClient(); }
