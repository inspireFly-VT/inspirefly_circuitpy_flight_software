"""
CircuitPython driver for PySquared satellite board.
PySquared Hardware Version: mainboard-v01
CircuitPython Version: 9.0.0 alpha
Library Repo:

* Author(s): Nicole Maggard and Michael Pham
"""

# Common CircuitPython Libs
import board, microcontroller
import busio, time, sys, traceback
import digitalio, pwmio
from debugcolor import co
import gc

# Hardware Specific Libs
import neopixel  # RGB LED
import adafruit_pca9685  # LED Driver
import adafruit_tca9548a  # I2C Multiplexer
import adafruit_pct2075  # Temperature Sensor
import adafruit_ina219  # Power Monitor

# CAN Bus Import
from adafruit_mcp2515 import MCP2515 as CAN

# Common CircuitPython Libs
from os import listdir, stat, statvfs, mkdir, chdir
from bitflags import bitFlag, multiBitFlag, multiByte
from micropython import const

# Thermoucouple ADC
import adafruit_ads1x15.ads1015 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

# NVM register numbers
_BOOTCNT = const(0)
_VBUSRST = const(6)
_STATECNT = const(7)
_TOUTS = const(9)
_ICHRG = const(11)
_DIST = const(13)
_FLAG = const(16)

SEND_BUFF = bytearray(252)


class Satellite:
    # General NVM counters
    c_boot = multiBitFlag(register=_BOOTCNT, lowest_bit=0, num_bits=8)
    c_vbusrst = multiBitFlag(register=_VBUSRST, lowest_bit=0, num_bits=8)
    c_state_err = multiBitFlag(register=_STATECNT, lowest_bit=0, num_bits=8)
    c_distance = multiBitFlag(register=_DIST, lowest_bit=0, num_bits=8)
    c_ichrg = multiBitFlag(register=_ICHRG, lowest_bit=0, num_bits=8)

    # Define NVM flags
    f_softboot = bitFlag(register=_FLAG, bit=0)
    f_solar = bitFlag(register=_FLAG, bit=1)
    f_burnarm = bitFlag(register=_FLAG, bit=2)
    f_brownout = bitFlag(register=_FLAG, bit=3)
    f_triedburn = bitFlag(register=_FLAG, bit=4)
    f_shtdwn = bitFlag(register=_FLAG, bit=5)
    f_burned = bitFlag(register=_FLAG, bit=6)
    f_fsk = bitFlag(register=_FLAG, bit=7)

    # Turns all of the Faces On (Defined before init because this fuction is called by the init)
    def all_faces_on(self):
        # Faces MUST init in this order or the uController will brown out. Cause unknown
        if self.hardware["FLD"]:
            self.Face0.duty_cycle = 0xFFFF
            self.hardware["Face0"] = True
            self.Face1.duty_cycle = 0xFFFF
            self.hardware["Face1"] = True
            self.Face2.duty_cycle = 0xFFFF
            self.hardware["Face2"] = True
            self.Face3.duty_cycle = 0xFFFF
            self.hardware["Face3"] = True
            self.Face4.duty_cycle = 0xFFFF
            self.hardware["Face4"] = True

    def all_faces_off(self):
        # De-Power Faces
        if self.hardware["FLD"]:
            self.Face0.duty_cycle = 0x0000
            time.sleep(0.1)
            self.hardware["Face0"] = False
            self.Face1.duty_cycle = 0x0000
            time.sleep(0.1)
            self.hardware["Face1"] = False
            self.Face2.duty_cycle = 0x0000
            time.sleep(0.1)
            self.hardware["Face2"] = False
            self.Face3.duty_cycle = 0x0000
            time.sleep(0.1)
            self.hardware["Face3"] = False
            self.Face4.duty_cycle = 0x0000
            time.sleep(0.1)
            self.hardware["Face4"] = False

    def debug_print(self, statement):
        if self.debug:
            print(co("[BATTERY][Pysquared]" + str(statement), "red", "bold"))

    def __init__(self):
        """
        Big init routine as the whole board is brought up.
        """
        self.debug = True  # Define verbose output here. True or False
        self.BOOTTIME = 1577836800
        self.debug_print(f"Boot time: {self.BOOTTIME}s")
        self.CURRENTTIME = self.BOOTTIME
        self.UPTIME = 0
        self.heating = False
        self.NORMAL_TEMP = 20
        self.NORMAL_BATT_TEMP = 1  # Set to 0 BEFORE FLIGHT!!!!!
        self.NORMAL_MICRO_TEMP = 20
        self.NORMAL_CHARGE_CURRENT = 0.5
        self.NORMAL_BATTERY_VOLTAGE = 6.9  # 6.9
        self.CRITICAL_BATTERY_VOLTAGE = 6.6  # 6.6
        self.urate = 115200
        self.send_buff = memoryview(SEND_BUFF)
        self.micro = microcontroller
        self.hardware = {
            "WDT": False,
            "NEO": False,
            "TCA": False,
            "SOLAR": False,
            "PWR": False,
            "FLD": False,
            "TEMP": False,
            "COUPLE": False,
            "CAN": False,
            "Face0": False,
            "Face1": False,
            "Face2": False,
            "Face3": False,
            "Face4": False,
        }

        # Define burn wires:
        self._relayA = digitalio.DigitalInOut(board.BURN_RELAY_A)
        self._relayA.switch_to_output(drive_mode=digitalio.DriveMode.OPEN_DRAIN)
        self._resetReg = digitalio.DigitalInOut(board.VBUS_RESET)
        self._resetReg.switch_to_output(drive_mode=digitalio.DriveMode.OPEN_DRAIN)

        # Define SPI,I2C,UART | paasing I2C1 to BigData
        try:
            self.i2c0 = busio.I2C(board.I2C0_SCL, board.I2C0_SDA, timeout=5)
            self.spi0 = busio.SPI(board.SPI0_SCK, board.SPI0_MOSI, board.SPI0_MISO)
            self.i2c1 = busio.I2C(
                board.I2C1_SCL, board.I2C1_SDA, timeout=5, frequency=100000
            )
            self.spi1 = busio.SPI(board.SPI1_SCK, board.SPI1_MOSI, board.SPI1_MISO)
            self.uart = busio.UART(board.TX, board.RX, baudrate=self.urate)
        except Exception as e:
            self.debug_print(
                "ERROR INITIALIZING BUSSES: " + "".join(traceback.format_exception(e))
            )

        # Initialize LED Driver
        try:
            self.faces = adafruit_pca9685.PCA9685(self.i2c0, address=int(0x56))
            self.faces.frequency = 2000
            self.hardware["FLD"] = True
        except Exception as e:
            self.debug_print(
                "[ERROR][LED Driver]" + "".join(traceback.format_exception(e))
            )

        # Initialize all of the Faces and their sensors
        try:
            self.Face0 = self.faces.channels[0]
            self.Face1 = self.faces.channels[1]
            self.Face2 = self.faces.channels[2]
            self.Face3 = self.faces.channels[3]
            self.Face4 = self.faces.channels[4]
            self.all_faces_on()
        except Exception as e:
            self.debug_print(
                "ERROR INITIALIZING FACES: " + "".join(traceback.format_exception(e))
            )

        # Define I2C Reset
        self._i2c_reset = digitalio.DigitalInOut(board.I2C_RESET)
        self._i2c_reset.switch_to_output(value=True)

        if self.c_boot > 200:
            self.c_boot = 0

        if self.f_softboot:
            self.f_softboot = False

        # Define radio
        _rf_cs1 = digitalio.DigitalInOut(board.SPI0_CS0)
        self.enable_rf = digitalio.DigitalInOut(board.RF_ENABLE)

        # self.enable_rf.switch_to_output(value=False) # if U21
        self.enable_rf.switch_to_output(value=True)  # if U7
        _rf_cs1.switch_to_output(value=True)

        # Define Heater Pins
        try:
            if self.hardware["FLD"]:
                self.heater = self.faces.channels[15]
        except Exception as e:
            self.debug_print(
                "[WARNING][Battery_Heater]" + "".join(traceback.format_exception(e))
            )

        # Initialize Neopixel
        try:
            self.neopwr = digitalio.DigitalInOut(board.NEO_PWR)
            self.neopwr.switch_to_output(value=True)
            self.neopixel = neopixel.NeoPixel(
                board.NEOPIXEL, 1, brightness=0.2, pixel_order=neopixel.GRB
            )
            self.neopixel[0] = (0, 0, 255)
            self.hardware["NEO"] = True
        except Exception as e:
            self.debug_print(
                "[WARNING][Neopixel]" + "".join(traceback.format_exception(e))
            )

        # Initialize Power Monitor
        try:
            time.sleep(1)
            self.pwr = adafruit_ina219.INA219(self.i2c0, addr=int(0x40))
            self.hardware["PWR"] = True
        except Exception as e:
            self.debug_print(
                "[ERROR][Power Monitor]" + "".join(traceback.format_exception(e))
            )

        # Initialize Solar Power Monitor
        try:
            time.sleep(1)
            self.solar = adafruit_ina219.INA219(self.i2c0, addr=int(0x44))
            self.hardware["SOLAR"] = True
        except Exception as e:
            self.debug_print(
                "[ERROR][SOLAR Power Monitor]" + "".join(traceback.format_exception(e))
            )

        # Define Charge Indicate Pin
        self.charge_indicate = digitalio.DigitalInOut(board.IS_CHARGING)
        self.charge_indicate.switch_to_input(pull=digitalio.Pull.DOWN)

        # Initialize PCT2075 Temperature Sensor
        try:
            self.pct = adafruit_pct2075.PCT2075(self.i2c0, address=0x4F)
            self.hardware["TEMP"] = True
        except Exception as e:
            self.debug_print(
                "[ERROR][TEMP SENSOR]" + "".join(traceback.format_exception(e))
            )

        # Initialize Thermocouple ADC
        try:
            self.thermocouple = ADS.ADS1015(self.i2c0, address=0x48)
            self.hardware["COUPLE"] = True
            self.debug_print("[ACTIVE][Thermocouple]")
        except Exception as e:
            self.debug_print(
                "[ERROR][THERMOCOUPLE]" + "".join(traceback.format_exception(e))
            )

        # Initialize TCA
        try:
            self.tca = adafruit_tca9548a.TCA9548A(self.i2c0, address=int(0x77))
            for channel in range(8):
                if self.tca[channel].try_lock():
                    self.debug_print("Channel {}:".format(channel))
                    addresses = self.tca[channel].scan()
                    print([hex(address) for address in addresses if address != 0x70])
                    self.tca[channel].unlock()
            self.hardware["TCA"] = True
        except Exception as e:
            self.debug_print("[ERROR][TCA]" + "".join(traceback.format_exception(e)))

        # Initialize CAN Transceiver
        try:
            self.spi1cs0 = digitalio.DigitalInOut(board.SPI1_CS0)
            self.spi1cs0.switch_to_output()
            self.can_bus = CAN(self.spi1, self.spi1cs0, loopback=True, silent=True)
            self.hardware["CAN"] = True

        except Exception as e:
            self.debug_print(
                "[ERROR][CAN TRANSCEIVER]" + "".join(traceback.format_exception(e))
            )

        # Prints init state of PySquared hardware
        self.debug_print(str(self.hardware))

        # set PyCubed power mode
        self.power_mode = "normal"

    def reinit(self, dev):
        if dev == "pwr":
            self.pwr.__init__(self.i2c0)
        elif dev == "fld":
            self.faces.__init__(self.i2c0)
        else:
            self.debug_print("Invalid Device? ->" + str(dev))

    # =======================================================#
    # RGB Setter / Getter                                   #
    # =======================================================#
    @property
    def RGB(self):
        return self.neopixel[0]

    @RGB.setter
    def RGB(self, value):
        if self.hardware["NEO"]:
            try:
                self.neopixel[0] = value
            except Exception as e:
                self.debug_print("[ERROR]" + "".join(traceback.format_exception(e)))
        else:
            self.debug_print("[WARNING] neopixel not initialized")

    # =======================================================#
    # Before Flight Flags                                   #
    # =======================================================#
    # These flags should be set as follows before flight:   #
    # burnarm = True                                        #
    # burned = False                                        #
    # dist = 0                                              #
    # =======================================================#
    @property
    def burnarm(self):
        return self.f_burnarm

    @burnarm.setter
    def burnarm(self, value):
        self.f_burnarm = value

    @property
    def burned(self):
        return self.f_burned

    @burned.setter
    def burned(self, value):
        self.f_burned = value

    @property
    def dist(self):
        return self.c_distance

    @dist.setter
    def dist(self, value):
        self.c_distance = int(value)

    def arm_satellite(self):
        self.burnarm = True
        self.burned = False
        self.dist = 0
        print("[Satellite Armed]")

    def disarm_satellite(self):
        self.burnarm = False
        self.burned = True
        self.dist = 0
        print("[Satellite Disarmed]")

    # =======================================================#
    # Getting and Setting Power for Individual Faces        #
    # =======================================================#
    @property
    def Face0_state(self):
        return self.hardware["Face0"]

    @Face0_state.setter
    def Face0_state(self, value):
        if self.hardware["FLD"]:
            if value:
                try:
                    self.Face0 = 0xFFFF
                    self.hardware["Face0"] = True
                    self.debug_print("z Face Powered On")
                except Exception as e:
                    self.debug_print(
                        "[WARNING][Face0]" + "".join(traceback.format_exception(e))
                    )
                    self.hardware["Face0"] = False
            else:
                self.Face0 = 0x0000
                self.hardware["Face0"] = False
                self.debug_print("z+ Face Powered Off")
        else:
            self.debug_print("[WARNING] LED Driver not initialized")

    @property
    def Face1_state(self):
        return self.hardware["Face1"]

    @Face1_state.setter
    def Face1_state(self, value):
        if self.hardware["FLD"]:
            if value:
                try:
                    self.Face1 = 0xFFFF
                    self.hardware["Face1"] = True
                    self.debug_print("z- Face Powered On")
                except Exception as e:
                    self.debug_print(
                        "[WARNING][Face1]" + "".join(traceback.format_exception(e))
                    )
                    self.hardware["Face1"] = False
            else:
                self.Face1 = 0x0000
                self.hardware["Face1"] = False
                self.debug_print("z- Face Powered Off")
        else:
            self.debug_print("[WARNING] LED Driver not initialized")

    @property
    def Face2_state(self):
        return self.hardware["Face2"]

    @Face2_state.setter
    def Face2_state(self, value):
        if self.hardware["FLD"]:
            if value:
                try:
                    self.Face2 = 0xFFFF
                    self.hardware["Face2"] = True
                    self.debug_print("y+ Face Powered On")
                except Exception as e:
                    self.debug_print(
                        "[WARNING][Face2]" + "".join(traceback.format_exception(e))
                    )
                    self.hardware["Face2"] = False
            else:
                self.Face2 = 0x0000
                self.hardware["Face2"] = False
                self.debug_print("y+ Face Powered Off")
        else:
            self.debug_print("[WARNING] LED Driver not initialized")

    @property
    def Face3_state(self):
        return self.hardware["Face3"]

    @Face3_state.setter
    def Face3_state(self, value):
        if self.hardware["FLD"]:
            if value:
                try:
                    self.Face3 = 0xFFFF
                    self.hardware["Face3"] = True
                    self.debug_print("x- Face Powered On")
                except Exception as e:
                    self.debug_print(
                        "[WARNING][Face3]" + "".join(traceback.format_exception(e))
                    )
                    self.hardware["Face3"] = False
            else:
                self.Face3 = 0x0000
                self.hardware["Face3"] = False
                self.debug_print("x- Face Powered Off")
        else:
            self.debug_print("[WARNING] LED Driver not initialized")

    @property
    def Face4_state(self):
        return self.hardware["Face4"]

    @Face4_state.setter
    def Face4_state(self, value):
        if self.hardware["FLD"]:
            if value:
                try:
                    self.Face4 = 0xFFFF
                    self.hardware["Face4"] = True
                    self.debug_print("x+ Face Powered On")
                except Exception as e:
                    self.debug_print(
                        "[WARNING][Face4]" + "".join(traceback.format_exception(e))
                    )
                    self.hardware["Face4"] = False
            else:
                self.Face4 = 0x0000
                self.hardware["Face4"] = False
                self.debug_print("x+ Face Powered Off")
        else:
            self.debug_print("[WARNING] LED Driver not initialized")

    # =======================================================#
    # Getters for State of Health Monitoring                #
    # =======================================================#
    @property
    def battery_voltage(self):
        if self.hardware["PWR"]:
            voltage = 0
            try:
                for _ in range(50):
                    voltage += self.pwr.bus_voltage
                return voltage / 50 + 0.2  # volts and corection factor
            except Exception as e:
                self.debug_print(
                    "[WARNING][PWR Monitor]" + "".join(traceback.format_exception(e))
                )
        else:
            self.debug_print("[WARNING] Power monitor not initialized")

    @property
    def system_voltage(self):
        if self.hardware["PWR"]:
            voltage = 0
            try:
                for _ in range(50):
                    voltage += self.pwr.bus_voltage + self.pwr.shunt_voltage
                return voltage / 50  # volts
            except Exception as e:
                self.debug_print(
                    "[WARNING][PWR Monitor]" + "".join(traceback.format_exception(e))
                )
        else:
            self.debug_print("[WARNING] Power monitor not initialized")

    @property
    def current_draw(self):
        if self.hardware["PWR"]:
            idraw = 0
            try:
                for _ in range(50):  # average 50 readings
                    idraw += self.pwr.current
                return idraw / 50
            except Exception as e:
                self.debug_print(
                    "[WARNING][PWR Monitor]" + "".join(traceback.format_exception(e))
                )
        else:
            self.debug_print("[WARNING] Power monitor not initialized")

    @property
    def is_charging(self):
        return not (self.charge_indicate.value)

    @property
    def charge_voltage(self):
        if self.hardware["SOLAR"]:
            voltage = 0
            try:
                for _ in range(50):
                    voltage += self.solar.bus_voltage
                return voltage / 50 + 0.2  # volts and corection factor
            except Exception as e:
                self.debug_print(
                    "[WARNING][SOLAR PWR Monitor]"
                    + "".join(traceback.format_exception(e))
                )
        else:
            self.debug_print("[WARNING] SOLAR Power monitor not initialized")

    @property
    def charge_current(self):
        if self.hardware["SOLAR"]:
            ichrg = 0
            try:
                for _ in range(50):  # average 50 readings
                    ichrg += self.solar.current
                return ichrg / 50
            except Exception as e:
                self.debug_print(
                    "[WARNING][SOLAR PWR Monitor]"
                    + "".join(traceback.format_exception(e))
                )
        else:
            self.debug_print("[WARNING] SOLAR Power monitor not initialized")

    @property
    def uptime(self):
        self.CURRENTTIME = const(time.time())
        return self.CURRENTTIME - self.BOOTTIME

    @property
    def reset_vbus(self):
        try:
            self._resetReg.drive_mode = digitalio.DriveMode.PUSH_PULL
            self._resetReg.value = 1
        except Exception as e:
            self.debug_print(
                "vbus reset error: " + "".join(traceback.format_exception(e))
            )

    # =======================================================#
    # Thermal Management                                    #
    # =======================================================#
    @property
    def internal_temperature(self):
        return self.pct.temperature

    @property
    def battery_temperature(self):
        if self.hardware["COUPLE"]:
            chan = AnalogIn(self.thermocouple, ADS.P1)
            tip = (chan.voltage - 1.25) / 0.005
            return tip
        else:
            self.debug_print("[WARNING] Thermocouple not initialized")

    def heater_on(self):
        if self.hardware["FLD"]:
            try:
                self._relayA.drive_mode = digitalio.DriveMode.PUSH_PULL
                if self.f_brownout:
                    pass
                else:
                    self.f_brownout = True
                    self.heating = True
                    self._relayA.value = 1
                    self.RGB = (255, 165, 0)
                    # Pause to ensure relay is open
                    time.sleep(0.25)
                    self.heater.duty_cycle = 0x7FFF
            except Exception as e:
                self.debug_print(
                    "[ERROR] Cant turn on heater: "
                    + "".join(traceback.format_exception(e))
                )
                self.heater.duty_cycle = 0x0000
        else:
            self.debug_print("[WARNING] LED Driver not initialized")

    def heater_off(self):
        if self.hardware["FLD"]:
            try:
                self.heater.duty_cycle = 0x0000
                self._relayA.value = 0
                self._relayA.drive_mode = digitalio.DriveMode.OPEN_DRAIN
                if self.heating == True:
                    self.heating = False
                    self.f_brownout = False
                    self.debug_print("Battery Heater off!")
                    self.RGB = (0, 0, 0)
            except Exception as e:
                self.debug_print(
                    "[ERROR] Cant turn off heater: "
                    + "".join(traceback.format_exception(e))
                )
                self.heater.duty_cycle = 0x0000
        else:
            self.debug_print("[WARNING] LED Driver not initialized")

    def burn(self, burn_num, dutycycle=0, freq=1000, duration=1):
        """
        Operate burn wire circuits. Wont do anything unless the a nichrome burn wire
        has been installed.

        IMPORTANT: See "Burn Wire Info & Usage" of https://pycubed.org/resources
        before attempting to use this function!

        burn_num:  (string) which burn wire circuit to operate, must be either '1' or '2'
        dutycycle: (float) duty cycle percent, must be 0.0 to 100
        freq:      (float) frequency in Hz of the PWM pulse, default is 1000 Hz
        duration:  (float) duration in seconds the burn wire should be on
        """
        try:
            # convert duty cycle % into 16-bit fractional up time
            dtycycl = int((dutycycle / 100) * (0xFFFF))
            self.debug_print("----- BURN WIRE CONFIGURATION -----")
            self.debug_print(
                "\tFrequency of: {}Hz\n\tDuty cycle of: {}% (int:{})\n\tDuration of {}sec".format(
                    freq, (100 * dtycycl / 0xFFFF), dtycycl, duration
                )
            )
            # create our PWM object for the respective pin
            # not active since duty_cycle is set to 0 (for now)
            if "1" in burn_num:
                burnwire = pwmio.PWMOut(board.BURN_ENABLE, frequency=freq, duty_cycle=0)
            else:
                return False
            # Configure the relay control pin & open relay
            self._relayA.drive_mode = digitalio.DriveMode.PUSH_PULL
            self._relayA.value = 1
            self.RGB = (255, 165, 0)
            # Pause to ensure relay is open
            time.sleep(0.5)
            # Set the duty cycle over 0%
            # This starts the burn!
            burnwire.duty_cycle = dtycycl
            time.sleep(duration)
            # Clean up
            self._relayA.value = 0
            burnwire.duty_cycle = 0
            self.RGB = (0, 0, 0)
            # burnwire.deinit()
            self._relayA.drive_mode = digitalio.DriveMode.OPEN_DRAIN
            return True
        except Exception as e:
            self.debug_print(
                "Error with Burn Wire: " + "".join(traceback.format_exception(e))
            )
            return False
        finally:
            self._relayA.value = 0
            burnwire.duty_cycle = 0
            self.RGB = (0, 0, 0)
            burnwire.deinit()
            self._relayA.drive_mode = digitalio.DriveMode.OPEN_DRAIN


print("Initializing Power Management Systems...")
cubesat = Satellite()
