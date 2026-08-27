#include <Arduino.h>
#include <TMCStepper.h>

// Pins (ESP32-WROOM-32, 36-pin devkit) ────────────────────
#define EN_PIN     19
#define STEP_PIN   18
#define DIR_PIN    25
#define UART_RX    16   // ESP32 UART2 RX <- PDN_UART (přímo)
#define UART_TX    17   // ESP32 UART2 TX -> přes 1k -> stejný uzel PDN_UART
#define FLOW_PIN   4
#define PH_PIN     26   // ADC1_CH6, pouze vstup — pro analogRead OK
#define TEMP_PIN   39
#define HEIGHT_PIN1 27  // Spodni hladinovy snimac
#define HEIGHT_PIN2 36  // Horni hladinovy snimac

// Stepper + Driver
#define DRIVER_ADDRESS  0b00
#define R_SENSE         0.11f
#define MOTOR_FULL_STEPS  200
#define MICROSTEPS         16
#define STEPS_PER_REV     (MOTOR_FULL_STEPS * MICROSTEPS)
#define RUN_CURRENT_MA    1000   
#define HOLD_CURRENT_MA   200    
#define IHOLD_DELAY         6
#define TARGET_RPM        220UL
#define MAX_DRIVER_ATTEMPS 5

constexpr unsigned long FULL_SPEED_US =
    60000000UL / (TARGET_RPM * (unsigned long)STEPS_PER_REV);

#define RAMP_STEPS  50000UL
constexpr unsigned long START_SPEED_US = FULL_SPEED_US * 3; 
#define PUMP_DIR  HIGH

// Temperature
volatile float currentTemp = 0.0;
#define R_FIXED 10000.0        // Your bridge fixed resistor
#define BETA 3950.0            // Beta coefficient of your NTC
#define NOMINAL_RESISTOR 10000.0 // Resistance at 25C (298.15K)
#define NOMINAL_TEMP_K 298.15  // 25 degrees Celsius in Kelvin

// Flow sensor
#define PULSES_PER_LITER  5025.83
#define FLOW_INTERVAL_MS  100UL

volatile float currentPH = 0.0;
volatile float rawPH = 0.0;
#define PH_REFERENCE_TEMP_C 25.0f
#define MEASURE_TIME_MS 30000UL
#define TARGET_VOLUME_ML 80.0f
#define PURGE_TIME_MS 5000UL
#define MAX_PUMP_TIME_MS 300000UL
#define NO_FLOW_TIMEOUT_MS 5000UL
#define LEVEL_CONFIRMATION_MS 3000UL
#define MIN_FLOW_PULSES_PER_INTERVAL 1UL
volatile unsigned long pulseCount = 0;   // Instant flow rate
volatile unsigned long totalPulses = 0;  // Total volume
volatile float pumpedVolume_mL = 0.0;
volatile unsigned long lastFlowPulseAt = 0;
volatile bool measurementActive = false;
volatile bool pumpingActive = false;
volatile bool purgeActive = false;
volatile bool prepareActive = false;
volatile bool backActive = false;

void IRAM_ATTR pulseCounter() { //use instruction RAM instead of FLASH, because esp32 can disable flash while running rtos
  pulseCount++;
  totalPulses++;
}

// pH sensor
#define PH_SAMPLING_INTERVAL 20
#define PH_PRINT_INTERVAL    800
#define PH_ARRAY_LENGTH      40
#define PH_401_VOLTAGE       2.300f  // Dosad namerene napeti v pufru pH 4.01
#define PH_686_VOLTAGE       1.689f  // Dosad namerene napeti v pufru pH 6.86
#define PH_1200_VOLTAGE      1.000f  // Dosad namerene napeti v pufru pH 12.00

// Objects for UART and TMC2209
HardwareSerial  TMCSerial(2);
TMC2209Stepper  driver(&TMCSerial, R_SENSE, DRIVER_ADDRESS);

static unsigned long rampStepsDone   = 0;
static bool          rampComplete    = false;
static unsigned long lastStepTime    = 0;
static unsigned long currentInterval = START_SPEED_US;
static unsigned long totalMilliLitres = 0; // Celkový protečený objem v ml

unsigned long rampedInterval(unsigned long stepsDone) {
  if (stepsDone >= RAMP_STEPS) return FULL_SPEED_US;
  unsigned long span = START_SPEED_US - FULL_SPEED_US;
  return START_SPEED_US - (span * stepsDone) / RAMP_STEPS;
}

inline void stepPulse() {
  digitalWrite(STEP_PIN, HIGH);
  delayMicroseconds(2);
  digitalWrite(STEP_PIN, LOW);
}

// Driver configuration
bool configureDriver() {
  TMCSerial.begin(115200, SERIAL_8N1, UART_RX, UART_TX);
  driver.begin();
  vTaskDelay(pdMS_TO_TICKS(100));
  
  // Ověř, že jsme se úspěšně připojili
  uint8_t ver = driver.version();
  if (ver != 0x21) {
    return false;  // Komunikace selhala, zkus znova
  }
  
  driver.toff(4);              // Standardní čas vypnutí pro SpreadCycle (3-5)
  driver.blank_time(24);
  driver.rms_current(RUN_CURRENT_MA);
  driver.ihold((uint8_t)map(HOLD_CURRENT_MA, 0, RUN_CURRENT_MA, 0, 31)); // map does the math: HOLD/CURRENT*31
  driver.iholddelay(IHOLD_DELAY);
  driver.microsteps(MICROSTEPS);
  driver.intpol(true);
  
  driver.en_spreadCycle(true);  
  driver.pwm_autoscale(false);
  
  // CoolStep nastavení (snižuje proud, když motor zrovna nepřemáhá odpor)
  driver.semin(2);             // Pokud SG klesne pod 2, zvedni proud
  driver.semax(1);             // Pokud SG stoupne, sniž proud
  driver.sedn(0b01);
  driver.SGTHRS(50);           // Citlivost StallGuardu
  driver.shaft(false);
  
  return true;  // Konfigurace úspěšná
}

// StateMachine
enum SystemState {
  STATE_INIT,
  STATE_PREPARE,
  STATE_MEASURE,
  STATE_PUMPING,
  STATE_PURGE,
  STATE_DONE,
  STATE_ERROR
};

volatile SystemState currentState = STATE_INIT;

HardwareSerial& s = Serial;
static char command[32];
volatile bool commandReady = false;
volatile uint8_t heightSensorState_1 = 0;
volatile uint8_t heightSensorState_2 = 0;
volatile uint8_t errorCode = 0;