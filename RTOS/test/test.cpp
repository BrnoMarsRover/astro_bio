#include <Arduino.h>
#include <ESP32Servo.h>

#define SERVO_PIN 32
#define SERVO_CLOSED_ANGLE 0
#define SERVO_OPEN_ANGLE 180

Servo myServo;

static void moveLid(int angle) {
  int clamped = constrain(angle, SERVO_CLOSED_ANGLE, SERVO_OPEN_ANGLE);
  myServo.write(clamped);
  Serial.printf("lid angle=%d\n", clamped);
}

void setup() {
  Serial.begin(115200);
  delay(500);

  myServo.setPeriodHertz(50);
  myServo.attach(SERVO_PIN, 500, 2400);

  // The full 0..180 range is often not mechanically usable on small lids.
  // Many servos hit the internal stop or the mechanism before the horn reaches the extremes.
  // Keep the motion inside the real working range of the lid.
  moveLid(SERVO_CLOSED_ANGLE);
  delay(500);

  moveLid(SERVO_OPEN_ANGLE);
  delay(500);
}

void loop() {
  static int angle = SERVO_CLOSED_ANGLE;
  static int step = 1;

  moveLid(angle);
  delay(25);

  angle += step;
  if (angle <= SERVO_CLOSED_ANGLE || angle >= SERVO_OPEN_ANGLE) {
    step = -step;
    angle = constrain(angle, SERVO_CLOSED_ANGLE, SERVO_OPEN_ANGLE);
  }
}