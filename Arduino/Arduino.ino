
// Pins for motors A and B
int P1[2] = {4, 6};   // IN1
int P2[2] = {5, 7};   // IN2
bool INV[2] = {false, false};


const unsigned long RETRACT_MS  = 5000;   // Retract duration per action
const unsigned long EXTEND_MS   = 5000;   // Extend-back (home) duration
const unsigned long INTERVAL_MS = 10000;  // Pause between two actions (10 seconds)
const unsigned long HOME_MS     = 5000;   // Power-on homing: both extend out

// State machine
enum { IDLE, RETRACT, EXTEND, WAIT };
byte phase = IDLE;
int curMotor = 1;            // Used for alternating; first action makes it 0 (motor A)
unsigned long phaseStart = 0;

bool personPresent = false;


void mExtend(int i) {
  if (INV[i]) { digitalWrite(P1[i], LOW);  digitalWrite(P2[i], HIGH); }
  else        { digitalWrite(P1[i], HIGH); digitalWrite(P2[i], LOW);  }
}
void mRetract(int i) {
  if (INV[i]) { digitalWrite(P1[i], HIGH); digitalWrite(P2[i], LOW);  }
  else        { digitalWrite(P1[i], LOW);  digitalWrite(P2[i], HIGH); }
}
void mHold(int i) { digitalWrite(P1[i], LOW); digitalWrite(P2[i], LOW); }

void setup() {
  Serial.begin(9600);
  delay(50);
  Serial.println("=== FW: A=2/3 B=4/5 | one-at-a-time ===");

  for (int i = 0; i < 2; i++) { pinMode(P1[i], OUTPUT); pinMode(P2[i], OUTPUT); }

  // Power-on homing: both extend out
  mExtend(0); mExtend(1);
  delay(HOME_MS);
  mHold(0); mHold(1);
  phase = IDLE;

  Serial.println("READY");
}

void loop() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == 'E') personPresent = true;
    else if (c == 'S') personPresent = false;
  }

  unsigned long now = millis();

  switch (phase) {
    case IDLE:
      if (personPresent) {                 // Person detected: start the first action immediately
        curMotor = 1 - curMotor;           // Alternate
        mRetract(curMotor);
        phase = RETRACT; phaseStart = now;
        Serial.print("Action "); Serial.print(curMotor == 0 ? "A" : "B"); Serial.println(" retract");
      }
      break;

    case RETRACT:
      // Nobody present partway through: just extend back home
      if (!personPresent) { mExtend(curMotor); phase = EXTEND; phaseStart = now; break; }
      if (now - phaseStart >= RETRACT_MS) {
        mExtend(curMotor);                 // Retract done: extend back home
        phase = EXTEND; phaseStart = now;
      }
      break;

    case EXTEND:
      if (now - phaseStart >= EXTEND_MS) {
        mHold(curMotor);                   // Back at extended position, stop
        phase = WAIT; phaseStart = now;
      }
      break;

    case WAIT:
      if (!personPresent) { phase = IDLE; break; }
      if (now - phaseStart >= INTERVAL_MS) {   // After the 10s interval, switch to the other motor
        curMotor = 1 - curMotor;
        mRetract(curMotor);
        phase = RETRACT; phaseStart = now;
        Serial.print("Action"); Serial.print(curMotor == 0 ? "A" : "B"); Serial.println(" retract");
      }
      break;
  }
}
