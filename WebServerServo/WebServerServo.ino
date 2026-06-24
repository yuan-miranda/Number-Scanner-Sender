#include <ESP32Servo.h>
#include <WebServer.h>
#include <WiFi.h>

const char *ssid = "TP-Link_Extender";
const char *password = "VILLAROSA1225!";

WebServer server(80);
Servo servos[5];
const int servoPins[5] = {4, 13, 14, 25, 26};

void handleActivate() {
  if (server.hasArg("servo") && server.hasArg("angle")) {
    int servoNum = server.arg("servo").toInt();
    int targetAngle = server.arg("angle").toInt();
    int resetAngle =
        server.hasArg("reset_angle") ? server.arg("reset_angle").toInt() : 0;
    int durationMs =
        server.hasArg("duration") ? server.arg("duration").toInt() : 500;

    if (servoNum >= 1 && servoNum <= 5) {
      int index = servoNum - 1;
      int currentAngle = resetAngle;
      int steps = abs(targetAngle - currentAngle);
      if (steps > 0 && durationMs > 0) {
        int stepDir = (targetAngle > currentAngle) ? 1 : -1;
        float delayPerStep = (float)durationMs / steps;
        unsigned long startTime = millis();

        for (int i = 1; i <= steps; i++) {
          servos[index].write(currentAngle + (i * stepDir));
          // Wait precisely until the next step timeframe
          while (millis() - startTime < (unsigned long)(i * delayPerStep)) {
            delay(1);
          }
        }
      } else {
        servos[index].write(targetAngle);
      }

      delay(50);
      servos[index].write(resetAngle);

      server.send(200, "text/plain", "OK");
      return;
    }
  }
  server.send(400, "text/plain", "Invalid Request Parameters");
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < 5; i++) {
    servos[i].attach(servoPins[i]);
    servos[i].write(0);
  }
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
  }
  Serial.println(WiFi.localIP());
  server.on("/activate", HTTP_GET, handleActivate);
  server.begin();
}

void loop() { server.handleClient(); }