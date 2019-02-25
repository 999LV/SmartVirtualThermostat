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
            0.3.9: GitHub contribution from @SoaR245: add support for domoticz webserver authentication if enabled
            0.4.0: unpublished version (test of Domoticz.Connection method vs. urllib.request)
            0.4.1: Cumulative update:
                   (1) add option to force or not minimum heating per cycle even if target temperature
                       is already reached (thanks to domoticz forum user @jake)
                   (2) check heater(s) switch status before an update to avoid issuing uncessary switching commands
                       (thanks to domoticz forum user @jake)
                   (3) some minor error checks added when parsing parameters at startup
                   (4) implement new debugging levels introduced by @dnpwwo
                   (5) some code cleanup and optimization
            0.4.2: Unpublished test
            0.4.3: Call domoticz json API at least every 5 minutes to avoid the 10 mins timout of connections that
                    floods domoticz's log with "Incoming Connection from ...." messages. Take advantage of this to
                    update the temperature readings more often than upon each calculation
            0.4.4: implement list of active/dead sensors to avoid continuous logging of dead sensors,
            		also added some ´fool proof´ error handlers based on users feedback
            0.4.5: adjust uservariable creation to breaking change introduced by domoticz version 4.10298
                    thanks to GitHub contributor @informagico
            0.4.6: some clean up of logging levels (accept older versions of domoticz) and variables scoping/typing
            0.4.7: slight adjustement to verbose logging to reduce clutter
            0.4.8: change behavior if no valid inside temperature reading: switch heating off instead of switching
                    the thermostat off... then if and when there is a valid temperature the thermostat resumes work
                    Thanks to GitHub user @Kptn77 for suggestion.
"""
"""
<plugin key="SVT" name="Smart Virtual Thermostat" author="logread" version="0.4.7" wikilink="https://www.domoticz.com/wiki/Plugins/Smart_Virtual_Thermostat.html" externallink="https://github.com/999LV/SmartVirtualThermostat.git">
    <description>
        <h2>Smart Virtual Thermostat</h2><br/>
        Easily implement in Domoticz an advanced virtual thermostat based on time modulation<br/>
        and self learning of relevant room thermal characteristics (including insulation level)<br/>
        rather then more conventional hysteresis methods, so as to achieve a greater comfort.<br/>
        It is a port to Domoticz of the original Vera plugin from Antor.<br/>
        <h3>Set-up and Configuration</h3>
        See domoticz wiki above.<br/> 
    </description>
    <params>
        <param field="Address" label="Domoticz IP Address" width="200px" required="true" default="localhost"/>
        <param field="Port" label="Port" width="40px" required="true" default="8080"/>
        <param field="Username" label="Username" width="200px" required="false" default=""/>
        <param field="Password" label="Password" width="200px" required="false" default=""/>
        <param field="Mode1" label="Inside Temperature Sensors (csv list of idx)" width="100px" required="true" default="0"/>
        <param field="Mode2" label="Outside Temperature Sensors (csv list of idx)" width="100px" required="false" default=""/>
        <param field="Mode3" label="Heating Switches (csv list of idx)" width="100px" required="true" default="0"/>
        <param field="Mode4" label="Apply minimum heating per cycle" width="200px">
            <options>
				<option label="ony when heating required" value="Normal"  default="true" />
                <option label="always" value="Forced"/>
            </options>
        </param> 
        <param field="Mode5" label="Calculation cycle, Minimum Heating time per cycle, Pause On delay, Pause Off delay, Forced mode duration (all in minutes)" width="200px" required="true" default="30,0,2,1,60"/>
        <param field="Mode6" label="Logging Level" width="200px">
            <options>
                <option label="Normal" value="Normal"  default="true"/>
                <option label="Verbose" value="Verbose"/>
                <option label="Debug - Python Only" value="2"/>
                <option label="Debug - Basic" value="62"/>
                <option label="Debug - Basic+Messages" value="126"/>
                <option label="Debug - Connections Only" value="16"/>
                <option label="Debug - Connections+Queue" value="144"/>
                <option label="Debug - All" value="-1"/>
            </options>
        </param>
    </params>
</plugin>
"""
import Domoticz
import json
import urllib.parse as parse
import urllib.request as request
from datetime import datetime, timedelta
import time
import base64
import itertools
from distutils.version import LooseVersion

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
        self.ActiveSensors = {}
        self.InTempSensors = []
        self.OutTempSensors = []
        self.Heaters = []
        self.InternalsDefaults = {
            'ConstC': float(60),  # inside heating coeff, depends on room size & power of your heater (60 by default)
            'ConstT': float(1),  # external heating coeff,depends on the insulation relative to the outside (1 by default)
            'nbCC': 0,  # number of learnings for ConstC
            'nbCT': 0,  # number of learnings for ConstT
            'LastPwr': 0,  # % power from last calculation
            'LastInT': float(0),  # inside temperature at last calculation
            'LastOutT': float(0),  # outside temprature at last calculation
            'LastSetPoint': float(20),  # setpoint at time of last calculation
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
        self.loglevel = None
        self.statussupported = True
        self.intemperror = False
        return


    def onStart(self):

        # setup the appropriate logging level
        try:
            debuglevel = int(Parameters["Mode6"])
        except ValueError:
            debuglevel = 0
            self.loglevel = Parameters["Mode6"]
        if debuglevel != 0:
            self.debug = True
            Domoticz.Debugging(debuglevel)
            DumpConfigToLog()
            self.loglevel = "Verbose"
        else:
            self.debug = False
            Domoticz.Debugging(0)

        # check if the host domoticz version supports the Domoticz.Status() python framework function
        try:
            Domoticz.Status("This version of domoticz allows status logging by the plugin (in verbose mode)")
        except Exception:
            self.statussupported = False

        # create the child devices if these do not exist yet
        devicecreated = []
        if 1 not in Devices:
            Options = {"LevelActions": "||",
                       "LevelNames": "Off|Auto|Forced",
                       "LevelOffHidden": "false",
                       "SelectorStyle": "0"}
            Domoticz.Device(Name="Thermostat Control", Unit=1, TypeName="Selector Switch", Switchtype=18, Image=15,
                            Options=Options, Used=1).Create()
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
        self.WriteLog("Inside Temperature sensors = {}".format(self.InTempSensors), "Verbose")
        self.OutTempSensors = parseCSV(Parameters["Mode2"])
        self.WriteLog("Outside Temperature sensors = {}".format(self.OutTempSensors), "Verbose")
        self.Heaters = parseCSV(Parameters["Mode3"])
        self.WriteLog("Heaters = {}".format(self.Heaters), "Verbose")
        
        # build dict of status of all temp sensors to be used when handling timeouts
        for sensor in itertools.chain(self.InTempSensors, self.OutTempSensors):
            self.ActiveSensors[sensor] = True

        # splits additional parameters
        params = parseCSV(Parameters["Mode5"])
        if len(params) == 5:
            self.calculate_period = CheckParam("Calculation Period", params[0], 30)
            if self.calculate_period < 5:
                Domoticz.Error("Invalid calculation period parameter. Using minimum of 5 minutes !")
                self.calculate_period = 5
            self.minheatpower = CheckParam("Minimum Heating (%)", params[1], 0)
            if self.minheatpower > 100:
                Domoticz.Error("Invalid minimum heating parameter. Using maximum of 100% !")
                self.minheatpower = 100
            self.pauseondelay = CheckParam("Pause On Delay", params[2], 2)
            self.pauseoffdelay = CheckParam("Pause Off Delay", params[3], 0)
            self.forcedduration = CheckParam("Forced Mode Duration", params[4], 60)
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


    def onCommand(self, Unit, Command, Level, Color):

        Domoticz.Debug("onCommand called for Unit {}: Command '{}', Level: {}".format(Unit, Command, Level))

        if Unit == 3:  # pause switch
            self.pauserequestchangedtime = datetime.now()
            svalue = ""
            if str(Command) == "On":
                nvalue = 1
                self.pauserequested = True
            else:
                nvalue = 0
                self.pauserequested = False
        else:
            nvalue = 1 if Level > 0 else 0
            svalue = str(Level)

        Devices[Unit].Update(nValue=nvalue, sValue=svalue)

        if Unit in (1, 2, 4, 5): # force recalculation if control or mode or a setpoint changed
            self.nextcalc = datetime.now()
            self.learn = False
            self.onHeartbeat()


    def onHeartbeat(self):

        now = datetime.now()
        
        # fool proof checking.... based on users feedback
        if not all(device in Devices for device in (1,2,3,4,5,6)):
            Domoticz.Error("one or more devices required by the plugin is/are missing, please check domoticz device creation settings and restart !")
            return

        if Devices[1].sValue == "0":  # Thermostat is off
            if self.forced or self.heat:  # thermostat setting was just changed so we kill the heating
                self.forced = False
                self.endheat = now
                Domoticz.Debug("Switching heat Off !")
                self.switchHeat(False)

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
                    self.WriteLog("Pause is now Off", "Status")
                    self.pause = False

            elif not self.pause and self.pauserequested:  # we are not in pause and the pause switch is now on
                if self.pauserequestchangedtime + timedelta(minutes=self.pauseondelay) <= now:
                    self.WriteLog("Pause is now On", "Status")
                    self.pause = True
                    self.switchHeat(False)

            elif (self.nextcalc <= now) and not self.pause:  # we start a new calculation
                self.nextcalc = now + timedelta(minutes=self.calculate_period)
                self.WriteLog("Next calculation time will be : " + str(self.nextcalc), "Verbose")

                # make current setpoint used in calculation reflect the select mode (10= normal, 20 = economy)
                if Devices[2].sValue == "10":
                    self.setpoint = float(Devices[4].sValue)
                else:
                    self.setpoint = float(Devices[5].sValue)

                # call the Domoticz json API for a temperature devices update, to get the lastest temps...
                if self.readTemps():
                    # do the thermostat work
                    self.AutoMode()
                else:
                    # make sure we switch off heating if there was an error with reading the temp
                    self.switchHeat(False)

        if self.nexttemps <= now:
            # call the Domoticz json API for a temperature devices update, to get the lastest temps (and avoid the
            # connection time out time after 10mins that floods domoticz logs in versions of domoticz since spring 2018)
            self.readTemps()

        # check if need to refresh setpoints so that they do not turn red in GUI
        if self.nextupdate <= now:
            self.nextupdate = now + timedelta(minutes=int(Settings["SensorTimeout"]))
            Devices[4].Update(nValue=0, sValue=Devices[4].sValue)
            Devices[5].Update(nValue=0, sValue=Devices[5].sValue)


    def AutoMode(self):

        self.WriteLog("Temperatures: Inside = {} / Outside = {}".format(self.intemp, self.outtemp), "Verbose")

        if self.intemp > self.setpoint + self.deltamax:
            self.WriteLog("Temperature exceeds setpoint", "Verbose")
            overshoot = True
            power = 0
        else:
            overshoot = False
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

        # apply minimum power as required
        if power <= self.minheatpower and (Parameters["Mode4"] == "Forced" or not overshoot):
            self.WriteLog(
                "Calculated power is {}, applying minimum power of {}".format(power, self.minheatpower), "Verbose")
            power = self.minheatpower

        heatduration = round(power * self.calculate_period / 100)
        self.WriteLog("Calculation: Power = {} -> heat duration = {} minutes".format(power, heatduration), "Verbose")

        if power == 0:
            self.switchHeat(False)
            Domoticz.Debug("No heating requested !")
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
            self.WriteLog("New calc for ConstC = {}".format(ConstC), "Verbose")
            self.Internals['ConstC'] = round((self.Internals['ConstC'] * self.Internals['nbCC'] + ConstC) /
                                             (self.Internals['nbCC'] + 1), 1)
            self.Internals['nbCC'] = min(self.Internals['nbCC'] + 1, 50)
            self.WriteLog("ConstC updated to {}".format(self.Internals['ConstC']), "Verbose")
        elif self.outtemp is not None and self.Internals['LastSetPoint'] > self.Internals['LastOutT']:
            # learning ConstT
            ConstT = (self.Internals['ConstT'] + ((self.Internals['LastSetPoint'] - self.intemp) /
                                                  (self.Internals['LastSetPoint'] - self.Internals['LastOutT']) *
                                                  self.Internals['ConstC'] *
                                                  (timedelta.total_seconds(now - self.lastcalc) /
                                                   (self.calculate_period * 60))))
            self.WriteLog("New calc for ConstT = {}".format(ConstT), "Verbose")
            self.Internals['ConstT'] = round((self.Internals['ConstT'] * self.Internals['nbCT'] + ConstT) /
                                             (self.Internals['nbCT'] + 1), 1)
            self.Internals['nbCT'] = min(self.Internals['nbCT'] + 1, 50)
            self.WriteLog("ConstT updated to {}".format(self.Internals['ConstT']), "Verbose")


    def switchHeat(self, switch):

        # Build list of heater switches, with their current status,
        # to be used to check if any of the heaters is already in desired state
        switches = {}
        devicesAPI = DomoticzAPI("type=devices&filter=light&used=true&order=Name")
        if devicesAPI:
            for device in devicesAPI["result"]:  # parse the switch device
                idx = int(device["idx"])
                if idx in self.Heaters:  # this switch is one of our heaters
                    if "Status" in device:
                        switches[idx] = True if device["Status"] == "On" else False
                        Domoticz.Debug("Heater switch {} currently is '{}'".format(idx, device["Status"]))
                    else:
                        Domoticz.Error("Device with idx={} does not seem to be a switch !".format(idx))

        # fool proof checking.... based on users feedback
        if len(switches) == 0:
            Domoticz.Error("none of the devices in the 'heaters' parameter is a switch... no action !")
            return

        # flip on / off as needed
        self.heat = switch
        command = "On" if switch else "Off"
        Domoticz.Debug("Heating '{}'".format(command))
        for idx in self.Heaters:
            if switches[idx] != switch:  # check if action needed
                DomoticzAPI("type=command&param=switchlight&idx={}&switchcmd={}".format(idx, command))
        if switch:
            Domoticz.Debug("End Heat time = " + str(self.endheat))


    def readTemps(self):

        # set update flag for next temp update
        self.nexttemps = datetime.now() + timedelta(minutes=5)

        # fetch all the devices from the API and scan for sensors
        noerror = True
        listintemps = []
        listouttemps = []
        devicesAPI = DomoticzAPI("type=devices&filter=temp&used=true&order=Name")
        if devicesAPI:
            for device in devicesAPI["result"]:  # parse the devices for temperature sensors
                idx = int(device["idx"])
                if idx in self.InTempSensors:
                    if "Temp" in device:
                        Domoticz.Debug("device: {}-{} = {}".format(device["idx"], device["Name"], device["Temp"]))
                        # check temp sensor is not timed out
                        if not self.SensorTimedOut(idx, device["Name"], device["LastUpdate"]):
                            listintemps.append(device["Temp"])
                    else:
                        Domoticz.Error("device: {}-{} is not a Temperature sensor".format(device["idx"], device["Name"]))
                elif idx in self.OutTempSensors:
                    if "Temp" in device:
                        Domoticz.Debug("device: {}-{} = {}".format(device["idx"], device["Name"], device["Temp"]))
                        # check temp sensor is not timed out
                        if not self.SensorTimedOut(idx, device["Name"], device["LastUpdate"]):
                            listouttemps.append(device["Temp"])
                    else:
                        Domoticz.Error("device: {}-{} is not a Temperature sensor".format(device["idx"], device["Name"]))

        # calculate the average inside temperature
        nbtemps = len(listintemps)
        if nbtemps > 0:
            self.intemp = round(sum(listintemps) / nbtemps, 1)
            # update the dummy device showing the current thermostat temp
            Devices[6].Update(nValue=0, sValue=str(self.intemp), TimeOut=False)
            if self.intemperror:  # there was previously an invalid inside temperature reading... reset to normal
                self.intemperror = False
                self.WriteLog("Inside Temperature reading is now valid again: Resuming normal operation", "Status")
                Domoticz.Error("No Inside Temperature found... Switching heating Off")
                # we remove the timedout flag on the thermostat switch
                Devices[1].Update(nValue=Devices[1].nValue, sValue=Devices[1].sValue, TimedOut=False)
        else:
            # no valid inside temperature
            noerror = False
            if not self.intemperror:
                self.intemperror = True
                Domoticz.Error("No Inside Temperature found: Switching heating Off")
                self.switchHeat(False)
                # we mark both the thermostat switch and the thermostat temp devices as timedout
                Devices[1].Update(nValue=Devices[1].nValue, sValue=Devices[1].sValue, TimedOut=True)
                Devices[6].Update(nValue=Devices[6].nValue, sValue=Devices[6].sValue, TimedOut=True)

        # calculate the average outside temperature
        nbtemps = len(listouttemps)
        if nbtemps > 0:
            self.outtemp = round(sum(listouttemps) / nbtemps, 1)
        else:
            Domoticz.Debug("No Outside Temperature found...")
            self.outtemp = None

        Domoticz.Debug("Inside Temperature = {}".format(self.intemp))
        Domoticz.Debug("Outside Temperature = {}".format(self.outtemp))
        return noerror


    def getUserVar(self):

        variables = DomoticzAPI("type=command&param=getuservariables")
        if variables:
            # there is a valid response from the API but we do not know if our variable exists yet
            novar = True
            varname = Parameters["Name"] + "-InternalVariables"
            valuestring = ""
            if "result" in variables:
                for variable in variables["result"]:
                    if variable["Name"] == varname:
                        valuestring = variable["Value"]
                        novar = False
                        break
            if novar:
                # create user variable since it does not exist
                self.WriteLog("User Variable {} does not exist. Creation requested".format(varname), "Verbose")

                #check for Domoticz version:
                # there is a breaking change on dzvents_version 2.4.9, API was changed from 'saveuservariable' to 'adduservariable'
                # using 'saveuservariable' on latest versions returns a "status = 'ERR'" error

                # get a status of the actual running Domoticz instance, set the parameter accordigly
                parameter = "saveuservariable"
                domoticzInfo = DomoticzAPI("type=command&param=getversion")
                if domoticzInfo is None:
                    Domoticz.Error("Unable to fetch Domoticz info... unable to determine version")
                else:
                    if domoticzInfo and LooseVersion(domoticzInfo["dzvents_version"]) >= LooseVersion("2.4.9"):
                        self.WriteLog("Use 'adduservariable' instead of 'saveuservariable'", "Verbose")
                        parameter = "adduservariable"
                
                # actually calling Domoticz API
                DomoticzAPI("type=command&param={}&vname={}&vtype=2&vvalue={}".format(
                    parameter, varname, str(self.InternalsDefaults)))
                
                self.Internals = self.InternalsDefaults.copy()  # we re-initialize the internal variables
            else:
                try:
                    self.Internals.update(eval(valuestring))
                except:
                    self.Internals = self.InternalsDefaults.copy()
                return
        else:
            Domoticz.Error("Cannot read the uservariable holding the persistent variables")
            self.Internals = self.InternalsDefaults.copy()


    def saveUserVar(self):

        varname = Parameters["Name"] + "-InternalVariables"
        DomoticzAPI("type=command&param=updateuservariable&vname={}&vtype=2&vvalue={}".format(
            varname, str(self.Internals)))


    def WriteLog(self, message, level="Normal"):

        if (self.loglevel == "Verbose" and level == "Verbose") or level == "Status":
            if self.statussupported:
                Domoticz.Status(message)
            else:
                Domoticz.Log(message)
        elif level == "Normal":
            Domoticz.Log(message)


    def SensorTimedOut(self, idx, name, datestring):

        def LastUpdate(datestring):
            dateformat = "%Y-%m-%d %H:%M:%S"
            # the below try/except is meant to address an intermittent python bug in some embedded systems
            try:
                result = datetime.strptime(datestring, dateformat)
            except TypeError:
                result = datetime(*(time.strptime(datestring, dateformat)[0:6]))
            return result

        timedout = LastUpdate(datestring) + timedelta(minutes=int(Settings["SensorTimeout"])) < datetime.now()

        # handle logging of time outs... only log when status changes (less clutter in logs)
        if timedout:
            if self.ActiveSensors[idx]:
                Domoticz.Error("skipping timed out temperature sensor '{}'".format(name))
                self.ActiveSensors[idx] = False
        else:
            if not self.ActiveSensors[idx]:
                self.WriteLog("previously timed out temperature sensor '{}' is back online".format(name), "Status")
                self.ActiveSensors[idx] = True

        return timedout


global _plugin
_plugin = BasePlugin()


def onStart():
    global _plugin
    _plugin.onStart()


def onStop():
    global _plugin
    _plugin.onStop()


def onCommand(Unit, Command, Level, Color):
    global _plugin
    _plugin.onCommand(Unit, Command, Level, Color)


def onHeartbeat():
    global _plugin
    _plugin.onHeartbeat()


# Plugin utility functions ---------------------------------------------------

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


def DomoticzAPI(APICall):

    resultJson = None
    url = "http://{}:{}/json.htm?{}".format(Parameters["Address"], Parameters["Port"], parse.quote(APICall, safe="&="))
    Domoticz.Debug("Calling domoticz API: {}".format(url))
    try:
        req = request.Request(url)
        if Parameters["Username"] != "":
            Domoticz.Debug("Add authentification for user {}".format(Parameters["Username"]))
            credentials = ('%s:%s' % (Parameters["Username"], Parameters["Password"]))
            encoded_credentials = base64.b64encode(credentials.encode('ascii'))
            req.add_header('Authorization', 'Basic %s' % encoded_credentials.decode("ascii"))

        response = request.urlopen(req)
        if response.status == 200:
            resultJson = json.loads(response.read().decode('utf-8'))
            if resultJson["status"] != "OK":
                Domoticz.Error("Domoticz API returned an error: status = {}".format(resultJson["status"]))
                resultJson = None
        else:
            Domoticz.Error("Domoticz API: http error = {}".format(response.status))
    except:
        Domoticz.Error("Error calling '{}'".format(url))
    return resultJson


def CheckParam(name, value, default):

    try:
        param = int(value)
    except ValueError:
        param = default
        Domoticz.Error("Parameter '{}' has an invalid value of '{}' ! defaut of '{}' is instead used.".format(name, value, default))
    return param


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
