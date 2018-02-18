"""
Smart Virtual Thermostat python plugin for Domoticz

Author: Logread,
        adapted from the Vera plugin by Antor, see:
            http://www.antor.fr/apps/smart-virtual-thermostat-eng-2/?lang=en
            https://github.com/AntorFr/SmartVT

Version:    0.0.1: alpha
            0.0.2: beta, with new connection object from Domoticz Python plugin framework
            0.0.3: bug fixes + added thermostat temp update even if in Off or Forced modes
            0.0.4: more logging parameters
            0.1.0: Use the standard urllib.request module to call the Domoticz API due to ongoing changes to
                    the Domoticz.Connection method
            0.1.1: Address strange behavior of python plugin framework where no more than a couple of devices can be
                    created and updated in one pass in the same function... so devices are created in "OnStart()" as
                    required but are only updated after all required devices are created
            0.2.0: First incremental update:
                    - Code cleanup for calls to Domoticz API
                    - Timeout for temperature sensors, based on Domoticz preferences setting
            0.2.1: Fixed possible TypeError in datetime.strptime method in embedded Python
                    (known python 3.x bug, see https://bugs.python.org/issue27400)
            0.3.0: Fixed major bug in auto-learning + cosmetic improvements
            0.3.1: Force immediate recalculation when setpoint changes
            0.3.2: fix bug where thermostat might still switch on despite no valid inside temperature
            0.3.3: various improvements:
                    - end of heating cycle code adjusted to avoid potentially damaging quick off/on to the heater(s)
                    - fix start up sequence to kill heating if mode is off in case heating was manually on beforehand
                    - ensure heating is killed if invalid temperature reading (v.s. just setting mode to off)
            0.3.4: immediate recalculation if mode changed (and skip learning since incomplete cycle).
                    Thanks to domoticz forum user @Electrocut
            0.3.5: fixed bug if calculated power is below minimum power
                    Thanks to domoticz forum user @napo7
            0.3.6: fixed bug causing auto learning to fail if no outside temp is provided
                    Thanks to domoticz forum user @etampes
            0.3.7: changed the logic for minimum power: apply minimum power even if no heating needed (useful for
                    very high thermal inertia systems like heating floors that work better if kept warm enough)
                    Thanks to domoticz forum user @napo7
            0.3.8: minor improvements to thermostat temperature update logic, some code cleanup
            0.4.0: Stability upgrade: Drop use of urllib.request that can cause Domoticz to crash in some
                    configurations. Plugin now again uses the Domoticz python built in http transport
"""
"""
<plugin key="SVT" name="Smart Virtual Thermostat" author="logread" version="0.4.0" wikilink="https://www.domoticz.com/wiki/Plugins/Smart_Virtual_Thermostat.html" externallink="https://github.com/999LV/SmartVirtualThermostat.git">
    <params>
        <param field="Address" label="Domoticz IP Address" width="200px" required="true" default="127.0.0.1"/>
        <param field="Port" label="Port" width="40px" required="true" default="8080"/>
        <param field="Mode1" label="Inside Temperature Sensors (csv list of idx)" width="100px" required="true" default="0"/>
        <param field="Mode2" label="Outside Temperature Sensors (csv list of idx)" width="100px" required="false" default=""/>
        <param field="Mode3" label="Heating Switches (csv list of idx)" width="100px" required="true" default="0"/>
        <param field="Mode5" label="Calculation cycle, Minimum Heating time per cycle, Pause On delay, Pause Off delay, Forced mode duration (all in minutes)" width="200px" required="true" default="30,0,2,1,60"/>
        <param field="Mode6" label="Logging Level" width="75px">
            <options>
                <option label="Debug" value="Debug"/>
                <option label="Verbose" value="Verbose"/>
                <option label="Normal" value="Normal"  default="true" />
            </options>
        </param>
    </params>
</plugin>
"""
import Domoticz
import json
from urllib import parse
from datetime import datetime, timedelta
import time
import collections


class deviceparam:

    def __init__(self, unit, nvalue, svalue):
        self.unit = unit
        self.nvalue = nvalue
        self.svalue = svalue


class BasePlugin:

    def __init__(self):
        self.debug = False
        self.calculate_period = 30  # Time in minutes between two calculations (cycle)
        self.minheatpower = 0  # if heating is needed, minimum heat power (in % of calculation period)
        self.deltamax = 0.2  # allowed temp excess over setpoint temperature
        self.pauseondelay = 2  # time between pause sensor actuation and actual pause
        self.pauseoffdelay = 1  # time between end of pause sensor actuation and end of actual pause
        self.forcedduration = 60  # time in minutes for the forced mode
        self.InTempSensors = []
        self.OutTempSensors = []
        self.Heaters = []
        self.InternalsDefaults = {
            'ConstC': 60,  # inside heating coeff, depends on room size & power of your heater (60 by default)
            'ConstT': 1,  # external heating coeff,depends on the insulation relative to the outside (1 by default)
            'nbCC': 0,  # number of learnings for ConstC
            'nbCT': 0,  # number of learnings for ConstT
            'LastPwr': 0,  # % power from last calculation
            'LastInT': 0,  # inside temperature at last calculation
            'LastOutT': 0,  # outside temprature at last calculation
            'LastSetPoint': 20,  # setpoint at time of last calculation
            'ALStatus': 0}  # AutoLearning status (0 = uninitialized, 1 = initialized, 2 = disabled)
        self.Internals = self.InternalsDefaults.copy()
        self.heat = False
        self.pause = False
        self.pauserequested = False
        self.pauserequestchangedtime = datetime.now()
        self.forced = False
        self.intemp = 20.0
        self.outtemp = 20.0
        self.setpoint = 20.0
        self.endheat = datetime.now()
        self.nextcalc = self.endheat
        self.lastcalc = self.endheat
        self.nextupdate = self.endheat
        self.nexttemps = self.endheat
        self.learn = True
        self.APICallsQueue = collections.deque()
        self.connectionrequested = False
        return


    def onStart(self):
        if Parameters["Mode6"] == 'Debug':
            self.debug = True
            Domoticz.Debugging(1)
            DumpConfigToLog()
        else:
            Domoticz.Debugging(0)

        self.APIConnection = Domoticz.Connection(
            Name="RegisterAPICall", Transport="TCP/IP", Protocol="HTTP",
            Address=Parameters["Address"], Port=Parameters["Port"])

        # create the child devices if these do not exist yet
        devicecreated = []
        if 1 not in Devices:
            Options = {"LevelActions": "||",
                       "LevelNames": "Off|Auto|Forced",
                       "LevelOffHidden": "false",
                       "SelectorStyle": "0"}
            Domoticz.Device(Name="Thermostat Control", Unit=1, TypeName="Selector Switch", Switchtype=18, Image=15,
                            Options=Options, Used=1).Create()
            #Devices[1].Update(nValue=0, sValue="0")  # default is Off state
            devicecreated.append(deviceparam(1, 0, "0"))  # default is Off state
        if 2 not in Devices:
            Options = {"LevelActions": "||",
                       "LevelNames": "Off|Normal|Economy",
                       "LevelOffHidden": "true",
                       "SelectorStyle": "0"}
            Domoticz.Device(Name="Thermostat Mode", Unit=2, TypeName="Selector Switch", Switchtype=18, Image=15,
                            Options=Options, Used=1).Create()
            devicecreated.append(deviceparam(2, 0, "10"))  # default is normal mode
        if 3 not in Devices:
            Domoticz.Device(Name="Thermostat Pause", Unit=3, TypeName="Switch", Image=9, Used=1).Create()
            devicecreated.append(deviceparam(3, 0, ""))  # default is Off
        if 4 not in Devices:
            Domoticz.Device(Name="Setpoint Normal", Unit=4, Type=242, Subtype=1, Used=1).Create()
            devicecreated.append(deviceparam(4, 0, "20"))  # default is 20 degrees
        if 5 not in Devices:
            Domoticz.Device(Name="Setpoint Economy", Unit=5, Type=242, Subtype=1, Used=1).Create()
            devicecreated.append(deviceparam(5 ,0, "20"))  # default is 20 degrees
        if 6 not in Devices:
            Domoticz.Device(Name="Thermostat temp", Unit=6, TypeName="Temperature").Create()
            devicecreated.append(deviceparam(6, 0, "20"))  # default is 20 degrees

        # if any device has been created in onStart(), now is time to update its defaults
        for device in devicecreated:
            Devices[device.unit].Update(nValue=device.nvalue, sValue=device.svalue)

        # build lists of sensors and switches
        self.InTempSensors = parseCSV(Parameters["Mode1"])
        Domoticz.Debug("Inside Temperature sensors = {}".format(self.InTempSensors))
        self.OutTempSensors = parseCSV(Parameters["Mode2"])
        Domoticz.Debug("Outside Temperature sensors = {}".format(self.OutTempSensors))
        self.Heaters = parseCSV(Parameters["Mode3"])
        Domoticz.Debug("Heaters = {}".format(self.Heaters))

        # splits additional parameters
        params = parseCSV(Parameters["Mode5"])
        if len(params) == 5:
            self.calculate_period = params[0]
            if self.calculate_period < 5:
                Domoticz.Error("Invalid calculation period parameter. Using minimum of 5 minutes !")
                self.calculate_period = 5
            self.minheatpower = params[1]
            if self.minheatpower > 100:
                Domoticz.Error("Invalid minimum heating parameter. Using maximum of 100% !")
                self.minheatpower = 100
            self.pauseondelay = params[2]
            self.pauseoffdelay = params[3]
            self.forcedduration = params[4]
            if self.forcedduration < 30:
                Domoticz.Error("Invalid forced mode duration parameter. Using minimum of 30 minutes !")
                self.calculate_period = 30
        else:
            Domoticz.Error("Error reading Mode5 parameters")

        # loads persistent variables from dedicated user variable
        # note: to reset the thermostat to default values (i.e. ignore all past learning),
        # just delete the relevant "<plugin name>-InternalVariables" user variable Domoticz GUI and restart plugin
        self.getUserVar()

        # if mode = off then make sure actual heating is off just in case if was manually set to on
        if Devices[1].sValue == "0":
            self.switchHeat(False)


    def onStop(self):
        Domoticz.Debugging(0)


    def onConnect(self, Connection, Status, Description):
        Domoticz.Debug("onConnect called")
        if Status == 0:
            self.ProcessAPICalls()
            self.connectionrequested = False
        else:
            Domoticz.Error("Failed to connect ({}) to: {}: with error: ".format(
                Status, Parameters["Address"], Parameters["Port"], Description))


    def onDisconnect(self, Connection):
        Domoticz.Debug("onDisconnect called for connection to: " + Connection.Address + ":" + Connection.Port)


    def onMessage(self, Connection, Data):
        Domoticz.Debug("onMessage called")
        strData = Data["Data"].decode("utf-8", "ignore")
        Status = int(Data["Status"])
        Domoticz.Debug("HTTP Status = {}".format(Status))
        if Status == 200:
            Response = json.loads(strData)
            Domoticz.Debug("Received Domoticz API response for {}".format(Response["title"]))
            if Response["status"] == "OK" and "title" in Response:
                if Response["title"] == "Devices": # we process a request to poll the temperature sensors
                    self.ProcessTemps(Response)
                    if Devices[1].sValue == "10": # we are in auto mode, so do the thermostat work
                        self.AutoMode()
                elif Response["title"] == "SwitchLight": # we turned on or off a switch...
                    pass
                elif Response["title"] == "GetUserVariables":
                    self.getUserVar(Response)
                elif Response["title"] == "SaveUserVariable":
                    WriteLog(
                        "User Variable {}-InternalVariables created".format(Parameters["Name"]))
                elif Response["title"] == "UpdateUserVariable":
                    WriteLog(
                        "User Variable {}-InternalVariables updated".format(Parameters["Name"]), "Verbose")
                else:
                    Domoticz.Error("Unknown Domoticz API response {}".format(Response["title"]))
            else:
                Domoticz.Error("Domoticz API returned an error")
        else:
            Domoticz.Error("Domoticz HTTP connection error ".format(Status))
        return True


    def onCommand(self, Unit, Command, Level, Hue):
        Domoticz.Debug("onCommand called for Unit {}: Command '{}', Level: {}".format(Unit, Command, Level))
        if Unit == 3:
            self.pauserequestchangedtime = datetime.now()
            svalue = ""
            if str(Command) == "On":
                nvalue = 1
                self.pauserequested = True
            else:
                nvalue = 0
                self.pauserequested = False
        else:
            if Level > 0:
                nvalue = 1
            else:
                nvalue = 0
            svalue = str(Level)
        Devices[Unit].Update(nValue=nvalue, sValue=svalue)
        if Unit in (1, 2, 4, 5): # force recalculation if control or mode or a setpoint changed
            self.nextcalc = datetime.now()
            self.learn = False
            self.onHeartbeat()


    def onHeartbeat(self):
        now = datetime.now()

        # process any pending API calls
        if len(self.APICallsQueue) > 0:
            if self.APIConnection.Connected():
                self.ProcessAPICalls()
            elif not self.connectionrequested:
                self.APIConnection.Connect()
                self.connectionrequested = True

        if Devices[1].sValue == "0":  # Thermostat is off
            if self.forced or self.heat:  # thermostat setting was just changed so we kill the heating
                self.forced = False
                self.endheat = now
                Domoticz.Debug("Switching heat Off !")
                self.switchHeat(False)

            if self.nexttemps <= now:
                # call the Domoticz json API for a temperature devices update, to get the lastest temps...
                self.readTemps()

        elif Devices[1].sValue == "20":  # Thermostat is in forced mode
            if self.forced:
                if self.endheat <= now:
                    self.forced = False
                    self.endheat = now
                    Domoticz.Debug("Forced mode Off !")
                    Devices[1].Update(nValue=1, sValue="10")  # set thermostat to normal mode
                    self.switchHeat(False)
            else:
                self.forced = True
                self.endheat = now + timedelta(minutes=self.forcedduration)
                Domoticz.Debug("Forced mode On !")
                self.switchHeat(True)

            if self.nexttemps <= now:
                # call the Domoticz json API for a temperature devices update, to get the lastest temps...
                self.readTemps()

        else:  # Thermostat is in mode auto

            if self.forced:  # thermostat setting was just changed from "forced" so we kill the forced mode
                self.forced = False
                self.endheat = now
                self.nextcalc = now   # this will force a recalculation on next heartbeat
                Domoticz.Debug("Forced mode Off !")
                self.switchHeat(False)

            elif (self.endheat <= now or self.pause) and self.heat:  # heat cycle is over
                self.endheat = now
                self.heat = False
                if self.Internals['LastPwr'] < 100:
                    self.switchHeat(False)
                # if power was 100(i.e. a full cycle), then we let the next calculation (at next heartbeat) decide
                # to switch off in order to avoid potentially damaging quick off/on cycles to the heater(s)

            elif self.pause and not self.pauserequested:  # we are in pause and the pause switch is now off
                if self.pauserequestchangedtime + timedelta(minutes=self.pauseoffdelay) <= now:
                    Domoticz.Debug("Pause is now Off")
                    self.pause = False

            elif not self.pause and self.pauserequested:  # we are not in pause and the pause switch is now on
                if self.pauserequestchangedtime + timedelta(minutes=self.pauseondelay) <= now:
                    Domoticz.Debug("Pause is now On")
                    self.pause = True
                    self.switchHeat(False)

            elif self.pause and self.nexttemps <= now:  # added to update thermostat temp even in pause mode
                # call the Domoticz json API for a temperature devices update, to get the lastest temps...
                self.readTemps()

            elif (self.nextcalc <= now) and not self.pause:  # we start a new calculation
                self.nextcalc = now + timedelta(minutes=self.calculate_period)
                Domoticz.Debug("Next calculation time will be : " + str(self.nextcalc))

                # make current setpoint used in calculation reflect the select mode (10= normal, 20 = economy)
                if Devices[2].sValue == "10":
                    self.setpoint = float(Devices[4].sValue)
                else:
                    self.setpoint = float(Devices[5].sValue)

                # call the Domoticz json API for a temperature devices update, to get the lastest temps...
                self.readTemps()

        # check if need to refresh setpoints so that they do not turn red in GUI
        if self.nextupdate <= now:
            self.nextupdate = now + timedelta(minutes=int(Settings["SensorTimeout"]))
            Devices[4].Update(nValue=0, sValue=Devices[4].sValue)
            Devices[5].Update(nValue=0, sValue=Devices[5].sValue)


    def AutoMode(self):
        if self.intemp > self.setpoint + self.deltamax:
            Domoticz.Debug("Temperature exceeds setpoint: no heating")
            power = 0
        else:
            if self.learn:
                self.AutoCallib()
            else:
                self.learn = True
            if self.outtemp is None:
                power = round((self.setpoint - self.intemp) * self.Internals["ConstC"], 1)
            else:
                power = round((self.setpoint - self.intemp) * self.Internals["ConstC"] +
                              (self.setpoint - self.outtemp) * self.Internals["ConstT"], 1)

        if power < 0:
            power = 0  # lower limit
        elif power > 100:
            power = 100  # upper limit
        if power <= self.minheatpower:
            power = self.minheatpower  # minimum heating per cycle in % of cycle time

        heatduration = round(power * self.calculate_period / 100)
        WriteLog("Calculation: Power = {} -> heat duration = {} minutes".format(power, heatduration), "Verbose")

        if power == 0:
            self.switchHeat(False)
            Domoticz.Debug("No heating required !")
        else:
            self.endheat = datetime.now() + timedelta(minutes=heatduration)
            Domoticz.Debug("End Heat time = " + str(self.endheat))
            self.switchHeat(True)
            if self.Internals["ALStatus"] < 2:
                self.Internals['LastPwr'] = power
                self.Internals['LastInT'] = self.intemp
                self.Internals['LastOutT'] = self.outtemp
                self.Internals['LastSetPoint'] = self.setpoint
                self.Internals['ALStatus'] = 1
                self.saveUserVar()  # update user variables with latest learning

        self.lastcalc = datetime.now()


    def AutoCallib(self):
        now = datetime.now()
        if self.Internals['ALStatus'] != 1:  # not initalized... do nothing
            Domoticz.Debug("Fist pass at AutoCallib... no callibration")
            pass
        elif self.Internals['LastPwr'] == 0:  # heater was off last time, do nothing
            Domoticz.Debug("Last power was zero... no callibration")
            pass
        elif self.Internals['LastPwr'] == 100 and self.intemp < self.Internals['LastSetPoint']:
            # heater was on max but setpoint was not reached... no learning
            Domoticz.Debug("Last power was 100% but setpoint not reached... no callibration")
            pass
        elif self.intemp > self.Internals['LastInT'] and self.Internals['LastSetPoint'] > self.Internals['LastInT']:
            # learning ConstC
            ConstC = (self.Internals['ConstC'] * ((self.Internals['LastSetPoint'] - self.Internals['LastInT']) /
                                                  (self.intemp - self.Internals['LastInT']) *
                                                  (timedelta.total_seconds(now - self.lastcalc) /
                                                   (self.calculate_period * 60))))
            WriteLog("New calc for ConstC = {}".format(ConstC), "Verbose")
            self.Internals['ConstC'] = round((self.Internals['ConstC'] * self.Internals['nbCC'] + ConstC) /
                                             (self.Internals['nbCC'] + 1), 1)
            self.Internals['nbCC'] = min(self.Internals['nbCC'] + 1, 50)
            WriteLog("ConstC updated to {}".format(self.Internals['ConstC']), "Verbose")
        elif self.outtemp is not None and self.Internals['LastSetPoint'] > self.Internals['LastOutT']:
            # learning ConstT
            ConstT = (self.Internals['ConstT'] + ((self.Internals['LastSetPoint'] - self.intemp) /
                                                  (self.Internals['LastSetPoint'] - self.Internals['LastOutT']) *
                                                  self.Internals['ConstC'] *
                                                  (timedelta.total_seconds(now - self.lastcalc) /
                                                   (self.calculate_period * 60))))
            WriteLog("New calc for ConstT = {}".format(ConstT), "Verbose")
            self.Internals['ConstT'] = round((self.Internals['ConstT'] * self.Internals['nbCT'] + ConstT) /
                                             (self.Internals['nbCT'] + 1), 1)
            self.Internals['nbCT'] = min(self.Internals['nbCT'] + 1, 50)
            WriteLog("ConstT updated to {}".format(self.Internals['ConstT']), "Verbose")


    def switchHeat(self, switch):
        if switch:  # heating on
            self.heat = True
            Domoticz.Debug("Heating On")
            # switch on heater(s)
            for heater in self.Heaters:
                self.RegisterAPICall("/json.htm?type=command&param=switchlight&idx={}&switchcmd=On".format(heater))
            Domoticz.Debug("End Heat time = " + str(self.endheat))
        else:
            self.heat = False
            Domoticz.Debug("Heating Off")
            # switch off heater(s)
            for heater in self.Heaters:
                self.RegisterAPICall("/json.htm?type=command&param=switchlight&idx={}&switchcmd=Off".format(heater))


    def readTemps(self):
        # set update flag for next temp update (used only when in off, forced mode or pause is active)
        self.nexttemps = datetime.now() + timedelta(minutes=self.calculate_period)
        self.RegisterAPICall("/json.htm?type=devices&filter=temp&used=true&order=Name")


    def ProcessTemps(self, devicesAPI):
        # fetch all the devices from the API and scan for sensors
        listintemps = []
        listouttemps = []
        for device in devicesAPI["result"]:  # parse the devices for temperature sensors
            idx = int(device["idx"])
            Domoticz.Debug("Processing Device idx {}".format(idx))
            if idx in self.InTempSensors:
                if "Temp" in device:
                    Domoticz.Debug("device: {}-{} = {}".format(device["idx"], device["Name"], device["Temp"]))
                    # check temp sensor is not timed out
                    if not SensorTimedOut(device["LastUpdate"]):
                        listintemps.append(device["Temp"])
                    else:
                        Domoticz.Error("skipping timed out temperature sensor {}".format(device["Name"]))
                else:
                    Domoticz.Error("device: {}-{} is not a Temperature sensor".format(device["idx"], device["Name"]))
            elif idx in self.OutTempSensors:
                if "Temp" in device:
                    Domoticz.Debug("device: {}-{} = {}".format(device["idx"], device["Name"], device["Temp"]))
                    # check temp sensor is not timed out
                    if not SensorTimedOut(device["LastUpdate"]):
                        listouttemps.append(device["Temp"])
                    else:
                        Domoticz.Error("skipping timed out temperature sensor {}".format(device["Name"]))
                else:
                    Domoticz.Error("device: {}-{} is not a Temperature sensor".format(device["idx"], device["Name"]))

        # calculate the average inside temperature
        nbtemps = len(listintemps)
        if nbtemps > 0:
            self.intemp = round(sum(listintemps) / nbtemps, 1)
            Devices[6].Update(nValue=0,
                              sValue=str(self.intemp))  # update the dummy device showing the current thermostat temp
        else:
            Domoticz.Error("No Inside Temperature found... Switching Thermostat Off")
            Devices[1].Update(nValue=0, sValue="0")  # switch off the thermostat

        # calculate the average outside temperature
        nbtemps = len(listouttemps)
        if nbtemps > 0:
            self.outtemp = round(sum(listouttemps) / nbtemps, 1)
        else:
            Domoticz.Debug("No Outside Temperature found...")
            self.outtemp = None

        WriteLog("Inside Temperature = {}".format(self.intemp), "Verbose")
        WriteLog("Outside Temperature = {}".format(self.outtemp), "Verbose")


    def getUserVar(self, variablesAPI=False):
        if not variablesAPI:  # we initiate a connection
            self.RegisterAPICall("/json.htm?type=command&param=getuservariables")
        else:  # we respond to a connection !
            novar = True
            varname = Parameters["Name"] + "-InternalVariables"
            valuestring = ""
            if "result" in variablesAPI:
                for variable in variablesAPI["result"]:
                    if variable["Name"] == varname:
                        valuestring = variable["Value"]
                        novar = False
                        break
            if novar:
                # create user variable since it does not exist
                WriteLog("User Variable {} does not exist. Creation requested".format(varname), "Verbose")
                self.RegisterAPICall("/json.htm?type=command&param=saveuservariable&vname={}&vtype=2&vvalue={}".format(
                        varname, str(self.InternalsDefaults)))
                self.Internals = self.InternalsDefaults.copy()  # we re-initialize the internal variables
            else:
                try:
                    self.Internals.update(eval(valuestring))
                except:
                    self.Internals = self.InternalsDefaults.copy()
                    Domoticz.Error("Error parsing uservariable, Using default parameters")


    def saveUserVar(self):
        varname = Parameters["Name"] + "-InternalVariables"
        self.RegisterAPICall("/json.htm?type=command&param=updateuservariable&vname={}&vtype=2&vvalue={}".format(
            varname, str(self.Internals)))


    def RegisterAPICall(self, APICall):
        if len(self.APICallsQueue) < 10:
            self.APICallsQueue.append(parse.quote(APICall, safe="/&=?"))
            Domoticz.Debug("Registering API Call -> {}".format(APICall))
            Domoticz.Debug("Connected = {}, Connecting = {}".format(
                self.APIConnection.Connected(), self.APIConnection.Connecting()))
            if self.APIConnection.Connected():
                self.ProcessAPICalls()
            elif not self.connectionrequested:
                self.APIConnection.Connect()
                self.connectionrequested = True
        else:
            Domoticz.Error("API Calls queue full ! API Call dropped. This may cause SVT to misbehave")

    def ProcessAPICalls(self):
        Domoticz.Debug("API Calls queue is {} element(s)".format(len(self.APICallsQueue)))
        while len(self.APICallsQueue) > 0:
            APICall = self.APICallsQueue[0]
            if self.APIConnection.Connected():
                Domoticz.Debug("Processing API request = {}".format(APICall))
                data = ''
                headers = {'Content-Type': 'text/xml; charset=utf-8',
                           'Connection': 'keep-alive',
                           'Accept': 'Content-Type: text/html; charset=UTF-8',
                           'Host': Parameters["Address"] + ":" + Parameters["Port"],
                           'User-Agent': 'SVT/1.0',
                           'Content-Length': "%d" % (len(data))}
                self.APIConnection.Send({"Verb": "GET", "URL": APICall, "Headers": headers})
                self.APICallsQueue.popleft()
            else:
                Domoticz.Debug("Unable to establish http transport with Domoticz: API command not processed !")


global _plugin
_plugin = BasePlugin()


def onStart():
    global _plugin
    _plugin.onStart()


def onStop():
    global _plugin
    _plugin.onStop()


def onConnect(Connection, Status, Description):
    global _plugin
    _plugin.onConnect(Connection, Status, Description)


def onDisconnect(Connection):
    global _plugin
    _plugin.onDisconnect(Connection)


def onMessage(Connection, Data):
    global _plugin
    _plugin.onMessage(Connection, Data)


def onCommand(Unit, Command, Level, Hue):
    global _plugin
    _plugin.onCommand(Unit, Command, Level, Hue)


def onHeartbeat():
    global _plugin
    _plugin.onHeartbeat()


# Plugin utility functions ---------------------------------------------------

def SensorTimedOut(datestring):
    def LastUpdate(datestring):
        dateformat = "%Y-%m-%d %H:%M:%S"
        # the below try/except is meant to address an intermittent python bug in some embedded systems
        try:
            result = datetime.strptime(datestring, dateformat)
        except TypeError:
            result = datetime(*(time.strptime(datestring, dateformat)[0:6]))
        return result
    return LastUpdate(datestring) + timedelta(minutes=int(Settings["SensorTimeout"])) < datetime.now()

def parseCSV(strCSV):
    listvals = []
    for value in strCSV.split(","):
        try:
            val = int(value)
        except:
            pass
        else:
            listvals.append(val)
    return listvals


def WriteLog(message, level="Normal"):
    if Parameters["Mode6"] == "Verbose" or Parameters["Mode6"] == "Debug":
        Domoticz.Log(message)
    elif level == "Normal":
        Domoticz.Log(message)


# Generic helper functions
def DumpConfigToLog():
    for x in Parameters:
        if Parameters[x] != "":
            Domoticz.Debug("'" + x + "':'" + str(Parameters[x]) + "'")
    Domoticz.Debug("Device count: " + str(len(Devices)))
    for x in Devices:
        Domoticz.Debug("Device:           " + str(x) + " - " + str(Devices[x]))
        Domoticz.Debug("Device ID:       '" + str(Devices[x].ID) + "'")
        Domoticz.Debug("Device Name:     '" + Devices[x].Name + "'")
        Domoticz.Debug("Device nValue:    " + str(Devices[x].nValue))
        Domoticz.Debug("Device sValue:   '" + Devices[x].sValue + "'")
        Domoticz.Debug("Device LastLevel: " + str(Devices[x].LastLevel))
    return
