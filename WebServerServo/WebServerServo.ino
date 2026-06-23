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
    int angle = server.arg("angle").toInt();
    int resetAngle =
        server.hasArg("reset_angle") ? server.arg("reset_angle").toInt() : 0;

    if (servoNum >= 1 && servoNum <= 5) {
      int index = servoNum - 1;
      Serial.print("Moving Servo ");
      Serial.print(servoNum);
      Serial.print(" to ");
      Serial.println(angle);

      servos[index].write(angle);
      delay(500);
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