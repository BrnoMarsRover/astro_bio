#include "config.hpp"

bool takeCommand(const char *expected) {
  if (!commandReady) return false;
  bool accepted = strcmp(command, expected) == 0;
  commandReady = false;
  return accepted;
}

void setError(uint8_t code, const char *message) {
  digitalWrite(EN_PIN, HIGH);
  backActive = false;
  errorCode = code;
  currentState = STATE_ERROR;
  s.print(F("ERROR "));
  s.print(code);
  s.print(F(": "));
  s.println(message);
}

// ============================================================================
// TASK 1: MAIN CONTROL (STATE MACHINE)
// ============================================================================
void mainControlTask(void *pvParameters){
  int driverConfigAttempts = 0;
  bool driverReady = false;
  SystemState lastPromptedState = STATE_ERROR;
  unsigned long measureStartedAt = 0;
  unsigned long pumpStartedAt = 0;
  unsigned long lastFlowAt = 0;
  unsigned long upperLevelDetectedAt = 0;
  bool lowerLevelReported = false;
  bool upperLevelReported = false;

  for(;;){
    if (commandReady && strcmp(command, "stop") == 0) {
      commandReady = false;
      measurementActive = false;
      pumpingActive = false;
      prepareActive = false;
      purgeActive = false;
      backActive = false;
      setError(7, "Nouzove zastaveni prikazem stop");
    }

    if (currentState != lastPromptedState) {
      if (currentState == STATE_INIT) s.println(F("Pripraveno. Posli 'init'."));
      if (currentState == STATE_MEASURE) s.println(F("Zarizeni inicializovano. Posli 'meas'."));
      if (currentState == STATE_PREPARE) s.println(F("Mereni hotove. Posli 'prepare'."));
      if (currentState == STATE_PUMPING) s.println(F("Pumpovani aktivni."));
      if (currentState == STATE_PURGE) s.println(F("Objem napumpovan. Posli 'purg'."));
      if (currentState == STATE_DONE) s.println(F("Proces hotov. Posli 'reset' nebo 'back'. 'stop' zastavi motor."));
      if (currentState == STATE_ERROR) s.println(F("Chyba. Posli 'reset' pro novou inicializaci nebo 'back' pro zpetny chod."));
      lastPromptedState = currentState;
    }

    switch(currentState){
      case STATE_INIT: {
        if (takeCommand("init")) {
          pinMode(EN_PIN, OUTPUT);
          pinMode(STEP_PIN, OUTPUT);
          pinMode(DIR_PIN, OUTPUT);
          digitalWrite(EN_PIN, HIGH);

          pinMode(FLOW_PIN, INPUT);
          attachInterrupt(digitalPinToInterrupt(FLOW_PIN), pulseCounter, RISING);

          pinMode(TEMP_PIN, INPUT);
          pinMode(PH_PIN, INPUT);
          pinMode(HEIGHT_PIN1, INPUT);
          pinMode(HEIGHT_PIN2, INPUT);
          heightSensorState_1 = digitalRead(HEIGHT_PIN1);
          heightSensorState_2 = digitalRead(HEIGHT_PIN2);

          analogReadResolution(12);
          analogSetPinAttenuation(PH_PIN, ADC_11db);
          analogSetPinAttenuation(TEMP_PIN, ADC_11db);

          while(driverConfigAttempts < MAX_DRIVER_ATTEMPS && !driverReady){
            driverConfigAttempts++;
            s.println("Pokus inicializace TMC2209 " + String(driverConfigAttempts));
            driverReady = configureDriver();

            if(!driverReady){
              s.println("Inicializace selhala, retry za 500ms ...");
              vTaskDelay(pdMS_TO_TICKS(500));
            }
          }

          if (driverReady) {
            s.println("TMC2209 uspesne inicializovan.");
            currentState = STATE_MEASURE;
          } else {
            setError(1, "TMC2209 nenalezen");
          }
        }
        break;
      }

      case STATE_MEASURE: {
        if (takeCommand("meas")) {
          measureStartedAt = millis();
          measurementActive = true;
          s.println(F("Mereni teploty a pH po dobu 30 sekund..."));
        }
        if (measurementActive && measureStartedAt != 0 && millis() - measureStartedAt >= MEASURE_TIME_MS) {
          s.printf("Konec mereni. Teplota: %.2f C, pH: %.2f\n", currentTemp, currentPH);
          measureStartedAt = 0;
          measurementActive = false;
          currentState = STATE_PREPARE;
        }
        break;
      }

      case STATE_PREPARE: {
        if (!prepareActive && takeCommand("prepare")) {
          prepareActive = true;
          digitalWrite(DIR_PIN, PUMP_DIR);
          digitalWrite(EN_PIN, LOW);
          s.println(F("Motor se rozbiha. Prutok se zatim nemeri. Posli 'pump'."));
        }
        if (prepareActive && takeCommand("pump")) {
          prepareActive = false;
          pumpStartedAt = millis();
          lastFlowAt = pumpStartedAt;
          lastFlowPulseAt = pumpStartedAt;
          upperLevelDetectedAt = 0;
          lowerLevelReported = false;
          upperLevelReported = false;
          noInterrupts();
          totalPulses = 0;
          pulseCount = 0;
          interrupts();
          pumpedVolume_mL = 0.0f;
          pumpingActive = true;
          currentState = STATE_PUMPING;
          s.println(F("Zacinam merit a pumpovat do 80 mL."));
        }
        break;
      }

      case STATE_PUMPING: {
        if (pumpStartedAt != 0) {
          if (pumpedVolume_mL >= TARGET_VOLUME_ML) {
            digitalWrite(EN_PIN, HIGH);
            s.println(F("Cilovy objem 80 mL dosazen."));
            pumpStartedAt = 0;
            pumpingActive = false;
            currentState = STATE_PURGE;
          } else if (!heightSensorState_2) {
            if (!upperLevelReported) {
              s.printf("Horni hladinovy snimac: voda detekovana pri objemu %.2f ml\n",
                       pumpedVolume_mL);
              upperLevelReported = true;
            }
            if (upperLevelDetectedAt == 0) {
              upperLevelDetectedAt = millis();
            } else if (millis() - upperLevelDetectedAt >= LEVEL_CONFIRMATION_MS) {
              digitalWrite(EN_PIN, HIGH);
              s.println(F("Horni hladina potvrzena 3 s. Pumpovani ukonceno kvuli ochrane proti preteceni."));
              pumpStartedAt = 0;
              pumpingActive = false;
              currentState = STATE_PURGE;
            }
          } else {
            upperLevelDetectedAt = 0;
          }
          if (currentState == STATE_PUMPING && pumpingActive) {
            if (!heightSensorState_1 && !lowerLevelReported) {
              s.printf("Spodni hladinovy snimac: voda detekovana pri objemu %.2f ml\n",
                       pumpedVolume_mL);
              lowerLevelReported = true;
            } else if (heightSensorState_1) {
              lowerLevelReported = false;
            }
            lastFlowAt = lastFlowPulseAt;
            if (millis() - lastFlowAt >= NO_FLOW_TIMEOUT_MS) {
              setError(4, "Prutok se zastavil");
              pumpStartedAt = 0;
              pumpingActive = false;
            } else if (millis() - pumpStartedAt >= MAX_PUMP_TIME_MS) {
              setError(5, "Pumpovani prekrocilo casovy limit");
              pumpStartedAt = 0;
              pumpingActive = false;
            }
          }
        }
        break;
      }

      case STATE_PURGE: {
        if (takeCommand("purg")) {
          purgeActive = true;
          digitalWrite(DIR_PIN, PUMP_DIR == HIGH ? HIGH : LOW);
          digitalWrite(EN_PIN, LOW);
          s.println(F("Odpumpovani zbytku hadicky. Prutokomer je vypnuty."));
          vTaskDelay(pdMS_TO_TICKS(PURGE_TIME_MS));
          purgeActive = false;
          digitalWrite(EN_PIN, HIGH);
          s.println(F("Cisteni dokonceno."));
          currentState = STATE_DONE;
        }
        break;
      }

      case STATE_DONE: {
        digitalWrite(EN_PIN, HIGH); 
        if (takeCommand("reset")) {
          errorCode = 0;
          measurementActive = false;
          pumpingActive = false;
          purgeActive = false;
          prepareActive = false;
          backActive = false;
          currentState = STATE_INIT;
          driverConfigAttempts = 0;
          driverReady = false;
        } else if (takeCommand("back")) {
          currentState = STATE_ERROR;
          digitalWrite(DIR_PIN, PUMP_DIR == HIGH ? LOW : HIGH);
          digitalWrite(EN_PIN, LOW);
          backActive = true;
          s.println(F("Zpetny chod pumpy aktivni. Posli 'stop' pro zastaveni."));
        }
        break;
      }

      case STATE_ERROR: {
        if (!backActive) {
          digitalWrite(EN_PIN, HIGH);
        }
        if (commandReady) {
          if (!backActive && strcmp(command, "back") == 0) {
            commandReady = false;
            digitalWrite(DIR_PIN, PUMP_DIR == HIGH ? LOW : HIGH);
            digitalWrite(EN_PIN, LOW);
            backActive = true;
            s.println(F("Zpetny chod pumpy aktivni. Posli 'stop' pro zastaveni."));
          } else if (strcmp(command, "reset") == 0) {
            commandReady = false;
            errorCode = 0;
            measurementActive = false;
            pumpingActive = false;
            purgeActive = false;
            prepareActive = false;
            backActive = false;
            currentState = STATE_INIT;
            driverConfigAttempts = 0;
            driverReady = false;
          } else {
            commandReady = false;
          }
        }
        break;
      }   
    } 

    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

// ============================================================================
// TASK 2: SENSOR - TEMPERATURE
// ============================================================================

void tempTask(void *pvParameters){
  for(;;){
      if (currentState == STATE_MEASURE && measurementActive) {
      int raw = analogRead(TEMP_PIN);
        float vOut = raw * (3.3f / 4095.0f);
        if (vOut <= 0.01f || vOut >= 3.29f) {
          setError(6, "Neplatne mereni teploty");
          vTaskDelay(pdMS_TO_TICKS(1000));
          continue;
        }
        float rNtc = R_FIXED / (3.3f / vOut - 1.0f);

      float steinhart;
      steinhart = rNtc / NOMINAL_RESISTOR;
      steinhart = log(steinhart);
      steinhart /= BETA;
      steinhart += 1.0 / NOMINAL_TEMP_K;
      steinhart = 1.0 / steinhart;
      
      currentTemp = steinhart - 273.15; 
    }
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
}

// ============================================================================
// TASK 3: SENSOR - pH WITH FILTER
// ============================================================================
void pHTask(void *pvParameters) {
  int buf[10];
  for (;;) {
    if (currentState == STATE_MEASURE && measurementActive) {
      for (int i = 0; i < 10; i++) {
        // OPRAVA: Přejmenováno na analogReadMilliVolts, aby seděla matematika níže
        buf[i] = analogReadMilliVolts(PH_PIN); 
        vTaskDelay(pdMS_TO_TICKS(10)); 
      }
      
      // Třídění (Bubble Sort)
      for (int i = 0; i < 9; i++) {
        for (int j = i + 1; j < 10; j++) {
          if (buf[i] > buf[j]) {
            int temp = buf[i];
            buf[i] = buf[j];
            buf[j] = temp;
          }
        }
      }
      
      // Oříznutý průměr
      long avgValue = 0;
      for (int i = 2; i < 8; i++) {
        avgValue += buf[i];
      }
      
      float averageMilliVolts = (float)avgValue / 6.0;
      float currentVoltage = averageMilliVolts / 1000.0; // Teď je to správně ve Voltech
      
      float calculatedPh;
      bool onLowPhSegment =
          (currentVoltage - PH_686_VOLTAGE) *
          (PH_401_VOLTAGE - PH_686_VOLTAGE) >= 0.0f;
      if (onLowPhSegment) {
        calculatedPh = 6.86f + (currentVoltage - PH_686_VOLTAGE) *
                       (4.01f - 6.86f) / (PH_401_VOLTAGE - PH_686_VOLTAGE);
      } else {
        calculatedPh = 6.86f + (currentVoltage - PH_686_VOLTAGE) *
                       (12.00f - 6.86f) / (PH_1200_VOLTAGE - PH_686_VOLTAGE);
      }
      rawPH = calculatedPh;
      float temperatureKelvin = currentTemp + 273.15f;
      if (temperatureKelvin > 1.0f) {
        calculatedPh = 6.86f + (calculatedPh - 6.86f) *
                      ((PH_REFERENCE_TEMP_C + 273.15f) / temperatureKelvin);
      }

      if (calculatedPh < 0.0) calculatedPh = 0.0;
      if (calculatedPh > 14.0) calculatedPh = 14.0;

      currentPH = calculatedPh;
      
      s.printf("[pH Sensor] Napeti: %.3f V | pH: %.2f\n", currentVoltage, currentPH);
    }
    vTaskDelay(pdMS_TO_TICKS(500));
  }
}

// ============================================================================
// TASK 4: SENSOR - FLOW METR
// ============================================================================
void flowSensorTask(void *pvParameters) {
  for (;;) {
    if (currentState == STATE_PUMPING && pumpingActive) {
      noInterrupts();
      unsigned long pulses = pulseCount;
      unsigned long total = totalPulses;
      pulseCount = 0;
      interrupts();
      pumpedVolume_mL = (float)total / PULSES_PER_LITER * 1000.0f;
      float flowRate = (float)pulses / PULSES_PER_LITER *
               (60000000.0f / FLOW_INTERVAL_MS);
      if (pulses >= MIN_FLOW_PULSES_PER_INTERVAL) {
        lastFlowPulseAt = millis();
      }
      if (flowRate > 0.0f) {
        s.printf("Prutok: %.2f ml/min | Objem: %.2f ml\n", flowRate, pumpedVolume_mL);
      }
    }
    vTaskDelay(pdMS_TO_TICKS(FLOW_INTERVAL_MS)); 
  }
}

// ============================================================================
// TASK 5: PUMP MOTOR CONTROL
// ============================================================================

void pumpTask(void *pvParameters) {
  unsigned long stepsDone = 0;
  bool wasPumping = false;
  unsigned long lastStepTime = 0;
  unsigned long lastSGTime = 0; // Pomocný časovač pro SG (Safety Guard)

  for (;;) {
    if ((currentState == STATE_PUMPING && pumpingActive) ||
      (currentState == STATE_PREPARE && prepareActive) ||
      (currentState == STATE_PURGE && purgeActive) ||
      (currentState == STATE_ERROR && backActive)) {
      
      if (!wasPumping) {
        stepsDone = 0;
        wasPumping = true;
        lastStepTime = micros(); 
        lastSGTime = millis(); // Inicializace SG časovače
      }

      unsigned long currentInterval_us = rampedInterval(stepsDone);
      unsigned long currentMicros = micros();
      unsigned long elapsed = currentMicros - lastStepTime;

      // 1. KROK: Je čas na pulz motoru?
      if (elapsed >= currentInterval_us) {
        stepPulse();
        lastStepTime += currentInterval_us;
        stepsDone++;
      } 
      // 2. KROK: Čas ještě nenastal. Využijeme SLACK výpočty.
      else {
        unsigned long slack = currentInterval_us - elapsed; // Výpočet zbývajícího času

        // Pokud je slack velký (např. při rozjezdu), spíme regulérně přes RTOS
        if (slack > 2000UL) {
          vTaskDelay(pdMS_TO_TICKS(1)); 
        } 
        // Pokud je slack malý (při plné rychlosti), nastupuje SG (Safety Guard)
        else {
          // Každých 20 ms uvolníme na 1 ms procesor pro IDLE/Watchdog a průtokoměr
          if (millis() - lastSGTime >= 20) {
            vTaskDelay(pdMS_TO_TICKS(1));
            lastSGTime = millis(); // Reset SG časovače
          } else {
            taskYIELD(); 
          }
        }
      }
    } else {
      wasPumping = false;
      vTaskDelay(pdMS_TO_TICKS(50)); 
    }
  }
}

// ============================================================================
// UART COMMUNICATION
// ============================================================================

void uartTask(void *pvParatmeters){
  size_t cmdL = 0;

  for(;;){
    while(!commandReady && s.available()){
      char recieved = (char)s.read();

      if(recieved == '\n' || recieved == '\r'){
        if(cmdL > 0){
          command[cmdL] = '\0';

          // `id` is answered HERE rather than in the state machine. A host
          // probing the USB port has to be able to identify this board in any
          // state -- including while pumping -- and takeCommand() clears
          // commandReady for whatever word it was handed, so routing `id`
          // through the state machine would swallow it without a reply.
          if(strcmp(command, "id") == 0){
            s.printf("{\"firmware\":\"%s\",\"version\":\"%s\","
                     "\"protocol\":%d,\"state\":%d}\n",
                     FIRMWARE_NAME, FIRMWARE_VERSION,
                     FIRMWARE_PROTOCOL, (int)currentState);
            cmdL = 0;
            continue;
          }

          s.print("UART CMD: ");
          s.println(command);
          commandReady = true;
          cmdL = 0;
        }

        }else if(cmdL < sizeof(command) - 1){
          command[cmdL++] = recieved;   
      }

    }

    vTaskDelay(pdMS_TO_TICKS(50));
  }

}

// ============================================================================
// CAPACITANCE HEIGHT LEVEL SENSOR
// ============================================================================

void heightLevelTask(void *pvParameters){
  uint8_t lastState1 = HIGH;
  uint8_t lastState2 = HIGH;

  for(;;){
    if (currentState == STATE_PUMPING && pumpingActive) {
      heightSensorState_1 = digitalRead(HEIGHT_PIN1);
      heightSensorState_2 = digitalRead(HEIGHT_PIN2);
      if (heightSensorState_1 != lastState1 || heightSensorState_2 != lastState2) {
        s.printf("Hladinove snimace: PIN1=%u PIN2=%u\n",
                 heightSensorState_1, heightSensorState_2);
        lastState1 = heightSensorState_1;
        lastState2 = heightSensorState_2;
      }
      //s.print("DETEKCE: ");
      //s.println(heightSensorState ? "HIGH" : "LOW");
    }

    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

// ============================================================================
// ARDUINO SETUP & LOOP
// ============================================================================
void setup() {
  s.begin(115200);
  while(!s);
  s.println(F("Startuji RTOS system..."));

  xTaskCreate(mainControlTask, "MainControl", 4096, NULL, 1, NULL);
  xTaskCreate(flowSensorTask, "FlowSensor", 2048, NULL, 1, NULL);
  xTaskCreate(tempTask, "Temperature", 2048, NULL, 1, NULL);
  xTaskCreate(pHTask, "pH", 3072, NULL, 1, NULL);
  xTaskCreate(pumpTask, "PumpControl", 4096, NULL, 1, NULL);
  xTaskCreate(uartTask, "UART", 2048, NULL, 1, NULL);
  xTaskCreate(heightLevelTask, "HeightLevelTask", 2048, NULL, 1, NULL);
}

void loop() {
  vTaskDelete(NULL); 
}