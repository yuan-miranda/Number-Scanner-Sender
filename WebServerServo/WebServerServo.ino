#include <ESP32Servo.h>
#include <WebServer.h>
#include <WiFi.h>

const char *ssid = "WhiteHouse";
const char *password = "PLDTWIFIPv54q";

WebServer server(80);
Servo servos[5];
const int servoPins[5] = {4, 13, 14, 25, 26};

void handleActivate() {
  if (server.hasArg("servo")) {
    int servoNum = server.arg("servo").toInt();
    if (servoNum >= 1 && servoNum <= 5) {
      int index = servoNum - 1;
      Serial.print("Moving Servo ");
      Serial.println(servoNum);
      servos[index].write(180);
      delay(500);
      servos[index].write(0);
      server.send(200, "text/plain", "OK");
      return;
    }
  }
  server.send(400, "text/plain", "Invalid Servo");
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